# basev3-sim

Economics simulator for the **Alchemix V3 alUSD Base launch** — tests the "virtuous cycle" loop theory: loopers mint & sell alUSD → peg dips → duration-mismatched redemption arbitrage (instant buy, 28-day linear vest payout) repegs → repeat, with churn fees + Aerodrome emissions feeding LP depth.

## What's here

| File | What it is |
|---|---|
| `base_loop_sim.py` | Daily-timestep simulation, stdlib only. 8 scenarios (base, cold-start, yield-shock, panic-run, strategy-loss, high-arb, thin-LP) + DAO revenue Laffer sweep tables + analytical cross-checks. Run: `python3 base_loop_sim.py` |
| `VALIDATION_REPORT.md` | The verdict on the loop theory (~80% right: churn engine, not growth engine), scenario table, failure modes ranked, launch guidance, recalibration addendum, + v2.0 DAO fee-economics section |
| `lab/index.html` | Interactive **Loop Mechanics Lab** — a single-file Chart.js dashboard port of the simulator. ~40 dials, 8 presets, 10 live charts: peg / system size / liquidity / rates / deposit pulse / churn + **DAO revenue (perf fee vs protocolFee, trailing 365d)**, **perf-fee + protocol-fee Laffer sweeps** (full engine re-run per point, current-dial marker, DAO break-even line), **value-flow split** (where each yield dollar goes). Open it directly in a browser; no build step |

## Mechanics modeled (ground-truthed)

- 90% LTV, **no borrower interest** (debt is flat)
- 0.1% redemption fee paid by **debtors** (redeemer gets full 1:1)
- Transmuter: linear 28-day vest, 1:1 payout
- MYT yield with 15% performance fee
- 10%/day strategy deallocation cap

Three governors make the loop oscillate instead of running away: the **mint gate** (loopers stop tapping headroom below ~0.96 peg), **arb capital elasticity** (defender capital prices off T-bill parity and migrates on persistent discounts), and the **vest throttle** (28d drip + deallocation cap as the run circuit-breaker).

## Load-bearing identities

1. **Float neutrality** — redemption of r reopens ~0.99r of re-mint capacity; the internal loop churns supply but does not grow it
2. **T-bill parity** — p\* = 1 − (r_f + risk premium) × 28/365; the equilibrium discount IS the market's risk premium over Treasuries, amortized over the vest term
3. **~9x conversion** — the loop converts each incremental dollar of collateral (new equity or MYT yield) into ~9x mintable supply

## Verification

The Chart.js lab is a **bit-identical port** of the python simulator with `flow_noise=0` (all finals and daily checkpoints match exactly); with noise on it reproduces the python receipts within noise realization (mature peg 0.9939 vs 0.9940, launch dip 0.9805 vs 0.9803, panic wick 0.9000, churn 1.67 vs 1.7). The v2.0 fee-economics layer holds the same bar: DAO revenue streams, trailing-365 path, and both Laffer sweeps all match python to the last float digit (see VALIDATION_REPORT §6).

## DAO revenue accounting (v2.0)

Two independent streams, reported separately everywhere:
- **MYT perf fee** — accrues on yield: `C × myt_yield × perf_fee` (dominant at Base calibration: $2.77M/yr at 15% on a $534M 2-yr collateral base)
- **protocolFee on redemptions** — extracted from debtor collateral at vesting payout (`pay × protocol_fee`); $471k/yr at 10bp with ~1.7 churn cycles

Sweeps re-run the full engine per fee point (noiseless). Within this engine's demand model there is **no interior Laffer peak** up to 40% perf / 200bp protocol — looping capital is ~10x levered so even aggressive fees leave looper APR far above the 15% hurdle. Raise the `looper hurdle` dial to surface the peak.

## Model caveats

Daily timesteps; LP inventory loss as depth drift only; blended fee tier; single defender pool (sticky + arb); deterministic seeded noise. Strategy-loss scenarios intentionally surface insolvency when principal impairment breaks 1:1 backing (the python asserts; the lab shows a banner). This is a mechanism model, not a forecast.
