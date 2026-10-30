"""Historical calibration and event-diversified backtest for longshot NO.

The input CSV is a processed snapshot of closed Polymarket markets: the YES
price seven days before resolution, the final YES outcome, the event id and the
volume. It was built from Gamma closed-market metadata plus Data-API trades
(``/v2/trades?condition_id=...``); the raw trades are not stored.

The strategy buys NO when the YES price is 0.02-0.10, one market per event, and
holds to resolution. The backtest charges the entry fee and a conservative
0.005 spread buffer. There is no exit fee because the position is held to
resolution.
"""

from __future__ import annotations

import csv
import math
import random
import statistics
from pathlib import Path

from scipy.stats import binom

from egc.signals import fee

BUCKETS = (
    (0.00, 0.02),
    (0.02, 0.05),
    (0.05, 0.10),
    (0.10, 0.20),
    (0.20, 0.40),
    (0.40, 0.60),
    (0.60, 0.80),
    (0.80, 0.90),
    (0.90, 0.95),
    (0.95, 0.98),
    (0.98, 1.00),
)


def load_history(path: str | Path) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append(
                    {
                        "event_id": row["event_id"],
                        "year": row["year"],
                        "price": float(row["price"]),
                        "outcome": int(float(row["outcome"])),
                        "volume": float(row["volume"]),
                        "question": (row.get("question") or "").strip(),
                        "end_date": (row.get("end_date") or "").strip(),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def no_return(price: float, outcome: int, buffer: float = 0.005) -> float:
    """Return on capital for buying NO and holding to 0/1.

    ``price`` is the YES mid/last. Buying NO costs ``1 - price`` plus a spread
    buffer, so a larger buffer must make the entry **more expensive**. The
    earlier version subtracted the buffer, overstating every NO return.
    """
    cost = min(0.999, max(0.001, 1.0 - price + buffer))
    pnl = (1.0 - outcome) - cost - fee(cost)
    return pnl / cost


def calibration(
    rows: list[dict],
    *,
    buffer: float = 0.005,
    buckets: tuple[tuple[float, float], ...] = BUCKETS,
) -> list[dict]:
    out = []
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= r["price"] < hi]
        if not sub:
            continue
        n = len(sub)
        price = sum(r["price"] for r in sub) / n
        win_rate = sum(r["outcome"] for r in sub) / n
        returns = [no_return(r["price"], r["outcome"], buffer) for r in sub]
        out.append(
            {
                "bucket": f"{lo:.2f}-{hi:.2f}",
                "n": n,
                "price": round(price, 4),
                "yes_rate": round(win_rate, 4),
                "calibration": round(win_rate - price, 4),
                "no_return": round(sum(returns) / n, 6),
            }
        )
    return out


def _pick_events(sub: list[dict], target: float = 0.05) -> list[dict]:
    by_event: dict[str, dict] = {}
    for row in sub:
        score = abs(row["price"] - target)
        current = by_event.get(row["event_id"])
        if current is None or score < abs(current["price"] - target):
            by_event[row["event_id"]] = row
    return list(by_event.values())


def event_portfolio(
    rows: list[dict],
    *,
    min_price: float = 0.02,
    max_price: float = 0.10,
    buffer: float = 0.005,
    target: float = 0.05,
    bootstrap: int = 3000,
    seed: int = 7,
) -> dict | None:
    sub = [r for r in rows if min_price <= r["price"] <= max_price]
    picks = _pick_events(sub, target=target)
    if not picks:
        return None
    returns = [no_return(r["price"], r["outcome"], buffer) for r in picks]
    n = len(returns)
    mean = sum(returns) / n
    sd = statistics.pstdev(returns)
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    losses = sum(1 for r in picks if r["outcome"] == 1)
    expected = sum(r["price"] for r in picks)
    p_value = float(binom.cdf(losses, n, expected / n)) if expected > 0 else 0.0

    by_event: dict[str, list[float]] = {}
    for r in picks:
        by_event.setdefault(r["event_id"], []).append(
            no_return(r["price"], r["outcome"], buffer)
        )
    events = list(by_event)
    rng = random.Random(seed)
    means = []
    for _ in range(bootstrap):
        sample = []
        for _ in events:
            sample.extend(by_event[rng.choice(events)])
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]

    equity = []
    running = 0.0
    for r in sorted(picks, key=lambda x: (x["year"], x["event_id"])):
        running += no_return(r["price"], r["outcome"], buffer)
        equity.append(running)
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value - peak)

    yearly = {}
    for year in sorted({r["year"] for r in picks}):
        year_rows = [r for r in picks if r["year"] == year]
        vals = [no_return(r["price"], r["outcome"], buffer) for r in year_rows]
        yearly[year] = {
            "n": len(vals),
            "losses": sum(1 for r in year_rows if r["outcome"] == 1),
            "mean_return": round(sum(vals) / len(vals), 6),
        }
    return {
        "n": n,
        "events": len(events),
        "losses": losses,
        "expected_losses": round(expected, 2),
        "binomial_p": round(p_value, 6),
        "mean_return": round(mean, 6),
        "t": round(t_stat, 3),
        "win_rate": round(1.0 - losses / n, 4),
        "bootstrap_95": [round(lo, 6), round(hi, 6)],
        "total_return_units": round(running, 4),
        "max_drawdown_units": round(drawdown, 4),
        "yearly": yearly,
    }


def run_backtest(path: str | Path) -> dict:
    rows = load_history(path)
    return {
        "rows": len(rows),
        "calibration": calibration(rows),
        "portfolio_0.02_0.10": event_portfolio(rows),
        "portfolio_0.02_0.05": event_portfolio(rows, max_price=0.05),
        "portfolio_0.05_0.10": event_portfolio(rows, min_price=0.05),
    }


__all__ = [
    "calibration",
    "event_portfolio",
    "load_history",
    "no_return",
    "run_backtest",
]
