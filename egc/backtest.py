"""Historical backtest for EGC relative-value signals.

The backtest asks one question: when a market is dislocated from the fair
probability implied by its event graph, does the edge converge enough to pay
the spread and fees before a stop/time exit?

It uses the last N days of 5-minute-ish price history, reconstructs the fair
probability at every observed timestamp from the *other* markets in the same
event, enters at the executable side, and exits when the edge closes, a
take-profit/stop is hit, or the holding limit expires.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

import pm_lab

from .model import event_model
from .signals import fee, signals_for_model


def _series_for(markets: list[dict], days: int) -> dict[str, list[tuple[float, float]]]:
    start = time.time() - days * 86400
    end = time.time()
    out = {}
    for m in markets:
        tok = m["tokens"][0]
        try:
            out[m["condition_id"]] = pm_lab.price_series(tok, start, end)
        except Exception:  # noqa: BLE001 - one bad token must not kill the event
            out[m["condition_id"]] = []
    return out


def run_event(
    event: dict,
    series: dict[str, list[tuple[float, float]]],
    *,
    entry_edge: float = 0.03,
    exit_edge: float = 0.01,
    take_profit: float = 0.15,
    stop_loss: float = 0.08,
    max_bars: int = 12,
    stake: float = 20.0,
) -> list[dict]:
    markets = event["markets"]
    if any(not series.get(m["condition_id"]) for m in markets):
        return []
    lookups = {
        m["condition_id"]: pm_lab.make_lookup(series[m["condition_id"]])
        for m in markets
    }
    stamps = sorted({t for s in series.values() for t, _ in s})[::6]
    pos = None
    trades = []
    for ts in stamps:
        prices = {}
        for m in markets:
            p = lookups[m["condition_id"]](ts, tol=900)
            if p is None:
                break
            prices[m["condition_id"]] = p
        if len(prices) != len(markets):
            continue
        # Rebuild the event with the historical mids, keeping current spreads.
        hist = {"title": event["title"], "neg_risk": event["neg_risk"], "markets": []}
        for m in markets:
            mid = prices[m["condition_id"]]
            half = max(0.001, (m["ask"] - m["bid"]) / 2.0)
            hist["markets"].append(
                {
                    **m,
                    "mid": mid,
                    "bid": max(0.001, mid - half),
                    "ask": min(0.999, mid + half),
                }
            )
        model = event_model(hist)
        if not model:
            continue
        sigs = signals_for_model(model, event["title"], min_edge=entry_edge)
        if pos is None:
            if not sigs:
                continue
            sig = sigs[0]
            pos = {
                "sig": sig,
                "entry": sig["entry"],
                "entry_ts": ts,
                "bars": 0,
                "condition_id": sig["condition_id"],
            }
            continue
        pos["bars"] += 1
        m = next(m for m in hist["markets"] if m["condition_id"] == pos["condition_id"])
        q = next(
            q
            for mm, q in zip(model["markets"], model["q"])
            if mm["condition_id"] == pos["condition_id"]
        )
        if pos["sig"]["side"] == "YES":
            mark = m["bid"]
            ret = (mark - pos["entry"]) / pos["entry"]
            edge_now = q - m["ask"]
        else:
            mark = 1.0 - m["ask"]
            ret = (mark - pos["entry"]) / pos["entry"]
            edge_now = m["bid"] - q
        fees = fee(pos["entry"]) + fee(mark)
        ret_net = ret - fees
        if (
            edge_now <= exit_edge
            or ret_net >= take_profit
            or ret_net <= -stop_loss
            or pos["bars"] >= max_bars
        ):
            trades.append(
                {
                    "event": event["title"],
                    "question": pos["sig"]["question"],
                    "side": pos["sig"]["side"],
                    "entry": pos["entry"],
                    "exit": mark,
                    "ret": ret_net,
                    "pnl": ret_net * stake,
                    "bars": pos["bars"],
                    "entry_ts": pos["entry_ts"],
                    "exit_ts": ts,
                    "reason": "edge" if edge_now <= exit_edge else "tp/sl/time",
                }
            )
            pos = None
    return trades


def run_backtest(events: list[dict], days: int = 7, max_events: int = 12, **kw) -> dict:
    trades = []
    used = 0
    for e in events:
        if used >= max_events:
            break
        if not e.get("markets") or not (3 <= len(e["markets"]) <= 12):
            continue
        if not event_model(e):
            continue
        series = _series_for(e["markets"], days)
        if any(not s for s in series.values()):
            continue
        used += 1
        trades.extend(run_event(e, series, **kw))
    vals = [t["ret"] for t in trades]
    if not vals:
        return {"events": used, "trades": 0, "mean": None}
    mean = sum(vals) / len(vals)
    se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1) / len(vals))
    eq = []
    run = 0.0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        run += t["pnl"]
        eq.append(run)
    peak = eq[0]
    dd = 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, x - peak)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "events": used,
        "trades": len(vals),
        "mean": mean,
        "t": mean / se if se else None,
        "pnl": run,
        "max_dd": dd,
        "win": sum(1 for v in vals if v > 0) / len(vals),
        "detail": trades,
    }
