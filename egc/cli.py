"""EGC command line: snapshot and backtest."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .backtest import run_backtest
from .cross import build_graph, cross_snapshot
from .cross_backtest import run_cross_backtest
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


def cmd_cross(args) -> int:
    raw = fetch_events(max_pages=args.pages)
    events = [parse_event(e) for e in raw]
    snap = cross_snapshot(events, min_edge=args.min_edge, top=args.top)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": snap["summary"],
        "signals": snap["signals"],
        "violations": snap["violations"],
    }
    Path(args.out).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    s = snap["summary"]
    print(
        f"events={len(events)} markets={s['markets']} constraints={s['constraints']} "
        f"links={s['links']} components={s['components']} "
        f"max_cross_raw={s['max_cross_raw_violation']:.3f} "
        f"max_internal_raw={s['max_internal_raw_violation']:.3f} "
        f"unsolved={s['unsolved_components']}"
    )
    print("top cross-event violations:")
    for v in snap["violations"][:10]:
        print(
            f"  raw={v['raw_violation']:.4f} solved={v['solved_violation']:.4f} "
            f"{v['label'][:100]}"
        )
    print(f"signals={len(snap['signals'])}")
    for sig in snap["signals"][: args.top]:
        print(
            f"  {sig['edge']:+.4f} {sig['side']:3s} fair={sig['fair']:.3f} "
            f"mid={sig['mid']:.3f} adj={sig['cross_adj']:+.4f} "
            f"| {sig['question'][:70]}"
        )
    return 0


def cmd_cross_backtest(args) -> int:
    raw = fetch_events(max_pages=args.pages)
    events = [parse_event(e) for e in raw]
    graph = build_graph(events)
    result = run_cross_backtest(
        graph,
        days=args.days,
        max_links=args.links,
        entry=args.entry,
        exit_gap=args.exit_edge,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        max_bars=args.max_bars,
        stake=args.stake,
        min_volume=args.min_volume,
        max_spread=args.max_spread,
        cooldown_bars=args.cooldown,
    )
    Path(args.out).write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print("link diagnostics (edge_room vs round-trip cost):")
    for d in sorted(result.get("diagnostics") or [], key=lambda x: -x["edge_room"])[:8]:
        print(
            f"  {d['edge_room']:.4f} vs {d['cost_fraction']:.4f} "
            f"hl={d['half_life_bars']} tradeable={d['tradeable']} "
            f"| {d['label'][:80]}"
        )
    if not result.get("trades"):
        print(
            f"no trades: links={result['links']} "
            f"tradeable={result['tradeable_links']} "
            f"links_with_trades={result['links_with_trades']}"
        )
        return 0
    print(
        f"links={result['links']} tradeable={result['tradeable_links']} "
        f"with_trades={result['links_with_trades']} "
        f"trades={result['trades']} mean={result['mean']:+.2%} "
        f"t={result['t']:+.2f} pnl=${result['pnl']:+.2f} "
        f"dd=${result['max_dd']:+.2f} win={result['win']:.1%} "
        f"avg_bars={result['avg_bars']:.1f}"
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

    c = sub.add_parser("cross", help="current cross-event coherence signals")
    c.add_argument("--pages", type=int, default=10)
    c.add_argument("--min-edge", type=float, default=0.01)
    c.add_argument("--top", type=int, default=20)
    c.add_argument("--out", default="egc_cross_signals.json")
    c.set_defaults(func=cmd_cross)

    cb = sub.add_parser("cross-backtest", help="historical cross-event backtest")
    cb.add_argument("--pages", type=int, default=10)
    cb.add_argument("--days", type=int, default=7)
    cb.add_argument("--links", type=int, default=20)
    cb.add_argument("--entry", type=float, default=0.02)
    cb.add_argument("--exit-edge", type=float, default=0.005)
    cb.add_argument("--take-profit", type=float, default=0.15)
    cb.add_argument("--stop-loss", type=float, default=0.12)
    cb.add_argument("--max-bars", type=int, default=24)
    cb.add_argument("--stake", type=float, default=20.0)
    cb.add_argument("--min-volume", type=float, default=20000.0)
    cb.add_argument("--max-spread", type=float, default=0.05)
    cb.add_argument("--cooldown", type=int, default=12)
    cb.add_argument("--out", default="egc_cross_backtest.json")
    cb.set_defaults(func=cmd_cross_backtest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
