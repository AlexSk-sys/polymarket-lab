"""Capital and capacity projection for the longshot-NO strategy.

The edge is per position, so the dollar result is roughly
``stake * mean_return * number_of_positions``. This module makes the scaling
explicit: total P&L, peak simultaneous capital, return on peak capital, and a
stress case with extra losses.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics

from .backtest import load_history, no_return


def _picks(
    rows: list[dict], min_price: float, max_price: float, target: float = 0.05
) -> list[dict]:
    sub = [r for r in rows if min_price <= r["price"] <= max_price]
    by_event: dict[str, dict] = {}
    for row in sub:
        score = abs(row["price"] - target)
        current = by_event.get(row["event_id"])
        if current is None or score < abs(current["price"] - target):
            by_event[row["event_id"]] = row
    return list(by_event.values())


def _parse_date(value: str) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _concurrency(picks: list[dict]) -> dict:
    events = []
    for row in picks:
        end = _parse_date(row.get("end_date") or "")
        if end is None:
            continue
        events.append((end - 7 * 86400, 1))
        events.append((end, -1))
    if not events:
        return {"max_open": None, "avg_active": None, "active_days": None}
    events.sort()
    current = 0
    max_open = 0
    capital_days = 0.0
    active_days = 0.0
    last = None
    for ts, delta in events:
        if last is not None:
            days = (ts - last) / 86400.0
            capital_days += current * days
            if current > 0:
                active_days += days
        current += delta
        max_open = max(max_open, current)
        last = ts
    return {
        "max_open": max_open,
        "avg_active": capital_days / active_days if active_days else None,
        "active_days": active_days,
    }


def project(
    rows: list[dict],
    *,
    stake: float = 100.0,
    buffer: float = 0.005,
    min_price: float = 0.02,
    max_price: float = 0.10,
    target: float = 0.05,
    stress_losses: int = 3,
) -> dict | None:
    picks = _picks(rows, min_price, max_price, target)
    if not picks:
        return None
    returns = [no_return(r["price"], r["outcome"], buffer) for r in picks]
    n = len(returns)
    mean = sum(returns) / n
    sd = statistics.pstdev(returns)
    total_pnl = sum(returns) * stake
    by_year = {}
    for year in sorted({r["year"] for r in picks}):
        year_rows = [r for r in picks if r["year"] == year]
        year_returns = [no_return(r["price"], r["outcome"], buffer) for r in year_rows]
        by_year[year] = {
            "n": len(year_rows),
            "mean_return": round(sum(year_returns) / len(year_returns), 6),
            "pnl": round(sum(year_returns) * stake, 2),
        }
    concurrency = _concurrency(picks)
    peak_capital = (
        concurrency["max_open"] * stake if concurrency["max_open"] is not None else None
    )
    return {
        "n": n,
        "stake": stake,
        "mean_return": round(mean, 6),
        "sd": round(sd, 6),
        "t": round(mean / (sd / math.sqrt(n)), 3) if sd else None,
        "turnover": round(stake * n, 2),
        "total_pnl": round(total_pnl, 2),
        "return_on_turnover": round(total_pnl / (stake * n), 6),
        "by_year": by_year,
        "max_open": concurrency["max_open"],
        "avg_active_positions": round(concurrency["avg_active"], 2)
        if concurrency["avg_active"] is not None
        else None,
        "active_days": round(concurrency["active_days"], 1)
        if concurrency["active_days"] is not None
        else None,
        "peak_capital": round(peak_capital, 2) if peak_capital else None,
        "return_on_peak_capital": round(total_pnl / peak_capital, 6)
        if peak_capital
        else None,
        f"stress_{stress_losses}_extra_losses": round(
            total_pnl - stress_losses * stake, 2
        ),
    }


def project_table(rows: list[dict], stakes: list[float], **kwargs) -> list[dict]:
    return [
        p for stake in stakes if (p := project(rows, stake=stake, **kwargs)) is not None
    ]


__all__ = ["load_history", "project", "project_table"]
