#!/usr/bin/env python3
"""
Base V3 Launch — Loop Theory Validation Simulation
====================================================
Tests scoopy's "virtuous cycle" theory for the Alchemix Base launch:
  loopers mint & sell alUSD -> peg dips -> duration-mismatched redemption
  arbitrage (instant buy, 28d linear vest payout) repegs -> repeat, with
  churn fees + Aerodrome emissions feeding LP depth.

KEY MECHANICS (ground truth, repo/spec-verified 2026-09-01):
  - AlchemistV3 has NO borrower interest. Debt is flat. 90% LTV.
  - Redemption fee (protocolFee=10, 0.1%) is paid by DEBTORS (collateral
    extracted), redeemer gets full 1:1 USDC of MYT value.
  - Transmuter: alUSD deposited vests LINEARLY over 28 days; payout is 1:1.
  - MYT vault: 15% performance fee on yield. Yield anchor = Aave V3 Base
    USDC ~3.7% APY (DefiLlama, 2026-09-01).
  - Allocator deallocation cap modeled as 10%/day of collateral.

THREE GOVERNORS that make the loop oscillate instead of run away:
  G1 MINT GATE   - loopers stop tapping headroom when peg < ~0.96 (selling
                   face value at a deep discount is bad business).
  G2 ARB ELASTIC - redemption-arb capital floods in when discount APR is
                   juicy and drips out when it dries up (absorption engine).
  G3 VEST THROTTLE - payouts are limited by the 28d linear vest and the
                   10%/day strategy deallocation cap (the run circuit-breaker).

ANALYTICAL IDENTITIES (cross-checked at end of main()):
  1. FLOAT NEUTRALITY: redemption of r reopens headroom ~= 0.0991r; geometric
     re-mint capacity off that headroom ~= 0.99r. The internal loop CHURNS
     supply but does not GROW it. Growth = new equity (9x) + yield conversion
     (yield creates headroom, the loop leverages it ~9.6x).
  2. YIELD CONVERSION: M = 9*E*y_net / (1 - 0.9p) with E=C/10 -> at 3.15% net
     yield and p=0.996: ~29% of collateral per year in mintable supply.
  3. PEG FLOOR: p* = 1 - arb_hurdle * 28/365 (duration cost of locked capital).

Actor ledgers track WHERE value flows: loopers (equity), arbs (discount),
LPs (fees+emissions), protocol (debtor fee + perf fee). Mint attribution
splits every alUSD minted into replacement / yield-conversion / equity-
conversion so the "is it really expanding?" question is answered by identity.

All tunable parameters live in Config. Daily timestep, stdlib only.
Run:  python3 base_loop_sim.py
"""

import random
from dataclasses import dataclass, replace as dc_replace

# ---------------------------------------------------------------- config ---

@dataclass
class Config:
    # --- horizon ---
    days: int = 730                  # simulate 2 years
    seed: int = 7                    # RNG seed

    # --- protocol constants (Base deployment, spec-verified) ---
    ltv: float = 0.90                # max mintable = 90% of collateral
    redemption_days: int = 28        # timeToTransmute, linear vesting
    protocol_fee: float = 0.001      # 0.1% debtor-side redemption fee
    perf_fee: float = 0.15           # MYT performance fee on yield
    myt_yield: float = 0.05          # curated Base MYT target (Morpho menu
                                     # 4.3-5.9%: scoopy's "projected 5%")
    dealloc_cap_daily: float = 0.10  # max 10%/day of collateral pulled
    dao_fixed_cost: float = 300_000  # DAO overhead charged against Base
                                     # revenue (audits, dev, ops) — BE-style
                                     # fixed cost, for net-revenue reads

    # --- starting state ---
    initial_equity: float = 2_000_000    # day-0 USDC deposited by loopers
    initial_depth: float = 1_000_000     # seed LP depth ($) alUSD/USDC pools
    initial_arb: float = 1_500_000       # peg-defense capital ready at launch
    initial_peg: float = 0.999           # launch near peg
    seed_liquidity_team: float = 500_000 # team USDC seeded INTO transmuter

    # --- peg formation (flow-impact model) ---
    impact_k: float = 0.035          # $500k net sell on $4M depth -> ~0.44%
                                     # dip (scoopy calibration: stable pool +
                                     # tight CL range is flat near peg)
    sticky_frac: float = 0.10        # fraction of defender buys HELD, never
                                     # redeemed (real end-demand for alUSD --
                                     # this is the net-expansion channel)
    peg_max: float = 1.005           # mint-and-sell caps the premium
    peg_floor_hard: float = 0.90     # sanity clamp

    # --- looper behavior (G1: the mint gate) ---
    headroom_tap_daily: float = 0.35 # aggressive looping at ~50% equity APR
    gate_low: float = 0.95           # peg below this: no looping at all
    gate_full: float = 0.99          # peg at/above this: full tap rate
    deposit_base_daily: float = 60_000   # organic deposit inflow $/day
    deposit_sensitivity: float = 4_000_000  # extra $/yr per unit APR gap
    looper_hurdle: float = 0.15      # equity APR needed to attract capital
    exit_sensitivity: float = 30_000_000  # outflow $/yr per unit APR shortfall

    # --- arbitrageur behavior (G2: absorption engine) ---
    # peg defense priced off T-bills + premium. Scoopy's band: arb sits at
    # 0.993-0.994 (7-8% fixed yield) = r_f 5% + ~250-300bp premium:
    # p_bid = 1 - 0.0775*28/365 = 0.9941. Premium compresses toward 0.9962
    # (pure T-bill parity) as track record builds.
    arb_hurdle: float = 0.05         # act at/above T-bill parity
    arb_deploy_daily: float = 0.30   # near-immediate response (scoopy: the
                                     # arb back is immediate at 13%+ yields)
    arb_capture: float = 0.60        # fraction of visible discount bought
    arb_inflow_apr: float = 0.065    # T-bill + spread: migration threshold
    arb_inflow_daily: float = 200_000  # $/day of fresh arb capital when juicy
    arb_exit_apr: float = 0.05       # below T-bill parity capital leaves
    arb_exit_daily: float = 50_000   # $/day withdrawal when unattractive

    # --- LP / Aerodrome ---
    fee_tier: float = 0.0003         # blended 3bp on churn volume
    emissions_per_day: float = 3000  # $/day emissions directed to our pools
    lp_alt_apr: float = 0.06         # LP opportunity cost on Base stables
    lp_elasticity: float = 0.08      # depth fraction/day attracted per APR gap
    lp_decay: float = 0.30           # yearly depth fraction leaving when
                                     # APR < alternative (passive churn-out)
    lp_track: float = 0.015          # depth relaxation/day toward the
                                     # volume-implied target (depth = income
                                     # / lp hurdle — BE's depth = LP income/r)
    emissions_depth_weight: float = 0.35  # fraction of emission-supported
                                     # depth that is real (mercenary capital
                                     # unbuttons; fees are the sticky base)

    # --- standing float capture (the churn engine) ---
    float_capture_apr: float = 0.055  # arb APR needed to buy float at the
                                      # peg and queue redemption
    float_capture_rate: float = 0.10  # frac of float/day arbs try to absorb
    equity_exit_frac: float = 0.05    # frac of freed looper equity withdrawn
                                      # at redemption maturity (profit-take)

    # --- liquidity sinks (what keeps churn BELOW the 13.03 structural cap) ---
    lp_alusdb_share: float = 0.50     # alUSDb side of LP depth: every $ of
                                      # depth growth parks this much alUSDb
                                      # out of the float (pools are a SINK)
    lending_sink_rate: float = 0.001  # frac of float/day absorbed into
                                      # lending markets (Euler-style; grows
                                      # with lindy — dial it)
    emissions_cost_dao: bool = False  # veAERO-directed (not a cash cost);
                                      # ~$2M veAERO votes two pools (stable +
                                      # concentrated, 80/20 weight to stable)

    # --- scenario switches (all default off) ---
    cold_start: bool = False         # thin arb capital + thin absorption
    yield_shock_day: int = -1        # day myt_yield drops to shock_yield
    shock_yield: float = 0.005
    yield_recovery_day: int = -1     # day yield returns to normal
    run_day: int = -1                # day of a panic dump (fraction of float)
    run_frac: float = 0.20
    strategy_loss_day: int = -1      # MYT principal impairment (the real tail)
    strategy_loss_frac: float = 0.15 # 15% of collateral vaporizes instantly

    # --- MC noise ---
    flow_noise: float = 0.30         # +/- fractional noise on deposit/arb flows
    risk_premium: float = 0.0        # new-deployment premium over T-bills;
                                     # shifts the passive bid: 0bp=0.9962,
                                     # +300bp=0.9939, +800bp=0.9915

# ------------------------------------------------------------- simulation --

def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def run(cfg: Config, collect_path=True) -> dict:
    """Simulate `days` days. Returns final state, actor ledgers, in-run mint
    attribution (replacement / yield / equity), and weekly path."""
    rng = random.Random(cfg.seed)
    c = cfg

    # --- starting state ---
    C = c.initial_equity + c.seed_liquidity_team   # collateral in MYT (USDC)
    D = 0.0                                        # debt: alUSD minted (net)
    F = 0.0                                        # float: alUSD in market
    vesting = []                                   # [[deposit_day, amount], ...]
    p = c.initial_peg                              # market peg
    depth = c.initial_depth                        # LP depth $
    arb = c.initial_arb                            # idle arb capital $
    backlog = 0.0                                  # vesting owed but unpaid
    pending_replacement = 0.0                      # redeemed supply to re-mint
    pending_yield = 0.0                            # yield mint capacity to tap
    attr = {"replacement": 0.0, "yield": 0.0, "equity": 0.0}
    avg_D_sum = 0.0                                # for churn-cycle metric
    ema_disc = 0.0                                 # 30d EMA of discount
    p_min_daily = c.initial_peg                    # true daily peg minimum
    p_max_daily = c.initial_peg

    if c.cold_start:                               # launch with no defenders
        arb = 300_000
        depth = 600_000
        c = dc_replace(c, sticky_frac=0.05, deposit_base_daily=30_000,
                       risk_premium=0.03)         # unproven = wider discount

    led = dict(new_equity=0.0, arb_profit=0.0, lp_fees=0.0, lp_emissions=0.0,
               protocol_fee=0.0, perf_fee=0.0, debtor_fee_paid=0.0,
               redeem_vol=0.0, mint_vol=0.0, gross_yield=0.0,
               equity_exit=0.0, float_buys=0.0, pool_volume=0.0,
               lp_absorbed=0.0, lending_parked=0.0)
    path = []
    myt = c.myt_yield
    # daily DAO revenue streams (for trailing-annual series)
    rev_perf_daily = []
    rev_prot_daily = []

    for day in range(c.days):
        avg_D_sum += D
        # ----- scenario schedule -----
        if 0 <= c.yield_shock_day == day:
            myt = c.shock_yield
        if 0 <= c.yield_recovery_day == day:
            myt = c.myt_yield

        # 1) YIELD: collateral compounds net of 15% perf fee (debt has no
        #    interest — repo-verified). Yield creates mint capacity: the new
        #    collateral raises the 90% LTV ceiling, and the loop leverages
        #    that headroom ~1/(1-0.9p) ~= 9.6x when tapped.
        y = C * myt / 365
        y_net = y * (1 - c.perf_fee)
        C += y_net
        led['perf_fee'] += y * c.perf_fee
        led['gross_yield'] += y
        rev_perf_daily.append(y * c.perf_fee)
        rev_prot_daily.append(0.0)
        pending_yield += y_net * 0.9 / (1 - c.ltv * c.initial_peg)

        # 2) NEW EXTERNAL EQUITY: loopers chase levered carry
        lev = min(10.0, C / max(c.initial_equity, 1))
        looper_apr = lev * myt * (1 - c.perf_fee)
        gap = looper_apr - c.looper_hurdle
        noise = 1 + rng.uniform(-c.flow_noise, c.flow_noise)
        inflow = max(0.0, (c.deposit_base_daily +
                           c.deposit_sensitivity * max(0.0, gap) / 365)) * noise
        outflow = c.exit_sensitivity * max(0.0, -gap) / 365
        net_dep = inflow - outflow
        if net_dep < 0:
            # exits: buy back alUSD from float at market, repay, withdraw
            out = min(-net_dep, C * 0.02)
            repay = min(out / p, D)
            C -= repay * p
            D -= repay
            F = max(0.0, F - repay)
        else:
            C += net_dep
            led['new_equity'] += net_dep

        # 3) MINT / LOOP with the G1 mint gate: price-elastic looping.
        #    Carry-aware: a looper will eat a discount up to ~2 weeks of
        #    levered carry (fat APR -> less peg-sensitive, as in reality).
        H = max(0.0, c.ltv * C - D)
        tol = clamp(looper_apr / 365 * 14, 0.0, 0.05)
        gate = clamp((p + tol - c.gate_low) /
                     (c.gate_full - c.gate_low), 0.0, 1.0) ** 1.5
        m = H * c.headroom_tap_daily * gate
        # Passive-bid market: peg defenders quote at their T-bill indifference
        # price p_bid = 1 - (r_f + risk premium) * 28/365 ~= 0.996. Defender
        # capital IS near-peg liquidity (their quotes add effective depth up
        # to 2x the pool), so absorption capacity grows as capital arrives --
        # this is what compresses the premium back to T-bill parity. Bids
        # absorb mint flow up to daily capacity; only EXCESS moves price.
        p_bid = 1 - (c.arb_hurdle + c.risk_premium) * c.redemption_days / 365
        eq_disc = 1 - p_bid
        eff_depth = depth + min(arb * 0.5, depth * 2.0)
        # Discount-scaled absorption: when the peg is BELOW the bid, the
        # visible discount (not the equilibrium one) sizes the book defenders
        # will cross. At 1.5% discount the daily absorption capacity is ~4x
        # the parity-priced book — extra loop-flow goes into the FLOAT, not
        # the PRICE. This is what keeps d(peg)/d(yield) shallow.
        act_disc = max(eq_disc, 1.0 - p)
        bid_cap = min(arb * c.arb_deploy_daily,
                      act_disc * eff_depth / c.impact_k * 1.5)
        # Market clearing with resting bids:
        #  1. if market < bid, incoming mint flow clears INTO the bid book,
        #     gapping price back up toward p_bid, capacity-bound
        #  2. at-bid bids absorb additional flow at p_bid, no impact
        #  3. only flow beyond total bid capacity crosses the book (impact)
        taken = 0.0
        excess = 0.0
        if m > 0:
            lift_cap = max(0.0, (p_bid - p)) / p * eff_depth / c.impact_k
            from_book = min(lift_cap, bid_cap, m)
            taken += from_book
            p = min(p_bid, p + from_book / eff_depth * c.impact_k * p)
            remaining = m - from_book
            if p >= p_bid - 1e-9:
                at_bid = min(remaining, bid_cap - from_book)
                taken += at_bid
                remaining -= at_bid
            excess = remaining
        if m > 0:
            if excess > 0:
                p = clamp(p - excess / eff_depth * c.impact_k * p,
                          c.peg_floor_hard, c.peg_max)
            D += m
            F += m - taken
            led['mint_vol'] += m
            C += m * p                        # sale proceeds redeposit
            # defender bids spend REAL capital and recycle through the 28d
            # vest (sticky_frac held forever = end-demand channel)
            if taken > 0:
                vesting.append([day, taken * (1 - c.sticky_frac)])
                arb -= taken
                led['arb_profit'] += taken * (1 - p)
            # attribution: replacement -> yield capacity -> equity residual
            take_r = min(m, pending_replacement); pending_replacement -= take_r
            take_y = min(m - take_r, pending_yield); pending_yield -= take_y
            attr["replacement"] += take_r
            attr["yield"] += take_y
            attr["equity"] += m - take_r - take_y

        # 4b) FLOAT SWEEP: defenders absorb EXISTING discounted float below
        #     their bid (capital-bound/day) -- independent of new issuance.
        #     This is the panic-recovery channel: bids eat the dump over days.
        bid_left = max(0.0, bid_cap - taken)
        if p < p_bid - 1e-9 and F > 0 and arb > 0 and bid_left > 0:
            sweep = min(bid_left, F, arb)
            if sweep > 0:
                F -= sweep
                vesting.append([day, sweep * (1 - c.sticky_frac)])
                arb -= sweep
                p = min(p_bid, p + sweep / eff_depth * c.impact_k * p)
                led['arb_profit'] += sweep * (1 - p)

        # 4) ARBITRAGE (G2): sustained discounts draw NEW defender capital.
        #    (Intraday dip-buying is handled by the resting-bid clearing above;
        #    this is the slow migration of T-bill capital into the trade.)
        discount = 1.0 - p
        arb_apr = discount * 365 / c.redemption_days

        # 4c) STANDING FLOAT CAPTURE — the churn engine. With a persistent
        #     discount, arb desks continuously buy float and queue it for the
        #     28d vest. Capital-bound: each buy locks capital until payout,
        #     so churn is a function of defender capital supply.
        buy_f = 0.0
        if arb_apr >= c.float_capture_apr and F > 0 and arb > 0:
            want = F * c.float_capture_rate
            room = arb * c.arb_deploy_daily * 0.5
            buy_f = min(want, room, F)
            if buy_f > 0:
                arb -= buy_f * p                       # capital locked in vest
                F -= buy_f
                vesting.append([day, buy_f])
                p = min(p_bid, p + buy_f / eff_depth * c.impact_k * p)
                led['arb_profit'] += buy_f * (1 - p)
                led['float_buys'] += buy_f

        # arb capital elasticity: T-bill capital migrates on PERSISTENT
        # under-capacity, scaling with the APR SPREAD over the migration
        # threshold (a 2x APR = 4x daily migration: deep discounts recruit
        # fixed-income capital fast — at 13% vs 6.5% threshold the flood
        # is 4x, at 26% it is 16x). This keeps d(peg)/d(yield) shallow:
        # extra loop-flow settles into the FLOAT at a mildly wider
        # discount, not into a collapsed price.
        ema_disc = ema_disc + 0.10 * (discount - ema_disc)
        if ema_disc > eq_disc * 1.1 and arb_apr >= c.arb_inflow_apr:
            mult = min(50.0, (arb_apr / c.arb_inflow_apr) ** 2)
            arb += c.arb_inflow_daily * mult * noise
        elif arb_apr < c.arb_exit_apr and arb > 200_000:
            arb -= min(arb * 0.01, c.arb_exit_daily)

        # 5) VESTING PAYOUT (G3): linear drip over 28d, capped by deallocation
        owed = sum(a / c.redemption_days for _, a in vesting)
        wanted = owed + backlog * 0.1
        cap = C * c.dealloc_cap_daily
        pay = min(wanted, cap, C * 0.5)
        if pay > 0:
            C -= pay * (1 + c.protocol_fee)       # fee extracted from debtors
            D -= pay                              # burned liability
            # redemption maturity frees looper equity: a fraction is
            # withdrawn (profit-take) instead of silently re-looped —
            # this is the shrink half of the deposit pulse
            eq_x = min(pay * c.equity_exit_frac, C * 0.02)
            C -= eq_x
            led['equity_exit'] += eq_x
            led['protocol_fee'] += pay * c.protocol_fee
            led['debtor_fee_paid'] += pay * c.protocol_fee
            rev_prot_daily[-1] += pay * c.protocol_fee
            led['redeem_vol'] += pay
            pending_replacement += pay            # re-mint capacity reopens
            arb += pay                            # recycled to arb desks
            vesting = [[d0, a * (1 - 1 / c.redemption_days)]
                       for d0, a in vesting
                       if a * (1 - 1 / c.redemption_days) > 1]
        backlog = max(0.0, wanted - pay)
        if backlog > 0:                           # dry transmuter: confidence hit
            p = clamp(p - min(0.002, backlog / max(F, 1) * 0.01),
                      c.peg_floor_hard, c.peg_max)

        # 6) LP ECONOMICS: depth is a function of VOLUME. Pool fees accrue on
        #    every swap leg (mint sells + standing float buys + exit re-buys),
        #    and depth relaxes toward the income-implied target
        #    depth* = (fees + w*emissions) / lp hurdle  — BE's depth = income/r.
        #    The pools are also a LIQUIDITY SINK: depth growth parks alUSDb
        #    inventory (lp_alusdb_share per $ of depth) out of the float —
        #    alUSDb in the pool is alUSDb not in the transmuter.
        exit_buy = repay if net_dep < 0 else 0.0   # alUSD bought to exit
        vol = m + buy_f + exit_buy
        fees = vol * c.fee_tier
        income_ann = (fees + c.emissions_per_day) * 365
        target_depth = ((fees * 365) + c.emissions_per_day * 365
                        * c.emissions_depth_weight) / c.lp_alt_apr
        lp_apr = income_ann / max(depth, 1)
        led['lp_fees'] += fees
        led['lp_emissions'] += c.emissions_per_day
        led['pool_volume'] += vol
        depth_prev = depth
        depth += (target_depth - depth) * c.lp_track
        depth += depth * c.lp_elasticity * (lp_apr - c.lp_alt_apr) / 365
        if lp_apr < c.lp_alt_apr:
            depth -= depth * c.lp_decay / 365
        depth = max(depth, 100_000)
        # sink/release: alUSDb side of pool inventory moves with depth
        d_depth = depth - depth_prev
        if d_depth > 0:
            absorb = min(d_depth * c.lp_alusdb_share, F)
            F -= absorb
            led['lp_absorbed'] += absorb
        elif d_depth < 0:
            release = min(-d_depth * c.lp_alusdb_share,
                          max(0.0, led['lp_absorbed']))  # can't release more
            F += release                 # than was ever parked
            led['lp_absorbed'] -= release

        # 6b) LENDING SINK: alUSDb supplied to lending markets (Euler-style)
        #     — the lindy sink. Slow drain, sticky.
        if c.lending_sink_rate > 0 and F > 0:
            parked = F * c.lending_sink_rate
            F -= parked
            led['lending_parked'] += parked

        # 7) PANIC RUN (stress): float dumps; half of it also queues redemption
        if 0 <= c.run_day == day:
            dump = F * c.run_frac
            p = clamp(p - dump / depth * c.impact_k * p,
                      c.peg_floor_hard, c.peg_max)
            F -= dump * 0.5
            vesting.append([day, dump * 0.5])     # bought at crashed peg = arb

        # 7b) STRATEGY LOSS (the real tail risk): MYT principal impairment.
        # Yield shocks don't kill the loop (10x leverage keeps carry above
        # T-bills), but PRINCIPAL loss breaks the 1:1 backing. Redemptions
        # keep burning D against impaired C; if losses exceed ~1/9 of C at
        # full loop, float outruns backing and the transmuter dries up.
        if 0 <= c.strategy_loss_day == day:
            C *= (1 - c.strategy_loss_frac)

        if collect_path and day % 7 == 0:
            path.append(dict(day=day, peg=round(p, 4), C=round(C / 1e6, 2),
                             D=round(D / 1e6, 2), F=round(F / 1e6, 2),
                             arb=round(arb / 1e6, 2), depth=round(depth / 1e6, 2),
                             lp_apr=round(lp_apr * 100, 1),
                             arb_apr=round(arb_apr * 100, 1),
                             looper_apr=round(looper_apr * 100, 1),
                             dep=round(net_dep / 1e3, 1),
                             red=round(pay / 1e3, 1),
                             buyf=round(buy_f / 1e3, 1)))

        # solvency invariants (hard-fail on bug, never in a healthy run)
        p_min_daily = min(p_min_daily, p)
        p_max_daily = max(p_max_daily, p)
        assert D <= C * 1.0001 + 1, f"INSOLVENT day {day}: D={D:.0f} C={C:.0f}"
        assert -1 <= F <= D + 1, f"FLOAT BROKEN day {day}: F={F:.0f} D={D:.0f}"

    avg_D = avg_D_sum / max(c.days, 1)
    years = c.days / 365

    # trailing-365d DAO revenue series (aligned to day index; first year uses
    # expanding window) + the yr-2 annual read
    def trail(arr, d, n=365):
        return sum(arr[max(0, d - n + 1):d + 1])
    dao_rev_path = [dict(day=d,
                         perf=trail(rev_perf_daily, d),
                         prot=trail(rev_prot_daily, d))
                    for d in range(0, c.days, 7)]
    dao_perf_yr = trail(rev_perf_daily, c.days - 1)
    dao_prot_yr = trail(rev_prot_daily, c.days - 1)

    return dict(
        final_C=C, final_D=D, final_F=F, final_arb=arb, final_depth=depth,
        ledgers=led, redeems=led['redeem_vol'], total_mint=led['mint_vol'],
        attr=attr, avg_D=avg_D, p_min_daily=p_min_daily,
        p_max_daily=p_max_daily,
        dao_perf_yr=dao_perf_yr, dao_prot_yr=dao_prot_yr,
        dao_rev_path=dao_rev_path,
        churn_cycles=led['redeem_vol'] / max(avg_D * years, 1),
        churn_ratio=led['redeem_vol'] / max(led['mint_vol'], 1),
        path=path)


# --------------------------------------------------------------- scenarios -

def pct(x):
    return f"{x*100:.1f}%"

# --------------------------------------------------- fee laffer sweeps ----
# Re-runs the FULL engine per fee point (same seed -> comparable curves).
# Yields the two DAO revenue streams separately, exactly as the lab charts:
#   perf revenue  = MYT performance fee on yield
#   redem revenue = protocolFee extracted from debtor collateral at payout

PERF_SWEEP = [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20,
              0.225, 0.25, 0.275, 0.30, 0.325, 0.35, 0.375, 0.40]
PROT_SWEEP = [0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075,
              0.009, 0.010, 0.0125, 0.015, 0.0175, 0.020]


def sweep(cfg: Config, attr: str, values) -> list:
    out = []
    for v in values:
        s = run(dc_replace(cfg, **{attr: v}, flow_noise=0.0))  # noiseless:
        out.append(dict(fee=v,                    # smooth, JS-verifiable
                        perf_k=s['dao_perf_yr'] / 1e3,
                        prot_k=s['dao_prot_yr'] / 1e3,
                        D_M=s['final_D'] / 1e6, C_M=s['final_C'] / 1e6,
                        churn=s['churn_cycles'],
                        peg=s['path'][-1]['peg'] if s['path'] else float('nan')))
    return out


def print_dao_line(name, s, cfg):
    L = s['ledgers']
    total = s['dao_perf_yr'] + s['dao_prot_yr']
    emis_cost = L['lp_emissions'] if cfg.emissions_cost_dao else 0.0
    net = total - emis_cost - cfg.dao_fixed_cost
    tag = "veAERO" if not cfg.emissions_cost_dao else "cash"
    print(f"  DAO yr-2 revenue ${total/1e3:7.1f}k/yr = perf ${s['dao_perf_yr']/1e3:6.1f}k"
          f" + redemption ${s['dao_prot_yr']/1e3:5.1f}k"
          f" | net (− emis[{tag}] ${emis_cost/1e3:5.0f}k − fixed ${cfg.dao_fixed_cost/1e3:4.0f}k)"
          f" = ${net/1e3:+7.1f}k")


def main():
    base = Config()

    scenarios = {
        "BASE": base,
        "COLD-START": dc_replace(base, cold_start=True),
        "YIELD-SHOCK": dc_replace(base, yield_shock_day=150,
                                  yield_recovery_day=300),
        "PANIC-RUN": dc_replace(base, run_day=120, run_frac=0.20),
        "STRATEGY-LOSS": dc_replace(base, strategy_loss_day=150,
                                    strategy_loss_frac=0.15),
        "HIGH-ARB": dc_replace(base, initial_arb=5_000_000),
        "THIN-LP": dc_replace(base, initial_depth=400_000,
                              emissions_per_day=800),
    }

    print("=" * 78)
    print("BASE V3 LAUNCH — LOOP THEORY VALIDATION".center(78))
    print("=" * 78)

    for name, cfg in scenarios.items():
        try:
            s = run(cfg)
        except AssertionError as e:
            print(f"\n--- {name} " + "-" * (74 - len(name)))
            print(f"  ⚠ {e} — system broke (hard assert; the modeled tail).")
            continue
        L = s['ledgers']
        A = s['attr']
        pegs = [r['peg'] for r in s['path'] if r['day'] > 56]  # post-launch
        mature = [r['peg'] for r in s['path'] if r['day'] > 365]
        launch = [r['peg'] for r in s['path'] if r['day'] <= 56]
        print(f"\n--- {name} " + "-" * (74 - len(name)))
        print(f"  2yr: C=${s['final_C']/1e6:6.1f}M  D=${s['final_D']/1e6:5.1f}M  "
              f"float=${s['final_F']/1e6:5.1f}M  depth=${s['final_depth']/1e6:4.1f}M  "
              f"arb=${s['final_arb']/1e6:4.1f}M")
        print_dao_line(name, s, cfg)
        print(f"  peg launch(8wk): lo {min(launch):.3f} | daily extremes: "
              f"lo {s['p_min_daily']:.4f} hi {s['p_max_daily']:.4f} | "
              f"mature(yr2): mean {sum(mature)/len(mature):.4f}")
        print(f"  mints ${L['mint_vol']/1e6:6.1f}M = replacement ${A['replacement']/1e6:5.1f}M"
              f" + yield-conv ${A['yield']/1e6:5.1f}M + equity-conv ${A['equity']/1e6:5.1f}M")
        print(f"  redeemed ${s['redeems']/1e6:5.1f}M | churn {s['churn_cycles']:.1f} "
              f"cycles/yr | new equity ${L['new_equity']/1e6:5.1f}M "
              f"| float bought ${L['float_buys']/1e6:5.1f}M "
              f"| equity exit ${L['equity_exit']/1e6:5.1f}M "
              f"| pool vol ${L['pool_volume']/1e6:5.1f}M")
        print(f"  LP: fees ${L['lp_fees']/1e3:5.0f}K + emissions "
              f"${L['lp_emissions']/1e3:5.0f}K | arb profit ${L['arb_profit']/1e3:6.0f}K")
        shown = s['path'][:8] + s['path'][26::52][:4]
        for r in shown:
            print(f"    d{r['day']:3d} peg {r['peg']:.4f} C {r['C']:5.1f}M "
                  f"D {r['D']:5.1f}M F {r['F']:5.1f}M depth {r['depth']:4.1f}M "
                  f"lpAPR {r['lp_apr']:5.1f}% arbAPR {r['arb_apr']:5.1f}%")

    # ---------------- DAO revenue Laffer sweeps ----------------
    for title, attr, values, fmtv in [
            ("PERF-FEE LAFFER (redemption fee fixed)", "perf_fee",
             PERF_SWEEP, lambda v: f"{v*100:5.1f}%"),
            ("PROTOCOL-FEE LAFFER (perf fee fixed)", "protocol_fee",
             PROT_SWEEP, lambda v: f"{v*1e4:5.1f}bp")]:
        print("\n" + "=" * 78)
        print(title.center(78))
        print(f"(full engine re-run per point, noiseless; other dials at BASE)")
        print("=" * 78)
        rows = sweep(base, attr, values)
        print(f"  {'fee':>7s} {'perf $k/yr':>11s} {'redem $k/yr':>12s} "
              f"{'total $k/yr':>12s} {'D $M':>7s} {'C $M':>7s} {'churn/yr':>9s} {'peg':>7s}")
        for r in rows:
            print(f"  {fmtv(r['fee']):>7s} {r['perf_k']:11.1f} {r['prot_k']:12.1f} "
                  f"{r['perf_k']+r['prot_k']:12.1f} {r['D_M']:7.1f} {r['C_M']:7.1f} "
                  f"{r['churn']:9.1f} {r['peg']:7.4f}")

    # ---------------- analytical cross-checks ----------------
    print("\n" + "=" * 78)
    print("ANALYTICAL CROSS-CHECKS")
    print("=" * 78)
    headroom = base.ltv * (10 - 1 * (1 + base.protocol_fee)) - (9 - 1)
    capacity = headroom / (1 - base.ltv * base.initial_peg)
    print(f" 1. re-mint capacity after $1 redemption: {capacity:.3f}  "
          f"(identity ~0.99 -> internal loop is float-neutral)")
    y = base.myt_yield * (1 - base.perf_fee)
    conv = 0.9 * y / (1 - base.ltv * base.initial_peg) / 10 * 10
    print(f" 2. yield->supply conversion: {y*100:.2f}% net yield -> "
          f"~{conv*100:.0f}% of collateral per year in mintable supply")
    # 3. peg floor from the global risk-free curve (scoopy's T-bill point)
    rf = 0.05
    pstar = 1 - rf * base.redemption_days / 365
    print(f" 3. peg floor at T-bill parity (r_f={rf:.0%}, 28d term): "
          f"{pstar:.4f}  == spec target 0.996")
    for rp in (0.03, 0.08):
        pr = 1 - (rf + rp) * base.redemption_days / 365
        print(f"    with +{rp:.0%} new-deployment risk premium: {pr:.4f}")


if __name__ == "__main__":
    main()
