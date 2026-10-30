# EGC — Event Graph Coherence

A relative-value research system for Polymarket. It models markets as a graph of
logical relations, estimates fair probabilities under those constraints, and
trades only when the executable round-trip edge clears spread, both fees, and a
data-quality floor.

## Math

**Candidate / winner sets** are exclusive. Given mid prices `p_i`, solve

```
min_q  sum_i w_i * (logit q_i - logit p_i)^2
s.t.   sum_i q_i = 1,  0 < q_i < 1
```

A latent **Other** outcome absorbs probability with no traded market. Without
it, non-exhaustive events (e.g. a nominee field with no "Other" market) would
look like arbitrage.

**Date ladders** ("Will X happen by <date>?") are cumulative: probability must
be nondecreasing with the date. **Strike ladders** ("Will BTC dip to $Y?") are
monotone in the strike, with direction parsed from the question. Both use
weighted isotonic projection (PAVA), not a sum constraint.

## Cross-event graph

`egc/cross.py` adds links between separate events:

- **Nomination → presidency**: the same person and year satisfy
  `P(win presidency) <= P(win nomination)`.
- **Party aggregate**: the candidates of a party satisfy
  `sum(P(candidate wins)) <= P(party wins)`.
- **Balance of Power**: the four Senate/House combinations satisfy the separate
  Senate and House control marginals:
  `P(D Senate, D House) + P(D Senate, R House) = P(D Senate)`, and the same for
  the House.
- **Cross-event date/strike ladders**: separate events with the same base
  question and different dates/strikes obey the same monotonicity as a ladder
  inside one event.

All links are linear constraints. Each connected component is solved in logit
space with SLSQP (penalty fallback if SLSQP fails):

```
min_q  sum_i w_i * (logit q_i - logit p_i)^2
s.t.   A_eq q = b_eq,   A_le q <= b_le,   0 < q_i < 1
```

A signal is attributed to the **cross-event** adjustment `q_cross - q_within`,
not to the within-event projection. If the adjustment cannot pay the
round-trip spread and both fees, no signal is emitted.

## Data-quality and cost gates

A quote is only considered if:

- spread <= 3 cents and volume >= $20k;
- the raw ladder is monotone within 10 cents;
- a candidate set prices within 0.20 of one;
- the edge is **round-trip**: enter at ask/bid, exit at the opposite side, pay
  the spread and the fee on both legs. `q - ask - fee(ask)` overstates the edge
  by half a spread plus the exit fee.

The cross backtest also computes, per link:

- `gap_mean`, `gap_std`, AR(1) half-life;
- basket capital, round-trip cost fraction, available `edge_room`;
- `tradeable = edge_room > 1.5 * cost_fraction` and half-life <= 48 bars.

## Commands

```bash
python -m egc.cli snapshot --pages 4 --top 20
python -m egc.cli backtest --pages 4 --events 8 --days 7 --min-edge 0.03
python -m egc.cli cross --pages 10 --top 20
python -m egc.cli cross-backtest --pages 10 --links 40 --days 7 --entry 0.02
python -m unittest tests.test_egc tests.test_cross
```

`snapshot` writes `egc_signals.json`; `backtest` writes `egc_backtest.json`;
`cross` writes `egc_cross_signals.json`; `cross-backtest` writes
`egc_cross_backtest.json` with per-link diagnostics and per-trade detail.

## Current result

**Within-event EGC:** 400 events -> 50 modelled -> **0 signals** after the
quality gates; the historical backtest produced **0 trades**. The earlier
126-signal snapshot was an artefact of wide/stale books.

**Cross-event EGC:** 1000 events -> 2784 markets, 775 constraints, 35 links,
1872 components. The largest raw cross-event violations on 2026-10-30 were:

| Link | Raw gap | Solved | Why it is not traded |
|---|---:|---:|---|
| Republican candidates <= Republican party win 2028 | +0.024 | 0.000 | round-trip edge ~0.001 vs cost ~0.004; half-life 42 bars |
| Balance of Power Senate D 2026 | +0.0065 | 0.000 | edge room 0.0116 vs cost 0.0192 |
| Balance of Power House D 2026 | +0.0050 | 0.000 | edge room 0.0113 vs cost 0.0240 |
| Massie presidency <= nomination | +0.0015 | 0.000 | below fees |

After the round-trip correction the live cross snapshot produces **0 signals**.
The 7-day cross backtest (33 links, $20 stake, 24-bar time exit, 12-bar cooldown)
produced **9 trades, all losing**, mean **-0.75%**, t = **-2.66**, P&L **-$1.35**;
**0 links were tradeable** under the cost gate. The R-party gap is a persistent
structural premium (mean +2.7 cents, half-life 43 bars), not a transient
dislocation. The BoP gap mean-reverts quickly (half-life ~5 bars) but is smaller
than the 2-cent round-trip cost floor.

The important negative result: on Polymarket, shorting a low-probability outcome
means buying NO at almost $1, so a multi-leg convergence basket ties up a lot of
capital for a few cents of edge. Logical coherence alone does not pay for that
capital and those spreads.

## Next research steps

- CLOB order-book depth and synchronized quotes instead of Gamma best bid/ask.
- A Bayesian version with uncertainty per `q_i`, so the signal requires the
  posterior edge to clear costs.
- Cointegration/state-space models for **statistical** cross-event spreads,
  instead of treating every logical link as a convergence-to-zero trade.
- Longer history. The public `prices-history` endpoint currently returns only
  the last 7 days for these markets, which is too short to estimate a stable
  half-life for slow political links.
