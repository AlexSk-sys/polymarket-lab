"""EGC command line: snapshot and backtest."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .backtest import run_backtest
from .gamma import fetch_events, parse_event
from .model import event_model
from .signals import signals_for_model


def cmd_snapshot(args) -> int:
    raw = fetch_events(max_pages=args.pages)
    events = [parse_event(e) for e in raw]
    signals = []
    modelled = 0
    for e in events:
        if len(e["markets"]) < 2:
            continue
        model = event_model(e)
        if not model:
            continue
        modelled += 1
        signals.extend(signals_for_model(model, e["title"], min_edge=args.min_edge))
    signals.sort(key=lambda x: -x["edge"])
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "events": len(events),
        "modelled_events": modelled,
        "signals": signals[: args.top],
    }
    Path(args.out).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"events={len(events)} modelled={modelled} signals={len(signals)}")
    for s in signals[: args.top]:
        print(
            f"  {s['edge']:+.3f} {s['side']:3s} fair={s['fair']:.3f} mid={s['mid']:.3f} "
            f"entry={s['entry']:.3f} | {s['question'][:70]}"
        )
    return 0


def cmd_backtest(args) -> int:
    raw = fetch_events(max_pages=args.pages)
    events = [parse_event(e) for e in raw]
    events = [e for e in events if len(e["markets"]) >= 3]
    result = run_backtest(
        events,
        days=args.days,
        max_events=args.events,
        entry_edge=args.min_edge,
        exit_edge=args.exit_edge,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        max_bars=args.max_bars,
    )
    Path(args.out).write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    if not result.get("trades"):
        print(f"no trades ({result})")
        return 0
    print(
        f"events={result['events']} trades={result['trades']} "
        f"mean={result['mean']:+.2%} t={result['t']:+.2f} "
        f"pnl=${result['pnl']:+.2f} dd=${result['max_dd']:+.2f} "
        f"win={result['win']:.1%}"
    )
    return 0


def main(argv=None) -> int:
    import sys

    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(prog="egc")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="current cross-market coherence signals")
    s.add_argument("--pages", type=int, default=4)
    s.add_argument("--min-edge", type=float, default=0.02)
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--out", default="egc_signals.json")
    s.set_defaults(func=cmd_snapshot)

    b = sub.add_parser("backtest", help="historical relative-value backtest")
    b.add_argument("--pages", type=int, default=4)
    b.add_argument("--events", type=int, default=10)
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--min-edge", type=float, default=0.03)
    b.add_argument("--exit-edge", type=float, default=0.01)
    b.add_argument("--take-profit", type=float, default=0.15)
    b.add_argument("--stop-loss", type=float, default=0.08)
    b.add_argument("--max-bars", type=int, default=12)
    b.add_argument("--out", default="egc_backtest.json")
    b.set_defaults(func=cmd_backtest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
