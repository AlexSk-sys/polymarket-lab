"""Live dashboard server for polymarket-lab.

Why this exists: the static dashboard could only show REALISED P&L, which
changes when a market resolves -- once a day at best. This server marks the open
positions to market on every poll, so the equity line actually moves.

It also keeps a minute-by-minute history in `live_history.jsonl`, because a
single number is not a chart.

    python serve.py [--port 8765] [--stake 20]
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
GAMMA = "https://gamma-api.polymarket.com"

CACHE_TTL = 60.0  # seconds between live re-marks
HISTORY_EVERY = 60.0  # seconds between history points

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}
_computing = False
_last_hist = 0.0
_stake = 20.0


def _get(url: str):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "pm-lab/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _market(cid: str) -> dict | None:
    try:
        rows = _get(f"{GAMMA}/markets?condition_ids={cid}&closed=false")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    for m in rows:
        if str(m.get("conditionId", "")).lower() == cid.lower():
            return m
    return None


def _mark(m: dict | None) -> float | None:
    if not m:
        return None
    try:
        bid, ask = float(m.get("bestBid")), float(m.get("bestAsk"))
        if 0 < bid <= ask < 1:
            return (bid + ask) / 2
    except (TypeError, ValueError):
        pass
    try:
        last = float(m.get("lastTradePrice"))
        if 0 < last < 1:
            return last
    except (TypeError, ValueError):
        pass
    return None


def _load_paper() -> list[dict]:
    p = BASE / "paper.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _history() -> list[dict]:
    p = BASE / "live_history.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-2000:]


def live_state() -> dict:
    global _computing
    now = time.time()
    with _lock:
        if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
            return _cache["data"]
        # A re-mark of ~82 markets takes tens of seconds. Without this guard the
        # page's poll would start a new batch while the previous one is still
        # running, and they pile up until nothing returns at all.
        if _computing and _cache["data"]:
            return _cache["data"]
        _computing = True
    try:
        return _recompute(now)
    finally:
        with _lock:
            _computing = False


def _recompute(now: float) -> dict:
    global _last_hist
    bets = _load_paper()
    settled = [b for b in bets if b.get("pnl") is not None]
    open_bets = [b for b in bets if b.get("pnl") is None]

    realized = sum(b["pnl"] for b in settled) * _stake

    with ThreadPoolExecutor(max_workers=12) as pool:
        marks = list(pool.map(lambda b: _mark(_market(b["condition_id"])), open_bets))

    rows = []
    unreal = 0.0
    for b, px in zip(open_bets, marks):
        if px is None:
            continue
        pnl = (px - b["entry"]) / b["entry"] if b["entry"] else 0.0
        unreal += pnl * _stake
        rows.append(
            {
                "q": (b.get("question") or "")[:60],
                "entry": b["entry"],
                "mark": round(px, 4),
                "pnl_pct": pnl,
                "pnl": pnl * _stake,
                "end_date": (b.get("end_date") or "")[:10],
            }
        )
    rows.sort(key=lambda r: r["pnl"], reverse=True)

    equity = realized + unreal
    state = {
        "ts": now,
        "equity": equity,
        "realized": realized,
        "unrealized": unreal,
        "settled": len(settled),
        "open": len(open_bets),
        "marked": len(rows),
        "stake": _stake,
        "positions": rows,
    }

    if now - _last_hist >= HISTORY_EVERY:
        _last_hist = now
        with (BASE / "live_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": now,
                        "equity": equity,
                        "realized": realized,
                        "unrealized": unreal,
                    }
                )
                + "\n"
            )
    state["history"] = _history()

    with _lock:
        _cache["ts"] = now
        _cache["data"] = state
    return state


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(BASE), **kw)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/live":
            try:
                body = json.dumps(live_state(), ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as exc:  # noqa: BLE001 - a bad mark must not hang the page
                body = json.dumps({"error": str(exc)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


def main() -> None:
    global _stake
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--stake", type=float, default=20.0)
    args = ap.parse_args()
    _stake = args.stake
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {BASE} on http://127.0.0.1:{args.port}/dashboard.html")
    print(f"live endpoint: /api/live   stake ${_stake:.0f} per bet")
    srv.serve_forever()


if __name__ == "__main__":
    main()
