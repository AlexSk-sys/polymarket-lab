"""Event Graph Coherence: Gamma event/market ingestion."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"


def _get(path: str, params: dict) -> list | dict:
    url = f"{GAMMA}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "pm-lab/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_market(m: dict) -> dict | None:
    toks = m.get("clobTokenIds")
    if isinstance(toks, str):
        try:
            toks = json.loads(toks)
        except json.JSONDecodeError:
            return None
    outs = m.get("outcomes")
    if isinstance(outs, str):
        try:
            outs = json.loads(outs)
        except json.JSONDecodeError:
            outs = []
    if not toks or len(toks) < 2:
        return None
    bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None
    return {
        "condition_id": m.get("conditionId") or "",
        "question": m.get("question") or "",
        "tokens": toks,
        "outcomes": outs or [],
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0,
        "volume": _f(m.get("volumeNum")),
        "liquidity": _f(m.get("liquidityNum")),
        "end_date": m.get("endDate") or "",
        "active": bool(m.get("active", True)) and not bool(m.get("closed")),
    }


def fetch_events(max_pages: int = 4, page_size: int = 100) -> list[dict]:
    out: list[dict] = []
    for page in range(max_pages):
        try:
            rows = _get(
                "/events",
                {
                    "closed": "false",
                    "limit": page_size,
                    "offset": page * page_size,
                    "with_nested_markets": "true",
                },
            )
        except Exception:  # noqa: BLE001 - network fallback, caller skips
            break
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < page_size:
            break
    return out


def parse_event(e: dict) -> dict:
    markets = []
    for m in e.get("markets") or []:
        pm = parse_market(m)
        if pm and pm["active"]:
            markets.append(pm)
    return {
        "id": e.get("id"),
        "slug": e.get("slug") or "",
        "title": e.get("title") or "",
        "neg_risk": bool(e.get("negRisk")),
        "markets": markets,
        "volume": _f(e.get("volume")),
        "liquidity": _f(e.get("liquidity")),
    }
