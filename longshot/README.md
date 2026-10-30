# Longshot NO

A research system for Polymarket longshot calibration. The original thesis:
seven days before resolution, buy **NO** on markets whose YES price is
**0.02–0.10**, one market per event, and hold to resolution.

**Status: broad scanning is paused.** A 211-event out-of-sample test on newly
fetched politics/crypto/geopolitics/economy tags did **not** confirm the edge
(mean −1.13%, 11 losses vs 10.1 expected, p=0.69). The 244-event core result
below is a historical, selection-biased sample, not a validated live edge. Only
existing paper positions are being resolved; no new broad positions are opened.

## Evidence

Processed snapshot of **2,780 closed Polymarket markets** (2024–2026) with the
YES price seven days before resolution, final outcome, event id, volume and end
date. Raw data: Gamma closed-market metadata + Data-API historical trades
(`/v2/trades?condition_id=...`). Processed file: `longshot/data/history.csv`.

Event-diversified portfolio, YES price 0.02–0.10, one market per event, entry
fee and a conservative 0.005 spread buffer:

| Metric | Value |
|---|---:|
| Events / positions | 244 |
| Losses (longshot won) | 2 |
| Expected losses at market prices | 11.21 |
| Binomial p-value (≤2 losses) | **0.00085** |
| Mean net return per position | **+3.37%** |
| t-stat | **+5.44** |
| Win rate | 99.2% |
| Bootstrap 95% mean | [+1.98%, +4.35%] |
| Total return, 1 unit per event | +8.23 |
| Max drawdown, 1 unit per event | −1.00 |
| 2024 subset | +3.22% (144 positions, 1 loss) |
| 2025 subset | +2.60% (56 positions, 1 loss) |
| 2026 subset | +4.86% (44 positions, 0 losses) |

Sub-buckets:

| YES bucket | n | Mean NO return | Binomial p |
|---|---:|---:|---:|
| 0.02–0.05 | 175 | +2.24% | 0.020 |
| 0.05–0.10 | 110 | +5.75% | 0.0036 |

The 1-day-before-resolution horizon has **no edge** (67 events, 2 losses,
p=0.38). The mispricing is in the final week, not the last hours. The live
scanner enters only when the market is 3–8 days from resolution.

## Out-of-sample failure

The 244-event core sample was built from a volume-ranked universe. A second,
independent sample was fetched from politics/crypto/geopolitics/economy tags:
**211 event-diversified longshots**, 11 losses vs 10.1 expected, mean **−1.13%**,
p=0.69. Reproduce with `python -m longshot.cli project --data data/oos_tags.csv`. Sports longshots were also negative (57 events, 8 losses vs 2.9
expected, −10.0%). The broad longshot edge does **not** survive this
out-of-sample test; the core result was selection and luck, not a durable
mispricing.

What remains, and is not core:

| Niche | n | Mean | Status |
|---|---:|---:|---|
| Crypto longshots | 88 | +2.61% | p=0.11, 2025 near zero |
| Mid 0.40–0.60 NO | 215 | +11.16% | p=0.038, but 2024 negative and t=1.64 |
| Sports longshots | 57 | −10.0% | rejected |

Broad scanning is paused. The next research direction is a structural edge
(order flow / market making / cross-venue), not another calibration bucket.

## Scaling (historical, not validated OOS)

`python -m longshot.cli project --stakes 20,100,500,1000` uses the historical
event dates to compute peak simultaneous capital. These numbers describe the
selection-biased core sample, not an expected live return:

| Stake | 3-year P&L | Max open | Peak capital | Return on peak | P&L after 3 extra losses |
|---:|---:|---:|---:|---:|---:|
| $20 | +$165 | 30 | $600 | +27.4% | +$105 |
| $100 | +$823 | 30 | $3,000 | +27.4% | +$523 |
| $500 | +$4,114 | 30 | $15,000 | +27.4% | +$2,614 |
| $1,000 | +$8,228 | 30 | $30,000 | +27.4% | +$5,228 |

The 27.4% is **total over 2024–2026**, not annualized; it is measured against
peak simultaneous capital, so idle cash drags the return down. The edge is per
position; absolute P&L scales linearly with stake and available capital.

## Experimental satellite

The 0.40–0.60 YES bucket also looks overpriced: buying NO gives +10.55% mean over
167 event-diversified positions (73 losses), but the binomial p is 0.073 and the
per-trade standard deviation is ~100%. It is **not** core; it runs in a separate
paper ledger with a $10 stake and a 10-position cap:

```
longshot_mid_paper.json
```

## Risks

- **Tail risk.** One YES outcome turns a ~$20 NO position into roughly −$20. The
  historical 2 losses are a small sample; size positions so a cluster of losses
  is survivable.
- **Correlation.** Event-level deduplication reduces but does not remove
  correlation (e.g. several Senate/House races, several Iran markets).
- **Resolution risk.** Ambiguous rules, delayed resolution or UMA disputes can
  change the payoff.
- **Capacity.** The edge is strongest in lower-volume markets; a large stake can
  move the price. The live scanner uses a 0.005 spread buffer and a $50k volume
  floor for the core sleeve.
- **Sample.** 2024 dominates the event count. 2025–2026 confirm the sign, but
  each year alone has wider confidence intervals.

## Commands

```bash
python -m longshot.cli backtest
python -m longshot.cli project --stakes 20,100,500,1000
python -m longshot.cli scan --stake 20 --max-new 5 --max-open 20
python -m longshot.cli resolve
python -m longshot.cli report --out longshot_report.json
python -m unittest tests.test_longshot
```

`scan` writes `longshot_paper.json`; `resolve` updates it from closed Gamma
markets; `report` writes `longshot_report.json`.

## Live paper ledger

The first core scan opened five positions, each $20, all 3–5 days from
resolution:

| Entry (NO) | Market |
|---:|---|
| 0.958 | Will Ethereum dip to $2,000 October 26–November 1? |
| 0.960 | Will Todd Achilles win the Idaho Senate race in 2026? |
| 0.960 | Will there be a Hormuz agreement and the Democrats win the Senate? |
| 0.963 | Will the Democratic Party win the TX-36 House seat? |
| 0.972 | Will the Democratic Party win the KS-04 House seat? |

Open stake: $100. The daily task `PolymarketLongshot` now runs **resolve and
report only** for the core and mid ledgers; new scanning is disabled until a
structurally validated edge replaces the broad longshot thesis.
