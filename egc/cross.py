"""Cross-event coherence graph for Polymarket.

The within-event model in :mod:`egc.model` only knows that the candidates of one
event are mutually exclusive. This module adds links *between* events:

* a presidential nominee is a superset of the presidential winner for the same
  person and year: ``P(win presidency) <= P(win nomination)``;
* the candidates of a party are a subset of the party winning the presidency:
  ``sum(P(candidate wins)) <= P(party wins)``;
* the four "Balance of Power" combinations are consistent with the separate
  Senate and House control markets;
* date and strike ladders that Polymarket publishes as separate events obey the
  same monotonicity as ladders inside one event.

Observed mids are noisy. We estimate fair probabilities by solving one
constrained problem per connected component:

    min_q  sum_i w_i * (logit q_i - logit p_i)^2
    s.t.   linear equalities and inequalities from the links above

The solver works in logit space, so probabilities stay strictly inside (0, 1)
and a violated link is distributed according to the weight (liquidity) of the
markets. A signal is only emitted when the *cross-event* adjustment, not the
within-event projection, clears spread and fees.
"""

from __future__ import annotations

import itertools
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

from .model import (
    EPS,
    _candidate_event,
    _direction,
    _logit,
    _parse_date,
    _parse_strike,
    event_model,
)
from .signals import fee

# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------

_MONTH_WORDS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

_DATE_PATTERNS = (
    rf"\b(?:by|before|on|in)\s+(?:{_MONTH_WORDS})\s+\d{{1,2}},?\s+\d{{4}}\b",
    rf"\b(?:by|before|on|in)\s+\d{{1,2}}\s+(?:{_MONTH_WORDS})\s+\d{{4}}\b",
    r"\b(?:by|before|on|in)\s+\d{4}-\d{2}-\d{2}\b",
    r"\b(?:by|before|on|in)\s+\d{4}\b",
)


def normalize_person(name: str) -> str:
    """Canonicalise a person name for exact cross-event matching.

    ``J.D. Vance`` and ``JD Vance`` must collide; ``Donald Trump`` and
    ``Donald Trump Jr.`` must not. Consecutive one-letter initials are merged,
    everything else is kept as words.
    """
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    tokens = [t for t in s.split() if t]
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if len(tokens[i]) == 1:
            j = i
            buf = ""
            while j < len(tokens) and len(tokens[j]) == 1:
                buf += tokens[j]
                j += 1
            out.append(buf)
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def party_of(text: str) -> str | None:
    s = text.lower()
    if "democrat" in s:
        return "D"
    if "republican" in s or re.search(r"\bgop\b", s):
        return "R"
    return None


def parse_person_ref(question: str) -> dict | None:
    """Parse a person/office/year reference from a market question."""
    q = question.strip()
    m = re.match(
        r"will\s+(.+?)\s+win\s+the\s+(\d{4})\s+(democratic|republican)\s+"
        r"presidential\s+nomination\??$",
        q,
        re.IGNORECASE,
    )
    if m:
        return {
            "person": normalize_person(m.group(1)),
            "party": "D" if m.group(3).lower().startswith("dem") else "R",
            "office": "nomination",
            "year": int(m.group(2)),
            "state": None,
        }
    m = re.match(
        r"will\s+(.+?)\s+win\s+the\s+(\d{4})\s+(?:us\s+)?presidential\s+election\??$",
        q,
        re.IGNORECASE,
    )
    if m:
        return {
            "person": normalize_person(m.group(1)),
            "party": None,
            "office": "president",
            "year": int(m.group(2)),
            "state": None,
        }
    m = re.match(
        r"will\s+(.+?)\s+win\s+the\s+(.+?)\s+governor\s+election\s+in\s+(\d{4})\??$",
        q,
        re.IGNORECASE,
    )
    if m and not m.group(1).lower().startswith("the "):
        return {
            "person": normalize_person(m.group(1)),
            "party": None,
            "office": "governor",
            "year": int(m.group(3)),
            "state": m.group(2).strip().lower(),
        }
    return None


def _year(text: str) -> int | None:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else None


def _strip_date(question: str) -> str:
    s = question.lower()
    for pattern in _DATE_PATTERNS:
        s = re.sub(pattern, " ", s)
    s = re.sub(r"[^a-z0-9% ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_strike(question: str) -> str:
    s = question.lower()
    s = re.sub(r"\$\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kmb]?\b", " __ ", s)
    s = re.sub(r"\b[0-9]+(?:\.[0-9]+)?\s*%", " __% ", s)
    s = re.sub(r"[^a-z0-9% ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


@dataclass
class Node:
    key: str
    market: dict | None
    event_id: str
    event_title: str
    prior: float
    weight: float
    latent: bool = False
    internal_q: float | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Constraint:
    kind: str  # "eq" | "le"
    coeffs: dict[str, float]
    rhs: float
    label: str
    source: str


@dataclass
class Link:
    """A tradeable cross-event relation, kept for the historical backtest."""

    kind: str
    label: str
    coeffs: dict[str, float]
    rhs: float
    constraint_kind: str
    weight: float = 0.0


@dataclass
class CrossGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def add_constraint(self, constraint: Constraint) -> None:
        if not constraint.coeffs:
            return
        self.constraints.append(constraint)

    def add_link(self, link: Link) -> None:
        self.links.append(link)

    def component_keys(self) -> list[list[str]]:
        parent = {k: k for k in self.nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for c in self.constraints:
            keys = [k for k in c.coeffs if k in parent]
            for k in keys[1:]:
                union(keys[0], k)
        groups: dict[str, list[str]] = defaultdict(list)
        for k in self.nodes:
            groups[find(k)].append(k)
        return list(groups.values())


def _node_from_market(event: dict, market: dict) -> Node:
    key = market["condition_id"]
    volume = max(0.0, float(market.get("volume") or 0.0))
    prior = min(1.0 - EPS, max(EPS, float(market.get("mid") or 0.5)))
    return Node(
        key=key,
        market=market,
        event_id=str(event.get("id") or ""),
        event_title=event.get("title") or "",
        prior=prior,
        weight=max(1.0, math.sqrt(volume)),
        meta={"volume": volume, "spread": market["ask"] - market["bid"]},
    )


def _event_kind(event: dict) -> str:
    ms = event.get("markets") or []
    title = (event.get("title") or "").lower()
    if "balance of power" in title and len(ms) >= 4:
        return "bop"
    if len(ms) == 2 and sorted(party_of(m["question"]) or "?" for m in ms) == [
        "D",
        "R",
    ]:
        return "complement"
    if len(ms) >= 3 and _candidate_event(event, ms):
        return "candidate"
    dates = [_parse_date(m["question"]) for m in ms]
    if (
        len(ms) >= 2
        and all(d is not None for d in dates)
        and len({tuple(d) for d in dates}) >= 2
        and all(
            " by " in m["question"].lower() or " before " in m["question"].lower()
            for m in ms
        )
    ):
        return "date"
    dirs = [_direction(m["question"]) for m in ms]
    strikes = [_parse_strike(m["question"]) for m in ms]
    if (
        len(ms) >= 2
        and all(d is not None for d in dirs)
        and len(set(dirs)) == 1
        and all(s is not None for s in strikes)
        and len({float(s) for s in strikes}) >= 2
    ):
        return "strike"
    return "single"


def _add_candidate_constraints(graph: CrossGraph, event: dict) -> None:
    ms = event["markets"]
    has_other = any(
        k in (m["question"] or "").lower()
        for m in ms
        for k in ("other", "field", "none of the above")
    )
    coeffs = {m["condition_id"]: 1.0 for m in ms}
    if has_other:
        graph.add_constraint(
            Constraint(
                "eq", coeffs, 1.0, f"exclusive:{event['title'][:60]}", "internal"
            )
        )
        return
    latent_key = f"latent:{event.get('id')}"
    prior = max(0.001, 1.0 - sum(float(m["mid"]) for m in ms))
    median_w = float(
        np.median([max(1.0, math.sqrt(float(m.get("volume") or 0.0))) for m in ms])
    )
    graph.nodes[latent_key] = Node(
        key=latent_key,
        market=None,
        event_id=str(event.get("id") or ""),
        event_title=event.get("title") or "",
        prior=min(1.0 - EPS, max(EPS, prior)),
        weight=max(1.0, median_w * 0.5),
        latent=True,
        meta={"kind": "latent_other"},
    )
    coeffs[latent_key] = 1.0
    graph.add_constraint(
        Constraint("eq", coeffs, 1.0, f"exclusive:{event['title'][:60]}", "internal")
    )


def _add_date_constraints(graph: CrossGraph, event: dict) -> None:
    ms = event["markets"]
    dated = []
    for m in ms:
        d = _parse_date(m["question"])
        if d is not None:
            dated.append((d, m))
    dated.sort(key=lambda x: x[0])
    for (_, a), (_, b) in itertools.pairwise(dated):
        graph.add_constraint(
            Constraint(
                "le",
                {a["condition_id"]: 1.0, b["condition_id"]: -1.0},
                0.0,
                f"date:{event['title'][:50]}:{a['question'][:30]}|{b['question'][:30]}",
                "internal",
            )
        )


def _add_strike_constraints(graph: CrossGraph, event: dict) -> None:
    ms = event["markets"]
    direction = _direction(ms[0]["question"])
    if direction is None:
        return
    ordered = sorted(ms, key=lambda m: float(_parse_strike(m["question"]) or 0.0))
    for a, b in itertools.pairwise(ordered):
        # "dip/below": P(low strike) <= P(high strike).
        # "reach/above": P(high strike) <= P(low strike).
        if direction == "down":
            coeffs = {a["condition_id"]: 1.0, b["condition_id"]: -1.0}
        else:
            coeffs = {b["condition_id"]: 1.0, a["condition_id"]: -1.0}
        graph.add_constraint(
            Constraint(
                "le",
                coeffs,
                0.0,
                f"strike:{event['title'][:50]}:{a['question'][:30]}|{b['question'][:30]}",
                "internal",
            )
        )


def _add_complement_constraint(graph: CrossGraph, event: dict) -> None:
    ms = event["markets"]
    coeffs = {m["condition_id"]: 1.0 for m in ms}
    graph.add_constraint(
        Constraint("le", coeffs, 1.0, f"complement:{event['title'][:60]}", "internal")
    )


def _find_control_nodes(events: list[dict], chamber: str, year: int) -> dict[str, str]:
    for e in events:
        title = (e.get("title") or "").lower()
        if chamber not in title or str(year) not in title:
            continue
        if "which party" not in title and "control" not in title:
            continue
        out = {}
        for m in e["markets"]:
            p = party_of(m["question"])
            if p and p not in out:
                out[p] = m["condition_id"]
        if "D" in out and "R" in out:
            return out
    return {}


def _add_bop_constraints(graph: CrossGraph, event: dict, events: list[dict]) -> None:
    combos: dict[tuple[str, str], str] = {}
    for m in event["markets"]:
        mm = re.search(
            r":\s*([DR])\s+senate,\s*([DR])\s+house", m["question"], re.IGNORECASE
        )
        if mm:
            combos[(mm.group(1).upper(), mm.group(2).upper())] = m["condition_id"]
    if len(combos) != 4:
        return
    year = _year(event.get("title") or "")
    if year is None:
        return
    senate = _find_control_nodes(events, "senate", year)
    house = _find_control_nodes(events, "house", year)
    if not senate or not house:
        return
    dd, dr = combos[("D", "D")], combos[("D", "R")]
    rd = combos[("R", "D")]
    # P(D Senate) = P(D Senate, D House) + P(D Senate, R House)
    graph.add_constraint(
        Constraint(
            "eq",
            {dd: 1.0, dr: 1.0, senate["D"]: -1.0},
            0.0,
            f"bop:senate-D:{year}",
            "bop",
        )
    )
    graph.add_link(
        Link(
            "bop",
            f"Balance of Power Senate D {year}",
            {dd: 1.0, dr: 1.0, senate["D"]: -1.0},
            0.0,
            "eq",
        )
    )
    # P(D House) = P(D Senate, D House) + P(R Senate, D House)
    graph.add_constraint(
        Constraint(
            "eq",
            {dd: 1.0, rd: 1.0, house["D"]: -1.0},
            0.0,
            f"bop:house-D:{year}",
            "bop",
        )
    )
    graph.add_link(
        Link(
            "bop",
            f"Balance of Power House D {year}",
            {dd: 1.0, rd: 1.0, house["D"]: -1.0},
            0.0,
            "eq",
        )
    )


def _nomination_maps(graph: CrossGraph) -> tuple[dict, dict]:
    nominations: dict[tuple[str, int], tuple[str, str, float]] = {}
    presidents: dict[tuple[str, int], tuple[str, float]] = {}
    for key, node in graph.nodes.items():
        if node.latent or not node.market:
            continue
        ref = parse_person_ref(node.market["question"])
        if not ref:
            continue
        volume = float(node.market.get("volume") or 0.0)
        ident = (ref["person"], ref["year"])
        if ref["office"] == "nomination" and ref["party"]:
            old = nominations.get(ident)
            if old is None or volume > old[2]:
                nominations[ident] = (key, ref["party"], volume)
        elif ref["office"] == "president":
            old = presidents.get(ident)
            if old is None or volume > old[1]:
                presidents[ident] = (key, volume)
    return nominations, presidents


def _add_nomination_links(
    graph: CrossGraph, nominations: dict, presidents: dict
) -> None:
    # A person cannot be the nominee of both parties in the same year; if the
    # data says otherwise, the link is ambiguous and is skipped.
    parties: dict[tuple[str, int], set[str]] = defaultdict(set)
    for (person, year), (_, party, _) in nominations.items():
        parties[(person, year)].add(party)
    for ident, (pres_key, _) in presidents.items():
        if ident not in nominations or len(parties[ident]) != 1:
            continue
        nom_key, party, _ = nominations[ident]
        label = f"nomination->president:{ident[0]}:{ident[1]}:{party}"
        graph.add_constraint(
            Constraint("le", {pres_key: 1.0, nom_key: -1.0}, 0.0, label, "nomination")
        )
        graph.add_link(
            Link(
                "nomination",
                label,
                {pres_key: 1.0, nom_key: -1.0},
                0.0,
                "le",
            )
        )


def _add_party_links(
    graph: CrossGraph, events: list[dict], nominations: dict, presidents: dict
) -> None:
    party_events = [
        e
        for e in events
        if "which party wins" in (e.get("title") or "").lower()
        and "presidential election" in (e.get("title") or "").lower()
    ]
    for party_event in party_events:
        year = _year(party_event.get("title") or "")
        if year is None:
            continue
        party_nodes: dict[str, str] = {}
        for m in party_event["markets"]:
            p = party_of(m["question"])
            if p and p not in party_nodes:
                party_nodes[p] = m["condition_id"]
        if len(party_nodes) < 2:
            continue
        graph.add_constraint(
            Constraint(
                "le",
                {k: 1.0 for k in party_nodes.values()},
                1.0,
                f"party-complement:{year}",
                "party",
            )
        )
        for party, party_key in party_nodes.items():
            candidates = [
                pres_key
                for (person, y), (pres_key, _) in presidents.items()
                if y == year
                and (person, y) in nominations
                and nominations[(person, y)][1] == party
            ]
            if len(candidates) < 3:
                continue
            coeffs = {key: 1.0 for key in candidates}
            coeffs[party_key] = -1.0
            label = f"party-candidates<=party-win:{party}:{year}"
            graph.add_constraint(Constraint("le", coeffs, 0.0, label, "party"))
            graph.add_link(Link("party", label, coeffs, 0.0, "le"))


def _add_cross_ladders(graph: CrossGraph) -> None:
    dates: dict[str, list[tuple[tuple[int, int, int], str, str]]] = defaultdict(list)
    strikes: dict[tuple[str, str], list[tuple[float, str, str]]] = defaultdict(list)
    for key, node in graph.nodes.items():
        if node.latent or not node.market:
            continue
        q = node.market["question"]
        d = _parse_date(q)
        if d is not None and (" by " in q.lower() or " before " in q.lower()):
            dates[_strip_date(q)].append((d, key, node.event_id))
        strike = _parse_strike(q)
        direction = _direction(q)
        if strike is not None and direction is not None:
            strikes[(_strip_strike(q), direction)].append(
                (float(strike), key, node.event_id)
            )

    for base, items in dates.items():
        by_date: dict[tuple[int, int, int], tuple[str, str]] = {}
        for d, key, event_id in items:
            if d not in by_date:
                by_date[d] = (key, event_id)
        ordered = sorted(by_date.items())
        for (_, (a_key, a_event)), (_, (b_key, b_event)) in itertools.pairwise(ordered):
            if a_event == b_event:
                continue
            label = f"date-cross:{base[:50]}:{a_key[:6]}->{b_key[:6]}"
            coeffs = {a_key: 1.0, b_key: -1.0}
            graph.add_constraint(Constraint("le", coeffs, 0.0, label, "date"))
            graph.add_link(Link("date", label, coeffs, 0.0, "le"))

    for (base, direction), items in strikes.items():
        by_strike: dict[float, tuple[str, str]] = {}
        for strike, key, event_id in items:
            if strike not in by_strike:
                by_strike[strike] = (key, event_id)
        ordered = sorted(by_strike.items())
        for (_, (a_key, a_event)), (_, (b_key, b_event)) in itertools.pairwise(ordered):
            if a_event == b_event:
                continue
            if direction == "down":
                coeffs = {a_key: 1.0, b_key: -1.0}
            else:
                coeffs = {b_key: 1.0, a_key: -1.0}
            label = f"strike-cross:{base[:50]}:{a_key[:6]}->{b_key[:6]}"
            graph.add_constraint(Constraint("le", coeffs, 0.0, label, "strike"))
            graph.add_link(Link("strike", label, coeffs, 0.0, "le"))


def build_graph(events: list[dict]) -> CrossGraph:
    """Build every node, internal constraint and cross-event link."""
    graph = CrossGraph(events=events)
    for event in events:
        ms = event.get("markets") or []
        if not ms:
            continue
        for m in ms:
            key = m.get("condition_id") or ""
            if key and key not in graph.nodes:
                graph.nodes[key] = _node_from_market(event, m)
        kind = _event_kind(event)
        if kind == "bop":
            _add_bop_constraints(graph, event, events)
        elif kind == "candidate":
            _add_candidate_constraints(graph, event)
        elif kind == "date":
            _add_date_constraints(graph, event)
        elif kind == "strike":
            _add_strike_constraints(graph, event)
        elif kind == "complement":
            _add_complement_constraint(graph, event)

    # Within-event fair values are the baseline for attributing the cross-event
    # adjustment. They are never used as the solver's target.
    for event in events:
        model = event_model(event)
        if not model:
            continue
        for m, q in zip(model["markets"], model["q"]):
            node = graph.nodes.get(m.get("condition_id") or "")
            if node is not None:
                node.internal_q = float(q)

    nominations, presidents = _nomination_maps(graph)
    _add_nomination_links(graph, nominations, presidents)
    _add_party_links(graph, events, nominations, presidents)
    _add_cross_ladders(graph)
    return graph


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _constraint_value(c: Constraint, q: dict[str, float]) -> float:
    return sum(coef * q.get(key, 0.0) for key, coef in c.coeffs.items()) - c.rhs


def _violation(c: Constraint, q: dict[str, float]) -> float:
    value = _constraint_value(c, q)
    if c.kind == "eq":
        return abs(value)
    return max(0.0, value)


def _solve_component(
    keys: list[str],
    nodes: dict[str, Node],
    constraints: list[Constraint],
    maxiter: int,
) -> tuple[dict[str, float], bool, str]:
    if not constraints:
        return {k: nodes[k].prior for k in keys}, True, "none"
    index = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    z0 = np.array([_logit(nodes[k].prior) for k in keys], dtype=float)
    w = np.array([max(1e-9, nodes[k].weight) for k in keys], dtype=float)
    median = float(np.median(w))
    if median > 0:
        w = w / median

    def objective(z: np.ndarray) -> float:
        return float(np.sum(w * (z - z0) ** 2))

    def gradient(z: np.ndarray) -> np.ndarray:
        return 2.0 * w * (z - z0)

    scipy_cons = []
    for c in constraints:
        idx = [(index[k], float(coef)) for k, coef in c.coeffs.items() if k in index]
        if not idx:
            continue
        rhs = float(c.rhs)

        def fun(z: np.ndarray, idx=idx, rhs=rhs, kind=c.kind) -> float:
            q = _sigmoid(z)
            value = sum(coef * q[i] for i, coef in idx) - rhs
            return value if kind == "eq" else -value

        def jac(z: np.ndarray, idx=idx, kind=c.kind) -> np.ndarray:
            q = _sigmoid(z)
            out = np.zeros(n)
            for i, coef in idx:
                out[i] += coef * q[i] * (1.0 - q[i])
            return out if kind == "eq" else -out

        scipy_cons.append(
            {"type": "eq" if c.kind == "eq" else "ineq", "fun": fun, "jac": jac}
        )

    res = minimize(
        objective,
        z0,
        jac=gradient,
        method="SLSQP",
        bounds=[(-30.0, 30.0)] * n,
        constraints=scipy_cons,
        options={"maxiter": maxiter, "ftol": 1e-10, "disp": False},
    )
    best_z = np.array(res.x, dtype=float)
    best_q = _sigmoid(best_z)
    best_viol = max(
        (
            _violation(c, {k: float(best_q[index[k]]) for k in keys})
            for c in constraints
        ),
        default=0.0,
    )
    status = "slsqp" if res.success else "slsqp-fallback"

    if not res.success or best_viol > 1e-5:

        def penalty(z: np.ndarray) -> float:
            q = _sigmoid(z)
            value = objective(z)
            for c in constraints:
                idx = [
                    (index[k], float(coef))
                    for k, coef in c.coeffs.items()
                    if k in index
                ]
                raw = sum(coef * q[i] for i, coef in idx) - c.rhs
                value += 1e5 * (raw * raw if c.kind == "eq" else max(0.0, raw) ** 2)
            return float(value)

        res2 = minimize(
            penalty,
            z0,
            method="L-BFGS-B",
            bounds=[(-30.0, 30.0)] * n,
            options={"maxiter": 2000},
        )
        q2 = _sigmoid(res2.x)
        viol2 = max(
            (
                _violation(c, {k: float(q2[index[k]]) for k in keys})
                for c in constraints
            ),
            default=0.0,
        )
        if viol2 < best_viol:
            best_q, best_viol, status = q2, viol2, "penalty"

    return {k: float(best_q[index[k]]) for k in keys}, best_viol <= 1e-5, status


def solve_graph(graph: CrossGraph, maxiter: int = 500) -> dict:
    """Solve all components and report raw and solved constraint violations."""
    q: dict[str, float] = {}
    components = graph.component_keys()
    statuses = []
    for comp in components:
        keys = sorted(comp)
        comp_constraints = [
            c for c in graph.constraints if any(k in comp for k in c.coeffs)
        ]
        values, ok, status = _solve_component(
            keys, graph.nodes, comp_constraints, maxiter
        )
        q.update(values)
        statuses.append({"size": len(keys), "ok": ok, "status": status})
    violations = []
    for c in graph.constraints:
        raw = _violation(c, {k: graph.nodes[k].prior for k in graph.nodes})
        solved = _violation(c, q)
        violations.append(
            {
                "label": c.label,
                "source": c.source,
                "kind": c.kind,
                "coeffs": c.coeffs,
                "rhs": c.rhs,
                "raw_violation": round(float(raw), 6),
                "solved_violation": round(float(solved), 6),
                "raw_gap": round(
                    float(
                        _constraint_value(
                            c, {k: graph.nodes[k].prior for k in graph.nodes}
                        )
                    ),
                    6,
                ),
            }
        )
    violations.sort(key=lambda x: -x["raw_violation"])
    return {
        "q": q,
        "components": components,
        "component_status": statuses,
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def cross_signals(
    graph: CrossGraph,
    solution: dict,
    *,
    min_edge: float = 0.01,
    max_spread: float = 0.03,
    min_volume: float = 20000.0,
    min_cross_adj: float = 0.002,
    min_raw_violation: float = 0.002,
    max_raw_violation: float = 0.15,
) -> list[dict]:
    """Executable signals driven by a violated cross-event link."""
    q = solution["q"]
    by_key: dict[str, list[dict]] = defaultdict(list)
    for v in solution["violations"]:
        if v["source"] == "internal":
            continue
        if v["raw_violation"] < min_raw_violation:
            continue
        for key in v["coeffs"]:
            by_key[key].append(v)

    out = []
    for key, node in graph.nodes.items():
        if node.latent or not node.market:
            continue
        m = node.market
        if float(m.get("volume") or 0.0) < min_volume:
            continue
        spread = float(m["ask"]) - float(m["bid"])
        if spread > max_spread:
            continue
        related = by_key.get(key, [])
        if not related:
            continue
        if max(v["raw_violation"] for v in related) > max_raw_violation:
            continue
        internal = node.internal_q if node.internal_q is not None else float(m["mid"])
        cross_adj = float(q[key]) - float(internal)
        if abs(cross_adj) < min_cross_adj:
            continue
        fees = fee(float(m["ask"])) + fee(float(m["bid"]))
        yes_edge = float(q[key]) - float(m["mid"]) - spread - fees
        no_edge = float(m["mid"]) - float(q[key]) - spread - fees
        if yes_edge >= no_edge and yes_edge > min_edge:
            side, edge, entry = "YES", yes_edge, float(m["ask"])
            token = m["tokens"][0]
        elif no_edge > min_edge:
            side, edge, entry = "NO", no_edge, 1.0 - float(m["bid"])
            token = m["tokens"][1]
        else:
            continue
        out.append(
            {
                "event": node.event_title,
                "question": m["question"],
                "condition_id": key,
                "token_id": token,
                "side": side,
                "edge": round(float(edge), 4),
                "fair": round(float(q[key]), 4),
                "mid": round(float(m["mid"]), 4),
                "entry": round(entry, 4),
                "bid": float(m["bid"]),
                "ask": float(m["ask"]),
                "spread": round(spread, 4),
                "volume": float(m.get("volume") or 0.0),
                "end_date": m.get("end_date") or "",
                "cross_adj": round(cross_adj, 4),
                "raw_violation": round(max(v["raw_violation"] for v in related), 4),
                "links": [v["label"] for v in related],
            }
        )
    out.sort(key=lambda x: -x["edge"])
    return out


def top_violations(solution: dict, limit: int = 10) -> list[dict]:
    rows = [v for v in solution["violations"] if v["source"] != "internal"]
    return rows[:limit]


def graph_summary(graph: CrossGraph, solution: dict) -> dict:
    sizes = [len(c) for c in solution["components"]]
    return {
        "nodes": len(graph.nodes),
        "markets": sum(1 for n in graph.nodes.values() if not n.latent),
        "latent": sum(1 for n in graph.nodes.values() if n.latent),
        "constraints": len(graph.constraints),
        "links": len(graph.links),
        "components": len(sizes),
        "largest_component": max(sizes) if sizes else 0,
        "solved_nodes": len(solution["q"]),
        "unsolved_components": sum(
            1 for s in solution["component_status"] if not s["ok"]
        ),
        "max_raw_violation": max(
            (v["raw_violation"] for v in solution["violations"]), default=0.0
        ),
        "max_cross_raw_violation": max(
            (
                v["raw_violation"]
                for v in solution["violations"]
                if v["source"] != "internal"
            ),
            default=0.0,
        ),
        "max_internal_raw_violation": max(
            (
                v["raw_violation"]
                for v in solution["violations"]
                if v["source"] == "internal"
            ),
            default=0.0,
        ),
        "max_solved_violation": max(
            (v["solved_violation"] for v in solution["violations"]), default=0.0
        ),
    }


def cross_snapshot(
    events: list[dict],
    *,
    min_edge: float = 0.01,
    top: int = 20,
    maxiter: int = 500,
) -> dict:
    graph = build_graph(events)
    solution = solve_graph(graph, maxiter=maxiter)
    signals = cross_signals(graph, solution, min_edge=min_edge)
    return {
        "summary": graph_summary(graph, solution),
        "signals": signals[:top],
        "violations": top_violations(solution, limit=15),
        "graph": graph,
        "solution": solution,
    }


__all__ = [
    "Constraint",
    "CrossGraph",
    "Link",
    "Node",
    "build_graph",
    "cross_signals",
    "cross_snapshot",
    "graph_summary",
    "normalize_person",
    "parse_person_ref",
    "party_of",
    "solve_graph",
    "top_violations",
]
