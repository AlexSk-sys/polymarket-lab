"""Fair-probability projection for coherent event graphs.

For an exclusive candidate set, observed mid prices p_i are noisy observations
of fair probabilities q_i. We solve

    min_q  sum_i w_i * (logit q_i - logit p_i)^2
    s.t.   sum_i q_i = 1,  0 < q_i < 1

A latent "Other" absorbs probability with no traded market. Without it, forcing
visible candidates to sum to one fabricates arbitrage in non-exhaustive events.

Date ladders ("Will X happen by <date>?") and strike ladders ("Will Bitcoin dip
to $Y?") are not exclusive: their probabilities are monotone, so they use
weighted isotonic projection (PAVA), not a sum constraint.
"""

from __future__ import annotations

import math
import re

import numpy as np
from scipy.optimize import minimize

EPS = 1e-6

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _logit(p: float) -> float:
    p = min(1.0 - EPS, max(EPS, p))
    return math.log(p / (1.0 - p))


def fair_exclusive(
    prices: list[float],
    weights: list[float] | None = None,
    other_price: float | None = None,
) -> list[float]:
    """Project mid prices onto the probability simplex in logit space."""
    if not prices:
        return []
    if weights is None:
        weights = [1.0] * len(prices)
    p = np.array([min(1.0 - EPS, max(EPS, x)) for x in prices], dtype=float)
    w = np.array([max(1e-6, x) for x in weights], dtype=float)
    has_other = other_price is not None
    if has_other:
        p = np.append(p, min(1.0 - EPS, max(EPS, float(other_price))))
        w = np.append(w, max(1e-6, float(np.median(w)) * 0.5))
    n = len(p)
    zp = np.array([_logit(x) for x in p])

    def objective(q: np.ndarray) -> float:
        z = np.array([_logit(float(x)) for x in q])
        return float(np.sum(w * (z - zp) ** 2))

    def grad(q: np.ndarray) -> np.ndarray:
        z = np.array([_logit(float(x)) for x in q])
        return 2.0 * w * (z - zp) / np.maximum(EPS, q * (1.0 - q))

    q0 = p / p.sum() if p.sum() > 0 else np.full(n, 1.0 / n)
    cons = (
        {
            "type": "eq",
            "fun": lambda q: float(np.sum(q) - 1.0),
            "jac": lambda q: np.ones(n),
        },
    )
    res = minimize(
        objective,
        q0,
        jac=grad,
        method="SLSQP",
        bounds=[(EPS, 1.0 - EPS)] * n,
        constraints=cons,
        options={"maxiter": 300, "ftol": 1e-12},
    )
    q = res.x if res.success else q0 / q0.sum()
    q = np.clip(q, EPS, 1.0 - EPS)
    q = q / q.sum()
    if has_other:
        return [float(x) for x in q[:-1]]
    return [float(x) for x in q]


def isotonic(
    values: list[float],
    weights: list[float] | None = None,
    increasing: bool = True,
) -> list[float]:
    """Weighted isotonic projection (PAVA) for nested date/strike markets."""
    v = [float(x) for x in values]
    w = [max(1e-9, float(x)) for x in (weights or [1.0] * len(v))]
    sign = 1.0 if increasing else -1.0
    y = [sign * x for x in v]
    blocks: list[list[float]] = []
    for val, wt in zip(y, w):
        blocks.append([val, wt, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v1, w1, c1 = blocks.pop()
            v0, w0, c0 = blocks.pop()
            tw = w0 + w1
            blocks.append([(v0 * w0 + v1 * w1) / tw, tw, c0 + c1])
    out = []
    for val, _, count in blocks:
        out.extend([sign * val] * count)
    return out


def _parse_strike(question: str) -> float | None:
    q = question.lower().replace(",", "")
    m = re.search(
        r"(?:dip to|reach|hit|above|below|over|under|close at|be above|be below|exceed)"
        r"\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s*([kmb])?",
        q,
    )
    if not m:
        return None
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2) or "", 1.0)
    try:
        return float(m.group(1)) * mult
    except ValueError:
        return None


def _parse_date(question: str) -> tuple[int, int, int] | None:
    q = question.lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", q)
    if m and m.group(1) in _MONTHS:
        return int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", q)
    if m and m.group(2) in _MONTHS:
        return int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))
    return None


def _direction(question: str) -> str | None:
    q = question.lower()
    if any(k in q for k in ("dip to", "below", "under", "lower")):
        return "down"
    if any(k in q for k in ("reach", "above", "higher", "exceed", "hit")):
        return "up"
    return None


def _single_clause(question: str) -> bool:
    q = question.lower()
    if q.count("?") > 1:
        return False
    return " and will " not in q and " and the " not in q


def _candidate_event(event: dict, ms: list[dict]) -> bool:
    title = (event.get("title") or "").lower()
    if not any(
        k in title
        for k in ("who will", "who wins", "which ", "nominee", "winner", "next ")
    ):
        return False
    # A true candidate set should price near one; a wild sum means the markets
    # are not the exclusive set the title claims.
    return abs(sum(m["mid"] for m in ms) - 1.0) <= 0.20


def event_model(event: dict) -> dict | None:
    """Return fair probabilities only for a confidently classified event."""
    ms = event.get("markets") or []
    if len(ms) < 3:
        return None
    qs = [m["question"] or "" for m in ms]
    prices = [m["mid"] for m in ms]
    weights = [max(1.0, math.sqrt(m["volume"])) for m in ms]
    if not all(_single_clause(q) for q in qs):
        return None

    if _candidate_event(event, ms):
        has_other = any(
            k in (m["question"] or "").lower()
            for m in ms
            for k in ("other", "field", "none of the above")
        )
        other = None if has_other else max(0.001, 1.0 - sum(prices))
        q = fair_exclusive(prices, weights, other_price=other)
        return {
            "kind": "candidate",
            "markets": ms,
            "q": q,
            "edges": [a - b for a, b in zip(q, prices)],
            "other": other,
        }

    dates = [_parse_date(q) for q in qs]
    if (
        all(d is not None for d in dates)
        and len({tuple(d) for d in dates}) >= 2
        and all((" by " in q.lower() or " before " in q.lower()) for q in qs)
    ):
        order = sorted(range(len(ms)), key=lambda i: dates[i])
        sorted_prices = [prices[i] for i in order]
        if any(
            sorted_prices[i] - sorted_prices[i + 1] > 0.10
            for i in range(len(sorted_prices) - 1)
        ):
            return None
        q_sorted = isotonic(sorted_prices, [weights[i] for i in order], increasing=True)
        q = [0.0] * len(ms)
        for pos, i in enumerate(order):
            q[i] = float(q_sorted[pos])
        return {
            "kind": "date",
            "markets": ms,
            "q": q,
            "edges": [a - b for a, b in zip(q, prices)],
        }

    dirs = [_direction(q) for q in qs]
    strikes = [_parse_strike(q) for q in qs]
    if (
        all(d is not None for d in dirs)
        and len(set(dirs)) == 1
        and all(s is not None for s in strikes)
    ):
        # "dip to $X": P is nondecreasing in X. "reach $X": nonincreasing.
        increasing = dirs[0] == "down"
        order = sorted(range(len(ms)), key=lambda i: strikes[i])
        sorted_prices = [prices[i] for i in order]
        # Reject ladders whose raw quotes are wildly non-monotone: that is a
        # stale/wide-book artefact, not a tradeable coherence gap.
        if increasing:
            bad = any(
                sorted_prices[i] - sorted_prices[i + 1] > 0.10
                for i in range(len(sorted_prices) - 1)
            )
        else:
            bad = any(
                sorted_prices[i + 1] - sorted_prices[i] > 0.10
                for i in range(len(sorted_prices) - 1)
            )
        if bad:
            return None
        q_sorted = isotonic(
            sorted_prices, [weights[i] for i in order], increasing=increasing
        )
        q = [0.0] * len(ms)
        for pos, i in enumerate(order):
            q[i] = float(q_sorted[pos])
        return {
            "kind": "strike",
            "markets": ms,
            "q": q,
            "edges": [a - b for a, b in zip(q, prices)],
        }
    return None
