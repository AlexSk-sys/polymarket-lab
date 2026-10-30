# EGC — Event Graph Coherence

A relative-value research system for Polymarket. It models an event as a graph
of related markets, estimates fair probabilities under logical constraints, and
trades only when the executable price deviates from fair value by more than
spread, fees, and the data-quality floor.

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

## Data-quality gates

A quote is only tradeable if:

- spread <= 3 cents and volume >= $20k;
- the raw ladder is monotone within 10 cents. A 35% market at 0.295 next to a
  36% market at 0.74 is a stale/wide-book artefact, not an edge;
- a candidate set prices within 0.20 of one.

Without these gates the first snapshot produced 126 "signals"; with them it
produces zero. That is the intended behaviour: no invented arbitrage.

## Commands

```bash
python -m egc.cli snapshot --pages 4 --top 20
python -m egc.cli backtest --pages 4 --events 8 --days 7 --min-edge 0.03
python -m unittest tests.test_egc
```

`snapshot` writes `egc_signals.json`; `backtest` writes `egc_backtest.json` with
mean, t-stat, P&L, drawdown, and per-trade detail. The backtest enters at the
executable side, exits when the edge closes, or on take-profit / stop / time.

## Current result

400 events -> 50 modelled -> **0 signals** after the quality gates; the historical
backtest on the modelled sample produced **0 trades**. The system is a research
platform, not a profitable strategy: the public books at this granularity do not
show a coherent, liquid gap that survives costs.

## Next research steps

- Cross-event edges (nested implications, party/candidate, state/national), not
  only within-event constraints.
- CLOB order-book depth and synchronized quotes instead of Gamma best bid/ask.
- A Bayesian version with uncertainty per `q_i`, so the signal requires the
  posterior edge to clear costs.
