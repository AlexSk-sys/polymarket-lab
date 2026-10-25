"""polymarket-lab -- read-only "smart money" sensor for Polymarket.

Why this exists
---------------
A viral post claims a bot turned $200 into $14,300 by scanning tens of
thousands of Polymarket wallets, ranking them on "profit and win rate", and
copying the entries of the best ones. Two of those three ideas do not survive
contact with the data this repo reads:

  1. WIN RATE IS A TRAP. On a prediction market the highest win rate belongs to
     whoever sells far out-of-the-money outcomes: many small wins, one total
     loss. Ranking by win rate systematically selects the *worst* risk-adjusted
     wallets. `wintrap` measures this on live wallets instead of asserting it.

  2. PNL ALONE IS NOT DIRECTIONAL EDGE. The top of the public leaderboard is
     dominated by market makers and liquidity-mining wallets whose "profit" is
     maker rebates and reward income, not prediction. Copying them copies
     nothing. `rank` splits the income sources and says so.

  3. ENTRIES ARE PUBLIC. Trades are on-chain, so by the time you detect an entry
     the price has already moved. `signals` prices that gap against the live
     CLOB book rather than pretending you get the whale's fill.

So this is a sensor plus a judge, never a promise:

  collect   sensor  snapshot leaderboard + per-wallet stats, frozen, no verdict
  rank      judge   honest scorecard; flags market makers and rebate farmers
  wintrap   judge   measures the win-rate trap on live wallets
  signals   sensor  fresh entries by watched wallets + real slippage vs the book
  evaluate  judge   did a frozen signal actually pay, from price history alone

Read-only. No API key, no orders, no wallet. Research instrument, not
investment advice.

Data sources (all public, verified live on 2026-10-23):
  https://data-api.polymarket.com/v2   Data API v2 (leaderboard, stats, trades,
                                       positions, holders, prices-history)
  https://clob.polymarket.com          CLOB order book
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import requests

DATA = "https://data-api.polymarket.com/v2"
CLOB = "https://clob.polymarket.com"
TIMEOUT = 30
MAX_RETRIES = 5
PAGE = 500

# --- thresholds for the honest verdict. Auditable, in one place. ------------
MIN_TRADES = 100  # fewer decided trades and the "record" is a coin flip
MIN_ROI = 0.0  # realized PnL must beat zero on its own volume
MAX_MM_SHARE = 0.50  # >half of profit from rebates/rewards => market maker
MIN_DIR_SHARE = 0.50  # >half of profit must come from directional positions
MAX_UNREALIZED = 0.50  # unbooked open loss >half of realized => not banked

_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"Accept": "application/json"})
        _local.session = s
    return s


def api_get(url: str, params: dict | None = None) -> dict | list:
    """GET with backoff. Honours 429 Retry-After (documented by Polymarket)."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = _session().get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2**attempt))
                time.sleep(min(wait, 30.0))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:  # network / 5xx
            last = exc
            time.sleep(min(0.5 * (2**attempt), 10.0))
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} tries: {last}")


def _rows(payload: dict | list) -> list:
    if isinstance(payload, list):
        return payload
    return payload.get("data") or []


def _cursor(payload: dict | list) -> str | None:
    if isinstance(payload, dict):
        return (payload.get("pagination") or {}).get("next_cursor")
    return None


def _paginate(path: str, params: dict, max_rows: int) -> list:
    rows: list = []
    cursor = None
    while len(rows) < max_rows:
        p = dict(params)
        p["limit"] = min(PAGE, max_rows - len(rows))
        if cursor:
            p["cursor"] = cursor
        payload = api_get(f"{DATA}/{path}", p)
        page = _rows(payload)
        if not page:
            break
        rows.extend(page)
        cursor = _cursor(payload)
        if not cursor:
            break
    return rows


def fnum(x: float | None, money: bool = False) -> str:
    if x is None:
        return "n/a"
    if money:
        return f"{x:,.0f}"
    return f"{x:,.3f}"


def fpct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:,.1f}%"


# ---------------------------------------------------------------------------
# sensor: collect
# ---------------------------------------------------------------------------


def fetch_leaderboard(category: str, period: str, order_by: str, limit: int) -> list:
    # v2 is cursor-only: sending `offset` is a 400, not a fallback.
    return _paginate(
        "leaderboard",
        {"category": category, "timePeriod": period, "orderBy": order_by},
        limit,
    )


def fetch_stats(wallet: str) -> dict:
    payload = api_get(f"{DATA}/user-stats", {"user": wallet})
    if isinstance(payload, dict):
        return payload.get("data") or {}
    return {}


def metrics(stats: dict) -> dict:
    """Turn one wallet's all-time PnL blob into comparable numbers."""
    p = (stats or {}).get("all_time_pnl") or {}

    def g(key: str) -> float:
        try:
            return float(p.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    realized = g("realized_pnl")
    unrealized = g("unrealized_pnl")
    position = g("position_pnl")
    rebates = g("maker_rebate") + g("taker_rebate")
    rewards = g("reward_income") + g("yield_income") + g("sponsored_income")
    non_dir = rebates + rewards
    volume = g("volume_usdc") or g("volume")

    trades = p.get("trade_count")
    try:
        trades = int(trades)
    except (TypeError, ValueError):
        trades = int(stats.get("trades") or 0)

    def ratio(num: float, den: float) -> float | None:
        return (num / den) if den > 0 else None

    return {
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "position_pnl": position,
        "non_directional": non_dir,
        "rebates": rebates,
        "rewards": rewards,
        "fees": g("fees"),
        "volume_usdc": volume,
        "trade_count": trades,
        "biggest_win": float(stats.get("biggest_win") or 0.0),
        "roi": ratio(realized, volume),
        "mm_share": ratio(non_dir, realized),
        "dir_share": ratio(position, realized),
    }


def cmd_collect(args) -> int:
    board = fetch_leaderboard(args.category, args.period, args.order_by, args.top)
    wallets = [r.get("user_id") or r.get("proxyWallet") for r in board]
    wallets = [w for w in wallets if w]

    stats: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(fetch_stats, w): w for w in wallets}
        for fut in as_completed(futs):
            w = futs[fut]
            try:
                stats[w] = fut.result()
            except RuntimeError as exc:
                print(f"warn: stats for {w}: {exc}", file=sys.stderr)

    snap = {
        "collected_at": datetime.now(UTC).isoformat(),
        "params": {
            "category": args.category,
            "period": args.period,
            "order_by": args.order_by,
            "top": args.top,
        },
        "entries": [
            {"leaderboard": row, "stats": stats.get(row.get("user_id") or "", {})}
            for row in board
        ],
    }
    out = Path(args.out)
    out.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"snapshot: {out}  wallets={len(board)}  at {snap['collected_at']}")
    return 0


# ---------------------------------------------------------------------------
# judge: rank
# ---------------------------------------------------------------------------


def classify(m: dict, a) -> tuple[str, list[str]]:
    """Decide whether this wallet is worth copying, and say exactly why not."""
    why: list[str] = []
    if m["trade_count"] < a.min_trades:
        why.append(f"only {m['trade_count']} trades")
    if m["realized_pnl"] <= 0:
        why.append("realized PnL <= 0")
    elif m["roi"] is not None and m["roi"] < a.min_roi:
        why.append(f"ROI {fpct(m['roi'])} < floor")
    if m["mm_share"] is not None and m["mm_share"] > a.max_mm_share:
        why.append(f"{fpct(m['mm_share'])} of profit is rebates/rewards (market maker)")
    if m["dir_share"] is not None and m["dir_share"] < a.min_dir_share:
        why.append(f"directional share only {fpct(m['dir_share'])}")
    if m["realized_pnl"] > 0 and m["unrealized_pnl"] < 0:
        open_loss = -m["unrealized_pnl"] / m["realized_pnl"]
        if open_loss > a.max_unrealized:
            why.append(f"open loss = {fpct(open_loss)} of realized (unbooked)")
    return ("COPY" if not why else "skip"), why


def cmd_rank(args) -> int:
    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    scored = []
    for e in snap.get("entries", []):
        row = e.get("leaderboard") or {}
        wallet = row.get("user_id") or row.get("proxyWallet") or ""
        m = metrics(e.get("stats") or {})
        m["wallet"] = wallet
        m["name"] = row.get("user_name") or row.get("userName") or ""
        m["board_pnl"] = float(row.get("pnl") or 0.0)
        m["board_volume"] = float(row.get("volume") or 0.0)
        m["verdict"], m["why"] = classify(m, args)
        scored.append(m)

    scored.sort(key=lambda m: m["realized_pnl"], reverse=True)

    if args.json:
        print(json.dumps(scored, indent=2))
        return 0

    print(f"snapshot {snap.get('params')} collected {snap.get('collected_at')}")
    print(
        f"{'#':>3} {'wallet':<12} {'realized':>12} {'ROI':>8} "
        f"{'MM%':>6} {'dir%':>6} {'trades':>8} {'verdict':>7}  why"
    )
    for i, m in enumerate(scored, 1):
        print(
            f"{i:>3} {m['wallet'][:10]:<12} {fnum(m['realized_pnl'], True):>12} "
            f"{fpct(m['roi']):>8} {fpct(m['mm_share']):>6} {fpct(m['dir_share']):>6} "
            f"{m['trade_count']:>8} {m['verdict']:>7}  {'; '.join(m['why'])}"
        )

    copiable = [m for m in scored if m["verdict"] == "COPY"]
    print(f"\ncopyable: {len(copiable)}/{len(scored)}")
    if copiable and args.watchlist:
        Path(args.watchlist).write_text(
            json.dumps([m["wallet"] for m in copiable], indent=2), encoding="utf-8"
        )
        print(f"watchlist: {args.watchlist}")
    return 0


# ---------------------------------------------------------------------------
# judge: wintrap
# ---------------------------------------------------------------------------


def fetch_positions(wallet: str, max_rows: int = 4000) -> list:
    return _paginate("positions", {"user": wallet}, max_rows)


SETTLED = {"REDEEMABLE", "REDEEMABLE_LOST", "CLOSED"}


def win_trap(wallet: str, min_decided: int = 1, basis: str = "settled") -> dict | None:
    """Per-position win rate over positions whose outcome is actually known.

    Three bases, and two of them lie quietly:

    `realized`  filters on `realized_pnl != 0`. An OPEN position has realized 0
                by definition, so this drops every open position -- keeping the
                cut losers and discarding the held winners. A wallet with 87
                positions shows 18 "decided" and a 2/18 win rate instead of
                35/87. Measured on 103 wallets this produced a +0.53 correlation
                between win rate and PnL, i.e. a confident wrong answer.

    `total`     uses `total_pnl` (realized + unrealized mark) on every row. This
                keeps open positions, but marks them to market: a wallet buying
                near-certain outcomes at 0.99 is usually slightly negative
                *before* resolution and wins *at* it. That inverts the sign for
                exactly the strategy the metric is supposed to describe. It moved
                the correlation to +0.15 -- still wrong, for a new reason.

    `settled`   only counts positions whose status is resolved
                (REDEEMABLE / REDEEMABLE_LOST / CLOSED) and takes their final
                P&L.

    The point is not which basis is nicest. Two defensible-looking choices gave
    opposite conclusions on the same wallets, which is why the basis is printed
    with every result instead of hidden.

    BUT ALL THREE ARE COMPUTED ON A CONTAMINATED FIELD. A positions row is not
    one entry: `realized_pnl` is the wallet's realised P&L across *every* trade
    it ever made in that market, while `current_size`/`total_cost_usdc` describe
    only the residual holding. A live example from this feed:

        status REDEEMABLE, outcome No, avg_price 0.9199, resolution price 1.0,
        current_size 0.1771, total_cost_usdc 0.1636  ->  value 0.1771 on cost
        0.1636, i.e. the holding itself is +0.0135
        total_pnl  = -20.5205

    The row is a winner and carries a 20-dollar loss at the same time. So a
    "share of winning rows" is not a "share of successful entries", and the
    number the viral post names cannot be read off this endpoint at all. The
    only correct route is to rebuild entries from `/v2/trades` and settle them
    against `/v2/resolutions`, which `wintrap` deliberately does NOT pretend to
    do.

    `contamination` below measures the share of rows where `total_pnl` disagrees
    with `current_value - total_cost_usdc`. When it is high, the win rate in this
    report is not a measurement of skill and must not be read as one.
    """
    pos = fetch_positions(wallet)
    decided = []
    checked = 0
    inconsistent = 0
    for p in pos:
        status = (p.get("status") or "").upper()
        if basis == "settled" and status not in SETTLED:
            continue
        if basis == "realized":
            key = "realized_pnl"
        else:
            key = "total_pnl"
        try:
            v = float(p.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if v != 0.0:  # breakeven positions are neither a win nor a loss
            decided.append(v)

        # integrity: for one holding, P&L should equal value - cost
        checked += 1
        try:
            implied = float(p.get("current_value") or 0.0) - float(
                p.get("total_cost_usdc") or 0.0
            )
        except (TypeError, ValueError):
            continue
        denom = max(abs(implied), abs(v), 1.0)
        if abs(v - implied) / denom > 0.5:
            inconsistent += 1

    if len(decided) < min_decided:
        return None
    wins = [x for x in decided if x > 0]
    losses = [x for x in decided if x < 0]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    return {
        "wallet": wallet,
        "basis": basis,
        "decided": len(decided),
        "win_rate": len(wins) / len(decided),
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "payoff": (avg_w / abs(avg_l)) if avg_l else None,
        "ev_per_position": sum(decided) / len(decided),
        "contamination": (inconsistent / checked) if checked else None,
        "realized_total": sum(decided),
    }


def sample_wallets_from_trades(n: int) -> list[str]:
    """Wallets taken from the *global* trade feed.

    The leaderboard is ranked by PnL, so any sample drawn from it is already
    selected on the very variable we want to predict -- it cannot test whether
    win rate works. The global feed has no such filter: it is whoever traded
    just now, the closest thing to an unbiased cross-section.
    """
    seen: list[str] = []
    cursor = None
    while len(seen) < n:
        params = {"limit": PAGE}
        if cursor:
            params["cursor"] = cursor
        payload = api_get(f"{DATA}/trades", params)
        page = _rows(payload)
        if not page:
            break
        for t in page:
            w = t.get("proxy_wallet")
            if w and w not in seen:
                seen.append(w)
                if len(seen) >= n:
                    break
        cursor = _cursor(payload)
        if not cursor:
            break
    return seen[:n]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def cmd_wintrap(args) -> int:
    if args.source == "trades":
        wallets = [(w, "") for w in sample_wallets_from_trades(args.top)]
        origin = f"global trade feed, {len(wallets)} wallets, no PnL filter"
    else:
        board = fetch_leaderboard(args.category, args.period, args.order_by, args.top)
        wallets = [(r.get("user_id") or "", r.get("user_name") or "") for r in board]
        wallets = [(w, n) for w, n in wallets if w]
        origin = (
            f"leaderboard top {len(wallets)} by {args.order_by} "
            f"({args.period}, {args.category}) -- SELECTED ON PNL"
        )

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(win_trap, w, args.min_decided, args.basis): (w, n)
            for w, n in wallets
        }
        for fut in as_completed(futs):
            w, n = futs[fut]
            try:
                r = fut.result()
            except RuntimeError as exc:
                print(f"warn: positions for {w}: {exc}", file=sys.stderr)
                continue
            if r:
                r["name"] = n
                results.append(r)

    results.sort(key=lambda r: r["win_rate"], reverse=True)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"sample: {origin}")
    print(f"outcome basis: {args.basis}")
    if args.basis == "settled":
        print("  only resolved positions; the only basis that answers the question")
    elif args.basis == "total":
        print("  open positions marked to market -- inverts near-certain outcomes")
    else:
        print("  open positions dropped -- discards held winners (known artefact)")
    print(
        f"{'#':>3} {'name':<18} {'winrate':>8} {'payoff':>8} {'EV/pos':>10} "
        f"{'outcome':>12} {'n':>5}"
    )
    for i, r in enumerate(results, 1):
        print(
            f"{i:>3} {(r['name'] or r['wallet'][:12])[:18]:<18} "
            f"{fpct(r['win_rate']):>8} {fnum(r['payoff']):>8} "
            f"{fnum(r['ev_per_position'], True):>10} "
            f"{fnum(r['realized_total'], True):>12} {r['decided']:>5}"
        )

    wr = [r["win_rate"] for r in results]
    rp = [r["realized_total"] for r in results]
    ev = [r["ev_per_position"] for r in results]
    with_payoff = [r for r in results if r["payoff"] is not None]
    c_pnl = pearson(wr, rp)
    c_ev = pearson(wr, ev)
    c_pay = pearson(
        [r["win_rate"] for r in with_payoff], [r["payoff"] for r in with_payoff]
    )

    print()
    print(f"n = {len(results)} wallets with >= {args.min_decided} decided positions")
    cont = [r["contamination"] for r in results if r["contamination"] is not None]
    mean_cont = (sum(cont) / len(cont)) if cont else None
    print(f"contamination (total_pnl != value - cost) on {fpct(mean_cont)} of rows")
    print(f"corr(win_rate, realized PnL) = {fnum(c_pnl)}")
    print(f"corr(win_rate, EV/position)  = {fnum(c_ev)}")
    print(
        f"corr(win_rate, payoff)       = {fnum(c_pay)}   "
        f"(negative = the trap: high win rate, small wins per loss)"
    )

    print()
    if mean_cont is not None and mean_cont > 0.20:
        print("=> INVALID MEASUREMENT. The positions field is contaminated:")
        print("   realized P&L is market-level, not entry-level. A row can be a")
        print("   winning holding and a 20-dollar loss at once. Win rate here is")
        print("   NOT a share of successful entries -- do not quote it as one.")
        print("   Correct route: rebuild entries from /v2/trades and settle them")
        print("   against /v2/resolutions. That is not what wintrap does.")
    elif c_pay is not None and c_pay < -0.3:
        print("=> TRAP VISIBLE: high win rates come with a worse payoff ratio.")
    elif c_pnl is not None and c_pnl > 0.3:
        print("=> On THIS sample win rate tracks PnL. The trap is not visible.")
        if args.source == "leaderboard":
            print("   But the sample is the PnL top -- selected on the very")
            print("   variable being predicted. Re-run with --source trades.")
    elif c_pnl is not None and c_pnl < -0.3:
        print("=> INVERTED: on this sample a higher win rate means a worse PnL.")
    else:
        print("=> No clear relationship on this sample. Say so; do not spin it.")
    return 0


# ---------------------------------------------------------------------------
# sensor: signals
# ---------------------------------------------------------------------------


def load_watch(path: str) -> list[str]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [w for w in raw if isinstance(w, str)]
    if isinstance(raw, dict):
        if isinstance(raw.get("entries"), list):
            return [
                e["wallet"]
                for e in raw["entries"]
                if e.get("verdict") == "COPY" and e.get("wallet")
            ]
        if isinstance(raw.get("watchlist"), list):
            return raw["watchlist"]
    raise ValueError(f"cannot read a wallet list from {path}")


def book_price(token_id: str, side: str) -> dict | None:
    """Best executable price on the live CLOB book, plus top depth."""
    try:
        book = api_get(f"{CLOB}/book", {"token_id": token_id})
    except RuntimeError:
        return None
    levels = book.get("asks" if side == "BUY" else "bids") or []
    if not levels:
        return None

    def px(lv: dict) -> float:
        return float(lv.get("price") or 0.0)

    best = min(levels, key=px) if side == "BUY" else max(levels, key=px)
    return {"price": px(best), "size": float(best.get("size") or 0.0)}


def cmd_signals(args) -> int:
    wallets = load_watch(args.watch)
    state_path = Path(args.state)
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    fresh: list[dict] = []
    candidates: list[tuple[float, str, dict, float, float, str]] = []
    now_ts = time.time()
    floor = now_ts - args.max_age
    for w in wallets:
        # On the first run there is no state, so without a floor every trade in
        # the wallet's recent history would be reported as "fresh" -- and the
        # slippage column would compare a hours-old fill to today's book.
        last = max(float(state.get(w, 0.0)), floor)
        try:
            trades = _rows(api_get(f"{DATA}/trades", {"user": w, "limit": 100}))
        except RuntimeError as exc:
            print(f"warn: trades for {w}: {exc}", file=sys.stderr)
            continue
        newest = last
        for t in trades:
            ts = float(t.get("timestamp") or 0)
            newest = max(newest, ts)
            if ts <= last:
                continue
            try:
                size = float(t.get("size") or 0.0)
                price = float(t.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            usdc = size * price
            if usdc < args.min_usdc:
                continue
            if not (args.min_price <= price <= args.max_price):
                continue
            side = t.get("side") or "BUY"
            candidates.append((ts, w, t, price, usdc, side))
        state[w] = newest

    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # The book lookup is one CLOB request per candidate, and a 24h catch-up can
    # produce hundreds. Keep the newest ones: the state is already advanced, so
    # a slow run can never loop forever with the same backlog.
    candidates.sort(key=lambda c: c[0], reverse=True)
    cap = args.max_new if args.max_new > 0 else len(candidates)
    for ts, w, t, price, usdc, side in candidates[:cap]:
        # A backfill entry is stale: the book no longer shows the price that was
        # available then, and pricing 24h of trades one CLOB request at a time
        # made the 5-minute task run for a quarter of an hour. Only live trades
        # get a book price; older rows are recorded without an executable entry.
        now = None
        if now_ts - ts <= args.live_age:
            now = book_price(t.get("token_id") or "", side)
        slip = None
        if now and price > 0:
            slip = (now["price"] - price) / price
            if side == "SELL":
                slip = -slip
        fresh.append(
            {
                "wallet": w,
                "name": t.get("name") or t.get("pseudonym") or "",
                "side": side,
                "usdc": usdc,
                "whale_price": price,
                "now_price": now["price"] if now else None,
                "moved": slip,
                "title": t.get("title") or "",
                "outcome": t.get("outcome") or "",
                "token_id": t.get("token_id") or "",
                "condition_id": t.get("condition_id") or "",
                "trade_ts": ts,
                "detected_at": datetime.now(UTC).isoformat(),
            }
        )

    if args.require_book:
        fresh = [s for s in fresh if s.get("now_price") is not None]

    if args.json:
        print(json.dumps(fresh, indent=2))
    else:
        print(
            f"fresh entries by {len(wallets)} watched wallets "
            f"(>= ${args.min_usdc:,.0f})"
        )
        if not fresh:
            print("  none")
        for s in fresh:
            print(
                f"  {s['side']:<4} ${s['usdc']:>10,.0f}  "
                f"whale {s['whale_price']:.3f} -> now "
                f"{fnum(s['now_price'])}  moved {fpct(s['moved'])}  "
                f"{s['outcome']} | {s['title'][:52]}"
            )
        print("\n  moved = price now vs the whale's fill. Positive means you would")
        print("  pay MORE than the whale; negative means the entry is cheaper.")

    if args.out:
        p = Path(args.out)
        if args.append and p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old = []
            old.extend(fresh)
            p.write_text(
                json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"frozen signals: {p} ({len(old)} accumulated)")
        else:
            # write an empty file anyway: a downstream step that reads a missing
            # file fails, and "no signals this hour" is a valid result
            p.write_text(
                json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"frozen signals: {p} ({len(fresh)})")
    return 0


# ---------------------------------------------------------------------------
# judge: backfill -- build a frozen signal set from history, honestly delayed
# ---------------------------------------------------------------------------


def _backfill_signals(wallets, lo, hi, args, horizons):
    max_h = max(horizons)
    jobs = []
    for w in wallets:
        try:
            trades = _paginate("trades", {"user": w}, args.trades_per_wallet)
        except RuntimeError as exc:
            print(f"warn: trades for {w}: {exc}", file=sys.stderr)
            continue
        cands = []
        for t in trades:
            try:
                ts = float(t.get("timestamp") or 0)
                size = float(t.get("size") or 0.0)
                price = float(t.get("price") or 0.0)
                tok = t.get("token_id") or ""
            except (TypeError, ValueError):
                continue
            usdc = size * price
            if not (lo <= ts <= hi) or usdc < args.min_usdc or not tok:
                continue
            cands.append((usdc, ts, t, price, tok))
        cands.sort(key=lambda c: c[0], reverse=True)
        for usdc, ts, t, price, tok in cands[: args.max_per_wallet]:
            jobs.append((w, usdc, ts, t, price, tok))

    # Group by token: one price series serves every signal on that market.
    # Sequential-per-signal costs one request each; popular sports and politics
    # markets are traded by many of the watched wallets, so this collapses the
    # request count instead of paying it N times.
    by_token: dict[str, list] = {}
    for job in jobs:
        by_token.setdefault(job[5], []).append(job)

    def one_token(tok, group):
        start = min(j[2] for j in group) + args.lag - 300
        end = max(j[2] for j in group) + max_h * 60 + 300
        series = price_series(tok, start, end)
        if not series:
            return []
        at = make_lookup(series)
        last_ts = series[-1][0]
        out = []
        for w, usdc, ts, t, price, _ in group:
            entry = at(ts + args.lag)
            if not entry or entry <= 0:
                continue
            side = t.get("side") or "BUY"
            returns = {}
            for h in horizons:
                tgt = ts + h * 60
                if tgt > last_ts + 600:
                    returns[h] = None
                    continue
                p = at(tgt, tol=max(900.0, 0.02 * h * 60))
                returns[h] = horizon_return(side, entry, p)
            out.append(
                {
                    "wallet": w,
                    "side": side,
                    "usdc": usdc,
                    "whale_price": price,
                    "now_price": entry,
                    "moved": (entry - price) / price if price > 0 else None,
                    "title": t.get("title") or "",
                    "outcome": t.get("outcome") or "",
                    "token_id": tok,
                    "condition_id": t.get("condition_id") or "",
                    "trade_ts": ts,
                    "detected_at": datetime.now(UTC).isoformat(),
                    "backfilled": True,
                    "returns": returns,
                }
            )
        return out

    out = []
    groups = list(by_token.items())
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(one_token, tok, g) for tok, g in groups]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except RuntimeError:
                continue
            out.extend(r)
            if i % 200 == 0:
                print(f"  ... {i}/{len(groups)} tokens", file=sys.stderr)
    return out


def cmd_backfill(args) -> int:
    """Reconstruct 'what a copy-trader would have seen' from past trades.

    The entry price is NOT the whale's fill. It is the market price a fixed
    number of seconds later (`--lag`), because that is the earliest moment you
    could have noticed the trade. Returns at every horizon are computed from the
    same single price series, so a run costs one request per signal.

    A horizon with no history is stored as null, never as the last known price.
    """
    wallets = load_watch(args.watch)
    horizons = [int(h) for h in args.horizons.split(",")]
    now = time.time()
    lo = now - args.hours * 3600.0
    hi = now - args.min_age_hours * 3600.0

    out = _backfill_signals(wallets, lo, hi, args, horizons)
    out.sort(key=lambda s: s["trade_ts"])

    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(
        f"backfilled {len(out)} signals from {len(wallets)} wallets, "
        f"window {args.hours:.0f}h ago .. {args.min_age_hours:.0f}h ago, "
        f"lag {args.lag:.0f}s -> {args.out}"
    )
    if out:
        moved = [s["moved"] for s in out if s["moved"] is not None]
        if moved:
            moved.sort()
            print(
                f"  move between whale fill and your entry: "
                f"mean {fpct(sum(moved) / len(moved))}, "
                f"median {fpct(moved[len(moved) // 2])}, n={len(moved)}"
            )
    return 0


# ---------------------------------------------------------------------------
# judge: evaluate
# ---------------------------------------------------------------------------


def price_at(token_id: str, ts: float, pad: int = 1800) -> float | None:
    series = price_series(token_id, ts - pad, ts + pad)
    at = make_lookup(series)
    return at(ts) if series else None


def price_series(token_id: str, start: float, end: float) -> list[tuple[float, float]]:
    """One request returns the whole window, instead of one request per horizon.

    Backfilling N signals with H horizons used to cost N*(H+1) requests. This
    costs one per signal, which is what makes a 20k-signal run feasible at all.
    """
    try:
        payload = api_get(
            f"{DATA}/prices-history",
            {"token_id": token_id, "start": int(start), "end": int(end)},
        )
    except RuntimeError:
        return []
    pts = []
    for p in _rows(payload):
        try:
            pts.append((float(p["timestamp"]), float(p["price"])))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort()
    return pts


def make_lookup(series: list[tuple[float, float]]):
    """As-of-forward lookup: the first observed price at or AFTER the target.

    Two wrong ways to do this, both of which I tried first:

    * nearest observation -- looks forward as often as backward, handing the
      strategy a price that had not printed yet;
    * last observation at or before -- returns a price from BEFORE the whale's
      trade, which no longer exists and is not executable.

    A copy-trader detects the trade at T+lag and can only transact at the next
    price that actually prints. So both the entry and every horizon use the
    first observation at or after the target, with the same tolerance. Same rule
    on both ends, so the skew does not systematically favour either side.
    """
    keys = [p[0] for p in series]
    vals = [p[1] for p in series]

    def at(ts: float, tol: float = 900.0) -> float | None:
        if not keys:
            return None
        i = bisect.bisect_left(keys, ts)
        if i >= len(keys):
            return None  # no observation after ts: unknown
        if keys[i] - ts > tol:
            return None  # next print too far away to call it our price
        return vals[i]

    return at


# A binary contract priced at 1-2 cents is effectively decided. A percentage
# return measured from such an entry divides by noise: on 2026-10-25 it produced
# rows of +544% and -104% and inflated the 60m mean to +862%. Below this price a
# row is left unscored instead of inventing a number.
MIN_EVALUABLE_ENTRY = 0.02


def resolution_return(
    side: str | None,
    entry: float | None,
    payout: float | None,
    min_entry: float = MIN_EVALUABLE_ENTRY,
) -> float | None:
    """Return from entry to the market's final 0/1 payout.

    None means "not resolved yet" or "no executable entry", never zero.
    """
    if payout is None or entry is None or entry < min_entry:
        return None
    sign = 1.0 if (side or "BUY") == "BUY" else -1.0
    return sign * (payout - entry) / entry


RESOLUTION_CACHE = Path(".resolution_cache.json")


def load_resolution_cache(path: Path = RESOLUTION_CACHE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_resolution_cache(cache: dict, path: Path = RESOLUTION_CACHE) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_resolution(condition_id: str, cache: dict) -> dict | None:
    """Closed markets are final and cached forever; open ones are re-read."""
    hit = cache.get(condition_id)
    if hit and hit.get("closed"):
        return hit
    try:
        market = api_get(f"{CLOB}/markets/{condition_id}", {})
    except RuntimeError:
        return hit
    tokens = {}
    for t in market.get("tokens") or []:
        tokens[t.get("token_id") or ""] = {
            "winner": bool(t.get("winner")),
            "outcome": t.get("outcome") or "",
        }
    rec = {"closed": bool(market.get("closed")), "tokens": tokens}
    cache[condition_id] = rec
    return rec


def entry_price(signal: dict) -> float | None:
    """The price a copy-trader could actually pay at detection.

    A live signal with no book has no executable entry: the whale's fill is
    gone. Scoring it at the whale's price is what turned six untradeable
    events into a fake +$89 in the overnight sample.
    """
    now = signal.get("now_price")
    if now:
        return float(now)
    if signal.get("backfilled") and signal.get("whale_price"):
        return float(signal["whale_price"])
    return None


def horizon_window_valid(signal: dict, horizon: int) -> bool:
    """False when the horizon ends before the signal was detectable.

    Returns are measured from the whale's fill to fill+horizon, while the entry
    is the price at detection. With a delayed capture the window can be
    negative, scoring a move that happened before we could enter.
    """
    if signal.get("backfilled"):
        return True
    detected, trade_ts = signal.get("detected_at"), signal.get("trade_ts")
    if not (detected and trade_ts):
        return True
    try:
        age = (
            datetime.fromisoformat(detected)
            - datetime.fromtimestamp(float(trade_ts), UTC)
        ).total_seconds()
    except (TypeError, ValueError):
        return True
    return age < horizon * 60


def strategy_rows(
    rows: list[dict],
    max_adverse_move: float | None = None,
    one_per_market: bool = False,
    max_lag: float | None = None,
) -> list[dict]:
    """Rows a copy strategy could actually hold, without double counting.

    * `max_adverse_move` drops entries whose price already moved against the
      whale by more than this fraction: chasing a falling knife was the single
      largest loss source in the overnight sample.
    * `one_per_market` keeps the earliest row per condition: ten watched
      wallets buying the same event is one position, not ten.
    * `max_lag` keeps only signals detected within this many seconds of the
      whale's fill. The replay edge decays from +7.8% (60s) to +2.3% (30m), so
      a slow sample and a fast sample are not the same strategy.
    """
    out = list(rows)
    if max_lag is not None:
        fresh = []
        for x in out:
            if x.get("backfilled"):
                fresh.append(x)
                continue
            detected, trade_ts = x.get("detected_at"), x.get("trade_ts")
            if not (detected and trade_ts):
                fresh.append(x)
                continue
            try:
                lag = (
                    datetime.fromisoformat(detected)
                    - datetime.fromtimestamp(float(trade_ts), UTC)
                ).total_seconds()
            except (TypeError, ValueError):
                fresh.append(x)
                continue
            if lag <= max_lag:
                fresh.append(x)
        out = fresh
    if max_adverse_move is not None:
        out = [
            x
            for x in out
            if x.get("moved") is None or float(x["moved"]) >= -max_adverse_move
        ]
    if one_per_market:
        first: dict[str, dict] = {}
        for x in sorted(out, key=_sort_ts):
            key = x.get("condition_id") or x.get("token_id") or str(id(x))
            first.setdefault(key, x)
        out = list(first.values())
    return out


def horizon_return(
    side: str | None,
    entry: float | None,
    p: float | None,
    min_entry: float = MIN_EVALUABLE_ENTRY,
) -> float | None:
    """Return on a binary token from `entry` to `p`, or None if unusable.

    None means "not evaluable", never zero: zero is a real flat outcome.
    """
    if p is None or entry is None or entry < min_entry:
        return None
    sign = 1.0 if (side or "BUY") == "BUY" else -1.0
    return sign * (p - entry) / entry


def stored_final_return(stored: dict, horizon: int) -> float | None:
    """A cached value is final only when it is a number.

    A stored None means the horizon had not printed yet at the previous run.
    Treating it as final froze 240m at zero settled rows: every later run reused
    the None instead of retrying the horizon.
    """
    v = stored.get(str(horizon), stored.get(horizon))
    return v if isinstance(v, (int, float)) else None


# How long to keep retrying a horizon that has no printable price. After this
# window it is left unscored: a 4h exit on a market that resolved in 2h has no
# print to observe, and retrying it forever only burns API calls.
RETRY_WINDOW = 48 * 3600


def horizon_status(
    horizon: int,
    base_ts: float,
    now_ts: float,
    retry_window: float = RETRY_WINDOW,
) -> str:
    """Classify a horizon with no cached value: future, retry, or expired."""
    target = base_ts + horizon * 60
    if target > now_ts:
        return "future"
    if now_ts - target > retry_window:
        return "expired"
    return "retry"


def _stats(vals: list[float]) -> tuple[int, float, float | None] | None:
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    if n < 2:
        return n, mean, None
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return n, mean, math.sqrt(var / n)


def _fifo(trades: list[dict]) -> tuple[list[dict], list[list]]:
    """Match sells against buys FIFO. Returns closed round-trips and open lots.

    This is the only way to get a real per-ENTRY outcome out of this API. The
    `positions` endpoint aggregates realised P&L per market, so a row can be a
    winning holding with a 20-dollar loss attached (documented in win_trap).
    Here each buy lot is tracked separately: a sell closes the oldest open lot
    and produces its own P&L.
    """
    closed: list[dict] = []
    open_lots: list[list] = []
    for t in trades:
        try:
            size = float(t.get("size") or 0.0)
            price = float(t.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if size <= 0 or price < 0:
            continue
        if (t.get("side") or "BUY") == "BUY":
            open_lots.append([size, price, t])
            continue
        remaining = size
        while remaining > 1e-9 and open_lots:
            lot = open_lots[0]
            take = min(remaining, lot[0])
            closed.append(
                {
                    "token_id": t.get("token_id") or "",
                    "title": t.get("title") or lot[2].get("title") or "",
                    "size": take,
                    "entry_price": lot[1],
                    "exit_price": price,
                    "pnl": (price - lot[1]) * take,
                    "entry_ts": float(lot[2].get("timestamp") or 0),
                    "exit_ts": float(t.get("timestamp") or 0),
                }
            )
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                open_lots.pop(0)
    return closed, open_lots


def cmd_entries(args) -> int:
    """Per-entry win rate from closed round-trips, not from position rows.

    Deliberate limitation: only buy-then-sell round-trips are counted. A wallet
    that buys and holds to resolution has no closed lot and is invisible here.
    That bias is real and is why `--settle` exists; without it, read the number
    as "share of closed entries that made money", never as "share of all entries".
    """
    wallets = load_watch(args.watch)
    now = time.time()
    lo = now - args.hours * 3600.0
    per_wallet: dict[str, list[dict]] = {}

    for w in wallets:
        try:
            trades = _paginate("trades", {"user": w}, args.trades_per_wallet)
        except RuntimeError as exc:
            print(f"warn: trades for {w}: {exc}", file=sys.stderr)
            continue
        rows = []
        by_tok: dict[str, list[dict]] = {}
        for t in trades:
            ts = float(t.get("timestamp") or 0)
            if ts < lo:
                continue
            usdc = float(t.get("size") or 0) * float(t.get("price") or 0)
            if usdc < args.min_usdc:
                continue
            by_tok.setdefault(t.get("token_id") or "", []).append(t)
        for ts_list in by_tok.values():
            ts_list.sort(key=lambda x: float(x.get("timestamp") or 0))
            closed, _open = _fifo(ts_list)
            rows.extend(closed)
        per_wallet[w] = rows

    out = []
    for w, rows in per_wallet.items():
        vals = [r["pnl"] for r in rows]
        st = _stats(vals)
        if not st:
            continue
        n, mean, se = st
        wins = sum(1 for v in vals if v > 0)
        out.append(
            {
                "wallet": w,
                "entries": n,
                "win_rate": wins / n,
                "mean_pnl": mean,
                "se": se,
                "total_pnl": sum(vals),
                "mean_pct": (
                    sum(r["pnl"] / (r["entry_price"] * r["size"]) for r in rows) / n
                ),
            }
        )

    out.sort(key=lambda r: r["total_pnl"], reverse=True)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(
        f"{len(out)} wallets, {sum(r['entries'] for r in out)} closed entries "
        f"(round-trips only, last {args.hours:.0f}h)"
    )
    print(
        f"{'wallet':<16} {'n':>5} {'win%':>7} {'mean $':>10} {'mean %':>8} {'total $':>12}"
    )
    for r in out[: args.show]:
        print(
            f"{r['wallet'][:14]:<16} {r['entries']:>5} {fpct(r['win_rate']):>7} "
            f"{r['mean_pnl']:>10,.2f} {fpct(r['mean_pct']):>8} "
            f"{r['total_pnl']:>12,.0f}"
        )

    all_vals = [v for rows in per_wallet.values() for v in (r["pnl"] for r in rows)]
    st = _stats(all_vals)
    if st:
        n, mean, se = st
        wr = sum(1 for v in all_vals if v > 0) / n
        print(
            f"\nall: n={n}  win rate {fpct(wr)}  mean P&L per entry "
            f"${mean:,.2f} (SE ${se:,.2f})"
        )
        print("  Entry-level win rate is NOT copyable win rate: a copied entry")
        print("  starts 0.5-1.6% worse and exits later. See `backfill` for that.")
    return 0


GAMMA = "https://gamma-api.polymarket.com"


def fetch_closed_markets(
    n: int,
    order: str = "volumeNum",
    ascending: bool = False,
    end_max: str | None = None,
) -> list:
    rows: list = []
    offset = 0
    while len(rows) < n:
        params = {
            "closed": "true",
            "limit": 100,
            "order": order,
            "ascending": "true" if ascending else "false",
            "offset": offset,
        }
        if end_max:
            params["end_date_max"] = end_max
        payload = api_get(f"{GAMMA}/markets", params)
        if not isinstance(payload, list) or not payload:
            break
        rows.extend(payload)
        offset += len(payload)
    return rows[:n]


def _closed_ts(raw: str) -> float | None:
    try:
        return (
            datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except (TypeError, ValueError):
        return None


def _iso_ts(raw: str) -> float | None:
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError, AttributeError):
        return None


def cmd_calib(args) -> int:
    """Is the market's own price calibrated? The one edge that needs no whales.

    For every resolved binary market, take the YES price a fixed number of hours
    before close and compare it with what actually happened. A calibrated market
    has realised frequency == price: buying YES at 0.05 wins 5% of the time.

    If longshots are overpriced (favorite-longshot bias), realised frequency sits
    BELOW price at the low end, and buying NO there is positive EV. That edge is
    structural -- it belongs to the market, not to any wallet -- so it does not
    decay when someone else copies it.
    """
    end_max = None
    if args.end_days_ago:
        cut = datetime.now(UTC).timestamp() - args.end_days_ago * 86400
        end_max = datetime.fromtimestamp(cut, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = fetch_closed_markets(args.markets, args.order, args.ascending, end_max)
    recs = []
    counts = {"total": len(markets), "binary": 0, "closed_ts": 0, "priced": 0}
    dists = []
    for m in markets:
        try:
            op = json.loads(m.get("outcomePrices") or "[]")
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if len(op) != 2 or len(toks) != 2:
            continue
        if not ((op[0] == "1" and op[1] == "0") or (op[0] == "0" and op[1] == "1")):
            continue
        counts["binary"] += 1
        vol = float(m.get("volumeNum") or 0.0)
        if vol < args.min_volume or (args.max_volume and vol > args.max_volume):
            continue
        if args.anchor == "end":
            closed_ts = _iso_ts(m.get("endDate") or "")
        else:
            closed_ts = _closed_ts(m.get("closedTime") or "")
        if closed_ts is None:
            continue
        counts["closed_ts"] += 1
        tgt = closed_ts - args.hours_before * 3600.0
        # Wide window: old markets only keep 3h/12h grains, so a narrow window
        # finds nothing. Take the observation nearest the target that is still
        # safely before the settlement print.
        series = price_series(toks[0], closed_ts - 7 * 86400, closed_ts - 1800)
        best = None
        for ts, px in series:
            if ts > closed_ts - 1800:
                continue
            d = abs(ts - tgt)
            if best is None or d < best[0]:
                best = (d, px)
        if best is None:
            continue
        if args.max_distance and best[0] > args.max_distance:
            continue
        counts["priced"] += 1
        dists.append(best[0])
        recs.append(
            {
                "p": best[1],
                "yes": op[0] == "1",
                "vol": vol,
                "q": m.get("question") or "",
                "ts": closed_ts,
            }
        )

    if args.verbose:
        print(
            f"funnel: {counts['total']} markets -> {counts['binary']} resolved "
            f"binary -> {counts['closed_ts']} with close time -> "
            f"{counts['priced']} with a usable pre-close price"
        )
        if dists:
            dists.sort()
            print(
                f"price distance from target: median {dists[len(dists) // 2] / 3600:.1f}h, "
                f"p90 {dists[int(len(dists) * 0.9)] / 3600:.1f}h"
            )

    if not recs:
        print("no usable resolved markets")
        return 0

    if args.json:
        print(json.dumps(recs, indent=2))
        return 0

    print(
        f"{len(recs)} resolved binary markets, YES price taken "
        f"{args.hours_before:.0f}h before close"
    )
    print(
        f"{'bucket':>10} {'n':>5} {'price':>7} {'realised':>9} {'±2SE':>7} "
        f"{'EV yes':>8} {'EV no':>8}"
    )
    edges = [i / 10 for i in range(11)]
    for i in range(10):
        lo, hi = edges[i], edges[i + 1]
        sel = [
            r for r in recs if (lo <= r["p"] < hi) or (i == 9 and lo <= r["p"] <= 1.0)
        ]
        if not sel:
            continue
        n = len(sel)
        price = sum(r["p"] for r in sel) / n
        freq = sum(1 for r in sel if r["yes"]) / n
        se = math.sqrt(freq * (1 - freq) / n) if n else None
        print(
            f"{lo:.1f}-{hi:.1f} {n:>8} {price:>7.3f} {freq:>9.1%} "
            f"{fpct(2 * se) if se else 'n/a':>7} "
            f"{fpct(freq - price):>8} {fpct(price - freq):>8}"
        )

    print()
    xs = [r["p"] for r in recs]
    ys = [1.0 if r["yes"] else 0.0 for r in recs]
    print(f"corr(price, outcome) = {fnum(pearson(xs, ys))}")

    print("\nstrategy: buy NO whenever YES price <= threshold, hold to resolution")
    print(f"{'threshold':>10} {'n':>5} {'mean px':>8} {'win%':>7} {'ROI':>8} {'SE':>7}")
    for thr in (0.02, 0.05, 0.10, 0.20, 0.30):
        sel = [r for r in recs if r["p"] <= thr]
        if len(sel) < 10:
            continue
        n = len(sel)
        cost = sum(1 - r["p"] for r in sel) / n
        payout = sum(1.0 for r in sel if not r["yes"]) / n
        roi = (payout - cost) / cost if cost else None
        per = [
            ((1.0 if not r["yes"] else 0.0) - (1 - r["p"])) / (1 - r["p"]) for r in sel
        ]
        st = _stats(per)
        print(
            f"{thr:>10.2f} {n:>5} {1 - cost:>8.3f} "
            f"{sum(1 for r in sel if not r['yes']) / n:>7.1%} "
            f"{fpct(roi):>8} {fpct(st[2]) if st and st[2] else 'n/a':>7}"
        )

    print("\nstrategy: buy YES whenever price >= threshold, hold to resolution")
    print(f"{'threshold':>10} {'n':>5} {'mean px':>8} {'win%':>7} {'ROI':>8} {'SE':>7}")
    for thr in (0.60, 0.70, 0.80, 0.90, 0.95):
        sel = [r for r in recs if r["p"] >= thr]
        if len(sel) < 10:
            continue
        n = len(sel)
        cost = sum(r["p"] for r in sel) / n
        payout = sum(1.0 for r in sel if r["yes"]) / n
        roi = (payout - cost) / cost if cost else None
        per = [((1.0 if r["yes"] else 0.0) - r["p"]) / r["p"] for r in sel]
        st = _stats(per)
        print(
            f"{thr:>10.2f} {n:>5} {cost:>8.3f} "
            f"{sum(1 for r in sel if r['yes']) / n:>7.1%} "
            f"{fpct(roi):>8} {fpct(st[2]) if st and st[2] else 'n/a':>7}"
        )
    print("\nSE is the standard error of per-bet ROI: |ROI| < 2*SE is noise.")

    if args.split:
        ordered = sorted(recs, key=lambda r: r["ts"])
        mid = ordered[len(ordered) // 2]["ts"]
        print("\nstability: same strategy on the two halves by close date")
        for label, part in (
            ("first", [r for r in ordered if r["ts"] <= mid]),
            ("second", [r for r in ordered if r["ts"] > mid]),
        ):
            sel = [r for r in part if r["p"] >= args.split_threshold]
            if len(sel) < 10:
                print(f"  {label}: n={len(sel)} -- too few")
                continue
            n = len(sel)
            cost = sum(r["p"] for r in sel) / n
            per = [((1.0 if r["yes"] else 0.0) - r["p"]) / r["p"] for r in sel]
            st = _stats(per)
            print(
                f"  {label}: n={n} mean px {cost:.3f} win "
                f"{sum(1 for r in sel if r['yes']) / n:.1%} "
                f"ROI {fpct(st[1])} (SE {fpct(st[2])})"
            )
    return 0


def cmd_mm(args) -> int:
    """Can a small account make markets? Spread + rebate vs adverse move.

    A maker quoting both sides captures half the spread per fill and earns the
    maker rebate on volume. What kills the strategy is adverse selection: the
    price moves through the stale quote before you can exit. That loss does not
    need an order book model -- it is observable as the typical short-horizon
    absolute move.

    So the quantity that decides viability is

        edge = spread/2 + rebate_per_volume - E|move over the holding window|

    measured per market. Everything below is that arithmetic, with the caveat
    that it ignores queue position, partial fills and inventory risk.
    """
    end = time.time()
    start = end - 86400
    rows = []
    for off in range(0, 1500, 100):
        page = api_get(
            f"{GAMMA}/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "volumeNum",
                "ascending": "false",
                "offset": off,
            },
        )
        if not isinstance(page, list) or not page:
            break
        rows.extend(page)
    # A market quoted at 0.001 has a "spread" of thousands of bps that is pure
    # arithmetic on a price near zero, and it never trades. Only the interior
    # of the probability range is a market a maker can actually operate in.
    rows = [
        m
        for m in rows
        if float(m.get("volumeNum") or 0) >= 10_000
        and 0.10 <= float(m.get("lastTradePrice") or 0) <= 0.90
    ][: args.markets]

    # Maker rebate observed on real market-making wallets, per dollar of volume.
    rebate_bps = 0.0
    ok = True
    if args.stats_snapshot and Path(args.stats_snapshot).exists():
        snap = json.loads(Path(args.stats_snapshot).read_text(encoding="utf-8"))
        rates = []
        for e in snap.get("entries", []):
            m = metrics(e.get("stats") or {})
            if m["volume_usdc"] > 10_000_000 and m["rebates"] > 0:
                rates.append(m["rebates"] / m["volume_usdc"] * 10_000)
        if rates:
            rates.sort()
            rebate_bps = rates[len(rates) // 2]
    else:
        ok = False

    def one(m: dict) -> dict | None:
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            return None
        if not toks:
            return None
        tok = toks[0]
        try:
            book = api_get(f"{CLOB}/book", {"token_id": tok})
        except RuntimeError:
            return None
        asks = [float(x["price"]) for x in (book.get("asks") or []) if x.get("price")]
        bids = [float(x["price"]) for x in (book.get("bids") or []) if x.get("price")]
        if not asks or not bids:
            return None
        ask, bid = min(asks), max(bids)
        mid = (ask + bid) / 2
        if mid <= 0:
            return None
        spread_bps = (ask - bid) / mid * 10_000

        series = price_series(tok, start, end)
        moves = []
        for (t1, p1), (t2, p2) in itertools.pairwise(series):
            if p1 > 0 and p2 > 0 and (t2 - t1) <= 1800:
                moves.append(abs(p2 - p1) / p1)
        if len(moves) < 5:
            return None
        adverse_bps = sum(moves) / len(moves) * 10_000
        return {
            "q": (m.get("question") or "")[:52],
            "mid": mid,
            "vol": float(m.get("volumeNum") or 0.0),
            "spread_bps": spread_bps,
            "adverse_bps": adverse_bps,
            "edge_bps": spread_bps / 2 + rebate_bps - adverse_bps,
        }

    out = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(one, rows):
            if r:
                out.append(r)

    if not out:
        print("no markets with both a book and enough price history")
        return 0

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"{len(out)} live markets, 24h of price history each")
    if ok:
        print(f"maker rebate on real MM wallets: {rebate_bps:.2f} bps of volume")
    else:
        print("maker rebate: unknown (pass --stats-snapshot snap_big.json)")

    buckets = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    print(
        f"\n{'mid price':>12} {'n':>4} {'spread':>8} {'move':>8} "
        f"{'edge':>8} {'viable':>7}"
    )
    for lo, hi in buckets:
        sel = [r for r in out if lo <= r["mid"] < hi]
        if not sel:
            continue
        n = len(sel)
        sp = sum(r["spread_bps"] for r in sel) / n
        mv = sum(r["adverse_bps"] for r in sel) / n
        ed = sum(r["edge_bps"] for r in sel) / n
        pos = sum(1 for r in sel if r["edge_bps"] > 0)
        print(
            f"{lo:.1f}-{hi:.1f} {n:>10} {sp:>7.0f}b {mv:>7.0f}b {ed:>+7.0f}b "
            f"{pos:>4}/{n}"
        )

    med_sp = sorted(r["spread_bps"] for r in out)[len(out) // 2]
    med_mv = sorted(r["adverse_bps"] for r in out)[len(out) // 2]
    viable = sum(1 for r in out if r["edge_bps"] > 0)
    print(
        f"\nmedian spread {med_sp:.0f} bps, median 5-min move {med_mv:.0f} bps, "
        f"positive edge on {viable}/{len(out)} markets"
    )
    print("\nNote: edge ignores queue position, partial fills and inventory risk.")
    print("A positive number here is a necessary condition, not a strategy.")
    return 0


def _simulate(px: list[float], times: list[float], outcome: float, args) -> dict:
    """Quote both sides on a real price path.

    `outcome` is what open inventory settles at. When `flatten_hours > 0` the
    path is cut before resolution and inventory settles at the last mark, so the
    result measures spread capture and inventory drift -- NOT the directional bet
    you accidentally end up holding. Conflating the two is what makes naive
    market-making backtests look random.
    """
    h = args.half_spread_bps / 10_000.0
    inv = 0.0
    cash = 0.0
    fills = 0
    peak_inv = 0.0
    for i in range(len(px) - 1):
        if times[i + 1] - times[i] > args.max_gap:
            continue
        mid, nxt = px[i], px[i + 1]
        if mid <= 0 or mid >= 1:
            continue
        bid, ask = mid - h, mid + h
        if bid <= 0 or ask >= 1:
            continue
        if nxt <= bid and (inv + args.size / bid) * mid <= args.max_inventory:
            shares = args.size / bid
            inv += shares
            cash -= args.size
            fills += 1
            peak_inv = max(peak_inv, inv * mid)
        elif nxt >= ask and inv * mid >= args.size:
            shares = args.size / ask
            inv -= shares
            cash += args.size
            fills += 1
    pnl = cash + inv * outcome
    return {"pnl": pnl, "fills": fills, "peak_inv": peak_inv, "open": inv > 1e-9}


def cmd_sim(args) -> int:
    """Does a wide spread survive fills? Simulate, do not assume."""
    cut = datetime.now(UTC).timestamp() - args.min_age_days * 86400
    end_max = datetime.fromtimestamp(cut, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = fetch_closed_markets(args.markets, "volumeNum", False, end_max)

    paths = []
    for m in markets:
        try:
            op = json.loads(m.get("outcomePrices") or "[]")
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if len(op) != 2 or len(toks) != 2:
            continue
        if not ((op[0] == "1" and op[1] == "0") or (op[0] == "0" and op[1] == "1")):
            continue
        end_ts = _iso_ts(m.get("endDate") or "")
        if end_ts is None:
            continue
        series = price_series(toks[0], end_ts - args.days * 86400, end_ts)
        if args.flatten_hours:
            cutoff = end_ts - args.flatten_hours * 3600
            series = [(t, p) for t, p in series if t <= cutoff]
        if len(series) < args.min_points:
            continue
        pxs = [p for _, p in series]
        if not (0.05 < sum(pxs) / len(pxs) < 0.95):
            continue
        settle = pxs[-1] if args.flatten_hours else (1.0 if op[0] == "1" else 0.0)
        paths.append(([t for t, _ in series], pxs, settle))

    if not paths:
        print("no usable resolved paths")
        return 0

    print(
        f"{len(paths)} resolved markets, last {args.days:.0f} days before "
        f"scheduled close, ${args.size:.0f} per fill, "
        f"max inventory ${args.max_inventory:.0f}"
    )
    print(
        f"\n{'half-spread':>12} {'fills':>7} {'total P&L':>11} {'per market':>11} "
        f"{'SE':>8} {'pos mkts':>9} {'avg inv':>8}"
    )
    for hs in args.sweep:
        args.half_spread_bps = hs
        res = [_simulate(px, ts, oc, args) for ts, px, oc in paths]
        pnls = [r["pnl"] for r in res]
        st = _stats(pnls)
        n, mean, se = st
        pos = sum(1 for v in pnls if v > 0)
        inv = sum(r["peak_inv"] for r in res) / n
        print(
            f"{hs:>10} bps {sum(r['fills'] for r in res):>7} "
            f"{sum(pnls):>11,.0f} {mean:>11,.2f} "
            f"{(se or 0):>8,.2f} {pos:>5}/{n:<3} {inv:>8,.0f}"
        )

    print("\nIf P&L falls as the spread widens, fills were selecting you against")
    print("the move. If it rises, you were being paid for real risk. Either way")
    print("this ignores queue position, partial fills and gas.")
    return 0


def _market_by(ident: str, prefer_closed: bool = False) -> dict | None:
    """Look a market up by slug or condition id.

    The filter only binds when `closed` is sent as well: without it Gamma
    silently ignores the identifier and returns the default page of 20 markets.
    That is a silent-wrong-answer bug, so the match is verified against the id
    we asked for and anything else is rejected.
    """
    order = ["true", "false"] if prefer_closed else ["false", "true"]
    is_addr = ident.startswith("0x") and len(ident) == 66
    key = "condition_ids" if is_addr else "slug"
    for closed in order:
        try:
            payload = api_get(f"{GAMMA}/markets", {key: ident, "closed": closed})
        except RuntimeError:
            continue
        if not isinstance(payload, list):
            continue
        for m in payload:
            got = m.get("conditionId") if is_addr else m.get("slug")
            if str(got).lower() == ident.lower():
                return m
    return None


def _yes_price(m: dict) -> float | None:
    for field in ("bestAsk", "lastTradePrice"):
        try:
            v = float(m.get(field))
        except (TypeError, ValueError):
            continue
        if 0 < v < 1:
            return v
    return None


def cmd_call(args) -> int:
    """Score your own forecasts against the market, before you risk money.

    `scan`   find live markets in a domain you actually know
    `record` freeze a probability with a reason, before the outcome
    `score`  Brier score of you vs the market price, paired

    The market price is captured by the tool at record time, not typed by you,
    so the comparison cannot be tilted afterwards. Every record is appended to
    an append-only file: editing history means editing a file by hand, which is
    exactly the thing this is designed to make visible.
    """
    path = Path(args.file)

    if args.action == "scan":
        payload = api_get(
            f"{GAMMA}/public-search",
            {"q": args.query, "limit_per_type": 10},
        )
        events = (payload or {}).get("events") or []
        print(f"search '{args.query}': {len(events)} events")
        for e in events:
            for m in (e.get("markets") or [])[:4]:
                if m.get("closed") or not m.get("active"):
                    continue
                px = _yes_price(m)
                if px is None:
                    continue
                print(
                    f"  {px:>5.3f}  {str(m.get('question'))[:58]:<58} "
                    f"ends {str(m.get('endDate'))[:10]}  "
                    f"vol ${float(m.get('volumeNum') or 0):,.0f}"
                )
                print(f"         slug: {m.get('slug')}")
        return 0

    if args.action == "record":
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        entries = raw if isinstance(raw, list) else [raw]
        lines = []
        for e in entries:
            ident = e.get("slug") or e.get("condition_id")
            if not ident:
                print(f"skip: no slug/condition_id in {e}", file=sys.stderr)
                continue
            m = _market_by(str(ident), prefer_closed=False)
            if not m:
                print(f"skip: market not found for {ident}", file=sys.stderr)
                continue
            if m.get("closed"):
                print(f"skip: {ident} already closed", file=sys.stderr)
                continue
            px = _yes_price(m)
            if px is None:
                print(f"skip: no price for {ident}", file=sys.stderr)
                continue
            lines.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "condition_id": m.get("conditionId"),
                    "slug": m.get("slug"),
                    "question": m.get("question"),
                    "my_prob": float(e["my_prob"]),
                    "market_prob": px,
                    "reason": e.get("reason") or "",
                }
            )
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"recorded {len(lines)} forecasts -> {path}")
        return 0

    if args.action == "score":
        if not path.exists():
            print(f"no such file: {path}")
            return 0
        calls = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # first forecast per market is the honest one
        first: dict[str, dict] = {}
        for c in calls:
            first.setdefault(c.get("condition_id") or c.get("slug"), c)

        rows = []
        for c in first.values():
            m = _market_by(
                str(c.get("condition_id") or c.get("slug")), prefer_closed=True
            )
            if not m or not m.get("closed"):
                continue
            try:
                op = json.loads(m.get("outcomePrices") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if len(op) != 2 or op[0] not in ("0", "1") or op[0] == op[1]:
                continue
            yes = 1.0 if op[0] == "1" else 0.0
            rows.append(
                {
                    "question": c.get("question") or m.get("question"),
                    "my": float(c["my_prob"]),
                    "mkt": float(c["market_prob"]),
                    "yes": yes,
                }
            )

        if not rows:
            print(f"0 of {len(first)} frozen forecasts resolved yet")
            return 0

        mine = [(r["my"] - r["yes"]) ** 2 for r in rows]
        theirs = [(r["mkt"] - r["yes"]) ** 2 for r in rows]
        diff = [a - b for a, b in zip(mine, theirs)]
        st = _stats(diff)
        n, mean, se = st
        print(f"{n} resolved forecasts")
        print(f"  your Brier   {sum(mine) / n:.4f}")
        print(f"  market Brier {sum(theirs) / n:.4f}")
        if se is None:
            print(f"  difference   {mean:+.4f} (need >=2 resolved to estimate error)")
            return 0
        verdict = (
            "identical"
            if abs(mean) < 1e-9
            else ("you beat the market" if mean < 0 else "the market beats you")
        )
        print(f"  difference   {mean:+.4f} (SE {se:.4f})  {verdict}")
        if abs(mean) < 2 * se:
            print("  |diff| < 2*SE: indistinguishable from luck so far")
        print("\n  predicted vs realised")
        print(f"  {'your p':>8} {'market':>8} {'n':>4} {'realised':>9}")
        for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
            sel = [r for r in rows if lo <= r["my"] < lo + 0.2]
            if not sel:
                continue
            print(
                f"  {lo + 0.1:>8.1f} "
                f"{sum(r['mkt'] for r in sel) / len(sel):>8.2f} "
                f"{len(sel):>4} "
                f"{sum(r['yes'] for r in sel) / len(sel):>8.1%}"
            )
        return 0

    return 1


def _prev(series: list[tuple[float, float]], ts: float) -> float | None:
    """Last price at or before ts. Backward only, so no look-ahead."""
    keys = [p[0] for p in series]
    i = bisect.bisect_right(keys, ts)
    return series[i - 1][1] if i > 0 else None


def _run_sweep(paths: list, args) -> dict[float, tuple[int, float, float]]:
    out = {}
    for thr in args.sweep:
        rets = []
        for series in paths:
            i = 0
            while i < len(series):
                ts, p = series[i]
                p0 = _prev(series, ts - args.lookback * 3600)
                if p0 is None or p0 <= 0 or p <= 0:
                    i += 1
                    continue
                if not (args.min_price <= p <= args.max_price):
                    i += 1
                    continue
                move = (p - p0) / p0
                if abs(move) < thr:
                    i += 1
                    continue
                direction = 1.0 if move > 0 else -1.0
                if args.mode == "reversion":
                    direction = -direction
                exit_ts = ts + args.horizon * 3600
                p1 = _prev(series, exit_ts)
                if p1 is None or series[-1][0] < exit_ts:
                    break
                rets.append(direction * (p1 - p) / p)
                while i < len(series) and series[i][0] < exit_ts:
                    i += 1
        st = _stats(rets)
        if st:
            n, mean, se = st
            out[thr] = (n, mean, se, sum(1 for r in rets if r > 0) / n)
    return out


def _null_sweep(paths: list, args) -> tuple[float, float]:
    """Best t and best mean the same search finds on directionless data.

    Each replication flips the sign of every return at random, which keeps the
    magnitude distribution and destroys any directional predictability, then
    runs the identical four-threshold sweep. Whatever "best" comes out of that
    is luck by construction. If the real data does not beat this, it has no edge.
    """
    best_t, best_m = 0.0, -9.9
    for _ in range(args.null):
        fake = []
        for series in paths:
            px = [p for _, p in series]
            rs = [
                (px[i + 1] - px[i]) / px[i] if px[i] > 0 else 0.0
                for i in range(len(px) - 1)
            ]
            new = [px[0]]
            for r in rs:
                if random.random() < 0.5:
                    r = -r
                new.append(min(0.999, max(0.001, new[-1] * (1 + r))))
            fake.append(list(zip([t for t, _ in series], new)))
        for n, mean, se, _wins in _run_sweep(fake, args).values():
            if n < 20:
                continue
            t = mean / se if se else 0.0
            if abs(t) > abs(best_t):
                best_t = t
            best_m = max(best_m, mean)
    return best_t, best_m


def cmd_trend(args) -> int:
    """Does a price move predict the next one? Momentum vs reversion.

    Walk each resolved market forward. At every point, look BACK at the move
    over `lookback` and open one position in that direction (momentum) or
    against it (reversion). Exit after `horizon`. Only one position at a time
    per market, so overlapping trades cannot inflate the sample.

    Both the signal and the exit use prices that existed at that instant. There
    is no forward lookup in the signal path.
    """
    cut = datetime.now(UTC).timestamp() - args.min_age_days * 86400
    end_max = datetime.fromtimestamp(cut, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = fetch_closed_markets(args.markets, "volumeNum", False, end_max)

    paths = []
    for m in markets:
        try:
            op = json.loads(m.get("outcomePrices") or "[]")
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if len(op) != 2 or len(toks) != 2:
            continue
        if not ((op[0] == "1" and op[1] == "0") or (op[0] == "0" and op[1] == "1")):
            continue
        end_ts = _iso_ts(m.get("endDate") or "")
        if end_ts is None:
            continue
        series = price_series(toks[0], end_ts - args.days * 86400, end_ts)
        if len(series) < args.min_points:
            continue
        paths.append(series)

    if not paths:
        print("no usable price paths")
        return 0

    print(
        f"{len(paths)} resolved markets, {args.days:.0f} days of history each, "
        f"mode={args.mode}"
    )
    print(
        f"\n{'threshold':>10} {'trades':>7} {'mean':>9} {'SE':>8} "
        f"{'win%':>7} {'t-stat':>7}"
    )
    results = _run_sweep(paths, args)
    best_t = 0.0
    best_mean = -9.9
    for thr in args.sweep:
        st = results.get(thr)
        if not st:
            print(f"{thr:>10.2f} {0:>7}      n/a")
            continue
        n, mean, se, wins = st
        t = mean / se if se else float("nan")
        best_t = max(best_t, abs(t))
        best_mean = max(best_mean, mean)
        print(
            f"{thr:>10.2f} {n:>7} {fpct(mean):>9} {fpct(se):>8} "
            f"{fpct(wins):>7} {t:>7.2f}"
        )

    if args.null:
        print(f"\nrunning {args.null} directionless replications...", file=sys.stderr)
        null_t, null_mean = _null_sweep(paths, args)
        print(
            f"\nnull model ({args.null} shuffles, same sweep of "
            f"{len(args.sweep)} thresholds):"
        )
        print(f"  best |t| on random data      {null_t:.2f}")
        print(f"  best mean on random data     {fpct(null_mean)}")
        print(f"  best |t| on real data        {best_t:.2f}")
        print(f"  best mean on real data       {fpct(best_mean)}")
        print("\n  If real does not clearly beat the null, the 'plus' is what the")
        print("  search itself produces on data with no signal in it.")

    print("\nPositive t means the move continued (or reverted) more often than")
    print("chance. |t| < 2 is noise. Entry and exit are both real past prices.")
    return 0


def cmd_rewards(args) -> int:
    """Liquidity rewards minus the cost of getting filled.

    Polymarket pays `rewardsDailyRate` per market to orders resting inside
    `rewardsMaxSpread` of the mid with at least `rewardsMinSize`. That is income
    for not trading -- the opposite of every strategy tested so far.

    The cost side is fills. An order sitting `band` away from mid only fills if
    the price travels at least that far, and when it does you own inventory that
    is already `band` worse than mid. So:

        expected daily cost = P(|daily move| >= band) * band * shares

    P is measured from the market's own 7-day history, not assumed. Net yield is
    what is left after that cost, per dollar of resting capital.
    """
    markets = []
    for off in range(0, args.scan, 100):
        page = api_get(
            f"{GAMMA}/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "volumeNum",
                "ascending": "false",
                "offset": off,
            },
        )
        if not isinstance(page, list) or not page:
            break
        markets.extend(page)

    with_r = []
    for m in markets:
        rewards = m.get("clobRewards") or []
        rate = sum(float(r.get("rewardsDailyRate") or 0) for r in rewards)
        if rate <= 0:
            continue
        band = float(m.get("rewardsMaxSpread") or 0) / 100.0
        min_size = float(m.get("rewardsMinSize") or 0)
        if band <= 0 or min_size <= 0:
            continue
        with_r.append((m, rate, band, min_size))

    with_r.sort(key=lambda x: -x[1])
    with_r = with_r[: args.top]

    end = time.time()
    now_ts = end
    out = []
    for m, rate, band, min_size in with_r:
        end_ts = _iso_ts(m.get("endDate") or "")
        if end_ts is not None and end_ts - now_ts < 86400:
            continue  # resolves within a day: rewards are an intraday race, not a yield
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            continue
        if len(toks) != 2:
            continue
        try:
            book = api_get(f"{CLOB}/book", {"token_id": toks[0]})
        except RuntimeError:
            continue
        bids = [float(x["price"]) for x in (book.get("bids") or []) if x.get("price")]
        asks = [float(x["price"]) for x in (book.get("asks") or []) if x.get("price")]
        if not bids or not asks:
            continue
        mid = (max(bids) + min(asks)) / 2
        if not (0.01 < mid < 0.99):
            continue

        comp = 0.0
        for lv in (book.get("bids") or []) + (book.get("asks") or []):
            try:
                px, sz = float(lv["price"]), float(lv["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if abs(px - mid) <= band:
                comp += sz

        series = price_series(toks[0], end - 7 * 86400, end)
        if len(series) < 20:
            continue
        moves = []
        t = series[0][0]
        last_ts = series[-1][0]
        while t + 86400 <= last_ts:
            p1 = _prev(series, t)
            p2 = _prev(series, t + 86400)
            if p1 and p2 and p1 > 0:
                moves.append(abs(p2 - p1))
            t += 6 * 3600
        if len(moves) < 5:
            continue

        shares = max(min_size, args.shares)
        capital = shares * mid
        best = None
        # Sweep the distance from mid. Closer earns more reward but fills more.
        step = band / 10.0
        for k in range(1, 11):
            d = step * k
            score = (1.0 - d / band) ** 2  # convex spread score, standard shape
            weighted = shares * score
            reward = rate * weighted / (weighted + comp)
            p_fill = sum(1 for x in moves if x >= d) / len(moves)
            cost = p_fill * d * shares
            net = reward - cost
            if best is None or net > best["net"]:
                best = {
                    "d": d,
                    "score": score,
                    "reward": reward,
                    "p_fill": p_fill,
                    "cost": cost,
                    "net": net,
                    "net_pct": net / capital * 100 if capital else 0.0,
                }
        if best is None:
            continue
        best.update(
            {
                "q": (m.get("question") or "")[:46],
                "rate": rate,
                "band": band,
                "mid": mid,
                "comp": comp,
                "capital": capital,
            }
        )
        out.append(best)

    if not out:
        print("no markets with both rewards and price history")
        return 0

    out.sort(key=lambda r: -r["net_pct"])
    print(
        f"{len(out)} reward markets analysed, {args.shares:.0f} shares per order, "
        f"net after expected fill cost"
    )
    print(
        f"\n{'net%/day':>9} {'reward$':>9} {'cost$':>7} {'p(fill)':>8} "
        f"{'band':>6} {'capital':>8}  market"
    )
    for r in out[: args.show]:
        print(
            f"{r['net_pct']:>8.2f}% {r['reward']:>9.2f} {r['cost']:>7.2f} "
            f"{r['p_fill']:>8.0%} {r['band']:>6.2f} {r['capital']:>8.0f}  "
            f"{r['q']}"
        )

    pos = [r for r in out if r["net"] > 0]
    print(
        f"\npositive net on {len(pos)}/{len(out)} markets; "
        f"best {fpct(out[0]['net_pct'] / 100)} per day on capital"
    )
    print("\nModel ignores spread score (orders at the band edge earn less),")
    print("queue position, and the fact that fills cluster on news.")
    return 0


def cmd_paper(args) -> int:
    """Forward paper trading: record now, settle later, never re-fit.

    Every backtest in this repo was built by looking at history, which is exactly
    where selection creeps in. This command removes that degree of freedom: it
    writes the entry price and timestamp now, and the only thing that can change
    afterwards is the outcome.

    `record`  snapshot qualifying live markets into an append-only log
    `settle`  fill in outcomes for markets that have since resolved
    `report`  cumulative P&L with standard error
    """
    path = Path(args.file)

    if args.action == "record":
        rows = []
        for off in range(0, 1000, 100):
            page = api_get(
                f"{GAMMA}/markets",
                {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "volumeNum",
                    "ascending": "false",
                    "offset": off,
                },
            )
            if not isinstance(page, list) or not page:
                break
            rows.extend(page)

        seen = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    seen.add(json.loads(line)["condition_id"])

        new = []
        for m in rows:
            if len(new) + len(seen) >= args.max:
                break
            cid = m.get("conditionId")
            if not cid or cid in seen:
                continue
            if float(m.get("volumeNum") or 0) < args.min_volume:
                continue
            px = _yes_price(m)
            if px is None or px < args.threshold:
                continue
            if not (args.min_price <= px <= args.max_price):
                continue
            end_ts = _iso_ts(m.get("endDate") or "")
            # a 2028 election "settles" in 2028: it cannot inform a test that is
            # supposed to finish this month
            if args.max_days and (
                end_ts is None or end_ts - time.time() > args.max_days * 86400
            ):
                continue
            new.append(
                {
                    "condition_id": cid,
                    "question": (m.get("question") or "")[:80],
                    "entry": px,
                    "end_date": m.get("endDate"),
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "strategy": f"yes>={args.threshold}",
                    "outcome": None,
                    "pnl": None,
                }
            )
        with path.open("a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"recorded {len(new)} new paper bets -> {path}")
        return 0

    if not path.exists():
        print(f"no such file: {path}")
        return 0

    bets = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if args.action == "settle":
        changed = 0
        for b in bets:
            if b.get("outcome") is not None:
                continue
            m = _market_by(b["condition_id"], prefer_closed=True)
            if not m or not m.get("closed"):
                continue
            try:
                op = json.loads(m.get("outcomePrices") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if len(op) != 2 or op[0] not in ("0", "1") or op[0] == op[1]:
                continue
            yes = 1.0 if op[0] == "1" else 0.0
            b["outcome"] = yes
            b["pnl"] = (yes - b["entry"]) / b["entry"] if b["entry"] else 0.0
            changed += 1
        path.write_text(
            "\n".join(json.dumps(b, ensure_ascii=False) for b in bets) + "\n",
            encoding="utf-8",
        )
        print(
            f"settled {changed} bets ({sum(1 for b in bets if b.get('outcome') is None)} still open)"
        )
        return 0

    if args.action == "report":
        done = [b for b in bets if b.get("pnl") is not None]
        if not done:
            print(f"0 of {len(bets)} bets settled yet")
            return 0
        vals = [b["pnl"] for b in done]
        st = _stats(vals)
        n, mean, se = st
        print(f"{n} settled of {len(bets)} recorded")
        print(f"  mean P&L per bet  {fpct(mean)}")
        if se:
            print(f"  SE                {fpct(se)}")
            print(
                f"  t-stat            {mean / se:.2f}   "
                f"{'positive' if mean / se > 2 else 'not significant'}"
            )
        print(f"  win rate          {sum(1 for v in vals if v > 0) / n:.1%}")
        print(f"  worst             {fpct(min(vals))}")
        print(f"  best              {fpct(max(vals))}")
        print("\nThese are forward, timestamped bets. Nothing here was re-fit.")
        return 0

    return 1


def _norm_sf(z: float) -> float:
    """Upper tail of the standard normal: P(Z > z)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _sort_ts(x: dict) -> float:
    t = x.get("recorded_at") or x.get("trade_ts") or x.get("ts") or 0
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t).timestamp()
        except ValueError:
            return 0.0
    try:
        return float(t)
    except (TypeError, ValueError):
        return 0.0


def cmd_chart(args) -> int:
    """Equity curve for whatever is settled so far.

    Works on both the paper log (`.jsonl`, field `pnl`) and a frozen signal file
    (`.json`, field `returns[horizon]`). Unsettled rows are skipped, never
    zero-filled -- a missing outcome is not a flat bet.
    """
    path = Path(args.file)
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        rows = json.loads(raw)

    if (
        args.one_per_market
        or args.max_adverse_move is not None
        or args.max_lag is not None
    ):
        total = len(rows)
        rows = strategy_rows(
            rows, args.max_adverse_move, args.one_per_market, args.max_lag
        )
        print(f"strategy rows: {len(rows)} of {total}")

    pts = []
    for x in rows:
        if args.horizon:
            ret = x.get("returns") or {}
            v = ret.get(str(args.horizon), ret.get(args.horizon))
        else:
            v = x.get("pnl")
        if v is None:
            continue
        pts.append((_sort_ts(x), float(v)))
    if not pts:
        print(f"no settled rows in {path}")
        return 0
    pts.sort()

    equity = []
    run = 0.0
    for _, v in pts:
        run += v * args.stake
        equity.append(run)
    n = len(equity)
    final = equity[-1]
    peak = max(equity)
    dd = final - peak
    mean = sum(v for _, v in pts) / n
    se = None
    if n > 1:
        se = math.sqrt(sum((v - mean) ** 2 for _, v in pts) / (n - 1) / n)

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [3, 1]}, sharex=False
    )
    fig.patch.set_facecolor("#111418")
    for ax in (ax1, ax2):
        ax.set_facecolor("#111418")
        ax.tick_params(colors="#9aa4b2", labelsize=9)
        for s in ax.spines.values():
            s.set_color("#2a2f36")
        ax.grid(True, color="#1e242b", linewidth=0.8)

    xs = list(range(n + 1))
    curve = [0.0, *equity]
    ax1.plot(
        xs,
        curve,
        color="#4ade80",
        linewidth=2,
        marker="o" if n <= 30 else None,
        markersize=3,
    )
    ax1.fill_between(xs, 0, curve, color="#4ade80", alpha=0.12)
    ax1.axhline(0, color="#5b6470", linewidth=1)
    if n <= 30:
        ax1.set_xticks(range(n + 1))
    ax1.set_ylabel(f"cumulative P&L, $ ({args.stake:.0f} per bet)", color="#c9d1d9")
    ax1.set_title(
        f"{path.name}  |  n={n} settled  |  ${final:+,.2f}  |  "
        f"mean {mean * 100:+.1f}% per bet",
        color="#e6edf3",
        fontsize=11,
    )

    colors = ["#4ade80" if v >= 0 else "#f87171" for _, v in pts]
    ax2.bar(range(1, n + 1), [v * 100 for _, v in pts], color=colors, width=0.8)
    ax2.axhline(0, color="#5b6470", linewidth=1)
    if n <= 30:
        ax2.set_xticks(range(1, n + 1))
    ax2.set_ylabel("per bet, %", color="#c9d1d9")
    ax2.set_xlabel("bet number (chronological)", color="#9aa4b2")

    note = f"max drawdown ${dd:+,.2f}"
    if se:
        note += f"   |   t-stat {mean / se:.2f}"
        if abs(mean) < 2 * se:
            note += "  (not significant)"
    ax1.text(0.01, 0.03, note, transform=ax1.transAxes, color="#9aa4b2", fontsize=9)

    fig.tight_layout()
    fig.savefig(args.png, dpi=130, facecolor=fig.get_facecolor())
    print(f"chart: {args.png}")
    print(
        f"  n={n}  final ${final:+,.2f}  mean {mean * 100:+.2f}%  "
        f"peak ${peak:+,.2f}  drawdown ${dd:+,.2f}"
    )
    return 0


DASHBOARD_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>polymarket-lab — forward equity</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--line:#21262d;--txt:#c9d1d9;--dim:#8b949e;
        --green:#3fb950;--red:#f85149;--blue:#58a6ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;
         align-items:baseline;gap:16px;flex-wrap:wrap}
  h1{font-size:16px;margin:0;font-weight:600}
  .dim{color:var(--dim);font-size:12px}
  .wrap{padding:20px 24px;display:grid;gap:18px;grid-template-columns:repeat(4,1fr)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
  .card .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
  .pos{color:var(--green)} .neg{color:var(--red)} .muted{color:var(--dim)}
  .chartbox{grid-column:1/-1;background:var(--panel);border:1px solid var(--line);
            border-radius:10px;padding:14px 16px}
  #chart{width:100%;height:340px;display:block}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);font-size:13px}
  th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  @media(max-width:900px){.wrap{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<header>
  <h1>polymarket-lab · forward equity</h1>
  <span class="dim" id="stamp">loading…</span>
  <span class="dim"><span class="dot" id="live" style="background:var(--dim)"></span><span id="livetxt">offline</span></span>
</header>
<div class="wrap">
  <div class="card"><div class="k">Paper P&amp;L</div><div class="v" id="pnl">—</div></div>
  <div class="card"><div class="k">Settled bets</div><div class="v" id="n">—</div></div>
  <div class="card"><div class="k">Mean per bet</div><div class="v" id="mean">—</div></div>
  <div class="card"><div class="k">t-stat</div><div class="v" id="tstat">—</div></div>
  <div class="chartbox">
    <div id="chart"></div>
    <div class="dim" style="margin-top:8px">
      <span style="color:var(--green)">━</span> paper (forward) &nbsp;
      <span style="color:var(--blue)">━</span> signals 60m (forward) &nbsp;
      <span class="muted">· dashed = not significant (|t| &lt; 2)</span>
    </div>
  </div>
  <div class="chartbox">
    <table><thead><tr><th>#</th><th>market</th><th>entry</th><th>outcome</th><th>P&amp;L</th></tr></thead>
    <tbody id="rows"></tbody></table>
  </div>
</div>
<script>
const STAKE = __STAKE__;
let DATA = null;

function fmtMoney(v){return (v>=0?"+":"−")+"$"+Math.abs(v).toFixed(2);}
function fmtPct(v){return (v>=0?"+":"−")+(Math.abs(v)*100).toFixed(1)+"%";}

function draw(){
  const el=document.getElementById("chart");
  if(!el) return;
  const W=Math.max(320, el.clientWidth||1200), H=Math.max(220, el.clientHeight||340);
  if(!DATA){ el.innerHTML='<div style="color:#8b949e;padding:16px">no data</div>'; return; }
  const series=[];
  if(DATA.paper && DATA.paper.series && DATA.paper.series.length>1)
    series.push({pts:DATA.paper.series,color:"#3fb950",dash:false});
  for(const k of Object.keys(DATA.signals||{})){
    const s=DATA.signals[k];
    if(s && s.series && s.series.length>1)
      series.push({pts:s.series,color:"#58a6ff",dash:Math.abs(s.t||0)<2});
  }
  if(!series.length){
    el.innerHTML='<div style="color:#8b949e;padding:16px">'+
      'no settled bets yet — waiting for the first outcomes</div>';
    return;
  }
  const pad={l:64,r:16,t:14,b:28};
  let maxN=0,minV=0,maxV=0;
  for(const s of series){ maxN=Math.max(maxN,s.pts.length-1);
    for(const v of s.pts){ minV=Math.min(minV,v); maxV=Math.max(maxV,v); } }
  if(maxV===minV) maxV=minV+1;
  const X=i=>pad.l+(W-pad.l-pad.r)*(maxN?i/maxN:0);
  const Y=v=>pad.t+(H-pad.t-pad.b)*(1-(v-minV)/(maxV-minV));
  let o=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  for(let k=0;k<=4;k++){
    const v=minV+(maxV-minV)*k/4, y=Y(v).toFixed(1);
    o+=`<line x1="${pad.l}" y1="${y}" x2="${W-pad.r}" y2="${y}" stroke="#21262d"/>`;
    o+=`<text x="8" y="${+y+4}" fill="#8b949e" font-size="11" font-family="ui-sans-serif">$${v.toFixed(0)}</text>`;
  }
  o+=`<line x1="${pad.l}" y1="${Y(0).toFixed(1)}" x2="${W-pad.r}" y2="${Y(0).toFixed(1)}" stroke="#30363d"/>`;
  for(const s of series){
    const d=s.pts.map((v,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    o+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2"`+
       (s.dash?' stroke-dasharray="5 4"':'')+`/>`;
  }
  o+=`<text x="${W-pad.r-34}" y="${H-8}" fill="#8b949e" font-size="11">bet #</text>`;
  o+="</svg>";
  el.innerHTML=o;
}

function render(){
  if(!DATA) return;
  const p=DATA.paper||{};
  const pnlEl=document.getElementById("pnl");
  pnlEl.textContent=p.final!=null?fmtMoney(p.final):"—";
  pnlEl.className="v "+(p.final>=0?"pos":"neg");
  document.getElementById("n").textContent=p.n!=null?p.n:"—";
  document.getElementById("mean").textContent=p.mean!=null?fmtPct(p.mean):"—";
  const tEl=document.getElementById("tstat");
  if(p.t!=null){ tEl.textContent=p.t.toFixed(2)+(Math.abs(p.t)<2?" ?":""); }
  else tEl.textContent="—";
  document.getElementById("stamp").textContent="updated "+(DATA.generated_at||"").replace("T"," ").slice(0,19)+" UTC";

  const tb=document.getElementById("rows"); tb.innerHTML="";
  for(const r of (p.recent||[])){
    const tr=document.createElement("tr");
    const cls=r.pnl>=0?"pos":"neg";
    tr.innerHTML=`<td class="muted">${r.i}</td><td>${r.q}</td>
      <td>${r.entry.toFixed(3)}</td><td>${r.outcome}</td>
      <td class="${cls}">${fmtPct(r.pnl)}</td>`;
    tb.appendChild(tr);
  }
  if(!(p.recent||[]).length){
    tb.innerHTML=`<tr><td colspan="5" class="muted">no settled bets yet</td></tr>`;
  }
  draw();
}

async function tick(){
  try{
    const r=await fetch("dashboard_data.json?t="+Date.now(),{cache:"no-store"});
    if(!r.ok) throw new Error(r.status);
    DATA=await r.json();
    document.getElementById("live").style.background="var(--green)";
    document.getElementById("livetxt").textContent="live";
    render();
  }catch(e){
    document.getElementById("live").style.background="var(--red)";
    document.getElementById("livetxt").textContent="offline";
  }
}
tick(); setInterval(tick, 30000);
window.addEventListener("resize", draw);
</script></body></html>
"""


def cmd_dashboard(args) -> int:
    """Live local dashboard: an HTML page that polls the local server.

    Static PNGs answer "how did it look when I ran the script". This answers
    "how is it doing right now": the page re-fetches `/api/live` every 30 s,
    served by `serve.py`. The JSON written next to it is a frozen snapshot for
    humans; `build-static` owns the published `dashboard_data.json`.
    """
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def cumulative(rows: list[dict], field: str, horizon: int | None) -> dict:
        pts = []
        for x in rows:
            if horizon:
                ret = x.get("returns") or {}
                v = ret.get(str(horizon), ret.get(horizon))
            else:
                v = x.get(field)
            if v is None:
                continue
            pts.append((_sort_ts(x), float(v), x))
        pts.sort(key=lambda t: t[0])
        if not pts:
            return {"n": 0, "series": [], "recent": []}
        vals = [p[1] for p in pts]
        n = len(vals)
        mean = sum(vals) / n
        se = (
            math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1) / n)
            if n > 1
            else None
        )
        series, run = [0.0], 0.0
        for v in vals:
            run += v * args.stake
            series.append(run)
        recent = []
        for i, (_, v, x) in enumerate(pts[-12:], start=max(1, n - 11)):
            outcome = x.get("outcome")
            if outcome is None:
                outcome = "YES" if v > 0 else "NO"
            elif isinstance(outcome, float):
                outcome = "YES" if outcome == 1.0 else "NO"
            recent.append(
                {
                    "i": i,
                    "q": (x.get("question") or "")[:52],
                    "entry": float(x.get("entry") or x.get("now_price") or 0.0),
                    "outcome": outcome,
                    "pnl": v,
                }
            )
        recent.reverse()
        return {
            "n": n,
            "final": run,
            "mean": mean,
            "t": (mean / se) if se else None,
            "series": series,
            "recent": recent,
        }

    payload = {"generated_at": datetime.now(UTC).isoformat(), "stake": args.stake}

    paper_path = Path(args.paper)
    if paper_path.exists():
        raw = paper_path.read_text(encoding="utf-8")
        rows = [json.loads(x) for x in raw.splitlines() if x.strip()]
        payload["paper"] = cumulative(rows, "pnl", None)
    else:
        payload["paper"] = {"n": 0, "series": [], "recent": []}

    sig_path = Path(args.signals)
    payload["signals"] = {}
    if sig_path.exists():
        try:
            rows = json.loads(sig_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rows = []
        rows = strategy_rows(
            rows, args.max_adverse_move, args.one_per_market, args.max_lag
        )
        for h in (60, 240):
            payload["signals"][str(h)] = cumulative(rows, "", h)

    # The live page polls /api/live; this file is only a frozen snapshot for
    # humans. It must not be called dashboard_data.json: build-static writes the
    # published payload there, and the hourly local task used to overwrite it
    # with a different schema, which breaks the GitHub Pages dashboard.
    (out_dir / "dashboard_local_data.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    tpl_path = Path(__file__).resolve().parent / "dashboard_template.html"
    tpl = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else DASHBOARD_HTML
    (out_dir / "dashboard_local.html").write_text(
        tpl.replace("__STAKE__", str(args.stake)).replace("__SOURCE__", "/api/live"),
        encoding="utf-8",
    )
    p = payload["paper"]
    print(
        f"dashboard -> {out_dir / 'dashboard_local.html'}\n"
        f"  paper: n={p.get('n', 0)} final ${p.get('final', 0):+,.2f}"
    )
    return 0


def cmd_build_static(args) -> int:
    """Render the dashboard for a static host such as GitHub Pages.

    Pages cannot run the live server, so the state is computed once here and the
    page polls a committed JSON instead of /api/live. Everything else -- the
    marking, the history, the layout -- is the same code path as the live server.
    """
    import serve

    serve._stake = args.stake
    state = serve.live_state()

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dashboard_data.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    tpl_path = Path(__file__).resolve().parent / "dashboard_template.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    html = tpl.replace("__STAKE__", str(args.stake)).replace(
        "__SOURCE__", "dashboard_data.json"
    )
    (out / "dashboard.html").write_text(html, encoding="utf-8")
    # GitHub Pages serves index.html at the site root; without it the README wins
    (out / "index.html").write_text(html, encoding="utf-8")
    print(
        f"static dashboard -> {out / 'dashboard.html'}  "
        f"equity {state['equity']:+.2f}  open {state['open']}  "
        f"marked {state['marked']}  history {len(state['history'])}"
    )
    return 0


def cmd_evaluate(args) -> int:
    signals = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    horizons = [int(h) for h in args.horizons.split(",")]
    now_ts = time.time()
    rows = []
    idxs = []
    cleared = []
    for idx, s in enumerate(signals):
        entry = entry_price(s)
        stored = s.get("returns") or {}
        if not entry:
            # No executable entry: clear any earlier score, otherwise a stale
            # number survives in the file and keeps showing up in charts.
            cleared.append(idx)
            continue
        evaluable = entry >= MIN_EVALUABLE_ENTRY
        row = {
            "wallet": s.get("wallet"),
            "title": s.get("title"),
            "entry": entry,
            "side": s.get("side"),
            "usdc": s.get("usdc"),
            "trade_ts": s.get("trade_ts") or 0.0,
            "detected_at": s.get("detected_at") or "",
            "condition_id": s.get("condition_id") or "",
            "moved": s.get("moved"),
            "returns": {},
        }
        for h in horizons:
            # A stored number is final, but a stored None only means "the
            # horizon had not printed yet" and must be retried. This is what
            # kept 240m empty: the first run wrote nulls and every later run
            # reused them.
            cached = stored_final_return(stored, h)
            if cached is not None and evaluable:
                row["returns"][h] = cached
                continue
            if not evaluable:
                row["returns"][h] = None
                continue
            if not horizon_window_valid(s, h):
                row["returns"][h] = None
                continue
            tok, base_ts = s.get("token_id"), s.get("trade_ts")
            if not (tok and base_ts):
                row["returns"][h] = None
                continue
            status = horizon_status(h, float(base_ts), now_ts)
            if status != "retry":
                row["returns"][h] = None
                continue
            p = price_at(tok, base_ts + h * 60)
            row["returns"][h] = horizon_return(s.get("side"), entry, p)
        rows.append(row)
        idxs.append(idx)

    if args.write_back:
        # persist what was just computed, so the dashboard and the next run do
        # not have to fetch the same prices again
        for row, idx in zip(rows, idxs):
            signals[idx]["returns"] = {str(h): v for h, v in row["returns"].items()}
        for idx in cleared:
            signals[idx]["returns"] = {str(h): None for h in horizons}
        Path(args.signals).write_text(
            json.dumps(signals, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"returns written back to {args.signals}")

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    entries = [entry_price(s) for s in signals]
    unpriced = sum(1 for e in entries if e is None)
    thin = sum(1 for e in entries if e is not None and 0 < e < MIN_EVALUABLE_ENTRY)
    print(f"{len(rows)} frozen signals, {len(horizons)} horizons")
    if unpriced:
        print(
            f"  {unpriced} live signals have no book price and are not scored: "
            "the whale's fill is not an executable entry"
        )
    if thin:
        print(
            f"  {thin} signals left unscored: entry below ${MIN_EVALUABLE_ENTRY:.2f} "
            "(decided or illiquid book, a return there is division by noise)"
        )

    def table(sub_rows: list[dict]) -> None:
        print(
            f"{'horizon':>8} {'n':>4} {'flat':>7} {'mean':>9} {'median':>9} "
            f"{'win%*':>7} {'SE':>7}"
        )
        for h in horizons:
            vals = [
                r["returns"][h] for r in sub_rows if r["returns"].get(h) is not None
            ]
            if not vals:
                print(f"{h:>7}m {0:>4}      n/a")
                continue
            vals.sort()
            n = len(vals)
            mean = sum(vals) / n
            med = vals[n // 2]
            # A zero return is not a loss: the market simply did not move.
            # Counting those as losses makes a dead-flat strategy look like a
            # 30% win rate, so they are reported separately and excluded.
            flat = sum(1 for v in vals if v == 0) / n
            moved = [v for v in vals if v != 0]
            wins = (sum(1 for v in moved if v > 0) / len(moved)) if moved else None
            var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
            se = math.sqrt(var / n) if n > 1 else None
            print(
                f"{h:>7}m {n:>4} {fpct(flat):>7} {fpct(mean):>9} {fpct(med):>9} "
                f"{fpct(wins):>7} {fpct(se):>7}"
            )

    table(rows)
    print("\n* win% is over non-flat outcomes only. SE is the standard error of")
    print("  the mean: if |mean| < 2*SE the result is indistinguishable from zero.")
    print("Entry is the price available AFTER detection, so the delay is already")
    print("in the number. A mean that does not clear zero is not an edge.")

    print("\nconcentration: is the mean carried by a few trades?")
    for h in horizons:
        vals = sorted(
            [r["returns"][h] for r in rows if r["returns"].get(h) is not None],
            reverse=True,
        )
        if not vals:
            continue
        k = max(1, len(vals) // 100)
        total = sum(vals)
        top = sum(vals[:k])
        rest = (total - top) / (len(vals) - k) if len(vals) > k else None
        share = (top / total) if total else None
        print(
            f"  {h:>5}m  best {k:>3}/{len(vals):<5} = {fpct(share):>8} of total "
            f"return; mean without them = {fpct(rest)}"
        )
    print("  A mean that lives in the top 1% is a lottery ticket, not a strategy.")

    if (
        args.one_per_market
        or args.max_adverse_move is not None
        or args.max_lag is not None
    ):
        picked = strategy_rows(
            rows, args.max_adverse_move, args.one_per_market, args.max_lag
        )
        print()
        print(
            f"=== executable strategy: {len(picked)} of {len(rows)} rows "
            f"(one per market={args.one_per_market}, "
            f"max adverse move={args.max_adverse_move}, "
            f"max lag={args.max_lag}) ==="
        )
        table(picked)
        print(
            "  One position per market and no chasing: this is what a portfolio "
            "could actually have held."
        )

    if args.by_wallet:
        print()
        print(f"=== per-wallet expectancy, min {args.min_n} signals ===")
        for h in horizons:
            groups: dict[str, list[float]] = {}
            for r in rows:
                v = r["returns"].get(h)
                w = r["wallet"] or "?"
                groups.setdefault(w, [])
                if v is not None:
                    groups[w].append(v)
            scored = []
            for w, vals in groups.items():
                st = _stats(vals)
                if st and st[0] >= args.min_n:
                    scored.append((w, *st))
            if not scored:
                print(f"\n{h}m: no wallet with {args.min_n}+ signals")
                continue
            scored.sort(key=lambda x: x[2], reverse=True)
            sig = [
                s for s in scored if s[3] is not None and s[2] > 2 * s[3] and s[2] > 0
            ]
            print(
                f"\n{h}m: {len(scored)} wallets, {sum(s[1] for s in scored)} signals, "
                f"{len(sig)} with mean > 2*SE"
            )
            print(f"  {'wallet':<16} {'n':>5} {'mean':>9} {'SE':>7} {'2SE':>9}  flag")
            shown = (
                scored[:8] + ([None] if len(scored) > 16 else []) + scored[-4:]
                if len(scored) > 16
                else scored
            )
            for s in shown:
                if s is None:
                    print("  ...")
                    continue
                w, n, mean, se = s
                flag = "SIG" if (se is not None and mean > 2 * se and mean > 0) else ""
                print(
                    f"  {w[:14]:<16} {n:>5} {fpct(mean):>9} {fpct(se):>7} "
                    f"{fpct(2 * se) if se else 'n/a':>9}  {flag}"
                )

    if args.holdout:
        print()
        print("=== out-of-sample: select on the first half, judge on the second ===")
        ordered = sorted(rows, key=lambda r: r["trade_ts"])
        if len(ordered) < 10:
            print("too few signals to split")
            return 0
        mid = ordered[len(ordered) // 2]["trade_ts"]
        early = [r for r in ordered if r["trade_ts"] <= mid]
        late = [r for r in ordered if r["trade_ts"] > mid]
        print(f"early n={len(early)}, late n={len(late)}")
        for h in horizons:
            ev = _stats([r["returns"].get(h) for r in early])
            lv = _stats([r["returns"].get(h) for r in late])
            ge: dict[str, list[float]] = {}
            for r in early:
                v = r["returns"].get(h)
                ge.setdefault(r["wallet"] or "?", [])
                if v is not None:
                    ge[r["wallet"] or "?"].append(v)
            gl: dict[str, list[float]] = {}
            for r in late:
                v = r["returns"].get(h)
                gl.setdefault(r["wallet"] or "?", [])
                if v is not None:
                    gl[r["wallet"] or "?"].append(v)
            picked = []
            for w, vals in ge.items():
                st = _stats(vals)
                if st and st[0] >= args.min_n:
                    _, mean, se = st
                    if se is not None and mean > 2 * se and mean > 0:
                        picked.append(w)
            pairs = []
            for w, vals in ge.items():
                se_st = _stats(vals)
                sl_st = _stats(gl.get(w, []))
                if (
                    se_st
                    and sl_st
                    and se_st[0] >= args.min_n
                    and sl_st[0] >= args.min_n
                ):
                    pairs.append((se_st[1], sl_st[1]))
            if len(pairs) >= 3:
                c = pearson([p[0] for p in pairs], [p[1] for p in pairs])
                print(
                    f"{h:>5}m  persistence: corr(early, late) over {len(pairs)} "
                    f"wallets = {fnum(c)}"
                )
            picked = []
            for w, vals in ge.items():
                st = _stats(vals)
                if st and st[0] >= args.min_n:
                    _, mean, se = st
                    if se is not None and mean > 2 * se and mean > 0:
                        picked.append(w)
            lv_pick = _stats(
                [r["returns"].get(h) for r in late if r["wallet"] in picked]
            )
            if not lv_pick:
                print(f"{h:>5}m: picked {len(picked)} wallets, no late data")
                continue
            n, mean, se = lv_pick
            holds = se is not None and mean > 2 * se and mean > 0
            print(
                f"{h:>5}m  early {fpct(ev[1]) if ev else 'n/a':>7}"
                f"+-{fpct(ev[2]) if ev and ev[2] else 'n/a':<6} | "
                f"late {fpct(lv[1]) if lv else 'n/a':>7}"
                f"+-{fpct(lv[2]) if lv and lv[2] else 'n/a':<6} | "
                f"picked {len(picked):>2} -> late mean "
                f"{fpct(mean)} (SE {fpct(se)}, n={n})  "
                f"{'HOLDS' if holds else 'does NOT hold'}"
            )
        print("\nA flag that does not survive its own holdout is luck, not skill.")
    if args.walk_forward and args.walk_forward > 1:
        k = args.walk_forward
        ordered = sorted(rows, key=lambda r: r["trade_ts"])
        n = len(ordered)
        buckets = [ordered[i * n // k : (i + 1) * n // k] for i in range(k)]
        print()
        print(f"=== walk-forward: {k} consecutive time buckets, {n} signals ===")
        min_per_bucket = max(3, args.min_n // 4)
        for h in horizons:
            stats = [_stats([r["returns"].get(h) for r in b]) for b in buckets]
            cells = "  ".join(f"{fpct(s[1]) if s else 'n/a':>7}" for s in stats)
            pos = sum(1 for s in stats if s and s[1] > 0)
            print(f"{h:>5}m  {cells}   ({pos}/{k} positive)")
            for i in range(k - 1):
                a: dict[str, list[float]] = {}
                for r in buckets[i]:
                    v = r["returns"].get(h)
                    a.setdefault(r["wallet"] or "?", [])
                    if v is not None:
                        a[r["wallet"] or "?"].append(v)
                c: dict[str, list[float]] = {}
                for r in buckets[i + 1]:
                    v = r["returns"].get(h)
                    c.setdefault(r["wallet"] or "?", [])
                    if v is not None:
                        c[r["wallet"] or "?"].append(v)
                pairs = []
                for w, vals in a.items():
                    sa, sc = _stats(vals), _stats(c.get(w, []))
                    if (
                        sa
                        and sc
                        and sa[0] >= min_per_bucket
                        and sc[0] >= min_per_bucket
                    ):
                        pairs.append((sa[1], sc[1]))
                if len(pairs) >= 4:
                    cc = pearson([p[0] for p in pairs], [p[1] for p in pairs])
                    print(
                        f"        corr(bucket {i + 1} -> {i + 2}) over "
                        f"{len(pairs)} wallets = {fnum(cc)}"
                    )
        print("\nA strategy that is positive in some buckets but not others, with")
        print("no cross-bucket correlation, is a coin landing heads a few times.")

    if args.fdr:
        print()
        print(
            f"=== Benjamini-Hochberg, q={args.q:.2f}, "
            f"min {args.min_n} signals per wallet ==="
        )
        for h in horizons:
            groups: dict[str, list[float]] = {}
            for r in rows:
                v = r["returns"].get(h)
                groups.setdefault(r["wallet"] or "?", [])
                if v is not None:
                    groups[r["wallet"] or "?"].append(v)
            tests = []
            for w, vals in groups.items():
                st = _stats(vals)
                if st and st[0] >= args.min_n and st[2]:
                    n, mean, se = st
                    tests.append((_norm_sf(mean / se), w, n, mean, mean / se))
            if not tests:
                print(f"{h:>5}m: no wallet with enough signals")
                continue
            tests.sort()
            m = len(tests)
            passed = 0
            for i, (p, *_rest) in enumerate(tests, 1):
                if p <= i / m * args.q:
                    passed = i
            print(
                f"{h:>5}m  tested {m} wallets, {passed} survive FDR; "
                f"expected false positives if nothing is real: {m * args.q:.1f}"
            )
            for p, w, n, mean, t in tests[:passed]:
                print(
                    f"        {w[:14]:<16} n={n:<4} mean {fpct(mean):>7} "
                    f"t={t:>5.2f} p={p:.4f}"
                )
            if not passed:
                print("        none. The best-looking wallet is what testing")
                print("        many wallets produces on noise alone.")
    return 0


# ---------------------------------------------------------------------------


def cmd_resolve(args) -> int:
    """Fill in final 0/1 outcomes and the hold-to-resolution return.

    Closed markets are cached forever; open ones are re-read on the next run.
    This is the only horizon where a copy-trade thesis has time to play out:
    the 60m exit is mostly noise.
    """
    if not Path(args.signals).exists():
        print(f"{args.signals}: no file yet")
        return 0
    signals = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    cache = load_resolution_cache(Path(args.cache))
    changed = 0
    for s in signals:
        cid = s.get("condition_id")
        if not cid:
            continue
        rec = fetch_resolution(cid, cache) or {}
        token = (rec.get("tokens") or {}).get(s.get("token_id") or "")
        payout = None
        if rec.get("closed") and token is not None:
            payout = 1.0 if token.get("winner") else 0.0
        ret = resolution_return(s.get("side"), entry_price(s), payout)
        if s.get("resolution") != payout or (s.get("returns") or {}).get("res") != ret:
            changed += 1
        s["resolution"] = payout
        s.setdefault("returns", {})["res"] = ret
    save_resolution_cache(cache, Path(args.cache))

    closed = [s for s in signals if s.get("resolution") is not None]
    open_rows = [s for s in signals if s.get("resolution") is None]
    scored = [s for s in closed if (s.get("returns") or {}).get("res") is not None]
    picked = strategy_rows(scored, args.max_adverse_move, True)
    vals = [s["returns"]["res"] for s in picked]
    print(
        f"{args.signals}: {len(signals)} signals, "
        f"{len(closed)} resolved, {len(open_rows)} open"
    )
    if args.write_back:
        Path(args.signals).write_text(
            json.dumps(signals, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  written back ({changed} changed)")
    if len(vals) < 2:
        print(
            f"  hold to resolution: {len(vals)} resolved markets, not enough to judge"
        )
        return 0
    st = _stats(vals)
    n, mean, se = st
    vals_sorted = sorted(vals)
    win = sum(1 for v in vals if v > 0) / n
    print(
        f"  hold to resolution, one per market, no-chase: n={n} "
        f"mean={fpct(mean)} median={fpct(vals_sorted[n // 2])} win={win:.1%} "
        f"SE={fpct(se)} t={mean / se:+.2f} P&L=${mean * n * args.stake:+,.2f}"
    )
    return 0


def cmd_select(args) -> int:
    """Rank wallets by their closed hold-to-resolution record and write a watchlist.

    Selection uses only outcomes that were already decided, so the weeks that
    follow are out of sample for the ranking. `--min-events` is the guard
    against a 2-3 trade fluke: on the month of history, 5+ events gave +33.8%
    per out-of-sample event (t=4.8), 8+ gave +25.9% (t=3.7).
    """
    files = [Path(p.strip()) for p in args.files.split(",") if p.strip()]
    cache = load_resolution_cache(Path(args.cache))
    rows = []
    for p in files:
        for s in json.loads(p.read_text(encoding="utf-8")):
            cid = s.get("condition_id")
            if not cid:
                continue
            rec = fetch_resolution(cid, cache) or {}
            token = (rec.get("tokens") or {}).get(s.get("token_id") or "")
            payout = None
            if rec.get("closed") and token is not None:
                payout = 1.0 if token.get("winner") else 0.0
            ret = resolution_return(s.get("side"), entry_price(s), payout)
            if ret is None:
                continue
            s = dict(s)
            s["_res"] = ret
            rows.append(s)
    save_resolution_cache(cache, Path(args.cache))

    first: dict[str, dict] = {}
    for s in sorted(rows, key=_sort_ts):
        key = s.get("condition_id") or s.get("token_id") or ""
        first.setdefault(key, s)
    ev = [s for s in first.values() if (s.get("moved") or 0) >= -args.max_adverse_move]
    per: dict[str, list[float]] = {}
    for s in ev:
        per.setdefault(s.get("wallet") or "", []).append(s["_res"])
    ranked = sorted(
        (
            (w, len(v), sum(v) / len(v))
            for w, v in per.items()
            if len(v) >= args.min_events
        ),
        key=lambda t: -t[2],
    )
    if not ranked:
        print(f"no wallet with {args.min_events}+ resolved markets")
        return 1
    k = max(1, int(len(ranked) * args.top))
    picked = [w for w, _, _ in ranked[:k]]
    Path(args.out).write_text(json.dumps(picked, indent=2), encoding="utf-8")
    print(
        f"{len(ranked)} wallets with {args.min_events}+ resolved markets, "
        f"top {k} -> {args.out}"
    )
    for w, n, m in ranked[: min(k, 12)]:
        print(f"  {w[:16]:18s} n={n:3d} mean={fpct(m)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pm_lab", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="sensor: snapshot leaderboard + stats")
    c.add_argument("--top", type=int, default=50)
    c.add_argument("--category", default="OVERALL")
    c.add_argument("--period", default="DAY", choices=["DAY", "WEEK", "MONTH", "ALL"])
    c.add_argument("--order-by", default="PNL", choices=["PNL", "VOL"])
    c.add_argument("--workers", type=int, default=6)
    c.add_argument("--out", default="snap_latest.json")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("rank", help="judge: honest scorecard for a snapshot")
    r.add_argument("snapshot")
    r.add_argument("--min-trades", type=int, default=MIN_TRADES)
    r.add_argument("--min-roi", type=float, default=MIN_ROI)
    r.add_argument("--max-mm-share", type=float, default=MAX_MM_SHARE)
    r.add_argument("--min-dir-share", type=float, default=MIN_DIR_SHARE)
    r.add_argument("--max-unrealized", type=float, default=MAX_UNREALIZED)
    r.add_argument("--watchlist")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_rank)

    w = sub.add_parser("wintrap", help="judge: measure the win-rate trap")
    w.add_argument("--top", type=int, default=20)
    w.add_argument("--source", default="trades", choices=["leaderboard", "trades"])
    w.add_argument(
        "--basis", default="settled", choices=["settled", "total", "realized"]
    )
    w.add_argument("--min-decided", type=int, default=50)
    w.add_argument("--category", default="OVERALL")
    w.add_argument("--period", default="MONTH", choices=["DAY", "WEEK", "MONTH", "ALL"])
    w.add_argument("--order-by", default="PNL", choices=["PNL", "VOL"])
    w.add_argument("--workers", type=int, default=6)
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_wintrap)

    s = sub.add_parser("signals", help="sensor: fresh entries + slippage vs book")
    s.add_argument("--watch", required=True)
    s.add_argument("--min-usdc", type=float, default=5000.0)
    s.add_argument("--min-price", type=float, default=0.10)
    s.add_argument("--max-price", type=float, default=0.90)
    s.add_argument(
        "--max-age",
        type=float,
        default=3600.0,
        help="ignore trades older than this many seconds",
    )
    s.add_argument(
        "--max-new",
        type=int,
        default=200,
        help="cap book lookups per run, newest first (0 = no cap)",
    )
    s.add_argument(
        "--live-age",
        type=float,
        default=600.0,
        help="only price the book for trades this fresh; older entries are n/a",
    )
    s.add_argument(
        "--require-book",
        action="store_true",
        help="drop signals without an executable book price (strategy ledger)",
    )
    s.add_argument("--state", default="signals_state.json")
    s.add_argument("--out")
    s.add_argument("--append", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_signals)

    b = sub.add_parser("backfill", help="judge: rebuild past signals, delayed entry")
    b.add_argument("--watch", required=True)
    b.add_argument("--hours", type=float, default=24.0, help="how far back to look")
    b.add_argument(
        "--min-age-hours",
        type=float,
        default=5.0,
        help="skip trades too recent to have outcome data",
    )
    b.add_argument("--min-usdc", type=float, default=1000.0)
    b.add_argument(
        "--lag",
        type=float,
        default=60.0,
        help="seconds between the whale's fill and your entry",
    )
    b.add_argument(
        "--max-per-wallet",
        type=int,
        default=30,
        help="cap candidates per wallet (one price lookup each)",
    )
    b.add_argument("--out", default="signals_backfill.json")
    b.add_argument("--horizons", default="30,60,240,1440")
    b.add_argument("--trades-per-wallet", type=int, default=1000)
    b.add_argument("--workers", type=int, default=10)
    b.set_defaults(func=cmd_backfill)
    en = sub.add_parser("entries", help="judge: per-entry win rate via FIFO")
    en.add_argument("--watch", required=True)
    en.add_argument("--hours", type=float, default=720.0)
    en.add_argument("--min-usdc", type=float, default=100.0)
    en.add_argument("--trades-per-wallet", type=int, default=3000)
    en.add_argument("--show", type=int, default=15)
    en.add_argument("--json", action="store_true")
    en.set_defaults(func=cmd_entries)

    ca = sub.add_parser("calib", help="judge: is the market price calibrated")
    ca.add_argument("--markets", type=int, default=800)
    ca.add_argument("--hours-before", type=float, default=6.0)
    ca.add_argument("--tol", type=int, default=1800)
    ca.add_argument("--verbose", action="store_true")
    ca.add_argument("--min-volume", type=float, default=0.0)
    ca.add_argument("--max-volume", type=float, default=0.0)
    ca.add_argument("--order", default="volumeNum")
    ca.add_argument("--ascending", action="store_true")
    ca.add_argument("--anchor", default="closed", choices=["closed", "end"])
    ca.add_argument("--split", action="store_true")
    ca.add_argument("--split-threshold", type=float, default=0.70)
    ca.add_argument(
        "--end-days-ago",
        type=float,
        default=0.0,
        help="only markets whose scheduled endDate is at least N days in the past",
    )
    ca.add_argument(
        "--max-distance",
        type=float,
        default=0.0,
        help="skip markets whose nearest price is further than this many seconds",
    )
    ca.add_argument("--json", action="store_true")
    ca.set_defaults(func=cmd_calib)

    mm = sub.add_parser("mm", help="judge: spread+rebate vs adverse move")
    mm.add_argument("--workers", type=int, default=8)
    mm.add_argument("--markets", type=int, default=120)
    mm.add_argument("--stats-snapshot", default="snap_big.json")
    mm.add_argument("--json", action="store_true")
    mm.set_defaults(func=cmd_mm)

    sm = sub.add_parser("sim", help="judge: simulate a market maker on real paths")
    sm.add_argument("--markets", type=int, default=300)
    sm.add_argument("--days", type=float, default=7.0)
    sm.add_argument("--min-age-days", type=float, default=8.0)
    sm.add_argument("--size", type=float, default=20.0)
    sm.add_argument("--max-inventory", type=float, default=200.0)
    sm.add_argument("--min-points", type=int, default=20)
    sm.add_argument("--max-gap", type=float, default=3600.0)
    sm.add_argument(
        "--flatten-hours",
        type=float,
        default=24.0,
        help="close inventory this long before resolution; 0 = hold to outcome",
    )
    sm.add_argument(
        "--sweep", type=float, nargs="+", default=[10.0, 25.0, 50.0, 100.0, 200.0]
    )
    sm.set_defaults(func=cmd_sim, half_spread_bps=50.0)

    cl = sub.add_parser("call", help="judge: score your own forecasts vs the market")
    cl.add_argument("action", choices=["scan", "record", "score"])
    cl.add_argument("--query", default="")
    cl.add_argument("--input")
    cl.add_argument("--file", default="calls.jsonl")
    cl.set_defaults(func=cmd_call)

    tr = sub.add_parser("trend", help="judge: momentum or reversion on price moves")
    tr.add_argument("--markets", type=int, default=600)
    tr.add_argument("--days", type=float, default=14.0)
    tr.add_argument("--min-age-days", type=float, default=8.0)
    tr.add_argument("--lookback", type=float, default=24.0, help="hours")
    tr.add_argument("--horizon", type=float, default=24.0, help="hours")
    tr.add_argument("--mode", default="momentum", choices=["momentum", "reversion"])
    tr.add_argument("--min-points", type=int, default=20)
    tr.add_argument("--min-price", type=float, default=0.0)
    tr.add_argument("--max-price", type=float, default=1.0)
    tr.add_argument(
        "--null",
        type=int,
        default=0,
        help="replications on directionless data, to calibrate the search itself",
    )
    tr.add_argument("--sweep", type=float, nargs="+", default=[0.02, 0.05, 0.10, 0.20])
    tr.set_defaults(func=cmd_trend)

    rw = sub.add_parser("rewards", help="judge: liquidity rewards minus fill cost")
    rw.add_argument("--scan", type=int, default=800)
    rw.add_argument("--top", type=int, default=60)
    rw.add_argument("--shares", type=float, default=200.0)
    rw.add_argument("--show", type=int, default=12)
    rw.set_defaults(func=cmd_rewards)

    pp = sub.add_parser("paper", help="forward paper trading, no re-fitting")
    pp.add_argument("action", choices=["record", "settle", "report"])
    pp.add_argument("--file", default="paper.jsonl")
    pp.add_argument("--threshold", type=float, default=0.70)
    pp.add_argument("--min-price", type=float, default=0.10)
    pp.add_argument("--max-price", type=float, default=0.90)
    pp.add_argument("--min-volume", type=float, default=1000.0)
    pp.add_argument("--max", type=int, default=200)
    pp.add_argument(
        "--max-days",
        type=float,
        default=14.0,
        help="only markets resolving within this many days; 0 = no limit",
    )
    pp.set_defaults(func=cmd_paper)

    ch = sub.add_parser("chart", help="plot the equity curve of settled bets")
    ch.add_argument("--file", default="paper.jsonl")
    ch.add_argument("--stake", type=float, default=20.0)
    ch.add_argument("--horizon", type=int, default=0)
    ch.add_argument("--png", default="equity.png")
    ch.add_argument(
        "--max-adverse-move",
        type=float,
        default=None,
        metavar="FRAC",
        help="skip entries already worse than the whale by more than FRAC (e.g. 0.02)",
    )
    ch.add_argument(
        "--one-per-market",
        action="store_true",
        help="count the earliest signal per market only",
    )
    ch.add_argument(
        "--max-lag",
        type=float,
        default=None,
        metavar="SECONDS",
        help="keep only signals detected within this many seconds of the whale",
    )
    ch.set_defaults(func=cmd_chart)

    db = sub.add_parser("dashboard", help="live HTML dashboard, auto-refreshing")
    db.add_argument("--dir", default=".")
    db.add_argument("--paper", default="paper.jsonl")
    db.add_argument("--signals", default="signals_hourly.json")
    db.add_argument("--stake", type=float, default=20.0)
    db.add_argument("--max-adverse-move", type=float, default=None, metavar="FRAC")
    db.add_argument("--one-per-market", action="store_true")
    db.add_argument("--max-lag", type=float, default=None, metavar="SECONDS")
    db.set_defaults(func=cmd_dashboard)

    bs = sub.add_parser("build-static", help="render dashboard for a static host")
    bs.add_argument("--dir", default=".")
    bs.add_argument("--stake", type=float, default=20.0)
    bs.set_defaults(func=cmd_build_static)

    e = sub.add_parser("evaluate", help="judge: did frozen signals pay")
    e.add_argument("signals")
    e.add_argument("--horizons", default="60,240,1440")
    e.add_argument("--by-wallet", action="store_true")
    e.add_argument("--holdout", action="store_true")
    e.add_argument("--fdr", action="store_true")
    e.add_argument("--write-back", action="store_true")
    e.add_argument("--q", type=float, default=0.10)
    e.add_argument(
        "--max-adverse-move",
        type=float,
        default=None,
        metavar="FRAC",
        help="in the executable view, skip entries already worse than the whale by more than FRAC",
    )
    e.add_argument(
        "--one-per-market",
        action="store_true",
        help="in the executable view, count the earliest signal per market only",
    )
    e.add_argument(
        "--max-lag",
        type=float,
        default=None,
        metavar="SECONDS",
        help="in the executable view, keep only signals detected within this many seconds",
    )
    e.add_argument(
        "--walk-forward",
        type=int,
        default=0,
        help="split signals into K consecutive time buckets",
    )
    e.add_argument("--min-n", type=int, default=20)
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_evaluate)

    rs = sub.add_parser(
        "resolve", help="fill 0/1 outcomes and hold-to-resolution returns"
    )
    rs.add_argument("signals")
    rs.add_argument("--write-back", action="store_true")
    rs.add_argument("--cache", default=str(RESOLUTION_CACHE))
    rs.add_argument("--max-adverse-move", type=float, default=0.02)
    rs.add_argument("--stake", type=float, default=20.0)
    rs.set_defaults(func=cmd_resolve)

    sel = sub.add_parser(
        "select", help="rank wallets by closed hold-to-resolution record"
    )
    sel.add_argument("--files", required=True, help="comma-separated signal files")
    sel.add_argument("--out", default="watch_selected.json")
    sel.add_argument("--min-events", type=int, default=8)
    sel.add_argument("--top", type=float, default=0.25)
    sel.add_argument("--max-adverse-move", type=float, default=0.02)
    sel.add_argument("--cache", default=str(RESOLUTION_CACHE))
    sel.set_defaults(func=cmd_select)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
