"""Turn Telegram news matches into paper positions.

Reads ``news_signals.json`` (written by ``tg_news.py``) and appends new
positions to ``news_paper.json`` in the same shape as the signal files, so the
existing ``pm_lab.py resolve`` settles them.

The direction layer is deliberately rule-based for now: public headlines are
fast, and a learned model needs forward labels. Rules cover the two event
families where direction is unambiguous: crypto hacks/negative news and
Hormuz/geopolitics escalation. Everything else stays unscored instead of
guessing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
SIGNALS = BASE / "news_signals.json"
LEDGER = BASE / "news_paper.json"
STAKE = 20.0
MAX_NEW_PER_RUN = 2
MAX_OPEN = 6


def direction_for(question: str, text: str) -> str | None:
    q, t = question.lower(), text.lower()
    crypto = any(
        x in q for x in ("bitcoin", "btc", "ethereum", "eth ", "solana", "crypto")
    )
    if crypto:
        bearish = any(
            x in t
            for x in (
                "hack",
                "взлом",
                "binance",
                "бинанс",
                "stolen",
                "ликвидац",
                "likvid",
                "падение",
                "обвал",
                "crash",
                "plunge",
                "exploit",
            )
        )
        bullish = any(
            x in t for x in ("approval", "одобрен", "etf", "rally", "рост", "surge")
        )
        if bearish:
            if any(x in q for x in ("dip to", "below", "drop", "under")):
                return "YES"
            if any(x in q for x in ("reach", "above", "hit", "higher")):
                return "NO"
        if bullish:
            if any(x in q for x in ("reach", "above", "hit", "higher")):
                return "YES"
            if any(x in q for x in ("dip to", "below", "drop")):
                return "NO"
    if "hormuz" in q and "normal" in q:
        if any(
            x in t
            for x in (
                "атак",
                "удар",
                "хусит",
                "война",
                "эскалац",
                "attack",
                "strike",
                "houthi",
            )
        ):
            return "NO"
    return None


def event_for(signal: dict) -> str:
    text = " ".join(signal.get("news") or []).lower()
    if any(x in text for x in ("binance", "бинанс", "взлом", "hack", "stolen")):
        return "binance_hack"
    if any(x in text for x in ("houthi", "хусит", "нефт", "hormuz", "ормуз")):
        return "houthi_oil"
    return "other"


def main() -> int:
    if not SIGNALS.exists():
        print("no news_signals.json")
        return 0
    data = json.loads(SIGNALS.read_text(encoding="utf-8"))
    signals = data.get("signals", [])
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []
    seen = {x.get("condition_id") for x in ledger}
    open_events = set()
    for x in ledger:
        if x.get("resolution") is not None:
            continue
        ev = x.get("event")
        if not ev:
            title = (x.get("title") or "").lower()
            ev = (
                "binance_hack"
                if any(
                    k in title for k in ("bitcoin", "btc", "ethereum", "eth ", "crypto")
                )
                else "houthi_oil"
                if "hormuz" in title
                else "other"
            )
            x["event"] = ev
        open_events.add(ev)
    open_now = sum(1 for x in ledger if x.get("resolution") is None)
    if open_now >= MAX_OPEN:
        print(f"ledger has {open_now} open positions; cap {MAX_OPEN}")
        return 0

    now = datetime.now(UTC)
    candidates = []
    for s in signals:
        q = s.get("question") or ""
        headlines = " ".join(s.get("news") or [])
        direction = direction_for(q, headlines)
        if not direction:
            continue
        if s.get("condition_id") in seen:
            continue
        change = float(s.get("change_1d") or 0.0)
        # The news direction is already priced if the market moved that way.
        if direction == "YES" and change > 0.05:
            continue
        if direction == "NO" and change < -0.05:
            continue
        tokens = s.get("token_ids")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                continue
        if not tokens or len(tokens) < 2:
            continue
        if direction == "YES":
            price = float(s.get("ask") or 0.0)
            token, outcome = tokens[0], "Yes"
        else:
            price = round(1.0 - float(s.get("bid") or 0.0), 4)
            token, outcome = tokens[1], "No"
        if not (0.05 <= price <= 0.95):
            continue
        score = min(1.0, (s.get("hits") or 0) / 10.0) + max(
            0.0, -change if direction == "YES" else change
        )
        ev = event_for(s)
        if ev != "other" and ev in open_events:
            continue
        candidates.append((score, s, direction, price, token, outcome, ev))

    candidates.sort(key=lambda x: -x[0])
    added = 0
    used_categories = set()
    for score, s, direction, price, token, outcome, ev in candidates:
        if added >= MAX_NEW_PER_RUN:
            break
        # One new position per market and one per event family per run.
        family = (
            "crypto"
            if direction in ("YES", "NO")
            and any(
                x in (s.get("question") or "").lower()
                for x in ("bitcoin", "btc", "ethereum", "crypto", "solana")
            )
            else "geopolitics"
        )
        if family in used_categories:
            continue
        used_categories.add(family)
        ledger.append(
            {
                "wallet": "news",
                "name": (s.get("sources") or ["news"])[0],
                "news_source": (s.get("sources") or ["news"])[0],
                "side": "BUY",
                "usdc": STAKE,
                "whale_price": price,
                "now_price": price,
                "moved": 0.0,
                "title": s.get("question") or "",
                "outcome": outcome,
                "token_id": token,
                "condition_id": s.get("condition_id") or "",
                "trade_ts": now.timestamp(),
                "detected_at": now.isoformat(),
                "news_headline": (s.get("news") or [""])[0][:240],
                "news_score": round(score, 3),
                "event": ev,
                "returns": {},
            }
        )
        added += 1
        open_events.add(ev)
        print(f"+ {family} {direction} {price:.3f} | {s.get('question', '')[:70]}")

    if added:
        LEDGER.write_text(
            json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    print(f"added {added}; ledger {len(ledger)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
