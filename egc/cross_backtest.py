"""Historical backtest for cross-event coherence links.

A link is a linear relation between market probabilities, e.g.

    P(president) - P(nomination) <= 0
    sum(P(candidate)) - P(party wins) <= 0
    P(D Senate, D House) + P(D Senate, R House) - P(D Senate) = 0

When the relation is violated, the trade is a convergence basket: buy the
cheap side, buy NO on the expensive side, one unit per coefficient. The basket
is held until the gap closes, take-profit/stop-loss is hit, or the time limit
expires. This is relative value, not a risk-free arbitrage: the gap can widen
before it converges, which is exactly the drawdown the strategy has to survive.
"""

from __future__ import annotations

import bisect
import math
import time
from datetime import UTC, datetime

import pm_lab

from .cross import CrossGraph, Link
from .signals import fee

DEFAULT_ENTRY = 0.02
DEFAULT_EXIT = 0.005
DEFAULT_TP = 0.15
DEFAULT_SL = 0.12
DEFAULT_MAX_BARS = 24


def _backward_lookup(series: list[tuple[float, float]]):
    keys = [t for t, _ in series]
    vals = [p for _, p in series]

    def at(ts: float, tol: int = 900) -> float | None:
        i = bisect.bisect_right(keys, ts) - 1
        if i >= 0 and ts - keys[i] <= tol:
            return vals[i]
        return None

    return at


def _half_spread(market: dict) -> float:
    return max(0.001, (float(market["ask"]) - float(market["bid"])) / 2.0)


def _leg_price(market: dict, mid: float, side: str, phase: str) -> float:
    half = _half_spread(market)
    bid = max(0.001, min(0.999, mid - half))
    ask = max(0.001, min(0.999, mid + half))
    if side == "YES":
        return ask if phase == "entry" else bid
    # A NO share costs 1 - bid(YES) to buy and pays 1 - ask(YES) to sell.
    return (1.0 - bid) if phase == "entry" else (1.0 - ask)


def _legs_for(link: Link, direction: int, graph: CrossGraph) -> list[dict] | None:
    legs = []
    for key, coef in link.coeffs.items():
        if abs(coef) < 1e-12:
            continue
        node = graph.nodes.get(key)
        if node is None or node.latent or not node.market:
            return None
        if direction > 0:
            side = "NO" if coef > 0 else "YES"
        else:
            side = "YES" if coef > 0 else "NO"
        legs.append(
            {
                "key": key,
                "side": side,
                "units": abs(float(coef)),
                "market": node.market,
                "question": node.market["question"],
            }
        )
    return legs or None


def _gap(link: Link, mids: dict[str, float]) -> float:
    return (
        sum(coef * mids.get(key, 0.0) for key, coef in link.coeffs.items()) - link.rhs
    )


def run_link(
    link: Link,
    graph: CrossGraph,
    series: dict[str, list[tuple[float, float]]],
    *,
    entry: float = DEFAULT_ENTRY,
    exit_gap: float = DEFAULT_EXIT,
    take_profit: float = DEFAULT_TP,
    stop_loss: float = DEFAULT_SL,
    max_bars: int = DEFAULT_MAX_BARS,
    stake: float = 20.0,
    step: int = 6,
    max_entry_gap: float = 0.20,
    cooldown_bars: int = 12,
) -> list[dict]:
    keys = [k for k in link.coeffs if abs(link.coeffs[k]) > 1e-12]
    if any(not series.get(k) for k in keys):
        return []
    back = {k: _backward_lookup(series[k]) for k in keys}
    fwd = {k: pm_lab.make_lookup(series[k]) for k in keys}
    reference = max(keys, key=lambda k: len(series[k]))
    stamps = [t for t, _ in series[reference]][::step]

    pos = None
    cooldown = 0
    trades = []
    for ts in stamps:
        decision = {k: back[k](ts) for k in keys}
        if any(v is None for v in decision.values()):
            continue
        execution = {k: fwd[k](ts) for k in keys}
        if any(v is None for v in execution.values()):
            continue
        gap = _gap(link, decision)

        if pos is None:
            if cooldown > 0:
                cooldown -= 1
                continue
            if abs(gap) <= entry:
                continue
            if abs(gap) > max_entry_gap:
                continue
            direction = 1 if gap > 0 else -1
            if link.constraint_kind == "le" and direction < 0:
                continue
            legs = _legs_for(link, direction, graph)
            if not legs:
                continue
            entry_prices = {
                leg["key"]: _leg_price(
                    leg["market"], execution[leg["key"]], leg["side"], "entry"
                )
                for leg in legs
            }
            cost = sum(leg["units"] * entry_prices[leg["key"]] for leg in legs)
            if cost <= 0:
                continue
            pos = {
                "direction": direction,
                "legs": legs,
                "entry_prices": entry_prices,
                "cost": cost,
                "entry_gap": gap,
                "entry_ts": ts,
                "bars": 0,
            }
            continue

        pos["bars"] += 1
        exit_prices = {
            leg["key"]: _leg_price(
                leg["market"], execution[leg["key"]], leg["side"], "exit"
            )
            for leg in pos["legs"]
        }
        value = sum(leg["units"] * exit_prices[leg["key"]] for leg in pos["legs"])
        fees = sum(
            leg["units"]
            * (fee(pos["entry_prices"][leg["key"]]) + fee(exit_prices[leg["key"]]))
            for leg in pos["legs"]
        )
        ret = (value - pos["cost"] - fees) / pos["cost"]
        should_exit = (
            abs(gap) <= exit_gap
            or ret >= take_profit
            or ret <= -stop_loss
            or pos["bars"] >= max_bars
        )
        if not should_exit:
            continue
        reason = (
            "converged"
            if abs(gap) <= exit_gap
            else "take-profit"
            if ret >= take_profit
            else "stop-loss"
            if ret <= -stop_loss
            else "time"
        )
        trades.append(
            {
                "link": link.label,
                "kind": link.kind,
                "direction": pos["direction"],
                "entry_gap": round(float(pos["entry_gap"]), 6),
                "exit_gap": round(float(gap), 6),
                "entry_ts": pos["entry_ts"],
                "exit_ts": ts,
                "bars": pos["bars"],
                "ret": round(float(ret), 6),
                "pnl": round(float(ret * stake), 4),
                "cost": round(float(pos["cost"]), 4),
                "reason": reason,
                "legs": [
                    {
                        "question": leg["question"][:90],
                        "side": leg["side"],
                        "units": leg["units"],
                        "entry": round(float(pos["entry_prices"][leg["key"]]), 4),
                        "exit": round(float(exit_prices[leg["key"]]), 4),
                    }
                    for leg in pos["legs"]
                ],
            }
        )
        pos = None
        cooldown = cooldown_bars

    if pos is not None and stamps:
        ts = stamps[-1]
        execution = {k: fwd[k](ts) for k in keys}
        if all(v is not None for v in execution.values()):
            exit_prices = {
                leg["key"]: _leg_price(
                    leg["market"], execution[leg["key"]], leg["side"], "exit"
                )
                for leg in pos["legs"]
            }
            value = sum(leg["units"] * exit_prices[leg["key"]] for leg in pos["legs"])
            fees = sum(
                leg["units"]
                * (fee(pos["entry_prices"][leg["key"]]) + fee(exit_prices[leg["key"]]))
                for leg in pos["legs"]
            )
            ret = (value - pos["cost"] - fees) / pos["cost"]
            trades.append(
                {
                    "link": link.label,
                    "kind": link.kind,
                    "direction": pos["direction"],
                    "entry_gap": round(float(pos["entry_gap"]), 6),
                    "exit_gap": round(float(_gap(link, execution)), 6),
                    "entry_ts": pos["entry_ts"],
                    "exit_ts": ts,
                    "bars": pos["bars"],
                    "ret": round(float(ret), 6),
                    "pnl": round(float(ret * stake), 4),
                    "cost": round(float(pos["cost"]), 4),
                    "reason": "eod",
                    "legs": [
                        {
                            "question": leg["question"][:90],
                            "side": leg["side"],
                            "units": leg["units"],
                            "entry": round(float(pos["entry_prices"][leg["key"]]), 4),
                            "exit": round(float(exit_prices[leg["key"]]), 4),
                        }
                        for leg in pos["legs"]
                    ],
                }
            )
    return trades


def link_stats(
    link: Link,
    graph: CrossGraph,
    series: dict[str, list[tuple[float, float]]],
    *,
    step: int = 6,
) -> dict | None:
    """Cost floor, mean reversion and available edge for one link."""
    keys = [k for k in link.coeffs if abs(link.coeffs[k]) > 1e-12]
    if any(not series.get(k) for k in keys):
        return None
    back = {k: _backward_lookup(series[k]) for k in keys}
    reference = max(keys, key=lambda k: len(series[k]))
    stamps = [t for t, _ in series[reference]][::step]
    gaps = []
    for ts in stamps:
        mids = {k: back[k](ts) for k in keys}
        if any(v is None for v in mids.values()):
            continue
        gaps.append(_gap(link, mids))
    if len(gaps) < 10:
        return None
    mean = sum(gaps) / len(gaps)
    variance = sum((g - mean) ** 2 for g in gaps) / max(1, len(gaps) - 1)
    std = math.sqrt(variance)
    phi = None
    half_life = None
    if len(gaps) > 3:
        x = gaps[:-1]
        y = gaps[1:]
        mx = sum(x) / len(x)
        my = sum(y) / len(y)
        denom = sum((v - mx) ** 2 for v in x)
        if denom > 1e-12:
            phi = sum((a - mx) * (b - my) for a, b in zip(x, y)) / denom
            if 0.0 < phi < 1.0:
                half_life = -math.log(2.0) / math.log(phi)
    direction = 1 if mean >= 0 else -1
    if link.constraint_kind == "le":
        direction = 1
    legs = _legs_for(link, direction, graph)
    if not legs:
        return None
    spread_cost = 0.0
    fees = 0.0
    cost = 0.0
    for leg in legs:
        market = leg["market"]
        mid = float(graph.nodes[leg["key"]].prior)
        ep = _leg_price(market, mid, leg["side"], "entry")
        xp = _leg_price(market, mid, leg["side"], "exit")
        units = leg["units"]
        cost += units * ep
        spread_cost += units * (float(market["ask"]) - float(market["bid"]))
        fees += units * (fee(ep) + fee(xp))
    if cost <= 0:
        return None
    cost_frac = (spread_cost + fees) / cost
    if link.constraint_kind == "le":
        edge_room = max(0.0, max(gaps) - max(0.0, mean)) / cost
    else:
        edge_room = max(abs(g - mean) for g in gaps) / cost
    tradeable = (
        edge_room > cost_frac * 1.5
        and half_life is not None
        and half_life <= 48
        and max(gaps) <= 0.25
    )
    return {
        "label": link.label,
        "kind": link.kind,
        "legs": len(legs),
        "gap_mean": round(mean, 6),
        "gap_std": round(std, 6),
        "gap_min": round(min(gaps), 6),
        "gap_max": round(max(gaps), 6),
        "half_life_bars": round(half_life, 1) if half_life is not None else None,
        "basket_cost": round(cost, 4),
        "cost_fraction": round(cost_frac, 6),
        "edge_room": round(edge_room, 6),
        "tradeable": bool(tradeable),
    }


def select_links(
    graph: CrossGraph,
    *,
    max_links: int = 20,
    min_volume: float = 20000.0,
    max_spread: float = 0.05,
    max_legs: int = 16,
) -> list[Link]:
    candidates = []
    for link in graph.links:
        keys = [k for k in link.coeffs if abs(link.coeffs[k]) > 1e-12]
        if len(keys) < 2 or len(keys) > max_legs:
            continue
        gap = 0.0
        ok = True
        for key in keys:
            node = graph.nodes.get(key)
            if node is None or node.latent or not node.market:
                ok = False
                break
            market = node.market
            if float(market.get("volume") or 0.0) < min_volume:
                ok = False
                break
            if float(market["ask"]) - float(market["bid"]) > max_spread:
                ok = False
                break
            gap += link.coeffs[key] * node.prior
        if not ok:
            continue
        candidates.append((abs(gap - link.rhs), link))
    candidates.sort(key=lambda x: -x[0])
    return [link for _, link in candidates[:max_links]]


def series_for_links(
    graph: CrossGraph,
    links: list[Link],
    days: int,
) -> dict[str, list[tuple[float, float]]]:
    start = time.time() - days * 86400
    end = time.time()
    out: dict[str, list[tuple[float, float]]] = {}
    for link in links:
        for key in link.coeffs:
            node = graph.nodes.get(key)
            if node is None or node.latent or not node.market or key in out:
                continue
            token = node.market["tokens"][0]
            try:
                out[key] = pm_lab.price_series(token, start, end)
            except Exception:  # noqa: BLE001 - one bad token must not kill the run
                out[key] = []
    return out


def run_cross_backtest(
    graph: CrossGraph,
    *,
    days: int = 7,
    max_links: int = 20,
    entry: float = DEFAULT_ENTRY,
    exit_gap: float = DEFAULT_EXIT,
    take_profit: float = DEFAULT_TP,
    stop_loss: float = DEFAULT_SL,
    max_bars: int = DEFAULT_MAX_BARS,
    stake: float = 20.0,
    min_volume: float = 20000.0,
    max_spread: float = 0.05,
    cooldown_bars: int = 12,
) -> dict:
    links = select_links(
        graph,
        max_links=max_links,
        min_volume=min_volume,
        max_spread=max_spread,
    )
    series = series_for_links(graph, links, days)
    diagnostics = [
        stats
        for link in links
        if (stats := link_stats(link, graph, series)) is not None
    ]
    trades = []
    tested = []
    for link in links:
        rows = run_link(
            link,
            graph,
            series,
            entry=entry,
            exit_gap=exit_gap,
            take_profit=take_profit,
            stop_loss=stop_loss,
            max_bars=max_bars,
            stake=stake,
            cooldown_bars=cooldown_bars,
        )
        if rows:
            tested.append(link.label)
        trades.extend(rows)
    vals = [t["ret"] for t in trades]
    if not vals:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "links": len(links),
            "tradeable_links": sum(1 for d in diagnostics if d["tradeable"]),
            "links_with_trades": 0,
            "trades": 0,
            "mean": None,
            "detail": [],
            "diagnostics": diagnostics,
        }
    mean = sum(vals) / len(vals)
    se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1) / len(vals))
    equity = []
    running = 0.0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        running += t["pnl"]
        equity.append(running)
    peak = equity[0]
    drawdown = 0.0
    for x in equity:
        peak = max(peak, x)
        drawdown = min(drawdown, x - peak)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "links": len(links),
        "tradeable_links": sum(1 for d in diagnostics if d["tradeable"]),
        "links_with_trades": len(tested),
        "trades": len(vals),
        "mean": mean,
        "t": mean / se if se else None,
        "pnl": running,
        "max_dd": drawdown,
        "win": sum(1 for v in vals if v > 0) / len(vals),
        "avg_bars": sum(t["bars"] for t in trades) / len(trades),
        "diagnostics": diagnostics,
        "detail": trades,
    }


__all__ = [
    "link_stats",
    "run_cross_backtest",
    "run_link",
    "select_links",
    "series_for_links",
]
