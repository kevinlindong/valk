"""Day 1 strategy v20 — v19 truth-anchored MM extended to A/B/C/D.

WHY v20:
  * v19 makes money on A but bleeds on B because it lets directional
    skew + size run unchecked while B's per-tick settlement grinds
    against any stale inventory. v20 keeps v19's truth oracle but
    is FUNDAMENTALLY A MARKET MAKER — quote two-sided on every
    symbol, ramp position with reveal count, dime to defend BBO,
    snipe only when book is grossly mispriced AND not a spoofer.
  * Two new exchange-traded derivatives extend the universe:
      - Market C  (binary call):   settles 100 if sum(X_i) >= K else 0
                                    → fair_c ∈ {0, 100} from t=0 (truth)
      - Market D  (realized range): settles max(X_i) - min(X_i)
                                    → fair_d = const from t=0 (truth)
  * Both C and D have other oracle-armed bots competing for the
    obvious edge: VANG (oracle_sweeper, C, 10-75s cadence, 5 levels)
    and GVAN (oracle_twapper, D, 4s cadence, size 1). We can't out-
    blitz them — we have to OUT-MM them by sitting tighter than the
    naive bots (VANA, VANC, AVAN, CVAN) while staying outside the
    HVALK/spoofer wide-quote bait zone.

WHAT v20 ADDS vs v19:
  * SYMS = ("A","B","C","D"). State, resting, in_flight, dime
    throttles auto-extend via parent's SYMS-driven loops.
  * Symbol-keyed dispatch helpers — _width_for, _flip_for,
    _min_edge_for, _size_min/max_for, _fair_for — so the parent's
    "if sym == 'A' else 'B'" branches don't need to be rewritten
    case-by-case.
  * `fair_c()` / `fair_d()` — truth-derived exact constants once
    `/api/admin/truth` resolves. Pre-truth fallback uses MC priors.
  * Two-sided MM on C/D with directional skew:
      - C: width=6 (look like a tighter VANA/VANC). Under truth=100,
        bid is tight (close to fair), ask is wide (parked near 100
        cap). Position ramp prevents loading until k > k_min.
      - D: width=4 (match AVAN/CVAN). Under truth-known range,
        dime AVAN/CVAN by 1 tick whenever edge >= penny_min_edge_d.
  * Position-cap ramp (per-symbol):
      - C: 25% * 50 → 100% * 50 (min_cap=10) — gradual load-up.
      - D: 25% * 75 → 100% * 75 (min_cap=15) — gradual load-up.
  * Size ramp (per-symbol): smaller quotes early, larger as truth
    materializes.
  * Anti-spoofer guards preserved per-symbol:
      - A: max_ioc_distance_ticks_a = 50 (HVALK is B-only but other
        wide quoters on A live).
      - B: max_ioc_distance_ticks_b = 50 (HVALK explicit spoofer).
      - C: max_ioc_distance_ticks_c = 90 (binary [0,100] needs deep
        sniping, but linger floor blocks lift below px=2 / above 98
        unless quote persists > c_linger_min_ms).
      - D: max_ioc_distance_ticks_d = 30 (D unbounded but realistic
        range is small; 30t past fair is almost certainly a spoof).
  * Discretion: NO initial blitz, NO one-sided quoting under truth.
    We blend in with the MM crowd via matched widths and reasonable
    sizes. Snipes only fire on grossly mispriced book levels (>=
    sweep_edge_ticks_X) — they look like normal cross-arb hits.
  * VANA/VANC are wide_width=20-30 — when truth diverges from their
    naive_mean=7 prior (e.g. truth says fair_c=100), every bid<99 or
    ask>1 they post is ours for the taking. v20 catches them on the
    cross-arb path (inter_sweep + on-fill).

BOT MAP (from bot_config_dump.json):
  * A bots: VALKA(naive_mm,w20), VALKB(noise), VALKC(informed_sniper,
    lead=1500ms,fire=0.4), VALKD(dimer,refresh=1s,min_spread=0),
    VALKE(mixed_sweeper), VALKF(predictive_mm,acc=0.55),
    VALKG(directional_taker,lean=0.7), VALKH(inventory_mm,w14),
    VALKJ(slow_informed,30-60s), VALKK(casual,w30) — wide+slow,
    VALKL(bayes_mm,w24), VALKM(bayes_taker,edge=2),
    VALKN(pull_event,ioc=50), VALKO(stale_quoter,5s_refresh,size=25)
    — BIG STALE QUOTES, easy snipe when truth contradicts.
  * B bots: AVALK(naive_avg_mm,w6), BVALK(noise),
    CVALK(informed_sniper_next,fire=1.0,lead=1s),
    DVALK(predictive_avg_mm,acc=0.55), EVALK(random_sweeper),
    FVALK(directional_taker_next,lean=0.7), GVALK(informed_twapper),
    HVALK(SPOOFER,size=10-30,linger_ms=300) — DON'T FILL,
    IVALK(true_mean_taker,edge=0), JVALK(order_stat_inv_mm,w6),
    KVALK(dimer,refresh=1s,improve=1-2t), SEAN(sean,w=4-8).
  * C bots: VANA(naive_binary_mm,w30,size 2-4),
    VANB(noise,max_buy=99/min_sell=2) — random hits between 2-99,
    VANC(moments_binary_mm,w20,size 2-4),
    VAND(dimer,min_spread=4,refresh=1.5s) — only dimes if BBO>=4,
    VANE(informed_binary_taker,25-35s,edge=0) — informed competitor,
    VANF(random_sweeper,max_buy=99/min_sell=2) — random sweeps,
    VANG(oracle_sweeper,10-75s,levels=5) — DIRECT COMPETITOR with
    insider info. Be tighter than VANG between its bursts.
  * D bots: AVAN(naive_range_mm,w4,size 3-5),
    BVAN(noise,size=2), CVAN(naive_range_mm,w4,size 3-5),
    DVAN(informed_range_taker,25-35s,n_sims=3000),
    EVAN(directional_taker,lean=0.65),
    FVAN(resting_layer,lead=1.5s pre-reveal),
    GVAN(oracle_twapper,4s+jitter,size=1) — slow oracle competitor.

DESIGN INVARIANTS:
  * Hard position_limit on A/B/C/D never breached (snipe paths still
    use ±L; MM uses the ramped soft cap).
  * Truth-anchored fair → tiny sigma → tight inter-sweep / cross-arb
    edges. The "looks like MM" disguise comes from the WIDTH of our
    posted quotes, not the sweep math.
  * NEVER lift HVALK-style 300ms-linger wide-quote bait — the
    max_ioc_distance + linger gate enforces this.
  * Dime defense fires per-symbol with independent throttles so a
    v=1 dime war on D doesn't starve A's REST budget.

Run:
    python day1/strategy20/run_combined20.py
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

# Path bootstrap: all dependencies live next to this file inside strategy20/.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sdk_client import GameClient  # noqa: E402
from strategy12 import (  # noqa: E402, F401
    URL, API_KEY, N_PRIOR_SIM, Posterior,
)
from strategy18 import _LiveThresholds  # noqa: E402
from strategy19 import (  # noqa: E402
    Strategy as StrategyV19,
    Config as ConfigV19,
    TruthOracle,
)

__all__ = [
    "URL", "API_KEY", "N_PRIOR_SIM",
    "Posterior", "TruthOracle",
    "Config", "Strategy",
]


# ===========================================================================
# Config v20 — extends v19 with C/D MM, ramps, dime, anti-spoofer knobs
# ===========================================================================
@dataclass
class Config(ConfigV19):
    # =================================================================
    # Symbol C — binary call, [0, 100], tick=1
    # =================================================================
    # MM width — VAND dimer has min_spread=4, so width=6 means VAND
    # only dimes us if our quote loosens to >=4t spread; we'll usually
    # be width=2-3 effective (after dime defense) which keeps VAND off
    # us. VANA/VANC quote width 20-30, so we'll always be inside them.
    # v20.1: widened from 6 → 10. VANA/VANC are oracle competitors;
    # the previous 6t put us at fair±3 where they'd dime us
    # systematically. width=10 keeps us at fair±5 — still inside
    # VANA/VANC's 20-30 width quotes but outside the dimer fight.
    quote_width_c: int = 10
    size_c: int = 4                # Match VANA/VANC size range
    skew_full_flip_c: int = 30     # ±30 position fully flips bid/ask sides

    # Position cap ramp for C — starts at 25% * 50 = 13, ramps to 50.
    # v20.1: lowered floor from 10 → 5. C losses (-$15k single session)
    # came from being adversely selected as MAKER by VANA/VANC. Lower
    # cap during the first half of the round limits the bleed.
    position_ramp_min_cap_c: int = 5

    # Size ramp for C
    size_ramp_min_size_c: int = 2

    # Sweep thresholds — under truth, fair is exact. Edge=2 ticks
    # means we IOC any ask <= fair-2 or bid >= fair+2. Looks like a
    # normal cross-arb hit, not an oracle blast.
    sweep_edge_ticks_c: float = 2.0
    inter_sweep_edge_ticks_c: float = 2.0
    cross_arb_edge_ticks_c: float = 2.0
    penny_min_edge_c: float = 2.0

    # IOC distance cap. C is bounded [0, 100] so a fair=100 with
    # ask=0 has distance 100 — but that's almost certainly real
    # mispricing on a binary contract, not a spoof. Cap at 90 so
    # extreme-corner asks (bid > 98, ask < 2) still go through the
    # linger gate below.
    max_ioc_distance_ticks_c: float = 90.0

    # Anti-spoof linger floor for C. Quotes at ask < 2 or bid > 98
    # must have been seen for >= c_linger_min_ms before we lift them.
    # HVALK spoof bots have linger=300ms; we require ~500ms to be
    # conservative.
    c_extreme_low: int = 2
    c_extreme_high: int = 98
    c_linger_min_ms: float = 500.0

    # =================================================================
    # Symbol D — realized range, tick=1
    # =================================================================
    # AVAN/CVAN quote width=4. We want to MATCH them or slightly
    # dime, not stick out. width=4 means we sit AT or 1t inside.
    quote_width_d: int = 4
    size_d: int = 4                # Match AVAN/CVAN size range
    skew_full_flip_d: int = 50     # D position limit 75 → flip at ±50

    position_ramp_min_cap_d: int = 15
    size_ramp_min_size_d: int = 2

    sweep_edge_ticks_d: float = 1.0
    inter_sweep_edge_ticks_d: float = 1.0
    cross_arb_edge_ticks_d: float = 1.0
    penny_min_edge_d: float = 0.5

    max_ioc_distance_ticks_d: float = 30.0

    # =================================================================
    # Discretion knobs
    # =================================================================
    # Initial blitz disabled — v20 is a market maker, not a sniper.
    # If you want to load up immediately on truth, set True.
    initial_cd_blitz_enabled: bool = False

    # Per-symbol dime defense throttles inherited from v18 default
    # (dime_defense_throttle_sec). C/D get their own to avoid
    # cross-symbol REST starvation.
    dime_defense_throttle_sec_c: float = 0.15
    dime_defense_throttle_sec_d: float = 0.15

    # Inter-sweep throttle per C/D (inherited from parent for A/B).
    inter_sweep_throttle_sec_c: float = 0.15
    inter_sweep_throttle_sec_d: float = 0.15

    # =================================================================
    # v20.1: Early-round take suppression (analysis of v20 logs showed
    # 85-95% taker% in first 5s vs v19's ~5%; the IOC blast fires the
    # instant TruthOracle returns, before reveal 1). Gates below shift
    # v20 from "take-first MM" to "MM-first take-when-edge-arrives".
    # =================================================================
    # Minimum reveal count before cross-arb and inter-sweep are armed.
    # Default 1: wait for at least one real reveal so the public book
    # has tightened around fair_a/fair_b and quotes reflect new info.
    cross_arb_min_reveals: int = 1
    inter_sweep_min_reveals: int = 1

    # After each reveal the public book takes ~15ms to update. Suppress
    # cross-arb for this many ms post-reveal so we're not racing stale
    # book snapshots.
    cross_arb_post_reveal_lockout_ms: float = 50.0

    # Early-round edge multiplier — when reveals_seen < early_round_reveals,
    # require this multiple of the normal cross-arb / inter-sweep edge.
    # Default 1.5x: with cross_arb_edge_ticks_c=2.0 → 3.0t required.
    early_round_edge_mult: float = 1.5
    early_round_reveals: int = 2
    # Absolute floor on early-round edge (in ticks). v19 inherits
    # cross_arb_edge_ticks=0.5 for A/B — 0.5*1.5=0.75 is too easy to
    # trigger, so a 2.0t hard floor ensures we only take obvious arbs
    # during the high-uncertainty opening.
    early_round_edge_floor: float = 2.0

    # Per-round per-symbol cross-arb qty budget cap. Once a symbol's
    # cumulative cross-arb fills exceed this, cross-arb skips it for the
    # rest of the round. Reset on phase → running. Hard ceiling against
    # the worst-case session blowout (-$15k C in one v20 session).
    #
    # v20.4: actual exchange position limits are A=±50, B=±100, C=±50,
    # D=±75. v20.3 had cross_arb_qty_budget_a=80 (OVER A's limit!) and
    # c=25 (50% of C cap). New budgets are 60% of each symbol's hard
    # limit, leaving room for MM to also touch the position.
    cross_arb_qty_budget_a: int = 30   # 60% of ±50
    cross_arb_qty_budget_b: int = 60   # 60% of ±100
    cross_arb_qty_budget_c: int = 15   # 30% of ±50 — C bleeds adversely
    cross_arb_qty_budget_d: int = 45   # 60% of ±75

    # =================================================================
    # v20.1: Symbol B anti-spoofer (HVALK) — extend the C-only linger
    # gate to B. v20 logs caught us filling 24 qty against HVALK in the
    # first 5s of rounds; max_ioc_distance=50 was too permissive.
    # =================================================================
    # B max IOC distance — tighter than the inherited 50 so HVALK
    # baits at fair±30-50 are rejected outright.
    max_ioc_distance_ticks_b: float = 25.0

    # Linger gate: a B price >= b_extreme_offset ticks off fair must
    # have lingered >= b_linger_min_ms before we lift it. HVALK quotes
    # have linger_ms=300; we require 400ms to be conservative.
    b_extreme_offset: float = 20.0
    b_linger_min_ms: float = 400.0

    # =================================================================
    # v20.1: MM aggressiveness under truth
    # =================================================================
    # When the truth oracle has cached a sequence, our fair_X is exact.
    # Quote tighter (shrink width by N ticks) and bigger (bump base
    # size by N) to capture more flow at known-zero variance.
    # Excludes C: VANA/VANC adverse-selection risk; we want C WIDER
    # under truth, not tighter.
    mm_aggressive_under_truth: bool = True
    mm_truth_width_shrink_ticks: int = 1
    mm_truth_size_bump: int = 1

    # =================================================================
    # v20.2: High-frequency MM throughout the round
    # =================================================================
    # Faster MM tick cadence than the inherited 0.4/1.5s — under truth
    # the fair is constant so re-quoting more often is cheap (only
    # produces a message when position shifts our skew or dime defense
    # triggers). Strategy rate gate (15/sec) still protects us.
    # v20.3: tightened further (0.20 → 0.15 / 0.50 → 0.30) to drive more
    # HF re-quote / dime-recapture flow throughout the round.
    mm_min_interval_sec: float = 0.15
    mm_refresh_sec: float = 0.30

    # v20.2: Re-enable pre-first-reveal flatten-bias even when truth
    # is locked. v19 disabled it because "no uncertainty = no need to
    # flatten". User directive: be FLAT going into reveal 1 so we have
    # full directional flexibility through every subsequent window.
    flatten_bias_under_truth: bool = True
    # Tighter window than the inherited 8.0s — gives us aggressive
    # flattening only in the final lead-up, not the whole pre-reveal
    # phase.
    pre_first_reveal_flatten_sec: float = 5.0

    # v20.2: Directional skew for C / D under truth.
    # When truth_fair − BBO_mid > min_edge, lean LONG (tight bid, wide
    # ask). When truth_fair − BBO_mid < −min_edge, lean SHORT (tight
    # ask, wide bid). Mirrors v19's A/B directional_skew_* knobs but
    # uses BBO midpoint as the "naive proxy" since we don't compute a
    # naive trajectory for C/D.
    cd_directional_skew_enabled: bool = True
    cd_directional_skew_min_edge_ticks: float = 1.0
    cd_directional_skew_tight_ticks: int = 1
    cd_directional_skew_wide_ticks: int = 4

    # =================================================================
    # v20.3: Reveal-time IOC sweep extended to C / D
    # =================================================================
    # v18's on_reveal sweeps A and B only. v20.3 adds a C/D pass right
    # after super().on_reveal() runs, using truth-derived fair_c /
    # fair_d and per-symbol sweep_edge / max_ioc_distance. This is the
    # mechanism that captures laggard quotes still showing at pre-reveal
    # prices during the ~15ms public-feed lag after a reveal.
    reveal_sweep_cd_enabled: bool = True

    # =================================================================
    # v20.3: Hit-and-retreat MM
    # =================================================================
    # On every fill (maker OR taker), immediately repost the OPPOSITE
    # side at a tight scalp-out price (inside BBO when room exists).
    # The intent: capture roundtrip P&L from MM rather than holding the
    # position and waiting for it to revalue. Under truth this is pure
    # edge — fair is exact, every roundtrip past the spread is profit.
    hit_and_retreat_enabled: bool = True
    # When we just bought, target ask = max(fair + min_edge,
    # best_ask - inside_ticks). When we sold, target bid mirrors.
    hit_and_retreat_inside_ticks: int = 1
    # Don't scalp under tiny positions — let the MM loop handle them.
    hit_and_retreat_min_abs_pos: int = 1
    # Throttle so a burst of fills (e.g. 5 lots @ multiple book levels)
    # doesn't fire 5 scalp-outs in one ms.
    hit_and_retreat_throttle_sec: float = 0.05

    # =================================================================
    # v20.3: Scalp-out at cap — keep MM alive after position saturates
    # =================================================================
    # When |position| >= soft cap, v20.2 quoted only the inventory-
    # reducing side. But skip_negative_ev would null even THAT side
    # whenever skew pulled it past fair → dead time R3-R6. v20.3 forces
    # the inventory-reducing quote to live AT fair (or up to N ticks
    # past it) regardless of skip_negative_ev, so we keep scalping out.
    # Hard floor stops us from giving away too much: clamp to fair±
    # scalp_out_max_loss_ticks of pure loss per fill.
    scalp_out_at_cap_enabled: bool = True
    scalp_out_max_loss_ticks: int = 2

    # =================================================================
    # v20.3: Aggressive BBO diming + redime watch
    # =================================================================
    # 5x lower dime throttle: every ~50ms we recheck and re-step inside
    # the BBO if a competitor dimed us. Pair with hf MM cadence — even
    # if quote-loop is throttled by mm_min_interval_sec, dime defense
    # fires on every book event independently.
    dime_defense_throttle_sec: float = 0.05
    dime_defense_throttle_sec_c: float = 0.05
    dime_defense_throttle_sec_d: float = 0.05
    # When we step inside the BBO, also park a SECOND order at
    # BBO+2t (further out) as a redime fallback — if a competitor dimes
    # us at BBO+1, we still have a quote at BBO+2 that captures the
    # downstream flow. Disabled by default; turn on once we measure
    # it's not eating REST budget.
    dime_double_layer_enabled: bool = False

    # =================================================================
    # v20.3: Tighter early-round taker suppression
    # =================================================================
    # v20.2 had early_round_edge_floor=2.0. v20.3 raises to 3.0 — under
    # truth the early-window cross-arb edge floor is now 3t (was 2t),
    # the 50%-higher bar leaves more flow for our MM to capture passively.
    early_round_edge_floor: float = 3.0
    # Cap cross-arb slice during early window (reveal_count <
    # early_round_reveals). v18 default cross_arb_max_slice=8; the
    # early cap forces 3-lot snipes so we don't blast inventory in
    # the opening few seconds.
    cross_arb_max_slice_early: int = 3

    # =================================================================
    # v20.3: Cross-market arb trigger on every fill
    # =================================================================
    # Already inherited from v18. The flag is here only as a kill-switch.
    cross_arb_on_fill_enabled: bool = True

    # =================================================================
    # v20.4: Per-symbol slice caps + inventory-aware throttle
    # =================================================================
    # Old code used a single sweep_max_slice=10 / cross_arb_max_slice=8
    # for ALL symbols. With A=±50 limit, even an 8-lot IOC eats 16% of
    # capacity in one shot — multiple cross-arb fires in the first 30s
    # saturate position. Per-symbol caps scale by hard limit:
    #   A (±50): 5  — was 10
    #   B (±100): 10 — same
    #   C (±50): 3  — was 10; C also has VANA/VANC adverse selection
    #   D (±75): 7  — was 10
    sweep_max_slice_a: int = 5
    sweep_max_slice_b: int = 10
    sweep_max_slice_c: int = 3
    sweep_max_slice_d: int = 7
    inter_sweep_max_slice_a: int = 5
    inter_sweep_max_slice_b: int = 10
    inter_sweep_max_slice_c: int = 3
    inter_sweep_max_slice_d: int = 7
    cross_arb_max_slice_a: int = 4
    cross_arb_max_slice_b: int = 8
    cross_arb_max_slice_c: int = 3
    cross_arb_max_slice_d: int = 6

    # Inventory-aware throttle. When |pos|/limit grows on a given side,
    # the SAME-side slice caps shrink. Reducing-side slice stays full.
    # This stops the directional ratchet (A→-47 saturation, C→+48
    # saturation) we saw in v20.3 logs.
    inventory_throttle_enabled: bool = True
    # When |pos|/L > frac_X, multiply same-side slice by mult_X.
    inventory_throttle_frac1: float = 0.4   # 40% of limit
    inventory_throttle_mult1: float = 0.5
    inventory_throttle_frac2: float = 0.6
    inventory_throttle_mult2: float = 0.25
    inventory_throttle_frac3: float = 0.8
    # Above this, same-side taker is blocked outright.
    inventory_throttle_hard_block_frac: float = 0.8

    # =================================================================
    # v20.4: Parallel reveal-time sweep across A/B/C/D
    # =================================================================
    # v18 on_reveal sweeps A then B sequentially via sync REST. With
    # RTT ~150ms each, last symbol's IOC ack lands ~600ms post-reveal
    # — way past the 15ms public-feed-lag window. v20.4 submits all
    # 4 sweeps to _ioc_executor in parallel so the first ack lands at
    # 1x RTT instead of 4x.
    reveal_sweep_parallel_enabled: bool = True
    reveal_sweep_parallel_timeout_sec: float = 1.5

    # =================================================================
    # v20.5: C/D market-making emphasis
    # =================================================================
    # Diagnosis from combined_log_v20_20260520_231914: C posted 24
    # limit orders across 30 reveals (~0.4/cycle) and filled 18% as
    # maker. Under truth-locked C, _quote_prices_cd posts symmetric
    # ±width/2 quotes — when fair=100, ask clamps to 100 (never
    # fills); when fair=0, bid clamps to 1 (also degenerate). Effect:
    # half the time we're posting a quote that can't fill.
    #
    # Fix: when truth is locked and fair_c is in the boundary region,
    # switch to STACKED ONE-SIDED MM:
    #   * fair_c >= c_one_sided_high → only post BIDS at fair-d1,
    #     fair-d2, fair-d3 (multiple stacked levels). Skip asks.
    #   * fair_c <= c_one_sided_low → only post ASKS at fair+d1,
    #     fair+d2, fair+d3. Skip bids.
    # Catches naive MMs (VANA/VANC) and noise traders who quote
    # around their wider (and slower) posterior.
    c_one_sided_mm_enabled: bool = True
    c_one_sided_high: float = 90.0
    c_one_sided_low: float = 10.0
    c_one_sided_levels: int = 3
    c_one_sided_step_ticks: int = 2    # 2,4,6 ticks under/over fair
    c_one_sided_size: int = 3
    c_one_sided_min_size: int = 1

    # Tighter C MM width when NOT in boundary regime — dimes naive
    # MMs (VANA width 30, VANC width 20). Was 10 → 6.
    quote_width_c_tight: int = 6
    # Higher refresh frequency for C/D — naive MM bots refresh every
    # 1000-1500ms, so a 150ms cadence lets us re-quote 6-10x per
    # naive update cycle.
    mm_refresh_sec_cd: float = 0.15

    # C/D cross-arb cooldown when our MM already sits inside with
    # equal or better edge — avoids the "fire IOC, then immediately
    # repost via hit-and-retreat" loop we saw at C boundaries.
    cd_skip_arb_when_mm_present: bool = True

    # =================================================================
    # v20.5: C↔A delta hedge
    # =================================================================
    # C is a digital on A above strike K=137. When |c_pos| is large,
    # implied A delta = c_pos × P'(A_final crosses strike per unit
    # change in current_sum) × payout. For a simple linear approx,
    # we treat A delta = c_pos × 0.01 × payout × indicator_near_strike.
    # When fair_c is near boundary (>=90 or <=10), delta is near 0
    # (gamma is exhausted) — no hedge needed. When fair_c is in
    # middle (10-90), delta is ~payout × pdf-at-strike ≈ small per
    # remaining reveal.
    #
    # In practice the actionable hedge is: lean A's MM bid/ask skew
    # in the same direction as our C exposure. We expose this as a
    # small extra tick of skew on A when |c_pos| > threshold.
    ca_delta_hedge_enabled: bool = True
    ca_delta_hedge_pos_threshold: int = 10  # |c_pos| >= 10 to act
    ca_delta_hedge_max_skew_ticks: int = 2  # at most ±2t extra on A
    # When fair_c is in the mid (uncertain) zone, hedge is strongest.
    ca_delta_hedge_mid_low: float = 25.0
    ca_delta_hedge_mid_high: float = 75.0

    # =================================================================
    # v20.14 — Inventory-aware quote shift (truth-anchored MM-out)
    # =================================================================
    # When we're directionally loaded (e.g. +60 on B out of ±100),
    # shift BOTH bid and ask in the flattening direction so the next
    # fill is more likely to offset rather than compound. Under truth
    # we know fair exactly, so the shift is clipped to stay on the
    # +EV side of fair — never crosses it. Effect is quadratic in
    # |position/cap| beyond `inventory_shift_threshold`: near-zero
    # for small inventories, full magnitude at the cap.
    inventory_shift_enabled: bool = True
    inventory_shift_threshold: float = 0.3   # |frac| < 0.3 → no shift
    inventory_shift_max_ticks_a: int = 4
    inventory_shift_max_ticks_b: int = 3
    inventory_shift_max_ticks_c: int = 4
    inventory_shift_max_ticks_d: int = 4

    # =================================================================
    # v20.7 — public-tape driven MM (dime + one-sided wide)
    # =================================================================
    # The public trade tape lags the private feed by ~15ms but is the
    # one signal that tells us where COUNTERPARTIES are actually willing
    # to transact. Two distinct exploits ride on it:
    #
    # 1. DIME — the price of the latest sell-aggressor print is exactly
    #    where some resting bid just got hit. Posting a new bid one tick
    #    above that price (and still ≤ fair − min_edge for +EV) puts us
    #    at the new top-of-book inside the gap before competitors
    #    re-quote. Symmetric for asks.
    # 2. ONE-SIDED — if ≥`tape_imbalance_min_prints` prints arrive in
    #    `tape_window_sec` and ≥`tape_imbalance_frac` are on a single
    #    side, the OPPOSITE side of the book has been depleted. Widen
    #    our quote on the depleted side by `tape_wide_offset_ticks` so
    #    that, when the next forced trade reaches further in, we capture
    #    the cheap fill against our known-truth fair.
    tape_enabled: bool = True
    tape_dime_enabled: bool = True
    tape_imbalance_enabled: bool = True
    # How fresh a tape print must be (sec) to be used as a dime anchor.
    tape_freshness_sec: float = 0.6
    # Rolling window for imbalance detection.
    tape_window_sec: float = 0.2
    # Minimum prints in the window before we'll act on imbalance.
    tape_imbalance_min_prints: int = 3
    # Fraction of prints on a single side that qualifies as one-sided.
    tape_imbalance_frac: float = 0.8
    # Extra ticks to widen the depleted side (cheap entry vs truth).
    tape_wide_offset_ticks_a: int = 5
    tape_wide_offset_ticks_b: int = 6
    tape_wide_offset_ticks_c: int = 8
    tape_wide_offset_ticks_d: int = 6
    # Deque cap (defensive — window-trimming should keep it short).
    tape_max_prints: int = 64

    # =================================================================
    # v20.8 — Truth cap bypass for A/D
    # =================================================================
    # Under truth, A and D have exact, constant fairs. Every fill
    # within the edge band is +EV — the soft-cap ramp throws that
    # edge away. Bypass it for A/D when truth_locked. B and C still
    # ramp (B intra-tick settlement risk; C oracle-competitor adverse
    # selection).
    truth_cap_bypass_enabled: bool = True

    # =================================================================
    # v20.12 — Proactive BBO step-inside (JOIN fallback)
    # =================================================================
    # `_maybe_penny` and `_maybe_dime_defense` try to IMPROVE the BBO
    # by 1 tick (target = best_bid + tick). That target is then gated
    # by `target <= fair - min_edge`. If BBO is at fair - min_edge
    # (e.g. C with min_edge=2, BBO at fair-2), IMPROVE would post at
    # fair-1 which fails the gate → we DON'T step in and sit at our
    # wider baseline (fair-3 from quote_width/2). Result: not at BBO
    # while the market is well-spread.
    #
    # The JOIN fallback posts AT the BBO (target = best_bid) when
    # IMPROVE fails the edge gate but JOIN still clears the looser
    # `bbo_join_min_edge_ticks` floor. Maker rebate / fee math means
    # any fill at fair - 1t is still +0.5/lot positive vs maker fee
    # 0.5, so the join floor of 1 covers fees and leaves zero buffer.
    bbo_join_enabled: bool = True
    # v20.14: under truth we know fair exactly. Maker rebate (≈0.5/lot)
    # plus zero adverse-selection risk means fills exactly AT fair are
    # still net +0.5/lot. Drop the JOIN floor to 0 so we join BBO when
    # truth-bots own the inside.
    bbo_join_min_edge_ticks: float = 0.0
    # v20.14: penny_max_step_ticks must cover pre_reveal_park_offset_ticks
    # (=30) — otherwise post-park recovery crawls 5t per book event and
    # we stay parked 6+ seconds. Override the v18 default of 5.
    penny_max_step_ticks: int = 35

    # =================================================================
    # v20.11 — Hard-bounds arbitrage
    # =================================================================
    # Revealed values pin DETERMINISTIC bounds on settlement:
    #   * A_final >= sum(revealed)          — future values ≥ 0
    #   * D_final >= max(revealed)-min(revealed) once k≥2
    #   * C_final == 100 once sum(revealed) >= strike
    # Plus exact bounds under truth (lower==upper==fair). Any ask
    # priced strictly below the lower bound (or bid above upper) is a
    # GUARANTEED +EV fill independent of our fair-estimate accuracy.
    #
    # This path is stricter than cross-arb (which requires an edge
    # against a possibly-wrong fair) and looser than the post-reveal
    # lockout (free money doesn't need to wait). It runs on every
    # reveal (new info → tighter bounds) and every public print
    # (book may now contain a laggard violator).
    bounds_arb_enabled: bool = True
    # Required edge over the bound in ticks. With min_edge=1.0 we
    # only take asks priced AT LEAST 1 tick BELOW the lower bound,
    # so we clear the per-trade fee and still profit.
    bounds_arb_min_edge_ticks: float = 1.0
    # Slice cap multiplier vs sweep_max_slice (per-symbol). Bounds
    # arb is risk-free so we let it push more size at once.
    bounds_arb_slice_mult: float = 1.5
    # Cap per round per symbol — defensive bound against a runaway
    # spoof book that posts hundreds of bound-violating prices.
    bounds_arb_qty_budget_a: int = 50    # full ±L on A
    bounds_arb_qty_budget_b: int = 60    # B has tick-settle risk
    bounds_arb_qty_budget_c: int = 50    # full ±L on C
    bounds_arb_qty_budget_d: int = 75    # full ±L on D

    # =================================================================
    # COID prefix
    # =================================================================
    client_order_id_prefix: str = "v20"


# ===========================================================================
# TapeState — per-symbol rolling tape state. Fed by BOTH the private fill
# feed (15ms ahead of public — see `on_fill_event`) and the public trade
# tape (`on_trade`). Trade IDs are deduped so the same transaction isn't
# counted twice; private always wins because it arrives first.
#
# OPTIMIZATION: running buy_count / sell_count keep _tape_imbalance O(1).
# The deque entries carry the aggressor side so we know which counter to
# decrement on window trim. The fast precompute loop (every 50ms) drains
# stale entries even when no new prints arrive — without that, a quiet
# market could leave a positive imbalance signal alive long after the
# burst has faded.
# ===========================================================================
@dataclass
class TapeState:
    # recent: (t, aggressor, price, qty) for prints in tape_window_sec.
    recent: Deque[Tuple[float, str, int, int]] = field(default_factory=deque)
    # Running side counters (kept consistent with deque on every mutation).
    buy_count: int = 0
    sell_count: int = 0
    # Latest aggressor=buy event → price where ask just got lifted.
    last_buy_agg_price: Optional[int] = None
    last_buy_agg_t: float = 0.0
    # Latest aggressor=sell event → price where bid just got hit.
    last_sell_agg_price: Optional[int] = None
    last_sell_agg_t: float = 0.0
    # Dedupe: trade_id → ingest_t. Bounded; old entries fall off with the
    # window trim. Set of trade_ids we've already counted (either from
    # private fill or earlier public print).
    seen_trade_ids: Dict[int, float] = field(default_factory=dict)


# ===========================================================================
# IdentityBounds — v20.13. Precomputed hard mathematical bounds for a
# single symbol's final settlement value, derived from REVEALED values +
# cross-market identities (no fair-estimate involvement). Used by
# `_bounds_arb_sweep` to find risk-free fills, and by any caller that
# wants a deterministic "no-worse-than" floor/ceiling on settlement.
#
# Each bound is either a float or None (None = unbounded on that side).
# `lower_source` / `upper_source` tags the IDENTITY that produced the
# bound so logs make it obvious which derivation kicked in.
#
# Identities consolidated here:
#   reveals    : sum(revealed) / range(revealed) / sum_ge_K direct
#   eor_pin    : C = 0 when reveal_count == n_total AND sum < K
#   cross_C0   : A_upper = K - 1 when C is pinned at 0
#   cross_D_A  : D_upper ≤ A_upper (D = max - min ≤ max ≤ sum = A)
#   truth      : (fair, fair) collapse under truth oracle
# ===========================================================================
@dataclass
class IdentityBounds:
    sym: str
    lower: Optional[float] = None
    upper: Optional[float] = None
    lower_source: str = "init"
    upper_source: str = "init"
    gen: int = 0


# ===========================================================================
# Strategy v20
# ===========================================================================
class Strategy(StrategyV19):
    """v19 truth oracle + two-sided MM extended to A/B/C/D."""

    SYMS = ("A", "B", "C", "D")

    # ------------------------------------------------------------------
    # __init__ — reads C strike + caches truth-derived C/D fairs
    # ------------------------------------------------------------------
    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[Config] = None):
        if config is None:
            config = Config()
        elif not isinstance(config, Config):
            config = Config(**{
                f.name: getattr(config, f.name)
                for f in config.__dataclass_fields__.values()
                if f.name in Config.__dataclass_fields__
            })

        # Read C strike BEFORE super() so the first fast-precompute
        # tick has a usable fair_c.
        self.c_strike: Optional[int] = None
        try:
            gs = client.game_state()
            cmeta = ((gs.get("instruments") or {}).get("C") or {})
            sp = cmeta.get("settlement_params") or {}
            k_raw = (sp.get("strike") if "strike" in sp
                     else sp.get("K") if "K" in sp
                     else sp.get("threshold"))
            if k_raw is not None:
                self.c_strike = int(k_raw)
        except Exception as e:
            print(f"[v20 STRIKE] failed to read C strike: {e!r}")

        # v20.6: single-pass precompute. A/C/D are CONSTANTS once seq
        # is known. B advances per reveal via _truth_b_seq[reveal_count].
        self._fair_a_truth: Optional[float] = None
        self._fair_c_truth: Optional[float] = None
        self._fair_d_truth: Optional[float] = None
        self._truth_b_seq: Optional[Tuple[int, ...]] = None
        self._cd_truth_for_seq: Optional[Tuple[int, ...]] = None

        # Per-quote linger tracking for C extreme-tick spoof filter.
        # Key: (side, price), value: first-seen timestamp.
        self._c_linger_seen: Dict[Tuple[str, int], float] = {}
        # Same shape for B (HVALK spoofer extreme-tick gate).
        self._b_linger_seen: Dict[Tuple[str, int], float] = {}

        # v20.1: per-round per-symbol cross-arb qty consumed.
        # Reset on phase → running.
        self._cross_arb_qty_used: Dict[str, int] = {
            s: 0 for s in self.SYMS}

        # v20.11: per-round per-symbol bounds-arb qty consumed +
        # cumulative fire counter for diagnostics.
        self._bounds_arb_qty_used: Dict[str, int] = {
            s: 0 for s in self.SYMS}
        self._bounds_arb_count: int = 0

        # v20.1: last reveal monotonic timestamp (drives the
        # post-reveal lockout for cross-arb).
        self._last_reveal_t: float = 0.0

        # v20.1: precomputed arbitrage table — per-symbol exact
        # buy-at-or-below / sell-at-or-above thresholds. Built from
        # truth-derived fair + per-symbol min edge. Refreshed on
        # every reveal and inside the fast-precompute loop.
        # Schema: { sym: { 'fair': float, 'buy_at_or_below': int,
        #                  'sell_at_or_above': int, 'gen': int } }
        self._arb_table: Dict[str, Dict[str, float]] = {}
        self._arb_gen: int = 0

        # v20.13: precomputed hard-identity bounds per symbol. Pinned
        # to deterministic math (revealed values + cross-market
        # identities), independent of fair estimates. Reads by
        # `_bounds_arb_sweep` (instant risk-free fills) and any
        # diagnostic / external consumer that needs a no-worse-than
        # floor/ceiling on settlement.
        self._identity_table: Dict[str, IdentityBounds] = {
            s: IdentityBounds(sym=s) for s in self.SYMS
        }
        self._identity_gen: int = 0

        # v20.3: per-symbol scalp-out throttle (hit-and-retreat).
        # Key: symbol → last scalp-out timestamp.
        self._last_scalp_out_t: Dict[str, float] = {s: 0.0 for s in self.SYMS}
        # v20.3: per-symbol reveal-sweep counter for diagnostics.
        self._reveal_sweep_cd_count: int = 0
        # v20.4: parallel reveal-sweep futures. Cleared after each
        # reveal cycle. Pre-kicked at on_reveal entry so the override
        # of _sweep_post_reveal joins on the future instead of running
        # sequentially.
        self._reveal_sweep_futures: Dict[str, object] = {}
        # v20.5: separate refresh cadence clock for C/D so we can
        # tick MM faster on them than on A/B.
        self._last_mm_refresh_t_cd: float = 0.0

        # v20.7: public-tape state per symbol. Populated by on_trade and
        # read by _tape_dime_prices / _tape_imbalance_skew before MM quoting.
        self._tape: Dict[str, TapeState] = {s: TapeState() for s in self.SYMS}
        # Diagnostic counters.
        self._tape_dime_count: Dict[str, int] = {s: 0 for s in self.SYMS}
        self._tape_widen_count: Dict[str, int] = {s: 0 for s in self.SYMS}
        self._tape_print_count: Dict[str, int] = {s: 0 for s in self.SYMS}

        super().__init__(client, posterior, config)

        # After super: state["C"] and state["D"] exist (parent loops
        # over self.SYMS, which resolves to v20's tuple).
        self._refresh_cd_truth_cache()
        # Rebuild live thresholds so MM hot paths see fresh C/D
        # snipe levels on their first tick.
        try:
            self._build_live_thresholds()
        except Exception as e:
            print(f"[v20 INIT-LIVE] {e!r}")

        c_lim = self.state["C"].position_limit
        d_lim = self.state["D"].position_limit
        c_tick = self.state["C"].tick
        d_tick = self.state["D"].tick
        print(f"[v20 INIT] C strike={self.c_strike}  "
              f"C lim={c_lim} tick={c_tick} mult={self._mult('C')}  "
              f"D lim={d_lim} tick={d_tick} mult={self._mult('D')}")
        print(f"[v20 INIT] widths: A={self.cfg.quote_width_a} "
              f"B={self.cfg.quote_width_b} C={self.cfg.quote_width_c} "
              f"D={self.cfg.quote_width_d}")
        print(f"[v20 INIT] sizes: A={self.cfg.size_a} B={self.cfg.size_b} "
              f"C={self.cfg.size_c} D={self.cfg.size_d}")
        print(f"[v20 INIT] pos_ramp_min_caps: A={self.cfg.position_ramp_min_cap} "
              f"C={self.cfg.position_ramp_min_cap_c} "
              f"D={self.cfg.position_ramp_min_cap_d}")
        if self._fair_c_truth is not None:
            print(f"[v20 INIT-TRUTH] fair_c={self._fair_c_truth:.0f} "
                  f"fair_d={self._fair_d_truth:.1f}")

    # ==================================================================
    # Multiplier helper (read once from game_state instruments).
    # ==================================================================
    def _mult(self, sym: str) -> float:
        try:
            gs = self.c.game_state()
            instr = (gs.get("instruments") or {}).get(sym) or {}
            return float(instr.get("multiplier") or 1.0)
        except Exception:
            return 1.0

    # ==================================================================
    # Symbol-keyed dispatch helpers — used by all the overrides below
    # to avoid copy-pasting "if sym == 'A' else if sym == 'B' else ..."
    # ==================================================================
    def _width_for(self, sym: str) -> int:
        if sym == "A":
            base = self.cfg.quote_width_a
        elif sym == "B":
            base = self.cfg.quote_width_b
        elif sym == "C":
            # v20.5: tighter C width when truth is locked AND we're in
            # the mid-range (boundary regime uses one-sided stacked
            # MM via _quote_prices_c_truth_boundary which bypasses
            # this width entirely).
            if self._truth_locked("C"):
                base = self.cfg.quote_width_c_tight
            else:
                base = self.cfg.quote_width_c
        else:
            base = self.cfg.quote_width_d
        # v20.1: shrink under truth (skips C — see _truth_width_shrink).
        return max(2, base - self._truth_width_shrink(sym))

    def _flip_for(self, sym: str) -> int:
        if sym == "A":
            return max(1, self.cfg.skew_full_flip_a)
        if sym == "B":
            return max(1, self.cfg.skew_full_flip_b)
        if sym == "C":
            return max(1, self.cfg.skew_full_flip_c)
        return max(1, self.cfg.skew_full_flip_d)

    def _min_edge_for(self, sym: str) -> float:
        if sym == "A":
            return self.cfg.penny_min_edge_a
        if sym == "B":
            return self.cfg.penny_min_edge_b
        if sym == "C":
            return self.cfg.penny_min_edge_c
        return self.cfg.penny_min_edge_d

    def _sweep_edge_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            return self.cfg.sweep_edge_ticks
        if sym == "C":
            return self.cfg.sweep_edge_ticks_c
        return self.cfg.sweep_edge_ticks_d

    def _inter_sweep_edge_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            base = self.cfg.inter_sweep_edge_ticks
        elif sym == "C":
            base = self.cfg.inter_sweep_edge_ticks_c
        else:
            base = self.cfg.inter_sweep_edge_ticks_d
        return self._apply_early_round_edge(base)

    def _cross_arb_edge_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            base = self.cfg.cross_arb_edge_ticks
        elif sym == "C":
            base = self.cfg.cross_arb_edge_ticks_c
        else:
            base = self.cfg.cross_arb_edge_ticks_d
        return self._apply_early_round_edge(base)

    def _early_round_mult(self) -> float:
        if self._reveal_count < self.cfg.early_round_reveals:
            return float(self.cfg.early_round_edge_mult)
        return 1.0

    def _apply_early_round_edge(self, base: float) -> float:
        """Apply both the multiplicative early-round bump and the
        absolute floor. Outside the early window returns base unchanged."""
        if self._reveal_count >= self.cfg.early_round_reveals:
            return base
        scaled = base * self.cfg.early_round_edge_mult
        return max(scaled, self.cfg.early_round_edge_floor)

    def _max_ioc_dist_for(self, sym: str) -> float:
        if sym == "A":
            return self.cfg.max_ioc_distance_ticks
        if sym == "B":
            return self.cfg.max_ioc_distance_ticks_b
        if sym == "C":
            return self.cfg.max_ioc_distance_ticks_c
        return self.cfg.max_ioc_distance_ticks_d

    def _dime_throttle_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            return self.cfg.dime_defense_throttle_sec
        if sym == "C":
            return self.cfg.dime_defense_throttle_sec_c
        return self.cfg.dime_defense_throttle_sec_d

    def _inter_sweep_throttle_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            return self.cfg.inter_sweep_throttle_sec
        if sym == "C":
            return self.cfg.inter_sweep_throttle_sec_c
        return self.cfg.inter_sweep_throttle_sec_d

    def _base_size_for(self, sym: str) -> int:
        if sym == "A":
            base = self.cfg.size_a
        elif sym == "B":
            base = self.cfg.size_b
        elif sym == "C":
            base = self.cfg.size_c
        else:
            base = self.cfg.size_d
        # v20.1: bump under truth (skips C).
        return base + self._truth_size_bump(sym)

    def _size_ramp_min_for(self, sym: str) -> int:
        if sym in ("A", "B"):
            return self.cfg.size_ramp_min_size
        if sym == "C":
            return self.cfg.size_ramp_min_size_c
        return self.cfg.size_ramp_min_size_d

    def _pos_ramp_min_for(self, sym: str) -> int:
        if sym in ("A", "B"):
            return self.cfg.position_ramp_min_cap
        if sym == "C":
            return self.cfg.position_ramp_min_cap_c
        return self.cfg.position_ramp_min_cap_d

    def _fair_for(self, sym: str) -> Tuple[float, float]:
        if sym == "A":
            return self.fair_a()
        if sym == "B":
            return self.fair_b()
        if sym == "C":
            return self.fair_c()
        return self.fair_d()

    # v20.1: per-symbol cross-arb qty budget (-1 = unlimited).
    def _cross_arb_budget_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.cross_arb_qty_budget_a
        if sym == "B":
            return self.cfg.cross_arb_qty_budget_b
        if sym == "C":
            return self.cfg.cross_arb_qty_budget_c
        return self.cfg.cross_arb_qty_budget_d

    # v20.4: per-symbol per-path slice caps.
    def _sweep_slice_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.sweep_max_slice_a
        if sym == "B":
            return self.cfg.sweep_max_slice_b
        if sym == "C":
            return self.cfg.sweep_max_slice_c
        return self.cfg.sweep_max_slice_d

    def _inter_sweep_slice_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.inter_sweep_max_slice_a
        if sym == "B":
            return self.cfg.inter_sweep_max_slice_b
        if sym == "C":
            return self.cfg.inter_sweep_max_slice_c
        return self.cfg.inter_sweep_max_slice_d

    def _cross_arb_slice_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.cross_arb_max_slice_a
        if sym == "B":
            return self.cfg.cross_arb_max_slice_b
        if sym == "C":
            return self.cfg.cross_arb_max_slice_c
        return self.cfg.cross_arb_max_slice_d

    # v20.4: inventory-aware throttle. Reduces SAME-side IOC slice as
    # |pos|/limit grows. Reducing-side slice stays full to drain.
    # Returns 0 if hard-blocked (too close to cap on same side).
    def _inventory_throttle(self, sym: str, side: str,
                            base_slice: int) -> int:
        if not self.cfg.inventory_throttle_enabled:
            return base_slice
        s = self.state[sym]
        pos = s.position
        if pos == 0:
            return base_slice
        # side="buy" adds to long; side="sell" adds to short.
        adding_long = (side == "buy" and pos > 0)
        adding_short = (side == "sell" and pos < 0)
        if not (adding_long or adding_short):
            return base_slice  # reducing |pos| → full slice
        frac = abs(pos) / max(1, s.position_limit)
        if frac >= self.cfg.inventory_throttle_hard_block_frac:
            return 0
        if frac >= self.cfg.inventory_throttle_frac3:
            return max(1, int(round(
                base_slice * self.cfg.inventory_throttle_mult2)))
        if frac >= self.cfg.inventory_throttle_frac2:
            return max(1, int(round(
                base_slice * self.cfg.inventory_throttle_mult2)))
        if frac >= self.cfg.inventory_throttle_frac1:
            return max(1, int(round(
                base_slice * self.cfg.inventory_throttle_mult1)))
        return base_slice

    # v20.1: True iff the truth oracle has cached a full sequence
    # → fair for every symbol is exact.
    def _truth_locked(self, sym: str) -> bool:
        if sym in ("C", "D"):
            return (self._fair_c_truth is not None
                    if sym == "C"
                    else self._fair_d_truth is not None)
        try:
            return self.truth.full_sequence() is not None
        except Exception:
            return False

    # v20.1: how many ticks to shrink MM width by when truth is locked.
    # C is excluded — oracle-competitor adverse-selection means we want
    # WIDER (not tighter) C quotes under truth.
    def _truth_width_shrink(self, sym: str) -> int:
        if not self.cfg.mm_aggressive_under_truth:
            return 0
        if sym == "C":
            return 0
        if not self._truth_locked(sym):
            return 0
        return max(0, self.cfg.mm_truth_width_shrink_ticks)

    # v20.1: how many lots to add to base size under truth.
    def _truth_size_bump(self, sym: str) -> int:
        if not self.cfg.mm_aggressive_under_truth:
            return 0
        if sym == "C":
            return 0
        if not self._truth_locked(sym):
            return 0
        return max(0, self.cfg.mm_truth_size_bump)

    # v20.2: override v19's hard-disable. We WANT flatten-bias during
    # pre-first-reveal even under truth — so MM, inter-sweep, and
    # cross-arb all skew toward reducing |pos| as reveal 1 approaches.
    def _in_flatten_bias_window(
            self, sigma_a: Optional[float] = None) -> bool:
        if not self.cfg.flatten_bias_enabled:
            return False
        if self.phase != "running":
            return False
        # v20.2: pre-first-reveal window, even with truth.
        if self.cfg.flatten_bias_under_truth and self._reveal_count == 0:
            nxt = self._next_reveal_at()
            if nxt is not None:
                eta = nxt - time.time()
                if 0.0 <= eta <= self.cfg.pre_first_reveal_flatten_sec:
                    return True
        # Defer to v19 (which defers to v18) for the sigma-trigger path.
        return super()._in_flatten_bias_window(sigma_a)

    # ==================================================================
    # v20.6: single precompute pass for ALL fair values
    # ==================================================================
    # Once the full sequence is known, the final values of A (sum), C
    # (digital on sum vs strike), and D (range max−min) are exact and
    # constant for the rest of the round. B is the per-reveal tick
    # value — settles at every reveal, so fair_B = sequence[next_idx]
    # which advances each cycle. Compute the constants once and cache
    # them; fair_b reads the per-reveal value off the cached list.
    def _refresh_cd_truth_cache(self) -> None:
        seq = self.truth.full_sequence() if self.cfg.truth_enabled else None
        if not seq:
            self._fair_a_truth = None
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._cd_truth_for_seq = None
            return
        seq_t = tuple(seq)
        if seq_t == self._cd_truth_for_seq:
            return
        self._cd_truth_for_seq = seq_t
        # A: final = sum(seq). Constant.
        self._fair_a_truth = float(sum(seq))
        # C: 100 if sum >= strike else 0. Constant.
        if self.c_strike is not None:
            self._fair_c_truth = (100.0
                                  if self._fair_a_truth >= self.c_strike
                                  else 0.0)
        else:
            self._fair_c_truth = None
        # D: range = max - min. Constant.
        self._fair_d_truth = float(max(seq) - min(seq))
        # B: per-reveal value vector. Reads as fair_b each cycle.
        self._truth_b_seq = tuple(int(v) for v in seq)
        print(f"[v20.6 TRUTH-ALL] seq={list(seq)}  "
              f"fair_a={self._fair_a_truth:.0f}  K={self.c_strike}  "
              f"fair_c={self._fair_c_truth}  "
              f"fair_d={self._fair_d_truth:.1f} "
              f"(max={max(seq)},min={min(seq)})  "
              f"fair_b_seq_len={len(self._truth_b_seq)}")

    # v20.6: read precomputed A from the same cache used for C/D.
    # super().fair_a (v19) hits the oracle every call; this hits a
    # local field after the first sequence-arrival pass.
    def fair_a(self) -> Tuple[float, float]:
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        if self._fair_a_truth is not None:
            return self._fair_a_truth, 0.5
        return super().fair_a()

    # v20.6: fair_B = sequence[next_reveal_index]. Advances per
    # reveal because reveal_count is the index of the upcoming
    # window. Once we pass the last reveal, B no longer settles
    # → fall back to super (which returns None / posterior estimate).
    def fair_b(self) -> Tuple[float, float]:
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        seq = self._truth_b_seq
        if seq is not None:
            i = self._reveal_count
            if 0 <= i < len(seq):
                return float(seq[i]), 0.5
        return super().fair_b()

    def fair_c(self) -> Tuple[float, float]:
        """Truth-anchored exact under truth; Normal-approx pre-truth."""
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        if self._fair_c_truth is not None:
            return self._fair_c_truth, 0.5

        # Pre-truth fallback. Bots VANA quote around N(70, ~12.6) for
        # sum. We approximate the same way so our quotes don't stick
        # out before truth arrives (rare — usually truth fetches at
        # round start).
        if self.c_strike is None:
            return 50.0, 20.0
        n_rem = self._n_remaining()
        if n_rem <= 0:
            mean_sum = self._running_sum
            sig_sum = 0.5
        else:
            mean_sum = self._running_sum + n_rem * 7.0
            sig_sum = math.sqrt(n_rem * 16.0)
        if sig_sum < 1e-6:
            return ((100.0 if mean_sum >= self.c_strike else 0.0), 0.5)
        z = (mean_sum - self.c_strike) / sig_sum
        from math import erf as _erf, sqrt as _sqrt
        p_ge = 0.5 * (1.0 + _erf(z / _sqrt(2.0)))
        return p_ge * 100.0, 30.0

    def fair_d(self) -> Tuple[float, float]:
        """Truth-anchored exact under truth; MC prior approx pre-truth."""
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        if self._fair_d_truth is not None:
            return self._fair_d_truth, 0.5

        # Pre-truth fallback: E[range | reveals so far] from posterior.
        revs = list(self.posterior.reveals)
        if not revs:
            return 5.0, 2.0
        n_rem = self._n_remaining()
        cur_min = min(revs)
        cur_max = max(revs)
        cur_range = cur_max - cur_min
        if n_rem <= 0:
            return float(cur_range), 0.5
        # Approximation: E[range] grows roughly as the support width
        # scaled by (1 - 1/(n+1)) — clamped.
        est = cur_range + 0.5 * n_rem
        return est, max(1.5, 0.3 * est)

    # ==================================================================
    # Position cap ramp — override to dispatch C/D
    # ==================================================================
    def _effective_position_cap(self, sym: str) -> int:
        L = self.state[sym].position_limit
        if not self.cfg.position_ramp_enabled:
            return L
        # v20.8: under truth, A and D have exact constant fairs and no
        # intra-round settlement risk. Every fill inside [fair-edge,
        # fair+edge] is +EV, so the ramp just wastes edge. Bypass to
        # full ±L. B is excluded because each reveal tick-settles
        # independently and we can't always unwind between ticks
        # (v19 -$3,291 B-bleed). C is excluded because the oracle
        # competitors (VANG/GVAN) cause adverse selection at the
        # boundary, so we want a tighter cap, not wider.
        if self.cfg.truth_cap_bypass_enabled and sym in ("A", "D") \
                and self._truth_locked(sym):
            return L
        prog = self._round_progress()
        frac = (self.cfg.position_ramp_start_frac
                + (self.cfg.position_ramp_end_frac
                   - self.cfg.position_ramp_start_frac) * prog)
        cap = int(round(L * frac))
        floor = self._pos_ramp_min_for(sym)
        cap = max(cap, floor)
        return min(cap, L)

    # ==================================================================
    # Size ramp — override to dispatch C/D
    # ==================================================================
    def _ramped_base_size(self, sym: str) -> int:
        base = self._base_size_for(sym)
        if not self.cfg.size_ramp_enabled:
            return base
        prog = self._round_progress()
        frac = (self.cfg.size_ramp_start_frac
                + (1.0 - self.cfg.size_ramp_start_frac) * prog)
        sz = int(round(base * frac))
        floor = self._size_ramp_min_for(sym)
        return max(sz, floor)

    # ==================================================================
    # v20.5: One-sided C MM under truth-locked boundary regime.
    # Returns (bid, ask) where exactly one side is None, or None
    # if the regime doesn't apply.
    # ==================================================================
    def _quote_prices_c_truth_boundary(
            self, fair: float, position: int
    ) -> Optional[Tuple[Optional[int], Optional[int]]]:
        s = self.state["C"]
        tick = s.tick
        ub = self._upper_bound("C")
        hi = self.cfg.c_one_sided_high
        lo = self.cfg.c_one_sided_low
        pad = self.cfg.mm_inventory_safety_pad
        if fair >= hi:
            # Buy-only mode. Truth says ~100, anything below is +EV.
            # Post tight at fair-1 (= 99 when truth=100). Skip the
            # skew that v18 layered for two-sided MM — at the
            # boundary there's no "balanced inventory" target.
            if position >= s.position_limit - pad:
                return (None, None)  # at long cap, stop adding
            bid_px = max(1, int(round(fair)) - 1)
            # Cap inside upper bound (ub=99 for C since ub-1=99).
            bid_px = min(bid_px, ub - 1)
            qty = self._quote_size("C", "bid")
            if qty <= 0 or bid_px < 1:
                return (None, None)
            return (bid_px, None)
        if fair <= lo:
            # Sell-only mode. Truth says ~0, anything above is +EV.
            if position <= -s.position_limit + pad:
                return (None, None)
            ask_px = min(ub, int(round(fair)) + 1)
            ask_px = max(ask_px, 1)
            qty = self._quote_size("C", "ask")
            if qty <= 0:
                return (None, None)
            return (None, ask_px)
        return None  # mid range → fall through to two-sided MM

    # ==================================================================
    # Quote prices — for A/B call super (v19 truth + dir-skew); for C/D
    # compute symmetric two-sided MM around truth-derived fair.
    # The wrapping override that applies scalp_out_at_cap is defined
    # further down (v20.3 section); this is the C/D dispatch helper.
    # ==================================================================
    def _quote_prices_cd(self, sym: str, fair: float, position: int,
                         mode: str
                         ) -> Tuple[Optional[int], Optional[int]]:
        """Two-sided MM around fair for C / D. Mirrors v18's quote
        math (half = width/2, position-skew = position/flip * half).

        Special handling for C — bounded [0, 100]:
          * bid in [1, 99], ask in [1, 99]. Cap at boundaries.
          * Under truth=100, normal-mode bid=fair-3 (e.g. 97),
            ask=fair+3 caps at 99 (since 103 > 100). Ask at 99 only
            loses 1t per fill, and only fires if a counterparty is
            buying through 99 (rare unless they also know truth).
          * Skew flips both sides DOWN with long position (encourage
            selling out via taker hits to our ask).
        """
        # v20.5: under truth, C near boundary uses one-sided MM.
        # Posting a sell@100 at truth=100 (or buy@1 at truth=0) is
        # a dead quote — it can never fill in our favour. Only
        # the "favourable" side fills (counterparty makes a
        # mistake selling cheap or buying expensive). Skip the
        # dead side and skip position-skew on the live side, so we
        # always present at the tightest profitable price.
        if (sym == "C" and mode == "normal"
                and self.cfg.c_one_sided_mm_enabled
                and self._truth_locked("C")):
            res = self._quote_prices_c_truth_boundary(fair, position)
            if res is not None:
                return res

        s = self.state[sym]
        tick = s.tick
        # v20.1: _width_for already applies truth-shrink (skipping C).
        width = self._width_for(sym)
        flip = self._flip_for(sym)

        if mode == "park":
            off = self.cfg.pre_reveal_park_offset_ticks * tick
            bid_px = ((int(round(fair - off))) // tick) * tick
            ask_px = ((int(round(fair + off))) // tick) * tick
            bid_px, ask_px = self._clamp_cd(sym, bid_px, ask_px)
            pad = self.cfg.mm_inventory_safety_pad
            if position >= s.position_limit - pad:
                bid_px = None
            if position <= -s.position_limit + pad:
                ask_px = None
            return bid_px, ask_px

        half = width / 2.0
        if self._reveal_count == 0:
            half *= self.cfg.no_info_widen_mult
        elif mode == "widen":
            half *= self.cfg.pre_reveal_widen_mult

        skew = (position / flip) * half
        bid_px = int(round(fair - half - skew))
        ask_px = int(round(fair + half - skew))
        bid_px = (bid_px // tick) * tick
        ask_px = (ask_px // tick) * tick

        # v20.2: directional skew under truth. fair=truth_fair already;
        # compare against BBO mid (naive proxy for what non-oracle
        # bots are pricing). If truth>mid → lean LONG (tighter bid,
        # wider ask). If truth<mid → lean SHORT.
        bid_px, ask_px = self._apply_cd_dir_skew(
            sym, fair, position, bid_px, ask_px, tick)

        # Boundary clamping (C only; D unbounded).
        bid_px, ask_px = self._clamp_cd(sym, bid_px, ask_px)

        if (bid_px is not None and ask_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + tick if (bid_px + tick) <= self._upper_bound(sym) else None

        if self.cfg.skip_negative_ev:
            if bid_px is not None and bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None

        # Soft cap (MM only) — if we're past the ramped cap, drop the
        # side that would push us further out.
        cap = self._effective_position_cap(sym)
        if position >= cap and bid_px is not None:
            bid_px = None
        if position <= -cap and ask_px is not None:
            ask_px = None

        # Hard pad — never quote into the side already at full hard limit.
        pad = self.cfg.mm_inventory_safety_pad
        if position >= s.position_limit - pad:
            bid_px = None
        if position <= -s.position_limit + pad:
            ask_px = None

        if bid_px is not None and bid_px < tick:
            bid_px = None
        return bid_px, ask_px

    def _apply_cd_dir_skew(self, sym: str, fair: float, position: int,
                           bid_px: Optional[int], ask_px: Optional[int],
                           tick: int
                           ) -> Tuple[Optional[int], Optional[int]]:
        """v20.2 C/D directional skew. Mirrors v19's A/B logic but uses
        BBO midpoint as the naive proxy. Only fires when truth is locked
        AND |truth-mid| >= min_edge AND we have a usable BBO."""
        if not self.cfg.cd_directional_skew_enabled:
            return bid_px, ask_px
        if not self._truth_locked(sym):
            return bid_px, ask_px
        s = self.state[sym]
        book = s.book or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return bid_px, ask_px
        try:
            best_bid = int(bids[0].get("price"))
            best_ask = int(asks[0].get("price"))
        except Exception:
            return bid_px, ask_px
        if best_ask <= best_bid:
            return bid_px, ask_px
        mid = 0.5 * (best_bid + best_ask)
        edge = fair - mid
        min_edge = self.cfg.cd_directional_skew_min_edge_ticks
        if abs(edge) < min_edge:
            return bid_px, ask_px

        tight = self.cfg.cd_directional_skew_tight_ticks * tick
        wide = self.cfg.cd_directional_skew_wide_ticks * tick

        if edge > 0:
            # truth > market mid → lean LONG. Tighten bid, widen ask.
            ideal_bid = ((int(round(fair - tight))) // tick) * tick
            ideal_ask = ((int(round(fair + wide))) // tick) * tick
            if bid_px is not None:
                bid_px = max(bid_px, ideal_bid)
            if ask_px is not None:
                ask_px = max(ask_px, ideal_ask)
        else:
            # truth < market mid → lean SHORT. Tighten ask, widen bid.
            ideal_ask = ((int(round(fair + tight))) // tick) * tick
            ideal_bid = ((int(round(fair - wide))) // tick) * tick
            if ask_px is not None:
                ask_px = min(ask_px, ideal_ask)
            if bid_px is not None:
                bid_px = min(bid_px, ideal_bid)
        return bid_px, ask_px

    def _upper_bound(self, sym: str) -> int:
        """Upper-bound for quotable price on C. D is unbounded → big."""
        if sym == "C":
            return 100
        return 10**9

    def _lower_bound(self, sym: str) -> int:
        """Lower-bound for quotable price (tick > 0)."""
        return self.state[sym].tick

    def _clamp_cd(self, sym: str, bid_px: Optional[int],
                  ask_px: Optional[int]
                  ) -> Tuple[Optional[int], Optional[int]]:
        """For C: clamp bid in [1, 99], ask in [1, 99]. Returning None
        for either side if the clamp would produce a non-positive
        price (D is unbounded — pass through)."""
        if sym != "C":
            return bid_px, ask_px
        if bid_px is not None:
            if bid_px < 1:
                bid_px = None
            elif bid_px > 99:
                bid_px = 99
        if ask_px is not None:
            if ask_px > 99:
                ask_px = 99
            elif ask_px < 1:
                ask_px = None
        return bid_px, ask_px

    # ==================================================================
    # MM refresh — parent loops self.SYMS but reads fair via hard-coded
    # branches; override to dispatch C/D too.
    # ==================================================================
    def _refresh_mm_quotes(self) -> None:
        if not self._can_post():
            return
        if self._endgame_active:
            # Endgame freezes A specifically; B/C/D continue to MM.
            pass  # Don't early-return; just skip A in the loop below.
        now = time.time()
        ab_due = (self.cfg.mm_min_interval_sec <= 0
                  or (now - self._last_mm_refresh_t)
                  >= self.cfg.mm_min_interval_sec)
        # v20.5: C/D get a faster refresh cadence so we keep dimed
        # quotes alive against the 1000-1500ms naive MMs.
        cd_due = (self.cfg.mm_refresh_sec_cd <= 0
                  or (now - self._last_mm_refresh_t_cd)
                  >= self.cfg.mm_refresh_sec_cd)
        if not (ab_due or cd_due):
            return
        if ab_due:
            self._last_mm_refresh_t = now
        if cd_due:
            self._last_mm_refresh_t_cd = now
        in_park = self._in_park_window()
        for sym in self.SYMS:
            if self._endgame_active and sym == "A":
                continue
            # v20.5: respect the per-cohort gate.
            if sym in ("A", "B") and not ab_due:
                continue
            if sym in ("C", "D") and not cd_due:
                continue
            try:
                fair, _ = self._fair_for(sym)
                pos = self.state[sym].position
                if in_park:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "park")
                else:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "normal")
                    bid_px, ask_px = self._maybe_penny(
                        sym, fair, pos, bid_px, ask_px)
                    # v20.14: re-apply inventory shift AFTER penny —
                    # the penny step overwrites prices with BBO targets
                    # and would otherwise undo the skew baked into
                    # _quote_prices.
                    bid_px, ask_px = self._apply_inventory_shift(
                        sym, fair, bid_px, ask_px)
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v20 MM:{sym}-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # Park loop — parent loops SYMS but hardcodes fair_a/fair_b lookup;
    # override to dispatch C/D too.
    # ==================================================================
    def _park_tick(self) -> None:
        if not self._can_post():
            return
        if not self._in_park_window():
            return
        if self._last_park_reveal_idx == self._reveal_count:
            return
        self._last_park_reveal_idx = self._reveal_count
        for sym in self.SYMS:
            try:
                fair, _ = self._fair_for(sym)
                pos = self.state[sym].position
                bid_px, ask_px = self._quote_prices(sym, fair, pos, "park")
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v20 PARK:{sym}-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # Penny — override to dispatch min_edge per symbol.
    # ==================================================================
    def _maybe_penny(self, sym: str, fair: float, position: int,
                     bid_px: Optional[int], ask_px: Optional[int]
                     ) -> Tuple[Optional[int], Optional[int]]:
        if not self.cfg.penny_enabled:
            return bid_px, ask_px
        book = self.state[sym].book
        if not book:
            return bid_px, ask_px
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        tick = self.state[sym].tick
        min_edge = self._min_edge_for(sym)
        max_step = self.cfg.penny_max_step_ticks
        join_edge = self.cfg.bbo_join_min_edge_ticks
        join_on = self.cfg.bbo_join_enabled
        ub = self._upper_bound(sym)

        if (bid_px is not None and best_bid is not None
                and best_bid >= bid_px):
            same = any(int(e.get("price") or 0) == best_bid
                       for e in self.state[sym].resting["bid"].values())
            # IMPROVE: post 1 tick ABOVE the BBO bid (taking top-of-book).
            improve_target = best_bid + tick if not same else best_bid
            improve_ok = (
                improve_target <= fair - min_edge
                and (improve_target - bid_px) <= max_step * tick
                and improve_target < (
                    best_ask if best_ask is not None else 10**9)
                and improve_target <= ub - 1)
            if improve_ok:
                bid_px = improve_target
            elif join_on and not same:
                # v20.12 JOIN fallback: IMPROVE failed (usually because
                # BBO+1t exceeds fair-min_edge); fall back to posting AT
                # the BBO. Maker fee 0.5 → fills at fair-1t are still +EV.
                join_target = best_bid
                if (join_target <= fair - join_edge
                        and (join_target - bid_px) <= max_step * tick
                        and join_target < (
                            best_ask if best_ask is not None else 10**9)
                        and join_target <= ub - 1):
                    bid_px = join_target

        if (ask_px is not None and best_ask is not None
                and best_ask <= ask_px):
            same = any(int(e.get("price") or 0) == best_ask
                       for e in self.state[sym].resting["ask"].values())
            improve_target = best_ask - tick if not same else best_ask
            improve_ok = (
                improve_target >= fair + min_edge
                and (ask_px - improve_target) <= max_step * tick
                and improve_target > (
                    best_bid if best_bid is not None else 0)
                and improve_target <= ub)
            if improve_ok:
                ask_px = improve_target
            elif join_on and not same:
                # v20.12 JOIN fallback for ask side.
                join_target = best_ask
                if (join_target >= fair + join_edge
                        and (ask_px - join_target) <= max_step * tick
                        and join_target > (
                            best_bid if best_bid is not None else 0)
                        and join_target <= ub):
                    ask_px = join_target
        return bid_px, ask_px

    # ==================================================================
    # Dime defense — override to dispatch fair+min_edge per symbol.
    # ==================================================================
    def _maybe_dime_defense(self, sym: str) -> None:
        if not self.cfg.dime_defense_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        now = time.time()
        throttle = self._dime_throttle_for(sym)
        if (now - s.last_dime_defense_t) < throttle:
            return
        book = s.book
        if not book:
            return
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return
        best_bid = int(bids[0]["price"])
        best_ask = int(asks[0]["price"])
        fair, _ = self._fair_for(sym)
        min_edge = self._min_edge_for(sym)
        join_edge = self.cfg.bbo_join_min_edge_ticks
        join_on = self.cfg.bbo_join_enabled
        tick = s.tick
        max_step_dist = self.cfg.penny_max_step_ticks * tick
        ub = self._upper_bound(sym)
        fired = False

        # --- Bid side ---
        my_best_bid = None
        for entry in s.resting["bid"].values():
            px = int(entry.get("price") or 0)
            if my_best_bid is None or px > my_best_bid:
                my_best_bid = px
        if my_best_bid is None or my_best_bid < best_bid:
            # IMPROVE first (post 1t inside BBO).
            target_bid = best_bid + tick
            improve_ok = (
                target_bid <= fair - min_edge
                and target_bid < best_ask
                and target_bid <= ub - 1
                and (my_best_bid is None
                     or (target_bid - my_best_bid) <= max_step_dist))
            if not improve_ok and join_on:
                # v20.12: JOIN BBO when IMPROVE breaches edge.
                target_bid = best_bid
                improve_ok = (
                    target_bid <= fair - join_edge
                    and target_bid < best_ask
                    and target_bid <= ub - 1
                    and (my_best_bid is None
                         or (target_bid - my_best_bid) <= max_step_dist))
                tag = "JOIN"
            else:
                tag = "STEP"
            if improve_ok:
                # v20.14: inventory-aware shift. When long-heavy, this
                # pulls target_bid DOWN — possibly below BBO, in which
                # case skip (we don't want more longs). When short-heavy,
                # leaves it alone (we want longs).
                shifted_bid, _ = self._apply_inventory_shift(
                    sym, fair, target_bid, None)
                if shifted_bid is not None and shifted_bid < target_bid:
                    if shifted_bid < best_bid:
                        # Shift wants us below BBO — skip dime entirely.
                        improve_ok = False
                    else:
                        target_bid = shifted_bid
            if improve_ok:
                qty = self._quote_size(sym, "bid")
                if qty > 0:
                    case = "absent" if my_best_bid is None else "dimed"
                    print(f"[v20 BBO:{sym}/bid {case}/{tag}] "
                          f"best={best_bid}(mine={my_best_bid}) "
                          f"→ {target_bid} fair={fair:.2f}")
                    self._apply_side(sym, "bid", target_bid, qty)
                    fired = True

        # --- Ask side ---
        my_best_ask = None
        for entry in s.resting["ask"].values():
            px = int(entry.get("price") or 0)
            if my_best_ask is None or px < my_best_ask:
                my_best_ask = px
        if my_best_ask is None or my_best_ask > best_ask:
            target_ask = best_ask - tick
            improve_ok = (
                target_ask >= fair + min_edge
                and target_ask > best_bid
                and target_ask <= ub
                and target_ask > 0
                and (my_best_ask is None
                     or (my_best_ask - target_ask) <= max_step_dist))
            if not improve_ok and join_on:
                target_ask = best_ask
                improve_ok = (
                    target_ask >= fair + join_edge
                    and target_ask > best_bid
                    and target_ask <= ub
                    and target_ask > 0
                    and (my_best_ask is None
                         or (my_best_ask - target_ask) <= max_step_dist))
                tag = "JOIN"
            else:
                tag = "STEP"
            if improve_ok:
                # v20.14: inventory-aware shift. When short-heavy, this
                # pulls target_ask UP — possibly above BBO. Skip dime
                # in that case (we don't want more shorts).
                _, shifted_ask = self._apply_inventory_shift(
                    sym, fair, None, target_ask)
                if shifted_ask is not None and shifted_ask > target_ask:
                    if shifted_ask > best_ask:
                        improve_ok = False
                    else:
                        target_ask = shifted_ask
            if improve_ok:
                qty = self._quote_size(sym, "ask")
                if qty > 0:
                    case = "absent" if my_best_ask is None else "dimed"
                    print(f"[v20 BBO:{sym}/ask {case}/{tag}] "
                          f"best={best_ask}(mine={my_best_ask}) "
                          f"→ {target_ask} fair={fair:.2f}")
                    self._apply_side(sym, "ask", target_ask, qty)
                    fired = True

        if fired:
            s.last_dime_defense_t = now

    # ==================================================================
    # Inter-reveal stale sweep — override to dispatch fair fallback +
    # per-symbol edge / max_dist.
    # ==================================================================
    def _maybe_inter_sweep(self, sym: str) -> None:
        # v20.5: defer A/B to v18 entirely so the maker/taker mix on
        # those symbols matches the v18 baseline. v20-specific
        # behaviour (per-symbol slice, inventory throttle) only
        # applies to C/D where we own the strategy.
        if sym in ("A", "B"):
            return super()._maybe_inter_sweep(sym)
        if not self.cfg.inter_sweep_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        # v20.1: gate until first reveal — same shape as cross-arb gate.
        if self._reveal_count < self.cfg.inter_sweep_min_reveals:
            return
        s = self.state[sym]
        now = time.time()
        throttle = self._inter_sweep_throttle_for(sym)
        if (now - s.last_inter_sweep_t) < throttle:
            return
        s.last_inter_sweep_t = now

        live_lift, live_hit, live_fair, in_flat = self._get_snipe_thresholds(
            sym, s.position)
        if live_fair == 0.0:
            live_fair, _ = self._fair_for(sym)
        fair = live_fair
        book = s.book
        if not book:
            return
        edge = self._inter_sweep_edge_for(sym)
        edge_buy = edge
        edge_sell = edge
        if in_flat:
            reduction = self.cfg.flatten_edge_reduction_ticks
            floor = self.cfg.flatten_min_edge_ticks
            if s.position < 0:
                edge_buy = max(floor, edge - reduction)
            if s.position > 0:
                edge_sell = max(floor, edge - reduction)
        min_size = self.cfg.inter_sweep_min_size
        # v20.4: per-symbol slice + inventory throttle.
        base_slice = self._inter_sweep_slice_for(sym)
        max_slice_buy = self._inventory_throttle(sym, "buy", base_slice)
        max_slice_sell = self._inventory_throttle(sym, "sell", base_slice)
        pad = self.cfg.sweep_position_pad
        max_dist = self._max_ioc_dist_for(sym)
        fired = 0
        # v20.10: respect ramped cap on inter-sweep IOC path.
        eff_cap = self._effective_position_cap(sym)

        room_buy = (eff_cap - pad) - s.position
        if room_buy > 0 and max_slice_buy > 0:
            max_slice = max_slice_buy
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    continue
                if fair - px < edge_buy:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "ask", int(px)):
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, int(px)):
                    taken += want
                    room_buy -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position < 0) else "INTER"
                    print(f"[v20 {tag}:{sym}] BUY {want}@{px} "
                          f"fair={fair:.2f} edge={fair - px:.2f}t "
                          f"pos={s.position}")
                else:
                    break

        room_sell = s.position - (-eff_cap + pad)
        if room_sell > 0 and max_slice_sell > 0:
            max_slice = max_slice_sell
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    continue
                if px - fair < edge_sell:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "bid", int(px)):
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, int(px)):
                    taken += want
                    room_sell -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position > 0) else "INTER"
                    print(f"[v20 {tag}:{sym}] SELL {want}@{px} "
                          f"fair={fair:.2f} edge={px - fair:.2f}t "
                          f"pos={s.position}")
                else:
                    break
        if fired:
            self._inter_sweep_count += fired

    # ==================================================================
    # Cross-symbol arb / on-fill / on-reveal stale snipe — override the
    # per-symbol loop to walk all 4 books.
    # ==================================================================
    def _try_cross_arb(self, source: str) -> int:
        if not self.cfg.cross_arb_enabled:
            return 0
        if not self._can_post():
            return 0
        if self._in_lockout():
            return 0
        if self._in_park_window():
            return 0

        # v20.1: gate until first reveal (kills the t=0 truth-blast).
        # Reveal-driven calls (source="reveal") bypass the gate — the
        # reveal IS the qualifying event.
        if (source != "reveal"
                and self._reveal_count < self.cfg.cross_arb_min_reveals):
            return 0

        # v20.1: post-reveal lockout. Public book lags ~15ms behind;
        # don't fire IOCs in the lag window unless we're being called
        # by the reveal handler itself.
        if source != "reveal" and self._last_reveal_t > 0.0:
            since_reveal_ms = (time.monotonic() - self._last_reveal_t) * 1000.0
            if since_reveal_ms < self.cfg.cross_arb_post_reveal_lockout_ms:
                return 0

        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks
        # v20.4: per-symbol slice (computed in the loop) + early gate.
        early = self._reveal_count < self.cfg.early_round_reveals
        depth = max(1, self.cfg.cross_arb_levels)
        pad = self.cfg.sweep_position_pad
        fired_total = 0

        with self._live_lock:
            live = dict(self._live_thresholds)

        for sym in self.SYMS:
            s = self.state[sym]
            book = s.book
            if not book:
                continue

            # v20.1: per-round per-symbol qty budget.
            budget = self._cross_arb_budget_for(sym)
            used = self._cross_arb_qty_used.get(sym, 0)
            if budget > 0 and used >= budget:
                continue
            room_budget = budget - used if budget > 0 else 10**9

            # v20.5: A/B fall through to v18 globals so the maker/taker
            # mix on those symbols matches the v18 baseline. Only C/D
            # get per-symbol slice + inventory throttle.
            if sym in ("A", "B"):
                base_slice_sym = self.cfg.cross_arb_max_slice
                if early:
                    base_slice_sym = min(base_slice_sym,
                                         self.cfg.cross_arb_max_slice_early)
                max_slice_buy = base_slice_sym
                max_slice_sell = base_slice_sym
            else:
                base_slice_sym = self._cross_arb_slice_for(sym)
                if early:
                    base_slice_sym = min(base_slice_sym,
                                         self.cfg.cross_arb_max_slice_early)
                max_slice_buy = self._inventory_throttle(sym, "buy", base_slice_sym)
                max_slice_sell = self._inventory_throttle(sym, "sell", base_slice_sym)

            base_edge = self._cross_arb_edge_for(sym)
            max_dist = self._max_ioc_dist_for(sym)
            lt = live.get(sym)
            if lt is not None:
                fair = lt.fair
                in_flat = lt.in_flatten_window
            else:
                fair, _ = self._fair_for(sym)
                in_flat = False
            edge_buy = base_edge
            edge_sell = base_edge
            if in_flat:
                if s.position < 0:
                    edge_buy = max(flat_min, base_edge - flat_reduction)
                if s.position > 0:
                    edge_sell = max(flat_min, base_edge - flat_reduction)

            # v20.10: use ramped (effective) cap, not hard position_limit.
            eff_cap = self._effective_position_cap(sym)
            room_buy = (eff_cap - pad) - s.position
            if room_buy > 0 and max_slice_buy > 0:
                max_slice = max_slice_buy
                taken = 0
                for lvl in (book.get("asks") or [])[:depth]:
                    if room_buy <= 0 or room_budget <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if fair - px < edge_buy:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    if self._is_spoof_like(sym, "ask", int(px)):
                        continue
                    want = min(max_slice - taken, room_buy, size, room_budget)
                    if want <= 0:
                        break
                    if self._ioc(sym, "buy", want, int(px)):
                        taken += want
                        room_buy -= want
                        room_budget -= want
                        self._cross_arb_qty_used[sym] = (
                            self._cross_arb_qty_used.get(sym, 0) + want)
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position < 0)
                               else f"ARB({source})")
                        print(f"[v20 {tag}:{sym}] BUY {want}@{px}  "
                              f"fair={fair:.2f} edge={fair - px:.2f}t "
                              f"pos={s.position} budget_left={room_budget}")
                    else:
                        break

            # v20.10: use ramped cap on sell side too.
            room_sell = s.position - (-eff_cap + pad)
            if room_sell > 0 and max_slice_sell > 0:
                max_slice = max_slice_sell
                taken = 0
                for lvl in (book.get("bids") or [])[:depth]:
                    if room_sell <= 0 or room_budget <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if px - fair < edge_sell:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    if self._is_spoof_like(sym, "bid", int(px)):
                        continue
                    want = min(max_slice - taken, room_sell, size, room_budget)
                    if want <= 0:
                        break
                    if self._ioc(sym, "sell", want, int(px)):
                        taken += want
                        room_sell -= want
                        room_budget -= want
                        self._cross_arb_qty_used[sym] = (
                            self._cross_arb_qty_used.get(sym, 0) + want)
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position > 0)
                               else f"ARB({source})")
                        print(f"[v20 {tag}:{sym}] SELL {want}@{px}  "
                              f"fair={fair:.2f} edge={px - fair:.2f}t "
                              f"pos={s.position}")
                    else:
                        break
        if fired_total:
            self._cross_arb_count += fired_total
        return fired_total

    # ==================================================================
    # v20.11: Hard-bounds arbitrage
    # ==================================================================
    # Bounds derived from revealed values are MATHEMATICALLY GUARANTEED:
    #   * A: A_final = sum(all_values), values >= 0 → A_final >= sum(revealed)
    #   * D: D_final = max - min, partial range never shrinks → D >= max(r)-min(r)
    #   * C: digital(sum>=K). Once sum(revealed) >= K, C_final == 100
    #     (future values are non-negative; sum only grows).
    # Under truth, all bounds collapse to (fair, fair). The IOC sweep
    # below takes any ask < lower or bid > upper as RISK-FREE profit.
    # ==================================================================
    def _hard_bounds_for(
            self, sym: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return (lower, upper) hard bounds on `sym` settlement.
        None means unbounded on that side.

        v20.13: reads from `_identity_table` (precomputed via
        `_recompute_identity_table`). Falls back to live recompute if
        the table is empty (defensive — fast-precompute keeps it
        populated, but a callable might fire before the first refresh
        e.g. inside __init__)."""
        ib = self._identity_table.get(sym)
        if ib is not None and ib.gen > 0:
            return (ib.lower, ib.upper)
        # Cold-path fallback: build it once on demand.
        self._recompute_identity_table()
        ib = self._identity_table.get(sym)
        if ib is not None:
            return (ib.lower, ib.upper)
        return (None, None)

    # ==================================================================
    # v20.13 — Precomputed cross-market identity table
    #
    # Consolidates ALL deterministic identities we can derive without
    # touching the posterior fair estimate. Each call walks the
    # revealed-values list once, applies symbol bounds, then layers
    # cross-market identities so a tightening on one symbol propagates
    # to others. Cheap (constant per call after revs are summed/maxed
    # /mined once) and called from on_reveal, phase → running, and the
    # fast-precompute loop.
    #
    # Identities (in order of application):
    #   1. A_lower = sum(revealed)                        # values ≥ 0
    #   2. D_lower = max(r) - min(r) for k ≥ 2            # range grows
    #   3. C_lower=C_upper=100  if sum(r) ≥ K             # digital fired
    #   4. (NEW eor_pin) C_lower=C_upper=0
    #         if reveals == n_total AND sum(r) < K        # digital dead
    #   5. (NEW cross_C0) A_upper = K - 1                 # C dead → A < K
    #         when C is pinned at 0
    #   6. (NEW cross_D_A) D_upper = A_upper              # D ≤ max ≤ sum
    #         when A_upper is known (only fires post #5 or under truth)
    #   7. truth: (fair, fair) collapse                   # exact value
    #
    # The output is read by `_hard_bounds_for` (O(1) lookup) and is the
    # exclusive ground truth for `_bounds_arb_sweep`. Any caller that
    # asks "is this price guaranteed to be a winning fill?" should
    # consult this table.
    # ==================================================================
    def _recompute_identity_table(self) -> None:
        revs = list(self.posterior.reveals)
        n_revealed = len(revs)
        n_total = getattr(self, "n_total", None)
        running_sum = sum(revs) if revs else 0
        K = self.c_strike

        new_tbl: Dict[str, IdentityBounds] = {
            s: IdentityBounds(sym=s) for s in self.SYMS
        }

        # --- A: sum(revealed) lower bound ---
        if n_revealed > 0:
            new_tbl["A"].lower = float(running_sum)
            new_tbl["A"].lower_source = "reveals"
        else:
            new_tbl["A"].lower = 0.0
            new_tbl["A"].lower_source = "init"

        # --- B: no deterministic bound without truth ---
        # (lower, upper) stay None — bound_arb skips B pre-truth.

        # --- C: digital(sum >= K) ---
        if K is None:
            new_tbl["C"].lower = 0.0
            new_tbl["C"].upper = 100.0
        else:
            if running_sum >= K:
                new_tbl["C"].lower = 100.0
                new_tbl["C"].upper = 100.0
                new_tbl["C"].lower_source = "sum_ge_K"
                new_tbl["C"].upper_source = "sum_ge_K"
            elif (n_total is not None and n_revealed >= n_total
                  and running_sum < K):
                # END-OF-ROUND pin: no reveals remaining and sum < K
                # → digital settles at 0 with certainty.
                new_tbl["C"].lower = 0.0
                new_tbl["C"].upper = 0.0
                new_tbl["C"].lower_source = "eor_pin"
                new_tbl["C"].upper_source = "eor_pin"
            else:
                new_tbl["C"].lower = 0.0
                new_tbl["C"].upper = 100.0

        # --- D: max(revealed) - min(revealed) lower bound ---
        if n_revealed >= 2:
            new_tbl["D"].lower = float(max(revs) - min(revs))
            new_tbl["D"].lower_source = "reveals"
        else:
            new_tbl["D"].lower = 0.0
            new_tbl["D"].lower_source = "init"

        # --- Cross-market: C pinned at 0 → A_upper = K - 1 ---
        # If C is mathematically forced to 0 (sum < K and no reveals
        # left), then A_final = sum(seq) is strictly less than K. So
        # A_upper ≤ K - 1.
        if (K is not None and new_tbl["C"].upper == 0.0
                and new_tbl["C"].upper_source == "eor_pin"):
            cand = float(K - 1)
            cur = new_tbl["A"].upper
            if cur is None or cur > cand:
                new_tbl["A"].upper = cand
                new_tbl["A"].upper_source = "cross_C0"

        # --- Cross-market: D ≤ max(seq) ≤ sum(seq) = A ---
        # D = max(seq) - min(seq) ≤ max(seq) ≤ sum(seq) = A_final.
        # So if A_upper is bounded, D_upper inherits the same bound.
        a_up = new_tbl["A"].upper
        if a_up is not None:
            cur = new_tbl["D"].upper
            if cur is None or cur > a_up:
                new_tbl["D"].upper = a_up
                new_tbl["D"].upper_source = "cross_D_A"

        # --- Truth overrides: collapse to (fair, fair) for any
        # truth-locked symbol. Done LAST so it strictly overrides any
        # weaker bound from the identities above (truth = exact). ---
        for sym in self.SYMS:
            if self._truth_locked(sym):
                try:
                    fair, _ = self._fair_for(sym)
                    new_tbl[sym].lower = float(fair)
                    new_tbl[sym].upper = float(fair)
                    new_tbl[sym].lower_source = "truth"
                    new_tbl[sym].upper_source = "truth"
                except Exception:
                    pass

        # Stamp generation; install.
        next_gen = self._identity_gen + 1
        for sym in self.SYMS:
            new_tbl[sym].gen = next_gen
        # Detect NEW cross-market / EOR pin tightenings vs prior gen
        # and log them once — useful for confirming the new identities
        # actually fire in real rounds, without spamming every fast
        # loop tick where nothing changed.
        prev = self._identity_table
        for sym in self.SYMS:
            new_ib = new_tbl[sym]
            old_ib = prev.get(sym)
            old_l_src = old_ib.lower_source if old_ib else "init"
            old_u_src = old_ib.upper_source if old_ib else "init"
            new_l_src = new_ib.lower_source
            new_u_src = new_ib.upper_source
            interesting = {"eor_pin", "cross_C0", "cross_D_A"}
            if (new_l_src in interesting and new_l_src != old_l_src) or (
                    new_u_src in interesting and new_u_src != old_u_src):
                lo = (f"{new_ib.lower:.2f}" if new_ib.lower is not None
                      else "None")
                up = (f"{new_ib.upper:.2f}" if new_ib.upper is not None
                      else "None")
                print(f"[v20.13 IDENTITY:{sym}] lower={lo}({new_l_src}) "
                      f"upper={up}({new_u_src})")
        self._identity_table = new_tbl
        self._identity_gen = next_gen

    def identity_table_snapshot(self) -> Dict[str, Dict[str, object]]:
        """Public accessor for the runner / debug commands."""
        out: Dict[str, Dict[str, object]] = {}
        for sym, ib in self._identity_table.items():
            out[sym] = {
                "lower": ib.lower,
                "upper": ib.upper,
                "lower_source": ib.lower_source,
                "upper_source": ib.upper_source,
                "gen": ib.gen,
            }
        return out

    def _bounds_arb_slice_for(self, sym: str) -> int:
        """Per-symbol slice cap for bounds-arb. Larger than cross-arb
        slice because bounds-arb fills are risk-free."""
        base = self._sweep_slice_for(sym)
        mult = self.cfg.bounds_arb_slice_mult
        return max(1, int(round(base * mult)))

    def _bounds_arb_budget_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.bounds_arb_qty_budget_a
        if sym == "B":
            return self.cfg.bounds_arb_qty_budget_b
        if sym == "C":
            return self.cfg.bounds_arb_qty_budget_c
        return self.cfg.bounds_arb_qty_budget_d

    def _bounds_arb_sweep(self, source: str) -> int:
        """Scan each symbol's book for prices violating hard bounds.
        Asks priced < (lower - min_edge) → IOC buy; bids > (upper +
        min_edge) → IOC sell. NO post-reveal lockout / NO min-reveals
        gate — bound-violations are deterministic +EV regardless of
        timing or fair-estimate state."""
        if not self.cfg.bounds_arb_enabled:
            return 0
        if not self._can_post():
            return 0
        if self._in_lockout():
            return 0
        if self._in_park_window():
            return 0

        pad = self.cfg.sweep_position_pad
        edge = self.cfg.bounds_arb_min_edge_ticks
        fired_total = 0

        for sym in self.SYMS:
            s = self.state[sym]
            book = s.book
            if not book:
                continue

            lower, upper = self._hard_bounds_for(sym)
            if lower is None and upper is None:
                continue

            budget = self._bounds_arb_budget_for(sym)
            used = self._bounds_arb_qty_used.get(sym, 0)
            if budget > 0 and used >= budget:
                continue
            room_budget = budget - used if budget > 0 else 10**9

            base_slice = self._bounds_arb_slice_for(sym)
            max_slice_buy = self._inventory_throttle(sym, "buy", base_slice)
            max_slice_sell = self._inventory_throttle(sym, "sell", base_slice)
            eff_cap = self._effective_position_cap(sym)

            # BUY asks priced strictly below lower bound (with edge).
            if lower is not None and max_slice_buy > 0:
                threshold = lower - edge
                room_buy = (eff_cap - pad) - s.position
                if room_buy > 0:
                    taken = 0
                    for lvl in (book.get("asks") or []):
                        if room_buy <= 0 or room_budget <= 0:
                            break
                        if taken >= max_slice_buy:
                            break
                        px = lvl.get("price")
                        size = int(lvl.get("qty") or 0)
                        if px is None or size <= 0:
                            continue
                        if px > threshold:
                            break  # asks sorted ascending — rest are worse
                        if self._is_spoof_like(sym, "ask", int(px)):
                            continue
                        want = min(max_slice_buy - taken, room_buy,
                                   size, room_budget)
                        if want <= 0:
                            break
                        if self._ioc(sym, "buy", want, int(px)):
                            taken += want
                            room_buy -= want
                            room_budget -= want
                            self._bounds_arb_qty_used[sym] = (
                                self._bounds_arb_qty_used.get(sym, 0)
                                + want)
                            fired_total += 1
                            print(f"[v20.11 BNDS({source}):{sym}] BUY "
                                  f"{want}@{px} lower={lower:.2f} "
                                  f"floor_edge={lower - px:.2f}t "
                                  f"pos={s.position}")
                        else:
                            break

            # SELL bids priced strictly above upper bound (with edge).
            if upper is not None and max_slice_sell > 0:
                threshold = upper + edge
                room_sell = s.position - (-eff_cap + pad)
                if room_sell > 0:
                    taken = 0
                    for lvl in (book.get("bids") or []):
                        if room_sell <= 0 or room_budget <= 0:
                            break
                        if taken >= max_slice_sell:
                            break
                        px = lvl.get("price")
                        size = int(lvl.get("qty") or 0)
                        if px is None or size <= 0:
                            continue
                        if px < threshold:
                            break  # bids sorted descending — rest are worse
                        if self._is_spoof_like(sym, "bid", int(px)):
                            continue
                        want = min(max_slice_sell - taken, room_sell,
                                   size, room_budget)
                        if want <= 0:
                            break
                        if self._ioc(sym, "sell", want, int(px)):
                            taken += want
                            room_sell -= want
                            room_budget -= want
                            self._bounds_arb_qty_used[sym] = (
                                self._bounds_arb_qty_used.get(sym, 0)
                                + want)
                            fired_total += 1
                            print(f"[v20.11 BNDS({source}):{sym}] SELL "
                                  f"{want}@{px} upper={upper:.2f} "
                                  f"floor_edge={px - upper:.2f}t "
                                  f"pos={s.position}")
                        else:
                            break

        if fired_total:
            self._bounds_arb_count += fired_total
        return fired_total

    # ==================================================================
    # v20.1: Precomputed arbitrage table — for each symbol, store the
    # exact max-buy-px and min-sell-px implied by current fair + the
    # symbol's MIN edge (penny_min_edge) capped against the symbol's
    # max_ioc_distance. The fast-arb scanner reads these without
    # recomputing fair on every event.
    #
    # Updated on every reveal (and on phase → running). The fast
    # precompute loop also refreshes it as a safety net.
    # ==================================================================
    def _recompute_arb_table(self) -> None:
        new_tbl: Dict[str, Dict[str, float]] = {}
        for sym in self.SYMS:
            try:
                fair, sigma = self._fair_for(sym)
            except Exception:
                continue
            s = self.state.get(sym)
            if s is None:
                continue
            tick = s.tick
            min_edge = self._min_edge_for(sym)
            sweep_edge = self._sweep_edge_for(sym)
            arb_edge = self._cross_arb_edge_for(sym)
            # Buy at or below: floor(fair - min_edge / tick) * tick.
            buy_at_or_below = (
                (int(math.floor(fair - min_edge))) // tick) * tick
            sell_at_or_above_raw = int(math.ceil(fair + min_edge))
            sell_at_or_above = (
                ((sell_at_or_above_raw + tick - 1) // tick) * tick)
            # Hard buy/sell — i.e. the "fire IOC immediately" level
            # using the cross-arb edge (which already factors in the
            # early-round multiplier).
            hard_buy = (
                (int(math.floor(fair - arb_edge))) // tick) * tick
            hard_sell = (
                ((int(math.ceil(fair + arb_edge)) + tick - 1) // tick) * tick)
            new_tbl[sym] = {
                "fair": float(fair),
                "sigma": float(sigma),
                "buy_at_or_below": float(buy_at_or_below),
                "sell_at_or_above": float(sell_at_or_above),
                "hard_buy": float(hard_buy),
                "hard_sell": float(hard_sell),
                "min_edge": float(min_edge),
                "sweep_edge": float(sweep_edge),
                "arb_edge": float(arb_edge),
                "truth_locked": 1.0 if self._truth_locked(sym) else 0.0,
                "gen": float(self._arb_gen + 1),
            }
        self._arb_table = new_tbl
        self._arb_gen += 1

    def arb_table_snapshot(self) -> Dict[str, Dict[str, float]]:
        """Public accessor for the runner / debug commands."""
        return {k: dict(v) for k, v in self._arb_table.items()}

    # ==================================================================
    # Live thresholds (FAST precompute) — extend to 4 symbols.
    # ==================================================================
    def _build_live_thresholds(self) -> None:
        t0 = time.perf_counter()
        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks

        # Refresh truth-derived C/D fairs whenever the truth oracle
        # caches a new sequence (cheap memoization on the seq tuple).
        self._refresh_cd_truth_cache()
        # v20.7: drain stale tape entries every fast-loop tick (~50ms).
        # Keeps _tape_imbalance / dime gates O(1) and accurate even in a
        # quiet market where no new prints arrive to trigger a trim
        # inside on_trade.
        self._decay_tape_all()

        fair_a, sigma_a = self.fair_a()
        fair_b, sigma_b = self.fair_b()
        fair_c, sigma_c = self.fair_c()
        fair_d, sigma_d = self.fair_d()
        in_flat = self._in_flatten_bias_window(sigma_a)

        new_live: Dict[str, _LiveThresholds] = {}
        per_sym = (
            ("A", fair_a, sigma_a, self.cfg.sweep_edge_ticks),
            ("B", fair_b, sigma_b, self.cfg.sweep_edge_ticks),
            ("C", fair_c, sigma_c, self.cfg.sweep_edge_ticks_c),
            ("D", fair_d, sigma_d, self.cfg.sweep_edge_ticks_d),
        )
        for sym, fair, sigma, sweep_edge in per_sym:
            tick = self.state[sym].tick
            lift_to = ((int(math.floor(fair - sweep_edge))) // tick) * tick
            hit_to_raw = int(math.ceil(fair + sweep_edge))
            hit_to = ((hit_to_raw + tick - 1) // tick) * tick

            relaxed = max(flat_min, sweep_edge - flat_reduction)
            lift_to_flat = ((int(math.floor(fair - relaxed))) // tick) * tick
            hit_to_flat_raw = int(math.ceil(fair + relaxed))
            hit_to_flat = (((hit_to_flat_raw + tick - 1) // tick) * tick)

            # v20.2: extend flatten-bias to C/D during pre-first-reveal.
            # User directive: be FLAT going into reveal 1 across all
            # four books. Once reveal 1 lands, C/D drop back to non-flat
            # (the sigma-trigger path doesn't apply to them since fair
            # is exact under truth).
            sym_in_flat = in_flat if (sym in ("A", "B")
                                       or self._reveal_count == 0) else False

            new_live[sym] = _LiveThresholds(
                sym=sym, fair=fair, sigma=sigma,
                lift_to=lift_to, hit_to=hit_to,
                lift_to_flat=lift_to_flat, hit_to_flat=hit_to_flat,
                in_flatten_window=sym_in_flat,
                gen=self._live_gen + 1)

        with self._live_lock:
            self._live_thresholds = new_live
            self._live_gen += 1
            self._fast_precompute_run_count += 1
            self._last_fast_precompute_us = (
                time.perf_counter() - t0) * 1e6
        # v20.1: refresh the precomputed arb table alongside live
        # thresholds. Same ~50ms cadence; cheap (4 floors + 4 ceils).
        self._recompute_arb_table()
        # v20.13: identity bounds also rebuilt on the fast loop so
        # bounds-arb sweeps off the trade tape (no reveal trigger)
        # still pick up tightenings that happen between reveals (e.g.
        # truth lock flipping mid-round refreshes (fair, fair)).
        try:
            self._recompute_identity_table()
        except Exception as e:
            print(f"[v20.13 IDENTITY-FAST-ERR] "
                  f"{type(e).__name__}: {e}")

    # ==================================================================
    # IOC — fully reimplement (NOT super()) because v18's _ioc hard-codes
    # the max-distance check against `fair_a()/fair_b()` only, which
    # would route C/D through `fair_b()` and reject every legitimate
    # C/D snipe.
    #
    # Order of checks mirrors v18 1801-1840:
    #   1. qty>0, no lockout
    #   2. can_post (game phase running)
    #   3. per-symbol max_ioc_distance vs the right fair (fair_for(sym))
    #   4. anti-spoof extreme-tick linger gate
    #   5. rate-limit can_send_now / sliding-window can_send(sym,side,qty)
    #   6. register_send (consumes a token)
    #   7. clamp qty to position_limit
    #   8. REST call (buy_ioc / sell_ioc) with 403 / rate-limit handling
    # ==================================================================
    def _ioc(self, sym: str, side: str, qty: int, price: int) -> bool:
        if qty <= 0 or self._in_lockout():
            return False
        if not self._can_post():
            return False

        max_dist = self._max_ioc_dist_for(sym)
        if max_dist > 0:
            fair, _ = self._fair_for(sym)
            if abs(price - fair) > max_dist:
                return False

        # Anti-spoof extreme-tick linger gate (currently only C).
        if self._is_spoof_like(sym, "ask" if side == "buy" else "bid", price):
            return False

        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        s = self.state[sym]
        cap_side = "bid" if side == "buy" else "ask"
        if not self._can_send(sym, cap_side, qty):
            return False
        self._register_send()
        # v20.10: IOC paths must respect the RAMPED (effective) cap, not
        # the hard position_limit. Previously cross-arb + sweep_post_reveal
        # blew through to ±100 B / ±50 C in the first 15-25s on every
        # reveal — then the rest of the round was inert (no room to MM).
        # _effective_position_cap returns full L for A/D under truth
        # (bypass), and ramped L for B/C.
        eff_cap = self._effective_position_cap(sym)
        if side == "buy" and (s.position + qty) > eff_cap:
            qty = eff_cap - s.position
            if qty <= 0:
                return False
        if side == "sell" and (s.position - qty) < -eff_cap:
            qty = s.position + eff_cap
            if qty <= 0:
                return False
        fn = self.c.buy_ioc if side == "buy" else self.c.sell_ioc
        try:
            fn(sym, price, qty)
            return True
        except Exception as e:
            msg = str(e).lower()
            is_403 = "403" in msg or "forbidden" in msg
            if is_403:
                self._handle_forbidden("IOC", sym, side, price, e)
            elif "limit" not in msg and "position" not in msg:
                print(f"[v20 IOC:{sym}/{side}@{price}x{qty}] "
                      f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # Anti-spoofer / extreme-tick linger check.
    # For C: quotes at ask < c_extreme_low or bid > c_extreme_high
    # must have been visible for >= c_linger_min_ms before we lift.
    # For B: HVALK is the spoofer; max_ioc_distance handles most of
    # the bait. Linger-check is a soft second line of defense for any
    # extreme price >40t off fair.
    # ==================================================================
    def _is_spoof_like(self, sym: str, side: str, price: int) -> bool:
        """Return True iff the price level is suspect (likely
        spoofer/wide-quote trap) and we should NOT lift it.

        `side` = the side we'd be HITTING ("ask" → we're buying at
        ask; "bid" → we're selling at bid)."""
        if sym == "C":
            # C extreme-tick gate (binary 0-100 boundary).
            if side == "ask" and price >= self.cfg.c_extreme_low:
                return False
            if side == "bid" and price <= self.cfg.c_extreme_high:
                return False
            key = (side, price)
            now = time.time()
            first_seen = self._c_linger_seen.get(key)
            if first_seen is None:
                self._c_linger_seen[key] = now
                return True
            return (now - first_seen) * 1000.0 < self.cfg.c_linger_min_ms

        if sym == "B":
            # B (HVALK spoofer) — extreme-offset linger gate.
            fair_b, _ = self.fair_b()
            if abs(price - fair_b) < self.cfg.b_extreme_offset:
                return False
            key = (side, price)
            now = time.time()
            first_seen = self._b_linger_seen.get(key)
            if first_seen is None:
                self._b_linger_seen[key] = now
                return True
            return (now - first_seen) * 1000.0 < self.cfg.b_linger_min_ms

        # A / D: max_ioc_distance is the only gate.
        return False

    # ==================================================================
    # Endgame tick — parent v19 handles A (last reveal) + B (per-tick).
    # C/D under truth are exact from t=0 → no special endgame is
    # needed; the regular MM + cross-arb paths already capture all
    # available edge. Defer entirely to parent.
    # ==================================================================
    def _endgame_tick(self) -> None:
        super()._endgame_tick()

    # ==================================================================
    # Phase / reveal hooks — extend to refresh C/D truth cache + sweep
    # C/D at reveal time (v18 only sweeps A/B).
    # ==================================================================
    def on_reveal(self, value: int) -> None:
        # v20.5: pre-kick parallel C/D sweep BEFORE super(). super
        # handles A/B sequentially via v18 (unchanged). The C/D
        # futures run on _ioc_executor concurrently with v18's
        # A/B work, hiding C/D's reveal-to-IOC latency behind A/B.
        parallel_kicked = False
        if (self.cfg.reveal_sweep_parallel_enabled
                and not self._in_lockout()):
            try:
                self._kick_parallel_reveal_sweep()
                parallel_kicked = True
            except Exception as e:
                print(f"[v20.5 PSWEEP-KICK-ERR] "
                      f"{type(e).__name__}: {e}")

        super().on_reveal(value)
        # v20.1: stamp last-reveal time for the cross-arb post-reveal
        # lockout, and immediately recompute the precomputed arb table
        # so cross-arb at source="reveal" reads fresh thresholds.
        self._last_reveal_t = time.monotonic()
        self._refresh_cd_truth_cache()
        self._recompute_arb_table()
        # v20.13: rebuild identity table FIRST so the bounds-arb sweep
        # below sees freshest deterministic bounds (new reveal tightens
        # A_lower / D_lower and may flip C to a pin).
        try:
            self._recompute_identity_table()
        except Exception as e:
            print(f"[v20.13 IDENTITY-REVEAL-ERR] "
                  f"{type(e).__name__}: {e}")
        # v20.11: a new reveal tightens hard bounds (A_lower grows, D
        # lower may grow, C may pin to 100). Fire bounds-arb FIRST —
        # bound-violations are risk-free and shouldn't wait for the
        # 50ms cross-arb lockout to expire.
        if self.cfg.bounds_arb_enabled:
            try:
                self._bounds_arb_sweep("reveal")
            except Exception as e:
                print(f"[v20.11 BNDS-REVEAL-ERR] "
                      f"{type(e).__name__}: {e}")
        # v20.3: post-reveal C/D sweep. v18's _sweep_post_reveal in
        # super().on_reveal only fired for A and B. Truth-locked C/D
        # have exact fair — any stale ask < fair_c-edge or bid >
        # fair_c+edge is a free hit.
        # v20.4: skip if parallel sweep already covered C/D.
        if self.cfg.reveal_sweep_cd_enabled and not parallel_kicked:
            for sym in ("C", "D"):
                try:
                    self._sweep_post_reveal_v20(sym)
                except Exception as e:
                    print(f"[v20.3 SWEEP:{sym}-ERR] "
                          f"{type(e).__name__}: {e}")
        # v20.4: drain futures (in case super never reached the
        # _sweep_post_reveal call — e.g. precompute miss).
        if parallel_kicked:
            self._drain_reveal_sweep_futures()
        # v20.14: park window left our quotes far from market (offset
        # 30t). Dime defense normally fires on the next book event,
        # which can be 100-500ms away. Trigger it immediately on
        # reveal so we re-establish BBO presence without waiting.
        if self.cfg.dime_defense_enabled and not self._in_park_window():
            for sym in self.SYMS:
                # Reset throttle so this synthetic call isn't gated by
                # a recent dime in the prior cycle.
                self.state[sym].last_dime_defense_t = 0.0
                try:
                    self._maybe_dime_defense(sym)
                except Exception as e:
                    print(f"[v20.14 DIME-REVEAL:{sym}-ERR] "
                          f"{type(e).__name__}: {e}")

    # ==================================================================
    # v20.4: parallel reveal-time sweep. Submits one future per
    # symbol to the parent's _ioc_executor, then either joins on
    # demand (via the _sweep_post_reveal override) or drains
    # eagerly at on_reveal exit.
    # ==================================================================
    def _kick_parallel_reveal_sweep(self) -> None:
        # v20.5: only C/D. A/B sweep stays on v18's sync path inside
        # super().on_reveal so v18's strategy is preserved exactly.
        self._reveal_sweep_futures = {}
        for sym in ("C", "D"):
            try:
                fut = self._ioc_executor.submit(
                    self._sweep_post_reveal_v20, sym)
                self._reveal_sweep_futures[sym] = fut
            except Exception as e:
                print(f"[v20.5 PSWEEP-SUBMIT:{sym}-ERR] "
                      f"{type(e).__name__}: {e}")

    def _drain_reveal_sweep_futures(self) -> None:
        if not self._reveal_sweep_futures:
            return
        timeout = self.cfg.reveal_sweep_parallel_timeout_sec
        for sym, fut in list(self._reveal_sweep_futures.items()):
            try:
                fut.result(timeout=timeout)
            except Exception as e:
                print(f"[v20.4 PSWEEP-DRAIN:{sym}-ERR] "
                      f"{type(e).__name__}: {e}")
        self._reveal_sweep_futures = {}

    # v20.5: A/B → v18 sync sweep, untouched. C/D → join future
    # kicked by _kick_parallel_reveal_sweep (covers the case where
    # super() reaches a sweep call for C/D too; in practice v18
    # only calls this for A/B so the future path is mostly drained
    # by _drain_reveal_sweep_futures at on_reveal exit).
    def _sweep_post_reveal(self, sym: str, lift_to: int, hit_to: int,
                           fair: float) -> int:
        if sym in ("A", "B"):
            return super()._sweep_post_reveal(sym, lift_to, hit_to, fair)
        fut = (self._reveal_sweep_futures.get(sym)
               if self._reveal_sweep_futures else None)
        if fut is not None:
            self._reveal_sweep_futures.pop(sym, None)
            try:
                return int(fut.result(
                    timeout=self.cfg.reveal_sweep_parallel_timeout_sec))
            except Exception as e:
                print(f"[v20.5 PSWEEP-JOIN:{sym}-ERR] "
                      f"{type(e).__name__}: {e}")
                return 0
        return super()._sweep_post_reveal(sym, lift_to, hit_to, fair)

    # ==================================================================
    # v20.3: per-symbol reveal sweep with truth-derived fair.
    # Mirrors v18 _sweep_post_reveal (line 1100) but uses
    # _max_ioc_dist_for(sym) and _sweep_edge_for(sym) so C/D's much
    # wider acceptable distance / smaller required edge are honored.
    # ==================================================================
    def _sweep_post_reveal_v20(self, sym: str) -> int:
        if self._in_lockout():
            return 0
        if not self._can_post():
            return 0
        s = self.state[sym]
        book = s.book
        if not book:
            return 0
        fair, _ = self._fair_for(sym)
        edge = self._sweep_edge_for(sym)
        tick = s.tick
        lift_to = ((int(math.floor(fair - edge))) // tick) * tick
        hit_to_raw = int(math.ceil(fair + edge))
        hit_to = ((hit_to_raw + tick - 1) // tick) * tick
        # v20.4: per-symbol slice, inventory-aware throttle.
        base_slice = self._sweep_slice_for(sym)
        max_slice_buy = self._inventory_throttle(sym, "buy", base_slice)
        max_slice_sell = self._inventory_throttle(sym, "sell", base_slice)
        pad = self.cfg.sweep_position_pad
        max_dist = self._max_ioc_dist_for(sym)
        fired = 0
        # v20.10: respect ramped cap on reveal-sweep IOC path.
        eff_cap = self._effective_position_cap(sym)

        # BUY asks priced <= lift_to.
        room_buy = (eff_cap - pad) - s.position
        if room_buy > 0 and max_slice_buy > 0:
            max_slice = max_slice_buy
        else:
            max_slice = 0
        if max_slice > 0 and room_buy > 0:
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px > lift_to:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "ask", int(px)):
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, int(px)):
                    taken += want
                    room_buy -= want
                    fired += 1
                    print(f"[v20.3 SWP:{sym}] BUY {want}@{px} "
                          f"fair={fair:.1f} lift_to={lift_to}")
                else:
                    break

        # SELL bids priced >= hit_to.
        room_sell = s.position - (-eff_cap + pad)
        if room_sell > 0 and max_slice_sell > 0:
            max_slice = max_slice_sell
        else:
            max_slice = 0
        if max_slice > 0 and room_sell > 0:
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px < hit_to:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "bid", int(px)):
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, int(px)):
                    taken += want
                    room_sell -= want
                    fired += 1
                    print(f"[v20.3 SWP:{sym}] SELL {want}@{px} "
                          f"fair={fair:.1f} hit_to={hit_to}")
                else:
                    break

        if fired:
            self._reveal_sweep_cd_count += fired
        return fired

    # ==================================================================
    # v20.7: public + private tape pipeline.
    #
    # PIPELINE (private-first by event ordering — public is ~15ms behind):
    #   on_fill_event  →  _ingest_private_fill   →  _record_tape_event
    #   on_trade       →  _record_tape_event(public)
    #   fast loop tick →  _decay_tape_all (every 50ms — trim stale)
    #
    # Each ingest is O(1) for the hot path (last-price update + counter
    # bump); only the stale-trim walks the deque, and the fast loop
    # spreads that cost across the round (≤ few µs per tick).
    #
    # Aggressor derivation from a private fill:
    #   maker side=buy  ↔ counterparty was the seller (agg=sell)
    #   maker side=sell ↔ counterparty was the buyer  (agg=buy)
    #   taker side=buy  ↔ we were the buyer-aggressor (agg=buy)
    #   taker side=sell ↔ we were the seller-aggressor (agg=sell)
    #
    # Dedupe key = trade_id (matches across private fill + public print).
    # ==================================================================
    @staticmethod
    def _aggressor_from_private(side: str, liquidity: str) -> Optional[str]:
        if side not in ("buy", "sell"):
            return None
        if liquidity == "maker":
            return "sell" if side == "buy" else "buy"
        # Default to taker semantics for any non-maker liquidity flag
        # (server sometimes omits or uses other strings).
        return side

    def _record_tape_event(self, sym: str, agg: str, price: int,
                           qty: int, trade_id: Optional[int],
                           source: str) -> None:
        """Unified ingest. Idempotent on `trade_id` — the second arrival
        (public following private) is a no-op except as a freshness
        confirmation."""
        if not self.cfg.tape_enabled:
            return
        if sym not in self.SYMS:
            return
        if agg not in ("buy", "sell") or price is None:
            return
        ts = self._tape[sym]
        now = time.time()
        # Dedupe — same trade_id from public/private fills only counts once.
        if trade_id is not None and trade_id in ts.seen_trade_ids:
            return
        # Append + counter bump.
        ts.recent.append((now, agg, price, int(qty)))
        if agg == "buy":
            ts.buy_count += 1
            ts.last_buy_agg_price = price
            ts.last_buy_agg_t = now
        else:
            ts.sell_count += 1
            ts.last_sell_agg_price = price
            ts.last_sell_agg_t = now
        if trade_id is not None:
            ts.seen_trade_ids[trade_id] = now
        # Defensive caps — the fast-loop decay handles steady-state, but
        # bursty markets could overrun before the next 50ms tick.
        cap = self.cfg.tape_max_prints
        if len(ts.recent) > cap:
            self._trim_tape(sym, now)
        if source == "public":
            self._tape_print_count[sym] += 1

    def on_trade(self, msg: dict) -> None:
        if not self.cfg.tape_enabled:
            return
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        agg = msg.get("aggressor")
        try:
            price = int(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        tid = msg.get("trade_id")
        try:
            tid_int = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid_int = None
        self._record_tape_event(sym, agg, price, qty, tid_int, "public")
        # v20.11: a public print means the book just shifted. The new
        # top-of-book ask/bid might violate a hard bound (e.g. an
        # exhausted ask layer revealed a deeper stale ask below
        # A_lower). Bounds-arb has its own slice budget so this is
        # cheap even on busy tape.
        if self.cfg.bounds_arb_enabled:
            try:
                self._bounds_arb_sweep("trade")
            except Exception as e:
                print(f"[v20.11 BNDS-TRADE-ERR] "
                      f"{type(e).__name__}: {e}")

    def _ingest_private_fill(self, msg: dict) -> None:
        """Pre-ingest a private fill into the tape ~15ms ahead of its
        public twin. The public arrival is deduped by trade_id."""
        if not self.cfg.tape_enabled:
            return
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        try:
            price = int(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        agg = self._aggressor_from_private(
            msg.get("side"), msg.get("liquidity") or "")
        if agg is None:
            return
        tid = msg.get("trade_id")
        try:
            tid_int = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid_int = None
        self._record_tape_event(sym, agg, price, qty, tid_int, "private")

    def _trim_tape(self, sym: str, now: float) -> None:
        """Drop entries older than tape_window_sec and decrement counters.
        Called from fast loop (steady state) and as a defensive top-cap
        guard inside _record_tape_event."""
        ts = self._tape[sym]
        cutoff = now - self.cfg.tape_window_sec
        recent = ts.recent
        # Stale window-trim — also walks counters down.
        while recent and recent[0][0] < cutoff:
            _, a, _, _ = recent.popleft()
            if a == "buy":
                ts.buy_count -= 1
            else:
                ts.sell_count -= 1
        cap = self.cfg.tape_max_prints
        # Hard-cap guard if a burst slipped past the time-window trim.
        while len(recent) > cap:
            _, a, _, _ = recent.popleft()
            if a == "buy":
                ts.buy_count -= 1
            else:
                ts.sell_count -= 1
        # Dedupe-set GC. Trade IDs older than 2× tape_window_sec can't
        # possibly arrive again and bloat the map otherwise.
        gc_cutoff = now - 2.0 * self.cfg.tape_window_sec
        if ts.seen_trade_ids:
            # Snapshot keys; OK to mutate during iteration when using list.
            for k in [k for k, t in ts.seen_trade_ids.items()
                      if t < gc_cutoff]:
                ts.seen_trade_ids.pop(k, None)

    def _decay_tape_all(self) -> None:
        """Trim stale tape entries for every symbol. Called by the fast
        precompute loop (50ms cadence) — keeps _tape_imbalance O(1) reads
        accurate even in a quiet market."""
        if not self.cfg.tape_enabled:
            return
        now = time.time()
        for sym in self.SYMS:
            self._trim_tape(sym, now)

    def _tape_imbalance(self, sym: str) -> Optional[str]:
        """O(1) read off the running counters. The fast loop keeps them
        fresh; we never walk the deque on the MM hot path."""
        if not self.cfg.tape_imbalance_enabled:
            return None
        ts = self._tape[sym]
        total = ts.buy_count + ts.sell_count
        if total < self.cfg.tape_imbalance_min_prints:
            return None
        frac = self.cfg.tape_imbalance_frac
        if ts.buy_count >= frac * total:
            return "buy"
        if ts.sell_count >= frac * total:
            return "sell"
        return None

    def _tape_dime_prices(
        self, sym: str, fair: float,
        bid_px: Optional[int], ask_px: Optional[int]
    ) -> Tuple[Optional[int], Optional[int]]:
        """Tighten bid/ask toward the last-seen tape prints, but only if
        still ≥ min_edge from fair and a tightening relative to the
        baseline quote (we never widen here)."""
        if not self.cfg.tape_dime_enabled:
            return bid_px, ask_px
        ts = self._tape[sym]
        now = time.time()
        s = self.state[sym]
        tick = s.tick
        ub = self._upper_bound(sym)
        min_edge = self._min_edge_for(sym)
        fresh = self.cfg.tape_freshness_sec

        # Dime the bid: sellers just hit a bid at last_sell_agg_price.
        # Posting at +1 tick is one step better than the consumed level.
        if (ts.last_sell_agg_price is not None and
                (now - ts.last_sell_agg_t) <= fresh):
            cand = ts.last_sell_agg_price + tick
            # +EV gate: must clear min_edge below fair.
            if cand <= int(fair) - min_edge and cand >= 1:
                if bid_px is None or cand > bid_px:
                    bid_px = min(cand, ub - 1)
                    self._tape_dime_count[sym] += 1

        # Dime the ask: buyers just lifted ask at last_buy_agg_price.
        if (ts.last_buy_agg_price is not None and
                (now - ts.last_buy_agg_t) <= fresh):
            cand = ts.last_buy_agg_price - tick
            if cand >= int(fair) + min_edge and cand >= 1:
                if ask_px is None or cand < ask_px:
                    ask_px = min(cand, ub)
                    self._tape_dime_count[sym] += 1

        return bid_px, ask_px

    def _tape_wide_offset_for(self, sym: str) -> int:
        return getattr(self.cfg,
                       f"tape_wide_offset_ticks_{sym.lower()}",
                       5)

    def _tape_imbalance_skew(
        self, sym: str, fair: float,
        bid_px: Optional[int], ask_px: Optional[int]
    ) -> Tuple[Optional[int], Optional[int]]:
        """When the tape is one-sided, widen on the DEPLETED side so a
        forced future trade has to reach further in. With truth-known
        fair, those forced fills are guaranteed +EV by `wide_offset`.

        v20.9: clamp widened quote to `best_*_X + 1 tick`. Without this
        clamp we sat 5+ ticks behind BBO during one-sided tapes and got
        dimed out by every other bot, going silent for 30-60s while the
        tape stayed imbalanced. Floor at fair±min_edge to stay +EV."""
        sig = self._tape_imbalance(sym)
        if sig is None:
            return bid_px, ask_px
        s = self.state[sym]
        tick = s.tick
        ub = self._upper_bound(sym)
        offset = self._tape_wide_offset_for(sym) * tick
        min_edge = self._min_edge_for(sym)
        book = s.book or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None

        # Buyers dominating → asks getting eaten → ask side depleted.
        # Push our ask higher (capture a richer fill if more buyers come).
        if sig == "buy":
            target = int(math.ceil(fair)) + offset
            target = min(target, ub)
            # BBO clamp: never sit more than 1 tick behind best ask.
            if best_ask is not None:
                cap = best_ask + tick
                floor = int(math.ceil(fair)) + min_edge
                cap = max(cap, floor)
                target = min(target, cap)
            if ask_px is None or target > ask_px:
                ask_px = target
                self._tape_widen_count[sym] += 1
            return bid_px, ask_px

        # Sellers dominating → bids getting eaten → bid side depleted.
        # Drop our bid lower (capture a cheaper fill if more sellers come).
        if sig == "sell":
            target = int(math.floor(fair)) - offset
            target = max(target, 1)
            # BBO clamp: never sit more than 1 tick below best bid.
            if best_bid is not None:
                cap = best_bid - tick
                floor = int(math.floor(fair)) - min_edge
                cap = min(cap, floor)
                target = max(target, cap)
            if bid_px is None or target < bid_px:
                bid_px = target
                self._tape_widen_count[sym] += 1
            return bid_px, ask_px

        return bid_px, ask_px

    # ==================================================================
    # v20.3: Hit-and-retreat. After every fill (maker OR taker), repost
    # the OPPOSITE side at a tight scalp-out price. Throttled per-symbol
    # so a multi-level taker burst doesn't fire 5 quotes.
    # ==================================================================
    def on_fill_event(self, msg: dict) -> None:
        # v20.7: private fills are the 15ms-leading edge of the tape.
        # Ingest BEFORE super() so the cross-arb / inter-sweep paths
        # under super()._try_cross_arb("fill") can read fresh tape
        # state (last_*_agg_price, imbalance counters) that won't be
        # visible on the public WS for another ~15ms.
        try:
            self._ingest_private_fill(msg)
        except Exception as e:
            print(f"[v20.7 TAPE-PRIV:ERR] {type(e).__name__}: {e}")
        super().on_fill_event(msg)
        if not self.cfg.hit_and_retreat_enabled:
            return
        sym = msg.get("symbol")
        side = msg.get("side")
        if sym not in self.SYMS:
            return
        if side not in ("buy", "sell"):
            return
        try:
            self._scalp_out_quote(sym, side)
        except Exception as e:
            print(f"[v20.3 SCALP:{sym}-ERR] {type(e).__name__}: {e}")

    def _scalp_out_quote(self, sym: str, fill_side: str) -> None:
        """Post the OPPOSITE side at fair ± min_edge (stepped inside
        the BBO if it improves the price). This is the round-trip
        capture on MM fills — we just bought, now post a tight sell
        to bank the spread."""
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        if abs(s.position) < self.cfg.hit_and_retreat_min_abs_pos:
            return
        now = time.time()
        last = self._last_scalp_out_t.get(sym, 0.0)
        if (now - last) < self.cfg.hit_and_retreat_throttle_sec:
            return
        self._last_scalp_out_t[sym] = now

        fair, _ = self._fair_for(sym)
        tick = s.tick
        min_edge = self._min_edge_for(sym)
        inside = self.cfg.hit_and_retreat_inside_ticks * tick
        ub = self._upper_bound(sym)
        book = s.book or {}

        # If we just bought, scalp out via ASK (sell tight).
        if fill_side == "buy":
            target = int(math.ceil(fair + min_edge))
            target = ((target + tick - 1) // tick) * tick
            asks = book.get("asks") or []
            if asks:
                best_ask = int(asks[0].get("price") or 0)
                if best_ask > 0 and target >= best_ask:
                    stepped = best_ask - inside
                    if stepped >= fair + 1:
                        target = stepped
            if target <= 0 or target > ub:
                return
            qty = self._quote_size(sym, "ask")
            if qty <= 0:
                return
            print(f"[v20.3 H&R:{sym}/ask] post {qty}@{target} "
                  f"(post-buy, fair={fair:.2f})")
            self._apply_side(sym, "ask", int(target), qty)
        else:  # sold → scalp out via BID
            target = int(math.floor(fair - min_edge))
            target = (target // tick) * tick
            bids = book.get("bids") or []
            if bids:
                best_bid = int(bids[0].get("price") or 0)
                if best_bid > 0 and target <= best_bid:
                    stepped = best_bid + inside
                    if stepped <= fair - 1:
                        target = stepped
            if target < tick:
                return
            qty = self._quote_size(sym, "bid")
            if qty <= 0:
                return
            print(f"[v20.3 H&R:{sym}/bid] post {qty}@{target} "
                  f"(post-sell, fair={fair:.2f})")
            self._apply_side(sym, "bid", int(target), qty)

    # ==================================================================
    # v20.3: Scalp-out at cap — keep MM alive after position saturates.
    # Wraps the per-symbol quote math; if the inventory-reducing side
    # came back None (skip_negative_ev / cap gate), force a quote AT
    # fair so we recycle the position via maker fills instead of sitting
    # frozen at the cap until settlement.
    # ==================================================================
    def _quote_prices(self, sym: str, fair: float, position: int,
                      mode: str) -> Tuple[Optional[int], Optional[int]]:
        if sym in ("A", "B"):
            bid_px, ask_px = super()._quote_prices(
                sym, fair, position, mode)
            # v20.5: C→A delta-hedge skew. C is a digital on A above
            # strike; long C ≡ already long delta on A. Skew A's MM
            # the same way we would to drain that delta. Only active
            # in mid fair_c zone where gamma matters.
            if sym == "A" and mode == "normal":
                bid_px, ask_px = self._apply_ca_delta_skew(
                    fair, bid_px, ask_px)
        else:
            bid_px, ask_px = self._quote_prices_cd(
                sym, fair, position, mode)
        # v20.7: tape-driven adjustments. Park mode keeps quotes far
        # from the market so the modify-replace primary stays alive —
        # we must not retighten or widen during park.
        if mode != "park" and self.cfg.tape_enabled:
            bid_px, ask_px = self._tape_dime_prices(
                sym, fair, bid_px, ask_px)
            bid_px, ask_px = self._tape_imbalance_skew(
                sym, fair, bid_px, ask_px)
        # v20.14: inventory-aware flattening shift (skipped in park).
        if mode != "park":
            bid_px, ask_px = self._apply_inventory_shift(
                sym, fair, bid_px, ask_px)
        if (mode == "park" or not self.cfg.scalp_out_at_cap_enabled):
            return bid_px, ask_px
        return self._scalp_out_at_cap(
            sym, fair, position, bid_px, ask_px)

    def _apply_ca_delta_skew(self, fair_a: float,
                             bid_px: Optional[int],
                             ask_px: Optional[int]
                             ) -> Tuple[Optional[int], Optional[int]]:
        if not self.cfg.ca_delta_hedge_enabled:
            return bid_px, ask_px
        c_pos = self.state["C"].position
        if abs(c_pos) < self.cfg.ca_delta_hedge_pos_threshold:
            return bid_px, ask_px
        # Only hedge in the mid zone where C has meaningful gamma.
        fair_c, _ = self._fair_for("C")
        lo = self.cfg.ca_delta_hedge_mid_low
        hi = self.cfg.ca_delta_hedge_mid_high
        if fair_c < lo or fair_c > hi:
            return bid_px, ask_px
        tick = self.state["A"].tick
        max_skew = self.cfg.ca_delta_hedge_max_skew_ticks * tick
        # Skew proportional to |c_pos|, capped at max_skew.
        c_limit = self.state["C"].position_limit
        frac = min(1.0, abs(c_pos) / max(1, c_limit))
        skew = int(round(max_skew * frac))
        if skew <= 0:
            return bid_px, ask_px
        # Long C → want to sell A (tighter ask, looser bid).
        # Short C → want to buy A (tighter bid, looser ask).
        if c_pos > 0:
            if ask_px is not None:
                ask_px = max(1, ask_px - skew)
            if bid_px is not None:
                bid_px = max(1, bid_px - skew)
        else:
            if bid_px is not None:
                bid_px = bid_px + skew
            if ask_px is not None:
                ask_px = ask_px + skew
        return bid_px, ask_px

    def _inventory_shift_max_ticks_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.inventory_shift_max_ticks_a
        if sym == "B":
            return self.cfg.inventory_shift_max_ticks_b
        if sym == "C":
            return self.cfg.inventory_shift_max_ticks_c
        return self.cfg.inventory_shift_max_ticks_d

    def _apply_inventory_shift(self, sym: str, fair: float,
                               bid_px: Optional[int],
                               ask_px: Optional[int]
                               ) -> Tuple[Optional[int], Optional[int]]:
        """v20.14: directional-inventory skew. When |position/cap| is
        large, slide both bid and ask in the flattening direction so
        we sell more easily when long / buy more easily when short.
        Clipped to the +EV side of truth — bid stays <= floor(fair),
        ask stays >= ceil(fair). Quadratic ramp so the shift only
        bites at meaningful inventory load."""
        if not self.cfg.inventory_shift_enabled:
            return bid_px, ask_px
        s = self.state[sym]
        cap = self._effective_position_cap(sym)
        if cap <= 0:
            return bid_px, ask_px
        frac = s.position / float(cap)
        threshold = self.cfg.inventory_shift_threshold
        if abs(frac) < threshold:
            return bid_px, ask_px
        max_shift = self._inventory_shift_max_ticks_for(sym)
        if max_shift <= 0:
            return bid_px, ask_px
        excess = (abs(frac) - threshold) / max(1e-9, 1.0 - threshold)
        excess = min(1.0, excess)
        shift_mag = (excess * excess) * max_shift
        shift_signed = math.copysign(shift_mag, frac)
        shift_ticks = int(round(shift_signed))
        if shift_ticks == 0:
            return bid_px, ask_px
        tick = s.tick
        ub = self._upper_bound(sym)
        # +EV floors against truth fair. Note: when long-heavy we drop
        # the ask toward fair (flatten side), so cap it at ceil(fair).
        # When short-heavy we raise the bid toward fair, cap at floor(fair).
        if bid_px is not None:
            bid_px = bid_px - shift_ticks * tick
            if frac < 0:
                bid_px = min(bid_px, int(math.floor(fair)))
            bid_px = max(bid_px, tick)
            bid_px = min(bid_px, ub - 1)
        if ask_px is not None:
            ask_px = ask_px - shift_ticks * tick
            if frac > 0:
                ask_px = max(ask_px, int(math.ceil(fair)))
            ask_px = max(ask_px, tick)
            ask_px = min(ask_px, ub)
        if (bid_px is not None and ask_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + tick
        return bid_px, ask_px

    def _scalp_out_at_cap(self, sym: str, fair: float, position: int,
                          bid_px: Optional[int], ask_px: Optional[int]
                          ) -> Tuple[Optional[int], Optional[int]]:
        cap = self._effective_position_cap(sym)
        s = self.state[sym]
        tick = s.tick
        ub = self._upper_bound(sym)
        max_loss = self.cfg.scalp_out_max_loss_ticks * tick

        # Long at-or-above cap → force ASK to drain.
        if position >= cap and ask_px is None:
            target = int(math.ceil(fair))
            target = ((target + tick - 1) // tick) * tick
            # Step inside BBO if best_ask is at/below target.
            book = s.book or {}
            asks = book.get("asks") or []
            if asks:
                best_ask = int(asks[0].get("price") or 0)
                if best_ask > 0 and best_ask <= target:
                    stepped = best_ask - tick
                    if stepped > 0:
                        target = stepped
            # Floor: don't go more than max_loss below fair.
            floor = int(math.floor(fair)) - max_loss
            target = max(target, floor)
            if 0 < target <= ub:
                ask_px = target

        # Short at-or-below cap → force BID to drain.
        if position <= -cap and bid_px is None:
            target = int(math.floor(fair))
            target = (target // tick) * tick
            book = s.book or {}
            bids = book.get("bids") or []
            if bids:
                best_bid = int(bids[0].get("price") or 0)
                if best_bid > 0 and best_bid >= target:
                    stepped = best_bid + tick
                    if stepped <= ub - 1:
                        target = stepped
            ceil = int(math.ceil(fair)) + max_loss
            target = min(target, ceil)
            if 0 < target <= ub:
                bid_px = target

        return bid_px, ask_px

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        prev_phase = self.phase
        super().on_phase_change(phase, reveals)
        if phase == "running" and prev_phase != "running":
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._cd_truth_for_seq = None
            self._c_linger_seen.clear()
            self._b_linger_seen.clear()
            # v20.1: reset per-round cross-arb qty budgets + reveal_t.
            self._cross_arb_qty_used = {s: 0 for s in self.SYMS}
            # v20.11: reset per-round bounds-arb qty budgets.
            self._bounds_arb_qty_used = {s: 0 for s in self.SYMS}
            self._last_reveal_t = 0.0
            # v20.12: comprehensive per-round reset. Without these,
            # consecutive rounds in one bot process inherit stale
            # state — most damaging is `state[sym].position` which
            # is never reset locally (it tracks the SERVER position
            # via fills, but if the server flattens between rounds
            # we don't see those fills and end up "still long N" in
            # our local view → MM/sweep code blocks the side that
            # would take us BACK to flat, and we go silent.)
            self._reset_per_round_state()
            self._refresh_cd_truth_cache()
            self._recompute_arb_table()
            # v20.13: fresh round = clean identity table built from
            # current reveals (typically empty list at round start).
            try:
                self._recompute_identity_table()
            except Exception as e:
                print(f"[v20.13 IDENTITY-PHASE-ERR] "
                      f"{type(e).__name__}: {e}")
        elif phase in ("completed", "settling", "settled"):
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._cd_truth_for_seq = None
            self._c_linger_seen.clear()
            self._b_linger_seen.clear()
            self._cross_arb_qty_used = {s: 0 for s in self.SYMS}
            self._bounds_arb_qty_used = {s: 0 for s in self.SYMS}
            self._arb_table = {}

    def _reset_per_round_state(self) -> None:
        """v20.12: zero everything that should be fresh for a new
        round. Called on phase → running.

        Critical: REFETCH server positions. The local
        `state[sym].position` is only updated by fill events; if the
        server flattened us at end-of-round and we missed those fill
        events, we'd think we still hold inventory and gate ourselves
        out of the new round."""
        # 1. Refetch positions (server-authoritative).
        try:
            ps = (self.c.positions() or {}).get("positions") or {}
            for sym in self.SYMS:
                new_pos = int(ps.get(sym, 0))
                old_pos = self.state[sym].position
                if new_pos != old_pos:
                    print(f"[v20.12 POS-SYNC:{sym}] "
                          f"local={old_pos} → server={new_pos}")
                self.state[sym].position = new_pos
        except Exception as e:
            print(f"[v20.12 POS-SYNC-ERR] {type(e).__name__}: {e}")

        # 2. Per-symbol timers + in-flight counters.
        now = time.time()
        for sym in self.SYMS:
            s = self.state[sym]
            s.last_inter_sweep_t = 0.0
            s.last_dime_defense_t = 0.0
            s.in_flight = {"bid": 0, "ask": 0}
            # Resting maps were cleared on settling; the server has
            # reset orders. Be defensive — clear again here.
            s.resting = {"bid": {}, "ask": {}}

        # 3. Bot-level MM cadence clocks.
        self._last_mm_refresh_t = 0.0
        self._last_mm_refresh_t_cd = 0.0

        # 4. Scalp-out and reveal-sweep clocks.
        self._last_scalp_out_t = {s: 0.0 for s in self.SYMS}
        self._reveal_sweep_futures = {}

        # 5. Tape state — stale last_buy_agg_price etc. fakes the
        # dime-from-tape logic into posting against ghost prints from
        # the prior round.
        for sym in self.SYMS:
            self._tape[sym] = TapeState()

        # 6. Diagnostic counters. Per-round so the run summary
        # reflects only the current round.
        self._tape_dime_count = {s: 0 for s in self.SYMS}
        self._tape_widen_count = {s: 0 for s in self.SYMS}
        self._tape_print_count = {s: 0 for s in self.SYMS}
        self._bounds_arb_count = 0
        self._reveal_sweep_cd_count = 0

    # ==================================================================
    # Status helpers (for the runner)
    # ==================================================================
    def cd_status_str(self) -> str:
        s_c = self.state.get("C")
        s_d = self.state.get("D")
        truth_c = (f"{self._fair_c_truth:.0f}"
                   if self._fair_c_truth is not None else "<none>")
        truth_d = (f"{self._fair_d_truth:.1f}"
                   if self._fair_d_truth is not None else "<none>")
        c_pos = s_c.position if s_c else 0
        d_pos = s_d.position if s_d else 0
        c_cap = self._effective_position_cap("C") if s_c else 0
        d_cap = self._effective_position_cap("D") if s_d else 0
        c_sz = self._ramped_base_size("C") if s_c else 0
        d_sz = self._ramped_base_size("D") if s_d else 0
        c_b = len(s_c.resting["bid"]) if s_c else 0
        c_a = len(s_c.resting["ask"]) if s_c else 0
        d_b = len(s_d.resting["bid"]) if s_d else 0
        d_a = len(s_d.resting["ask"]) if s_d else 0
        return (f"  C: pos={c_pos:+d}/cap={c_cap}/lim={s_c.position_limit if s_c else 0}  "
                f"truth_c={truth_c}  size={c_sz}  resting bid={c_b}/ask={c_a}\n"
                f"  D: pos={d_pos:+d}/cap={d_cap}/lim={s_d.position_limit if s_d else 0}  "
                f"truth_d={truth_d}  size={d_sz}  resting bid={d_b}/ask={d_a}\n"
                f"  c_linger_seen={len(self._c_linger_seen)} extreme quotes tracked")
