"""CLI for the longshot-NO paper strategy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backtest import run_backtest
from .ledger import (
    DEFAULT_LEDGER,
    DEFAULT_STAKE,
    add_positions,
    load_ledger,
    resolve,
    save_ledger,
    summarize,
)
from .project import project_table
from .scanner import scan


def cmd_scan(args) -> int:
    result = scan(
        days_min=args.days_min,
        days_max=args.days_max,
        min_price=args.min_price,
        max_price=args.max_price,
        min_volume=args.min_volume,
        max_spread=args.max_spread,
        max_pages=args.pages,
        strategy=args.strategy,
    )
    ledger = load_ledger(args.ledger)
    added = add_positions(
        ledger,
        result["candidates"],
        stake=args.stake,
        max_open=args.max_open,
        max_new=args.max_new,
        buffer=args.buffer,
    )
    save_ledger(ledger, args.ledger)
    print(
        f"markets={result['markets']} events={result['events']} "
        f"candidates={len(result['candidates'])} added={len(added)} "
        f"open={summarize(ledger)['open']}"
    )
    for c in result["candidates"][: args.top]:
        print(
            f"  YES={c['yes_mid']:.3f} NO={1 - c['yes_mid']:.3f} "
            f"sp={c['spread']:.3f} vol=${c['volume']:,.0f} "
            f"days={c['days_to_end']:.1f} | {c['question'][:80]}"
        )
    if added:
        print("new paper positions:")
        for p in added:
            print(
                f"  {p['entry_price']:.3f} x {p['shares']:.2f} = ${p['stake']:.2f} "
                f"| {p['question'][:80]}"
            )
    return 0


def cmd_resolve(args) -> int:
    ledger = load_ledger(args.ledger)
    resolved = resolve(ledger)
    save_ledger(ledger, args.ledger)
    print(f"resolved={len(resolved)}")
    for p in resolved:
        print(
            f"  {p['pnl']:+.2f} ({p['ret']:+.2%}) outcome_yes={p['outcome_yes']:.0f} "
            f"| {p['question'][:80]}"
        )
    print(json.dumps(summarize(ledger), indent=1))
    return 0


def cmd_report(args) -> int:
    ledger = load_ledger(args.ledger)
    summary = summarize(ledger)
    print(json.dumps(summary, indent=1))
    open_rows = [p for p in ledger["positions"] if p.get("status") == "open"]
    for p in open_rows[: args.top]:
        print(
            f"  {p['entry_price']:.3f} ${p['stake']:.2f} end={p.get('end_date', '')[:10]} "
            f"| {p['question'][:85]}"
        )
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"summary": summary, "positions": ledger["positions"]},
                indent=1,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return 0


def cmd_backtest(args) -> int:
    path = Path(args.data)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    result = run_backtest(path)
    print(f"rows={result['rows']}")
    print(f"{'bucket':>12} {'n':>5} {'price':>7} {'yes':>7} {'calib':>8} {'NOret':>9}")
    for row in result["calibration"]:
        print(
            f"{row['bucket']:>12} {row['n']:5d} {row['price']:7.3f} "
            f"{row['yes_rate']:7.3f} {row['calibration']:+8.3f} {row['no_return']:+9.2%}"
        )
    for key in ("portfolio_0.02_0.10", "portfolio_0.02_0.05", "portfolio_0.05_0.10"):
        p = result[key]
        if not p:
            continue
        print(f"\n{key}:")
        print(
            f"  n={p['n']} events={p['events']} losses={p['losses']} "
            f"expected={p['expected_losses']} binom_p={p['binomial_p']}"
        )
        print(
            f"  mean={p['mean_return']:+.2%} t={p['t']:+.2f} win={p['win_rate']:.1%} "
            f"boot95={p['bootstrap_95']} pnl_units={p['total_return_units']:+.2f} "
            f"max_dd={p['max_drawdown_units']:+.2f}"
        )
        print(f"  yearly={p['yearly']}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    return 0


def cmd_project(args) -> int:
    path = Path(args.data)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    from .backtest import load_history

    rows = load_history(path)
    stakes = [float(x) for x in args.stakes.split(",") if x.strip()]
    table = project_table(
        rows,
        stakes,
        buffer=args.buffer,
        stress_losses=args.stress,
        min_price=args.min_price,
        max_price=args.max_price,
        target=args.target,
    )
    print(
        f"{'stake':>8} {'events':>7} {'mean':>8} {'t':>6} "
        f"{'total P&L':>10} {'max open':>9} {'peak cap':>10} "
        f"{'ret/peak':>9} {'stress':>10}"
    )
    for p in table:
        stress_key = f"stress_{args.stress}_extra_losses"
        print(
            f"{p['stake']:8.0f} {p['n']:7d} {p['mean_return']:+8.2%} {p['t']:+6.2f} "
            f"{p['total_pnl']:+10.0f} {p['max_open'] or 0:9d} {p['peak_capital'] or 0:10.0f} "
            f"{p['return_on_peak_capital'] or 0:+9.2%} {p[stress_key]:+10.0f}"
        )
    print("\nby year:")
    for p in table:
        years = " ".join(
            f"{year}: n={v['n']} pnl=${v['pnl']:+.0f}"
            for year, v in p["by_year"].items()
        )
        print(f"  stake ${p['stake']:.0f}: {years}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(table, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    return 0


def main(argv=None) -> int:
    import sys

    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(prog="longshot")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "scan", help="find live longshot NO candidates and add paper positions"
    )
    s.add_argument("--ledger", default=DEFAULT_LEDGER)
    s.add_argument("--stake", type=float, default=DEFAULT_STAKE)
    s.add_argument("--days-min", type=int, default=3)
    s.add_argument("--days-max", type=int, default=8)
    s.add_argument("--min-price", type=float, default=0.02)
    s.add_argument("--max-price", type=float, default=0.10)
    s.add_argument("--min-volume", type=float, default=50_000.0)
    s.add_argument("--max-spread", type=float, default=0.03)
    s.add_argument("--buffer", type=float, default=0.005)
    s.add_argument("--max-open", type=int, default=20)
    s.add_argument("--max-new", type=int, default=5)
    s.add_argument("--pages", type=int, default=10)
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--strategy", default="core")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("resolve", help="resolve open paper positions")
    r.add_argument("--ledger", default=DEFAULT_LEDGER)
    r.set_defaults(func=cmd_resolve)

    rep = sub.add_parser("report", help="paper ledger summary")
    rep.add_argument("--ledger", default=DEFAULT_LEDGER)
    rep.add_argument("--out", default="")
    rep.add_argument("--top", type=int, default=20)
    rep.set_defaults(func=cmd_report)

    b = sub.add_parser("backtest", help="historical calibration and event portfolio")
    b.add_argument("--data", default="data/history.csv")
    b.add_argument("--out", default="")
    b.set_defaults(func=cmd_backtest)

    pr = sub.add_parser("project", help="P&L and capital at different stakes")
    pr.add_argument("--data", default="data/history.csv")
    pr.add_argument("--stakes", default="20,100,500,1000")
    pr.add_argument("--buffer", type=float, default=0.005)
    pr.add_argument("--min-price", type=float, default=0.02)
    pr.add_argument("--max-price", type=float, default=0.10)
    pr.add_argument("--target", type=float, default=0.05)
    pr.add_argument("--stress", type=int, default=3)
    pr.add_argument("--out", default="")
    pr.set_defaults(func=cmd_project)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
