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

CACHE_TTL = 5.0  # seconds between live re-marks (Gamma itself is the limit)
HISTORY_EVERY = 60.0  # seconds between persisted history points

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}
_computing = False
_last_hist = 0.0
_stake = 20.0


def _get(url: str, timeout: float = 5.0):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "pm-lab/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _markets(cids: list[str]) -> dict[str, dict]:
    """All open markets from Gamma in a few parallel small requests.

    One id per request took ~76 calls and tens of seconds. Repeated
    condition_ids in one URL works, but a 77-id query occasionally hung for the
    whole 20 s timeout on Gamma's side; chunks of 20 in parallel are ~0.2 s each
    and one slow chunk only loses its own marks for one cycle.
    """

    def chunk(part: list[str]) -> list:
        qs = "&".join("condition_ids=" + c for c in part)
        try:
            rows = _get(f"{GAMMA}/markets?limit=100&{qs}")
        except (urllib.error.URLError, TimeoutError, ValueError):
            return []
        return rows if isinstance(rows, list) else []

    parts = [cids[i : i + 20] for i in range(0, len(cids), 20)]
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for rows in pool.map(chunk, parts):
            for m in rows:
                cid = str(m.get("conditionId", "")).lower()
                if cid:
                    out[cid] = m
    return out


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


def _recompute_guarded() -> dict | None:
    global _computing
    with _lock:
        if _computing:
            return _cache["data"]
        _computing = True
    try:
        return _recompute(time.time())
    finally:
        with _lock:
            _computing = False


def live_state() -> dict:
    """Return the last snapshot immediately; the background thread refreshes it.

    Gamma occasionally takes 20 s for the batch. Computing on the request
    thread made the page wait that long; now the page always gets the last good
    state and the timestamp tells how fresh it is.
    """
    with _lock:
        if _cache["data"] is not None:
            return _cache["data"]
    while True:
        data = _recompute_guarded()
        if data:
            return data
        time.sleep(0.1)


def _background() -> None:
    while True:
        try:
            _recompute_guarded()
        except Exception:  # noqa: BLE001, S110 - keep the refresher alive
            pass
        time.sleep(CACHE_TTL)


def _recompute(now: float) -> dict:
    global _last_hist
    bets = _load_paper()
    settled = [b for b in bets if b.get("pnl") is not None]
    open_bets = [b for b in bets if b.get("pnl") is None]

    realized = sum(b["pnl"] for b in settled) * _stake

    by_id = _markets([b["condition_id"] for b in open_bets])

    rows = []
    unreal = 0.0
    for b in open_bets:
        px = _mark(by_id.get(b["condition_id"].lower()))
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
    # The file gets one point a minute (a day of history stays small). The chart
    # also gets a provisional point at `now`, so the last segment moves every
    # cache cycle instead of waiting for the next persisted point.
    if not state["history"] or state["history"][-1]["ts"] < now - 1:
        state["history"].append(
            {
                "ts": now,
                "equity": equity,
                "realized": realized,
                "unrealized": unreal,
            }
        )

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
    threading.Thread(target=_background, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {BASE} on http://127.0.0.1:{args.port}/dashboard_local.html")
    print(f"live endpoint: /api/live   stake ${_stake:.0f} per bet")
    srv.serve_forever()


if __name__ == "__main__":
    main()
