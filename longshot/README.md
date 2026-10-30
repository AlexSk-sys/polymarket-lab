# Longshot NO

A paper strategy for Polymarket: seven days before resolution, buy **NO** on
markets whose YES price is **0.02–0.10**, one market per event, and hold to
resolution.

The thesis is the longshot bias: low-probability outcomes are priced above their
empirical frequency. Buying NO collects the overpricing, but the position is
held to 0/1, so the tail is real: if the longshot wins, the NO position loses
90–98% of its stake.

This is a research/paper system. It places no orders.

## Evidence

Processed snapshot of **1,763 closed Polymarket markets** (2024–2026), with the
YES price seven days before resolution, the final outcome, event id and volume.
The raw data came from Gamma closed-market metadata and Data-API historical
trades (`/v2/trades?condition_id=...`); the processed file is
`longshot/data/history.csv`.

Event-diversified portfolio, YES price 0.02–0.10, one market per event, entry
fee and a conservative 0.005 spread buffer:

| Metric | Value |
|---|---:|
| Events / positions | 192 |
| Losses (longshot won) | 2 |
| Expected losses at market prices | 8.53 |
| Binomial p-value (≤2 losses) | **0.008** |
| Mean net return per position | **+4.04%** |
| t-stat | **+5.12** |
| Win rate | 99.0% |
| Bootstrap 95% mean | [+2.16%, +5.30%] |
| Total return, 1 unit per event | +7.76 |
| Max drawdown, 1 unit per event | −1.00 |
| 2024 subset | +4.29% (144 positions, 1 loss) |
| 2025 subset | +3.30% (48 positions, 1 loss) |

Sub-buckets:

| YES bucket | n | Mean NO return | Binomial p |
|---|---:|---:|---:|
| 0.02–0.05 | 144 | +3.17% | 0.046 |
| 0.05–0.10 | 78 | +6.36% | 0.028 |

The 1-day-before-resolution horizon has **no edge** (118 events, 7 losses,
p=0.77). The mispricing is in the final week, not in the last hours. The live
scanner therefore enters only when the market is 3–8 days from resolution.

## Risks

- **Tail risk is the strategy.** A 99% win rate is not risk-free. One YES outcome
  turns a ~$20 NO position into roughly −$20; the historical 2 losses are a small
  sample. Size positions so a cluster of losses is survivable.
- **Correlated events.** Event-level deduplication reduces but does not remove
  correlation (e.g. several Senate/House races, several Iran markets). Cap the
  total open exposure.
- **Resolution risk.** Ambiguous rules, delayed resolution or UMA disputes can
  change the payoff. The paper ledger uses Gamma `outcomePrices`.
- **Sample.** 2024 dominates the history; 2025 has only 48 longshot events. The
  edge is positive in both, but the 2025 confidence interval includes zero.
- **Execution.** The backtest uses the last trade before the 7-day cutoff plus a
  0.005 buffer. A live taker may pay more; the buffer is deliberately
  conservative.

## Commands

```bash
python -m longshot.cli backtest
python -m longshot.cli scan --stake 20 --max-new 5 --max-open 20
python -m longshot.cli resolve
python -m longshot.cli report --out longshot_report.json
python -m unittest tests.test_longshot
```

`scan` writes `longshot_paper.json`; `resolve` updates it from closed Gamma
markets; `report` writes `longshot_report.json`.

## Live paper ledger

The first scan opened five positions, each $20, all 3–5 days from resolution:

| Entry (NO) | Market |
|---:|---|
| 0.958 | Will Ethereum dip to $2,000 October 26–November 1? |
| 0.960 | Will Todd Achilles win the Idaho Senate race in 2026? |
| 0.960 | Will there be a Hormuz agreement and the Democrats win the Senate? |
| 0.963 | Will the Democratic Party win the TX-36 House seat? |
| 0.972 | Will the Democratic Party win the KS-04 House seat? |

Open stake: $100. The daily task runs `resolve → scan → report`.
