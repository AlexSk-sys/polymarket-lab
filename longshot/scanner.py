"""Longshot overpricing strategy for Polymarket.

The historical result behind this package: seven days before resolution,
markets whose YES price is 0.02-0.10 resolve YES less often than their price
implies. Buying NO on one market per event, holding to resolution, produced a
positive return after a conservative spread buffer and the entry fee.

This module only finds live candidates. It does not place orders.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

import pm_lab

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
MIN_PRICE = 0.02
MAX_PRICE = 0.10
MIN_VOLUME = 50_000.0
MAX_SPREAD = 0.03


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_end(value: str) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def parse_candidate(market: dict, now: float | None = None) -> dict | None:
    """Normalise one Gamma market, or return None if it fails the filters."""
    now = now or dt.datetime.now(dt.UTC).timestamp()
    bid = _f(market.get("bestBid"))
    ask = _f(market.get("bestAsk"))
    volume = _f(market.get("volumeNum"))
    if bid <= 0 or ask <= bid:
        return None
    mid = (bid + ask) / 2.0
    spread = ask - bid
    if not (MIN_PRICE <= mid <= MAX_PRICE):
        return None
    if spread > MAX_SPREAD or volume < MIN_VOLUME:
        return None
    end_ts = _parse_end(market.get("endDate") or "")
    if end_ts is None or end_ts <= now:
        return None
    events = market.get("events") or []
    event = events[0] if events else {}
    event_id = str(
        event.get("id") or event.get("slug") or market.get("conditionId") or ""
    )
    return {
        "event_id": event_id,
        "event_title": event.get("title") or "",
        "condition_id": market.get("conditionId") or "",
        "question": market.get("question") or "",
        "yes_bid": bid,
        "yes_ask": ask,
        "yes_mid": mid,
        "spread": spread,
        "volume": volume,
        "end_date": market.get("endDate") or "",
        "end_ts": end_ts,
        "days_to_end": (end_ts - now) / 86400.0,
        "tokens": market.get("clobTokenIds"),
    }


def fetch_live_markets(
    *,
    days_min: int = 1,
    days_max: int = 8,
    max_pages: int = 10,
) -> list[dict]:
    """Fetch open markets ending in the next ``days_min..days_max`` days."""
    now = dt.datetime.now(dt.UTC)
    start = (now + dt.timedelta(days=days_min)).strftime("%Y-%m-%d")
    end = (now + dt.timedelta(days=days_max)).strftime("%Y-%m-%d")
    out: list[dict] = []
    for page in range(max_pages):
        try:
            rows = pm_lab.api_get(
                GAMMA_MARKETS,
                {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "volumeNum",
                    "ascending": "false",
                    "end_date_min": start,
                    "end_date_max": end,
                    "offset": page * 100,
                },
            )
        except Exception:  # noqa: BLE001 - a failed page must not kill the scan
            break
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
    return out


def scan(
    *,
    days_min: int = 3,
    days_max: int = 8,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
    min_volume: float = MIN_VOLUME,
    max_spread: float = MAX_SPREAD,
    max_pages: int = 10,
    strategy: str = "core",
) -> dict:
    """Return one candidate per event, closest to the 0.05 target."""
    markets = fetch_live_markets(
        days_min=days_min, days_max=days_max, max_pages=max_pages
    )
    now = dt.datetime.now(dt.UTC).timestamp()
    by_event: dict[str, list[dict]] = defaultdict(list)
    for market in markets:
        candidate = parse_candidate(market, now=now)
        if candidate is None:
            continue
        if not (min_price <= candidate["yes_mid"] <= max_price):
            continue
        if candidate["volume"] < min_volume or candidate["spread"] > max_spread:
            continue
        candidate["strategy"] = strategy
        by_event[candidate["event_id"]].append(candidate)

    picks = []
    for candidates in by_event.values():
        candidates.sort(key=lambda c: (abs(c["yes_mid"] - 0.05), -c["volume"]))
        picks.append(candidates[0])
    picks.sort(key=lambda c: (-c["volume"], c["days_to_end"]))
    return {"markets": len(markets), "events": len(by_event), "candidates": picks}


__all__ = ["fetch_live_markets", "parse_candidate", "scan"]
