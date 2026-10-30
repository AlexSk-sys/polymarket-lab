"""Paper ledger for the longshot-NO strategy.

Each position buys NO, so it pays 1 if the market resolves NO and 0 if it
resolves YES. The stake is the NO cost; the maximum loss is the stake plus the
entry fee. No exit fee is charged because the position is held to resolution.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pm_lab
from egc.signals import fee

DEFAULT_LEDGER = "longshot_paper.json"
DEFAULT_STAKE = 20.0
DEFAULT_BUFFER = 0.005


def load_ledger(path: str | Path = DEFAULT_LEDGER) -> dict:
    p = Path(path)
    if not p.exists():
        return {
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "stake": DEFAULT_STAKE,
            "positions": [],
        }
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("positions", [])
    data.setdefault("stake", DEFAULT_STAKE)
    return data


def save_ledger(ledger: dict, path: str | Path = DEFAULT_LEDGER) -> None:
    Path(path).write_text(
        json.dumps(ledger, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def add_positions(
    ledger: dict,
    candidates: list[dict],
    *,
    stake: float = DEFAULT_STAKE,
    max_open: int = 20,
    max_new: int = 5,
    buffer: float = DEFAULT_BUFFER,
) -> list[dict]:
    """Add paper NO positions, one per event, skipping existing markets."""
    known = {p.get("id") for p in ledger["positions"]}
    known_events = {p.get("event_id") for p in ledger["positions"]}
    open_count = sum(1 for p in ledger["positions"] if p.get("status") == "open")
    added = []
    for candidate in sorted(
        candidates, key=lambda c: (abs(c["yes_mid"] - 0.05), -c["volume"])
    ):
        if len(added) >= max_new or open_count >= max_open:
            break
        cid = candidate.get("condition_id")
        event_id = candidate.get("event_id") or ""
        if not cid or cid in known or event_id in known_events:
            continue
        entry = min(0.999, 1.0 - float(candidate["yes_bid"]) + buffer)
        if entry <= 0.001:
            continue
        shares = stake / entry
        position = {
            "id": cid,
            "event_id": candidate.get("event_id") or "",
            "event_title": candidate.get("event_title") or "",
            "question": candidate.get("question") or "",
            "side": "NO",
            "yes_price_at_entry": round(float(candidate["yes_mid"]), 6),
            "entry_price": round(float(entry), 6),
            "stake": round(float(stake), 4),
            "shares": round(float(shares), 6),
            "entry_fee": round(float(fee(entry) * shares), 6),
            "entry_ts": dt.datetime.now(dt.UTC).isoformat(),
            "end_date": candidate.get("end_date") or "",
            "status": "open",
            "resolved_at": None,
            "outcome_yes": None,
            "pnl": None,
            "ret": None,
        }
        ledger["positions"].append(position)
        known.add(cid)
        known_events.add(event_id)
        open_count += 1
        added.append(position)
    return added


def _outcome(market: dict) -> float | None:
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return float(raw[0])
    except (TypeError, ValueError):
        return None


def resolve(
    ledger: dict,
    *,
    closed_markets: dict[str, dict] | None = None,
) -> list[dict]:
    """Resolve open positions from closed Gamma markets."""
    open_rows = [p for p in ledger["positions"] if p.get("status") == "open"]
    if not open_rows:
        return []
    cids = [p["id"] for p in open_rows]
    if closed_markets is None:
        closed_markets = pm_lab._gamma_markets(cids, closed=True)
    resolved = []
    for position in open_rows:
        market = closed_markets.get(position["id"].lower()) or closed_markets.get(
            position["id"]
        )
        if not market:
            continue
        outcome_yes = _outcome(market)
        if outcome_yes is None:
            continue
        outcome_no = 1.0 - outcome_yes
        pnl = (
            position["shares"] * (outcome_no - position["entry_price"])
            - position["entry_fee"]
        )
        position["status"] = "closed"
        position["resolved_at"] = dt.datetime.now(dt.UTC).isoformat()
        position["outcome_yes"] = outcome_yes
        position["pnl"] = round(float(pnl), 4)
        position["ret"] = (
            round(float(pnl / position["stake"]), 6) if position["stake"] else None
        )
        resolved.append(position)
    return resolved


def summarize(ledger: dict) -> dict:
    open_rows = [p for p in ledger["positions"] if p.get("status") == "open"]
    closed = [p for p in ledger["positions"] if p.get("status") == "closed"]
    pnls = [float(p["pnl"]) for p in closed if p.get("pnl") is not None]
    wins = sum(1 for p in closed if (p.get("pnl") or 0) > 0)
    equity = []
    running = 0.0
    for p in sorted(closed, key=lambda x: x.get("resolved_at") or ""):
        running += float(p.get("pnl") or 0)
        equity.append(running)
    peak = equity[0] if equity else 0.0
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value - peak)
    closed_stake = sum(float(p.get("stake") or 0) for p in closed)
    return {
        "open": len(open_rows),
        "closed": len(closed),
        "open_stake": round(sum(float(p.get("stake") or 0) for p in open_rows), 2),
        "closed_stake": round(closed_stake, 2),
        "pnl": round(sum(pnls), 2),
        "return_on_closed_stake": round(sum(pnls) / closed_stake, 6)
        if closed_stake
        else None,
        "win_rate": round(wins / len(closed), 4) if closed else None,
        "mean_return": round(
            sum(float(p.get("ret") or 0) for p in closed) / len(closed), 6
        )
        if closed
        else None,
        "max_drawdown": round(drawdown, 2),
        "expectancy_per_position": round(sum(pnls) / len(closed), 2)
        if closed
        else None,
    }


__all__ = ["add_positions", "load_ledger", "resolve", "save_ledger", "summarize"]
