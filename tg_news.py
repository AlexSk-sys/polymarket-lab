import asyncio, json, urllib.request, urllib.parse, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.sessions import StringSession

repo = Path(r"C:\Users\AgentPC\source\repos\telegram-mcp")
env = {}
for line in (repo / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")
reg = json.loads(
    Path(
        r"C:\Users\AgentPC\source\repos\polymarket-lab\tg_channel_registry.json"
    ).read_text(encoding="utf-8")
)
CH = [r["username"] for r in reg if r.get("username")]
ENT = {
    "iran": ["иран", "iran"],
    "israel": ["израил", "israel"],
    "hormuz": ["ормуз", "hormuz"],
    "oil": ["нефт", "oil", "opec", "саудов"],
    "crypto": ["binance", "бинанс", "bitcoin", "битко", "крипт", "ethereum", "эфир"],
    "trump": ["трамп", "trump"],
    "putin": ["путин", "putin"],
    "ukraine": ["украин", "ukraine"],
    "fed": ["фрс", "ставк", "fed", "fomc"],
    "saudi": ["саудов", "saudi"],
    "houthi": ["хусит", "houthi"],
}
CATS = {
    "iran": "iran",
    "israel": "israel",
    "hormuz": "hormuz",
    "oil": "oil",
    "bitcoin": "crypto",
    "btc": "crypto",
    "ethereum": "crypto",
    "binance": "crypto",
    "trump": "trump",
    "putin": "putin",
    "ukraine": "ukraine",
    "fed": "fed",
    "saudi": "saudi",
    "houthi": "houthi",
}


async def main():
    client = TelegramClient(
        StringSession(env["TELEGRAM_SESSION_STRING"]),
        int(env["TELEGRAM_API_ID"]),
        env["TELEGRAM_API_HASH"],
    )
    await client.connect()
    msgs = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    for ch in CH:
        try:
            ent = await client.get_entity(ch)
            async for m in client.iter_messages(ent, limit=25):
                if m.date and m.date.astimezone(timezone.utc) >= cutoff and m.message:
                    msgs.append(
                        {
                            "ch": ch,
                            "ts": m.date.astimezone(timezone.utc).isoformat(),
                            "text": m.message[:1000],
                        }
                    )
        except Exception:
            pass
    await client.disconnect()
    print("messages", len(msgs))

    def gget(params):
        req = urllib.request.Request(
            "https://gamma-api.polymarket.com/markets?"
            + urllib.parse.urlencode(params),
            headers={"Accept": "application/json", "User-Agent": "pm-lab/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    rows = []
    for off in range(0, 2000, 100):
        try:
            page = gget(
                {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "volumeNum",
                    "ascending": "false",
                    "offset": off,
                }
            )
        except Exception:
            break
        if not page:
            break
        rows += page
    now = datetime.now(timezone.utc)
    out = []
    for m in rows:
        q = m.get("question") or ""
        ql = q.lower()
        end = m.get("endDate") or ""
        try:
            ed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except:
            continue
        if ed > now + timedelta(days=240):
            continue
        keys = []
        for k, cat in CATS.items():
            if k in ql:
                keys += ENT[cat]
        tm = []
        if " vs " in ql or "vs." in ql:
            parts = re.split(r"\s+vs\.?\s+", q, flags=re.I)
            for p in parts:
                p = p.strip()
                if 3 <= len(p) <= 30:
                    tm.append(p.lower())
        if not keys and not tm:
            continue
        hits = []
        for msg in msgs:
            tl = msg["text"].lower()
            if any(k in tl for k in keys) or any(t in tl for t in tm if len(t) >= 4):
                hits.append(msg)
        if not hits:
            continue
        try:
            out.append(
                {
                    "question": q,
                    "slug": m.get("slug"),
                    "condition_id": m.get("conditionId"),
                    "token_ids": m.get("clobTokenIds"),
                    "bid": float(m.get("bestBid") or 0),
                    "ask": float(m.get("bestAsk") or 0),
                    "last": float(m.get("lastTradePrice") or 0),
                    "change_1d": float(m.get("oneDayPriceChange") or 0),
                    "volume": float(m.get("volumeNum") or 0),
                    "end": end,
                    "hits": len(hits),
                    "sources": sorted({h["ch"] for h in hits})[:5],
                    "news": [
                        h["text"][:240]
                        for h in sorted(hits, key=lambda x: x["ts"], reverse=True)[:3]
                    ],
                }
            )
        except:
            pass
    out.sort(key=lambda x: (-x["hits"], -x["volume"]))
    Path(__file__).resolve().parent / "news_signals.json".write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "channels": len(CH),
                "messages": len(msgs),
                "signals": out[:50],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print("signals", len(out), "written")


asyncio.run(main())
