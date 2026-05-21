"""Day 1 strategy v18 — SEAN-style latency-optimized dual-symbol MM.

Inspired by the SEAN bot (bot_config_dump.json):
  * Maker-only on A AND B, width=8/4 ticks, inventory skew with full-flip
    at |pos| = 50/100, and a pre-reveal pull/widen window.
  * Stage what to do for every possible next reveal BEFORE it lands, so
    on_reveal is a dict lookup + parallel IOC + apply quotes. No math.

WHAT'S DIFFERENT vs SEAN:
  * Park 0.8s before each reveal (SEAN parks at 1.0s) → 0.2s lead on the
    pre-reveal quote-pull race.
  * Pre-stage both passive QUOTES and IOC SWEEP THRESHOLDS (lift_to /
    hit_to per scenario). Most other MMs only stage quotes; we also stage
    the take-side so on_reveal can fire IOCs before competitors recompute.
  * Penny/dime BBO when between reveals: if our quote isn't at the BBO
    and stepping 1t inside still preserves floor edge, modify in.
  * Aggressive post-reveal sweep at 1.5t edge (vs the value-sniper bots'
    typical 2-3t threshold) — laggard `stale_quoter` (VALKO) and similar
    fixed-refresh MMs leave 1-2s of stale liquidity each reveal.
  * Wide-quoter trap filter (`max_ioc_distance_ticks=50`): refuses to
    IOC any price more than 50t from fair → ignores adversaries that
    quote "in the thousands" to lure value-snipers into bad fills.

DESIGN INVARIANTS:
  * NO HARDCODED round duration / reveal interval / position limit. All
    pulled from `game_state()`. Same code works whether the round is
    1.5min/15s/6-reveals or 5min/30s/10-reveals.
  * Strategy-level sliding-window rate cap (15/s) + SDK token bucket
    (18/s, 20 burst) → never trip the server's 20/s lockout.
  * Locked posterior + posterior-generation cache → fair_a/fair_b cost
    a dict lookup post-init unless the posterior moved.
  * `_apply_side` uses modify-replace by default (1 REST call vs 2 for
    cancel+post) AND keeps queue priority on pure qty-down.
  * Multi-order safety: resting[side] is a dict keyed by order_id, not
    a single slot. Reconcile cancels orphans on a 3s tick + bulk
    cancel_all circuit breaker if backlog ≥ 20.

WHAT WE EXPLOIT:
  * VALKO  (stale_quoter, refresh=5s, size=25, edge 2-6t): biggest
    target. After any reveal that moves fair by ≥1.5t, VALKO's quote
    is mispriced for 1-5s. Sweep on reveal AND in the inter-reveal
    book-event sweep.
  * Generic informed snipers (VALKC, CVALK, lead 1-1.5s) try to lift
    laggard maker quotes at T-1.0s..T-0s. We park at T-0.8s so they
    have nothing to lift.
  * Spoofers (HVALK, size 10-30 lots, linger 300ms): the
    max_ioc_distance_ticks=50 filter ignores their bait posts.
  * Wide-quoter humans (post ask@5000 with fair=50): same filter.
  * **15ms private-feed edge**: the public WS feed lags the private
    feed by ~15ms. Reveals and our own fills arrive private (no lag);
    counterparties only learn about them when the public print
    arrives ~15ms later. On every private event (fill OR reveal) we
    call `_try_cross_arb`, which scans BOTH books at fresh fairs and
    snipes mispriced levels while competitors are still reading the
    stale book.
  * **Cross-symbol arb**: A settles at running_sum + Σ remaining X,
    B settles at the next X. Implied_X_from_A = (mid_A - rs)/n_rem;
    implied_X_from_B = mid_B. In equilibrium they match; any
    divergence is arb. `_try_cross_arb` walks both books N levels
    deep and lifts/hits anything past `cross_arb_edge_ticks`.

ENDGAME BURST MODE:
  * When `_reveal_count == n_total` (last reveal), A's settlement
    value is KNOWN exactly (= running_sum). All uncertainty collapses.
  * `_endgame_active` flips → MM stops refreshing on A, our resting
    A quotes are cancelled, and `_endgame_loop` ticks every 40ms.
  * Each tick walks `endgame_max_levels=20` into both sides of A's
    book and fires IOCs at anything ≥ `endgame_edge_ticks_a=0.5t`
    from settlement. Race with other snipers for the same free money.

CONNECTION PRE-WARM:
  * Idle TCP can be reset by NAT/server. `_prewarm_loop` sends a
    cheap `my_orders` GET every `prewarm_interval_sec=25s` so the
    Session pool stays warm.
  * Burst pre-warm `prewarm_before_reveal_sec=1.0` BEFORE each
    reveal: confirms the pool is live so the reveal IOC fan-out
    doesn't pay a TCP handshake.

C++ FOOTNOTE: the python hot-path is ~3-5ms; the dominant latency is
the REST RTT (~13ms median LAN). A C/Rust IO core or asyncio-based
WS+HTTP rewrite of the SDK could shave another 3-5ms. Worth pursuing
if we ever close the python compute path, but not the bottleneck now.

Run:
    python day1/strategy18/run_combined18.py
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Path bootstrap: allow `import sdk.client` and `import strategy12`.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DAY1_DIR = os.path.dirname(_THIS_DIR)
_REPO = os.path.dirname(_DAY1_DIR)
for _p in (_REPO, _DAY1_DIR, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk.client import GameClient  # noqa: E402
from strategy12 import (  # noqa: E402, F401
    URL, API_KEY, N_PRIOR_SIM, Posterior, _install_nodelay,
)


# ===========================================================================
# Config
# ===========================================================================
@dataclass
class Config:
    # ---- MM widths/sizes (mirrored from SEAN, validated by v17 logs) ----
    quote_width_a: int = 8
    quote_width_b: int = 4
    size_a: int = 5
    size_b: int = 5
    # Inventory skew: quote midpoint shifts (position / skew_full_flip)
    # half-widths toward flat. At |pos|==flip the quote fully flips.
    skew_full_flip_a: int = 50
    skew_full_flip_b: int = 100

    # ---- Pre-reveal park (user spec: 0.8s before each reveal) ----
    # 0.8s gives us a 200ms lead on SEAN's 1.0s pre_reveal_lead. We PARK
    # the quote at fair ± park_offset (not cancel) — order stays alive
    # at a price the market won't fill into, so the post-reveal hot path
    # can MODIFY back to BBO in 1 REST call vs cancel+post = 2.
    pre_reveal_park_sec: float = 0.8
    pre_reveal_park_offset_ticks: int = 30
    # Multiplier when we widen instead of park (e.g. k=0 no-info phase).
    pre_reveal_widen_mult: float = 5.0
    # k=0 only: posterior is the raw prior, so the spread defaults very
    # wide regardless of timing.
    no_info_widen_mult: float = 8.0

    # ---- MM refresh (between-reveal slow loop) ----
    mm_refresh_sec: float = 1.5
    mm_min_interval_sec: float = 0.4
    # Re-quote when fair drifts ≥ this many ticks. Strategy17 used 2;
    # we use 1 to penny-stay-inside more aggressively (combined with
    # the penny logic below).
    quote_min_change_ticks: int = 1

    # ---- Penny/dime BBO (between reveals only) ----
    # When the inside BBO is wider than our default quote distance, we
    # already win. When it's tighter, we step 1t INSIDE the inside if
    # the post-step price still earns at least `penny_min_edge_*` per
    # fill (fair vs price, before fees). We do NOT step inside during
    # the pre-reveal window — being inside there = being a sniper's
    # target.
    penny_enabled: bool = True
    penny_min_edge_a: float = 1.0   # net of maker fee 0.5 → +0.5/lot
    penny_min_edge_b: float = 1.0
    # Cap on how many ticks we'll step inside (so a one-side dimer
    # collapsing 10t can't drag us 10t out of our default).
    penny_max_step_ticks: int = 5

    # ---- Dime-defense / BBO-claim (reactive, runs on every book event) ----
    # Competitors place v=1 orders one tick inside our BBO to claim
    # queue priority. The MM refresh loop only requotes every 0.4-1.5s,
    # so we sit dimed for up to a full reveal cycle. This path watches
    # every book event and ensures we're at the inside BBO whenever
    # stepping there still leaves >= penny_min_edge_* vs fair. Covers
    # three cases per side: dimed, absent (no resting), or outside (we
    # have resting but a tighter inside formed). Throttled per symbol
    # so a v=1 dime war doesn't blow REST budget.
    dime_defense_enabled: bool = True
    dime_defense_throttle_sec: float = 0.15

    # ---- Post-reveal IOC sweep (hot path) ----
    # Threshold edge for IOC'ing a laggard book level on reveal. 1.5t
    # is below the typical informed-bot threshold of 2-3t so we catch
    # short-lifetime stale liquidity before it gets repriced.
    sweep_edge_ticks: float = 1.5
    sweep_max_slice: int = 10
    sweep_position_pad: int = 5
    # Wide-quoter trap filter: refuse any IOC at price >50t off fair.
    # See bot_config_dump.json for spoofer (HVALK) behavior.
    max_ioc_distance_ticks: float = 50.0

    # ---- Stale-quote sweep between reveals (VALKO-style targets) ----
    # When a book_event arrives, if any level is past `inter_sweep_edge_ticks`
    # from our fair AND the size is ≥ inter_sweep_min_size, IOC it.
    # Tuned tight enough to catch refresh=5s stale_quoter, loose enough
    # not to fire on noise from naive_mm 1s refresh cycle.
    inter_sweep_enabled: bool = True
    inter_sweep_edge_ticks: float = 3.0
    inter_sweep_min_size: int = 4
    inter_sweep_max_slice: int = 10
    inter_sweep_throttle_sec: float = 0.3

    # ---- Cross-symbol arb / private-feed exploit ----
    # The public WS feed lags the private feed by ~15ms. When WE see a
    # fill on the private feed, the print hasn't propagated to the public
    # book yet — so for ~12-15ms, competitors are still pricing off a
    # book that doesn't know about our trade. Hit BOTH books during
    # that exclusive window using fresh fairs.
    #
    # Mathematical link: A settles at running_sum + sum(remaining X),
    # B settles at the NEXT X. So implied_X_from_A = (mid_A - rs)/n_rem
    # and implied_X_from_B = mid_B should be equal in equilibrium. Any
    # divergence is arb.
    cross_arb_enabled: bool = True
    # Lower edge than inter_sweep — we're sniping during an exclusive
    # window, not racing a refresh cycle, so we can take thinner edges.
    cross_arb_edge_ticks: float = 2.0
    cross_arb_max_slice: int = 8
    cross_arb_levels: int = 5     # walk this many price levels deep
    # Documented constant (matches the exchange spec). Used for telemetry
    # / sanity checks, not for sleeps.
    public_feed_lag_ms: float = 15.0

    # ---- Flatten bias before high-uncertainty events ----
    # Pre-first-reveal posterior IS the raw prior — sigma_A is huge. An
    # outlier reveal can blow PnL on any sized inventory. So in the
    # window before the first reveal, we *bias* sniping toward trades
    # that reduce |position|: lift_to is raised when we're SHORT (we'll
    # cover at slightly worse prices), hit_to is lowered when we're LONG
    # (we'll dump at slightly worse bids). Edges are reduced, never
    # taken below the taker fee — we DON'T force-flatten at a loss.
    # Also extends to any reveal whose sigma_A exceeds a threshold (e.g.
    # if posterior is wide for another reason — sparse priors, late join).
    flatten_bias_enabled: bool = True
    # Window before the first reveal (k=0) where bias is active.
    pre_first_reveal_flatten_sec: float = 8.0
    # Optional generalized trigger: bias is also active if sigma_a >=
    # this value (in price ticks). Setting to inf disables the
    # sigma-based trigger.
    flatten_bias_sigma_a_min: float = 12.0
    # Edge reduction in the position-reducing direction.
    flatten_edge_reduction_ticks: float = 1.5
    # Floor on the post-reduction edge — must still cover taker fee.
    # Taker fee = 0.5/lot, so 0.5t minimum still nets ~0 ignoring slippage.
    flatten_min_edge_ticks: float = 0.6

    # ---- Fast / slow precompute split ----
    # Slow loop (existing _precompute_loop): builds the per-scenario
    # table for the NEXT reveal — Bayesian update over every plausible
    # X, fair/sigma per branch, base sweep thresholds. Heavy, posterior-
    # driven, runs on reveal + on signal.
    # Fast loop (new _fast_precompute_loop): keeps the CURRENT-STATE
    # live snipe thresholds fresh — re-derived every 50ms from cached
    # fair_a/fair_b + current position + flatten-bias window. Cheap
    # (~50us per tick). Consumed by inter_sweep / cross_arb so they
    # don't re-compute fair on every book event.
    fast_precompute_tick_sec: float = 0.05

    # ---- Endgame burst mode ----
    # After the LAST reveal, A's settlement value is KNOWN exactly
    # (= running_sum_final). There is no posterior uncertainty. Any ask
    # below running_sum or bid above running_sum is literal free money —
    # the only thing in our way is other snipers. Switch to a tight loop
    # that walks the full book depth and fires IOCs continuously until
    # the game ends.
    endgame_enabled: bool = True
    # Tick interval for the endgame loop. 25Hz = one scan per 40ms; with
    # the ioc_parallel_workers in the pool we can fire multiple IOCs per
    # tick. Tighter than this risks 20/s rate-limit lockout.
    endgame_tick_sec: float = 0.04
    # Edge threshold in endgame: A has zero uncertainty, B is moot
    # (already settled per-reveal). 0.5t edge captures essentially every
    # genuine mispricing after fees (0.5t maker + 0.5t taker = 1t cost,
    # so 0.5t edge nets ~0).
    endgame_edge_ticks_a: float = 0.5
    endgame_max_levels: int = 20      # walk much deeper than the 5 default
    endgame_max_slice: int = 25       # bigger slices — race other snipers
    # Reserve some bucket headroom so the endgame burst doesn't lockout.
    endgame_min_tokens: float = 3.0
    # When _reveal_count >= n_total - 1 we're approaching the cliff. At
    # the LAST reveal, A's uncertainty collapses → activate.
    endgame_activate_at_remaining: int = 0

    # ---- Connection pre-warm ----
    # Keep-alive TCP idle for >~60s is at risk of NAT/server reset. A
    # cheap GET keeps the pool warm so the first hot-path POST after a
    # quiet stretch doesn't pay a fresh TCP handshake (~3ms LAN).
    prewarm_enabled: bool = True
    prewarm_interval_sec: float = 25.0
    # Fire a pre-warm burst T-park_sec before reveal so all pooled
    # connections are confirmed-alive for the reveal IOC fan-out.
    prewarm_before_reveal_sec: float = 1.0

    # ---- Precompute control ----
    precompute_min_prob: float = 1e-4
    precompute_max_x: int = 60

    # ---- Inventory safety ----
    # Stay this far inside the position limit when sizing MM quotes.
    mm_inventory_safety_pad: int = 10
    skip_negative_ev: bool = True
    # Hard cap on |pos| as a multiple of position_limit. Tripping this
    # triggers cancel_all + IOC flatten back into the pad.
    emergency_flatten_factor: float = 1.0

    # ---- Strategy-level rate-limit window ----
    # Server cap = 20/s with 3s lockout; SDK bucket = 18/s sustained,
    # 20 burst. We cap STRATEGY-side sends at 15/s sliding so the burst
    # plus dashboard manual writes never trip the server.
    max_sends_per_second: float = 15.0
    sends_window_sec: float = 1.0
    mm_send_min_tokens: float = 4.0
    reconcile_min_tokens: float = 8.0
    bulk_cancel_min_tokens: float = 6.0

    # ---- Reconcile ----
    reconcile_check_sec: float = 3.0
    reconcile_max_cancels_per_tick: int = 8
    reconcile_bulk_cancel_threshold: int = 20
    reconcile_min_age_sec: float = 3.0
    # Routine cleanup of 1-2 orphans is expected (race between POST ack
    # and resting-dict update). Only log when the orphan count crosses
    # this threshold OR cancels are being capped (something's stuck).
    reconcile_print_min: int = 3

    # ---- Server lockout backoff ----
    lockout_min_pad_ms: float = 100.0
    lockout_max_pad_ms: float = 3500.0
    post_backoff_sec: float = 5.0

    # ---- 403 circuit breaker ----
    # A 403 while we think phase=="running" usually means our local
    # phase view lagged the server (WS game_state hasn't arrived).
    # On any 403 we re-fetch game_state to confirm. If the server agrees
    # we're running but we still see N 403s in a window, hard-block sends
    # (some other server-side rule is rejecting us — auth, position, etc.)
    # and stop spamming retries.
    forbidden_circuit_max: int = 3
    forbidden_circuit_window_sec: float = 10.0
    # Phase re-check throttle so multiple parallel POSTs that all 403
    # don't all spam game_state requests.
    forbidden_phase_recheck_throttle_sec: float = 0.5

    # ---- Latency ----
    # Larger pool than the default 6 — endgame burst can fire 10+ IOCs
    # in the same 40ms tick and the connection pool should absorb that
    # without serializing.
    ioc_parallel_workers: int = 10
    use_modify_replace: bool = True

    # ---- Misc ----
    log_every_modify: bool = False
    client_order_id_prefix: str = "v18"
    reconcile_respect_manual_orders: bool = True


# ===========================================================================
# Per-symbol state
# ===========================================================================
@dataclass
class _SymState:
    sym: str
    position: int = 0
    position_limit: int = 100
    tick: int = 1
    book: Optional[dict] = None
    book_t: float = 0.0
    resting: Dict[str, Dict[int, dict]] = field(
        default_factory=lambda: {"bid": {}, "ask": {}})
    in_flight: Dict[str, int] = field(
        default_factory=lambda: {"bid": 0, "ask": 0})
    last_inter_sweep_t: float = 0.0
    last_dime_defense_t: float = 0.0


# ===========================================================================
# Precomputed scenario for a given next reveal value
# ===========================================================================
@dataclass
class _Scenario:
    """All actions to take if the next reveal is `value`. Pre-staged so
    the hot path is dict-lookup + parallel-fire only."""
    value: int
    prob: float
    fair_a: float
    sigma_a: float
    fair_b: float
    sigma_b: float
    # Passive quotes to repost (None = don't post that side)
    bid_a: Optional[int]
    ask_a: Optional[int]
    bid_b: Optional[int]
    ask_b: Optional[int]
    # IOC sweep thresholds: price above which we'd SELL (hit bids)
    # and below which we'd BUY (lift asks). Both expressed as ints
    # since the book prices are ints.
    lift_to_a: int  # buy asks with price <= this
    hit_to_a: int   # sell bids with price >= this
    lift_to_b: int
    hit_to_b: int


# ===========================================================================
# Live snipe thresholds — kept fresh by the FAST precompute loop
# ===========================================================================
@dataclass
class _LiveThresholds:
    """Snapshot of "would I snipe this level right now?" — refreshed by
    `_fast_precompute_loop` every ~50ms. inter_sweep, cross_arb, and any
    other live snipe path read this instead of re-deriving from fair.

    Two flavors per side:
      * lift_to       — BUY any ask with px ≤ this (normal edge)
      * hit_to        — SELL any bid with px ≥ this (normal edge)
      * lift_to_flat  — relaxed version, used when we're SHORT during
                        a flatten-bias window (closer to fair so we
                        cover at slightly worse prices)
      * hit_to_flat   — relaxed version, used when we're LONG during
                        a flatten-bias window (closer to fair so we
                        dump at slightly worse prices)
    """
    sym: str
    fair: float
    sigma: float
    lift_to: int
    hit_to: int
    lift_to_flat: int
    hit_to_flat: int
    in_flatten_window: bool
    gen: int


# ===========================================================================
# Strategy
# ===========================================================================
class Strategy:
    """Dual-symbol passive MM with reveal-time precompute hot path."""

    SYMS = ("A", "B")

    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[Config] = None):
        self.c = client
        self.posterior = posterior
        self.cfg = config or Config()
        self.lock = threading.RLock()

        # TCP_NODELAY adapter (sub-200B POSTs skip Nagle 40ms wait).
        ok = _install_nodelay(client, pool_maxsize=max(
            8, self.cfg.ioc_parallel_workers + 2))
        print(f"[v18 NODELAY] installed={ok}")

        # Bootstrap EVERYTHING from game_state. No hardcoded duration / N.
        gs = client.game_state()
        self.duration = gs["duration"]
        self.reveal_interval = gs["reveal_interval"]
        self.n_total = self.duration // self.reveal_interval
        fees = gs.get("fees", {})
        self.maker_fee = fees.get("maker_per_lot", 0.5)
        self.taker_fee = fees.get("taker_per_lot", 0.5)

        # Per-symbol state
        self.state: Dict[str, _SymState] = {}
        for sym in self.SYMS:
            instr = (gs.get("instruments") or {}).get(sym) or {}
            self.state[sym] = _SymState(
                sym=sym,
                position=0,
                position_limit=int(instr.get("position_limit") or 100),
                tick=int(instr.get("tick_size") or 1),
            )

        # Initial positions
        try:
            ps = (client.positions() or {}).get("positions") or {}
            for sym in self.SYMS:
                self.state[sym].position = int(ps.get(sym, 0))
        except Exception as e:
            print(f"[v18 INIT-POS] failed: {e}")

        # Phase + posterior bootstrap
        self.phase: Optional[str] = gs.get("phase")
        self.posterior.reset(gs.get("reveals") or [])
        self._reveal_count = len(self.posterior.reveals)
        self._running_sum = float(sum(self.posterior.reveals))
        self._phase_running_t: Optional[float] = (
            time.time() if self.phase == "running" else None)
        self._last_reveal_t: Optional[float] = None

        # Posterior-gen caches: bumped on every posterior change so fair_*
        # cost a dict lookup unless the posterior actually moved.
        self._fair_a_cache: Optional[Tuple[int, float, float]] = None
        self._fair_b_cache: Optional[Tuple[int, float, float]] = None
        self._posterior_gen: int = 0

        # Slow precompute: per-scenario table for the NEXT reveal.
        self._precompute: Dict[int, _Scenario] = {}
        self._precompute_lock = threading.RLock()
        self._precompute_for_cycle: int = -1
        self._precompute_request = threading.Event()
        self._precompute_run_count = 0
        self._last_precompute_us = 0.0

        # Fast precompute: live snipe thresholds for the CURRENT moment.
        # Updated every fast_precompute_tick_sec from cached fair_a/fair_b
        # plus position + flatten-bias window. Snipe paths (inter_sweep,
        # cross_arb) read this instead of re-deriving fair every event.
        self._live_thresholds: Dict[str, _LiveThresholds] = {}
        self._live_lock = threading.RLock()
        self._live_gen: int = 0
        self._fast_precompute_run_count = 0
        self._last_fast_precompute_us = 0.0

        # Park tracking: avoid re-parking multiple times for the same reveal.
        self._last_park_reveal_idx: int = -1

        # Counters
        self._modify_count = 0
        self._post_count = 0
        self._reveal_ioc_count = 0
        self._inter_sweep_count = 0
        self._cross_arb_count = 0
        self._endgame_count = 0
        self._coid_counter = 0
        self._sends_deferred = 0

        # Endgame state — flipped True once `_reveal_count` hits the
        # last index. Stays True for the remainder of the round.
        self._endgame_active = False
        self._endgame_pos_a_at_entry: int = 0
        # Pre-warm bookkeeping (last successful idle GET).
        self._last_prewarm_t: float = 0.0
        self._prewarm_before_reveal_idx: int = -1

        # Server-lockout cutoff + sliding-window send log.
        self._lockout_until = 0.0
        self._lockout_count = 0
        self._send_log: deque = deque(maxlen=64)
        self._send_log_lock = threading.Lock()
        self._last_mm_refresh_t = 0.0
        self._post_backoff_until: Dict[Tuple[str, str], float] = {}
        # Sticky terminal flag — set on the first 403 received while phase
        # is anything other than "running" (game ended mid-flight). All
        # POST/MODIFY paths short-circuit silently while it's set; clears
        # on phase→running transition. Prevents the 5s-backoff retry loop
        # from spamming 403s once the game is over.
        self._post_terminal_blocked: bool = False
        self._terminal_403_count: int = 0
        # 403 circuit breaker — track recent forbidden events to detect
        # cases where phase-locally claims running but server disagrees,
        # OR there's another persistent reject reason (auth/position/etc).
        self._forbidden_log: deque = deque(maxlen=32)
        self._forbidden_log_lock = threading.Lock()
        self._last_phase_recheck_t: float = 0.0
        self._forbidden_count: int = 0

        # Parallel IOC executor — pre-allocated so the hot path has no
        # thread-spawn cost.
        self._ioc_executor = ThreadPoolExecutor(
            max_workers=self.cfg.ioc_parallel_workers,
            thread_name_prefix="v18-ioc")

        # Threads
        self._stop = threading.Event()
        self._mm_wake = threading.Event()
        self._th_precompute = threading.Thread(
            target=self._precompute_loop, daemon=True,
            name="v18-precomp-slow")
        self._th_fast_precompute = threading.Thread(
            target=self._fast_precompute_loop, daemon=True,
            name="v18-precomp-fast")
        self._th_mm = threading.Thread(
            target=self._mm_loop, daemon=True, name="v18-mm")
        self._th_park = threading.Thread(
            target=self._park_loop, daemon=True, name="v18-park")
        self._th_reconcile = threading.Thread(
            target=self._reconcile_loop, daemon=True, name="v18-recon")
        self._th_endgame = threading.Thread(
            target=self._endgame_loop, daemon=True, name="v18-endgame")
        self._th_prewarm = threading.Thread(
            target=self._prewarm_loop, daemon=True, name="v18-prewarm")
        self._th_precompute.start()
        self._th_fast_precompute.start()
        self._th_mm.start()
        self._th_park.start()
        self._th_reconcile.start()
        self._th_endgame.start()
        self._th_prewarm.start()
        self._precompute_request.set()

        # Banner
        print(f"[v18] phase={self.phase}  n_total={self.n_total}  "
              f"reveal_interval={self.reveal_interval}s  "
              f"duration={self.duration}s")
        for sym in self.SYMS:
            s = self.state[sym]
            print(f"[v18 SYM:{sym}] pos_limit={s.position_limit}  "
                  f"tick={s.tick}  pos={s.position}")
        print(f"[v18 MM] A: width={self.cfg.quote_width_a}t "
              f"size={self.cfg.size_a} flip={self.cfg.skew_full_flip_a}  "
              f"B: width={self.cfg.quote_width_b}t "
              f"size={self.cfg.size_b} flip={self.cfg.skew_full_flip_b}")
        print(f"[v18 PARK] {self.cfg.pre_reveal_park_sec}s before each reveal "
              f"@ ±{self.cfg.pre_reveal_park_offset_ticks}t")
        print(f"[v18 SWEEP] reveal-edge={self.cfg.sweep_edge_ticks}t  "
              f"inter-edge={self.cfg.inter_sweep_edge_ticks}t  "
              f"arb-edge={self.cfg.cross_arb_edge_ticks}t  "
              f"max-dist={self.cfg.max_ioc_distance_ticks}t")
        print(f"[v18 ENDGAME] enabled={self.cfg.endgame_enabled}  "
              f"tick={self.cfg.endgame_tick_sec*1000:.0f}ms  "
              f"edge={self.cfg.endgame_edge_ticks_a}t  "
              f"depth={self.cfg.endgame_max_levels}  "
              f"workers={self.cfg.ioc_parallel_workers}")
        print(f"[v18 PREWARM] enabled={self.cfg.prewarm_enabled}  "
              f"interval={self.cfg.prewarm_interval_sec:.0f}s  "
              f"pre_reveal={self.cfg.prewarm_before_reveal_sec:.1f}s")
        print(f"[v18 FLATTEN] enabled={self.cfg.flatten_bias_enabled}  "
              f"pre_first={self.cfg.pre_first_reveal_flatten_sec}s  "
              f"sigma_min={self.cfg.flatten_bias_sigma_a_min}t  "
              f"edge_reduction={self.cfg.flatten_edge_reduction_ticks}t  "
              f"min_edge={self.cfg.flatten_min_edge_ticks}t")
        print(f"[v18 PRECOMP] slow=on-reveal  "
              f"fast={self.cfg.fast_precompute_tick_sec*1000:.0f}ms tick")

    # ==================================================================
    # Posterior wrappers
    # ==================================================================
    def _n_remaining(self) -> int:
        return max(self.n_total - self._reveal_count, 0)

    def _bump_posterior_gen(self) -> None:
        self._posterior_gen += 1
        self._fair_a_cache = None
        self._fair_b_cache = None

    def fair_a(self) -> Tuple[float, float]:
        """E[settle_A] = running_sum + n_rem * E[X], sigma via LotV."""
        cached = self._fair_a_cache
        if cached and cached[0] == self._posterior_gen:
            return cached[1], cached[2]
        n_rem = self._n_remaining()
        fair, sigma = self.posterior.predict_settle(self._running_sum, n_rem)
        self._fair_a_cache = (self._posterior_gen, fair, sigma)
        return fair, sigma

    def fair_b(self) -> Tuple[float, float]:
        """E[X], sigma — B settles on the next X."""
        cached = self._fair_b_cache
        if cached and cached[0] == self._posterior_gen:
            return cached[1], cached[2]
        ex, ex2 = 0.0, 0.0
        for (a, w), p in self.posterior.posterior.items():
            mean_x = a + w / 2.0
            var_x = w * (w + 2) / 12.0
            ex += p * mean_x
            ex2 += p * (var_x + mean_x * mean_x)
        sigma = math.sqrt(max(0.0, ex2 - ex * ex))
        self._fair_b_cache = (self._posterior_gen, ex, sigma)
        return ex, sigma

    # ==================================================================
    # Quote calculation: fair → (bid, ask) with skew + pre-reveal mode
    # ==================================================================
    def _quote_prices(self, sym: str, fair: float, position: int,
                      mode: str) -> Tuple[Optional[int], Optional[int]]:
        """Compute (bid, ask) for `sym` at given `fair` and `position`.

        mode:
          "normal"   — between-reveal MM, default widths
          "widen"    — pre-reveal or no-info: half_width * widen_mult
          "park"     — deep park during the pull window
        """
        s = self.state[sym]
        if sym == "A":
            width = self.cfg.quote_width_a
            flip = max(1, self.cfg.skew_full_flip_a)
        else:
            width = self.cfg.quote_width_b
            flip = max(1, self.cfg.skew_full_flip_b)
        tick = s.tick

        if mode == "park":
            off = self.cfg.pre_reveal_park_offset_ticks * tick
            bid_px = ((int(round(fair - off))) // tick) * tick
            ask_px = ((int(round(fair + off))) // tick) * tick
            if bid_px < tick:
                bid_px = None
            if (bid_px is not None and ask_px is not None
                    and ask_px <= bid_px):
                ask_px = bid_px + tick
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

        # Inventory skew: long → push both sides DOWN (encourage sells).
        skew = (position / flip) * half
        bid_px = int(round(fair - half - skew))
        ask_px = int(round(fair + half - skew))
        bid_px = (bid_px // tick) * tick
        ask_px = (ask_px // tick) * tick
        if ask_px <= bid_px:
            ask_px = bid_px + tick

        if self.cfg.skip_negative_ev:
            if bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None

        if bid_px is not None and bid_px < tick:
            bid_px = None

        # Inventory pad: don't quote the side that adds to exposure.
        pad = self.cfg.mm_inventory_safety_pad
        if position >= s.position_limit - pad:
            bid_px = None
        if position <= -s.position_limit + pad:
            ask_px = None
        return bid_px, ask_px

    # ==================================================================
    # Penny/dime BBO: step 1t inside the BBO if floor edge preserved
    # ==================================================================
    def _maybe_penny(self, sym: str, fair: float, position: int,
                     bid_px: Optional[int], ask_px: Optional[int]
                     ) -> Tuple[Optional[int], Optional[int]]:
        """Step 1t inside the inside BBO if doing so still preserves
        penny_min_edge_* per fill. Skipped during pre-reveal (we're
        the sniper's target inside, not their winner)."""
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
        min_edge = (self.cfg.penny_min_edge_a if sym == "A"
                    else self.cfg.penny_min_edge_b)
        max_step = self.cfg.penny_max_step_ticks

        # Bid side: if our default bid is BELOW best_bid and stepping
        # (best_bid + 1t) still earns >= min_edge, penny inside.
        if (bid_px is not None and best_bid is not None
                and best_bid >= bid_px):
            # Don't undercut ourselves (we may already BE best_bid).
            same = False
            for e in self.state[sym].resting["bid"].values():
                if int(e.get("price") or 0) == best_bid:
                    same = True
                    break
            target = best_bid + tick if not same else best_bid
            # Edge check: target ≤ fair − min_edge.
            if (target <= fair - min_edge
                    and (target - bid_px) <= max_step * tick
                    and target < (best_ask if best_ask is not None else 10**9)):
                bid_px = target

        # Ask side: if our default ask is ABOVE best_ask and stepping
        # (best_ask − 1t) still earns >= min_edge, penny inside.
        if (ask_px is not None and best_ask is not None
                and best_ask <= ask_px):
            same = False
            for e in self.state[sym].resting["ask"].values():
                if int(e.get("price") or 0) == best_ask:
                    same = True
                    break
            target = best_ask - tick if not same else best_ask
            if (target >= fair + min_edge
                    and (ask_px - target) <= max_step * tick
                    and target > (best_bid if best_bid is not None else 0)):
                ask_px = target
        return bid_px, ask_px

    # ==================================================================
    # Pre-reveal scheduling
    # ==================================================================
    def _next_reveal_at(self) -> Optional[float]:
        if self.phase != "running" or self._phase_running_t is None:
            return None
        if self._reveal_count >= self.n_total:
            return None
        if self._last_reveal_t is not None:
            return self._last_reveal_t + self.reveal_interval
        return (self._phase_running_t
                + (self._reveal_count + 1) * self.reveal_interval)

    def _in_park_window(self) -> bool:
        nxt = self._next_reveal_at()
        if nxt is None:
            return False
        now = time.time()
        # Park window = [next_reveal − park_sec, next_reveal + 0.5s]. The
        # +0.5s tail covers reveal-delay (server can fire 200-500ms late).
        return (nxt - self.cfg.pre_reveal_park_sec) <= now < (nxt + 0.5)

    # ==================================================================
    # Precompute (slow loop)
    # ==================================================================
    def _possible_x_values(self) -> List[int]:
        """Integer X values with non-negligible posterior probability."""
        seen: Dict[int, float] = {}
        for (a, w), p in self.posterior.posterior.items():
            share = p / (w + 1)
            for v in range(a, a + w + 1):
                seen[v] = seen.get(v, 0.0) + share
        out = [v for v, p in seen.items()
               if p >= self.cfg.precompute_min_prob
               and 0 <= v <= self.cfg.precompute_max_x]
        out.sort()
        return out

    # ==================================================================
    # SLOW precompute — per-scenario table for the NEXT reveal.
    # Heavy Bayesian work, signal-driven (runs on reveal + manual kick).
    # ==================================================================
    def _precompute_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._precompute_request.wait(timeout=2.0)
                if self._stop.is_set():
                    return
                self._precompute_request.clear()
                self._build_precompute()
            except Exception as e:
                print(f"[v18 SLOW-PRECOMP-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # FAST precompute — live snipe thresholds for the CURRENT moment.
    # Lightweight, time-driven (every fast_precompute_tick_sec). Caches
    # the "would I snipe this level right now?" decision so inter_sweep
    # and cross_arb don't recompute fair on every book event.
    # ==================================================================
    def _fast_precompute_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.phase == "running":
                    self._build_live_thresholds()
            except Exception as e:
                print(f"[v18 FAST-PRECOMP-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.fast_precompute_tick_sec)

    def _in_flatten_bias_window(self, sigma_a: Optional[float] = None) -> bool:
        """True if we should bias toward flattening |pos| before an event.

        Triggers:
          1. Approaching the FIRST reveal (k=0, raw prior, biggest sigma).
          2. Current fair_a sigma exceeds `flatten_bias_sigma_a_min` (any
             reveal where the posterior is unusually wide).
        """
        if not self.cfg.flatten_bias_enabled:
            return False
        if self.phase != "running":
            return False
        # Trigger 1: pre-first-reveal window.
        if self._reveal_count == 0:
            nxt = self._next_reveal_at()
            if nxt is not None:
                eta = nxt - time.time()
                if 0.0 <= eta <= self.cfg.pre_first_reveal_flatten_sec:
                    return True
        # Trigger 2: high-sigma reveal in general.
        if sigma_a is not None:
            if sigma_a >= self.cfg.flatten_bias_sigma_a_min:
                return True
        return False

    def _build_live_thresholds(self) -> None:
        """Recompute the live snipe thresholds for both symbols and swap
        them in. Reads cached fair_a/fair_b — O(few µs) when posterior
        hasn't moved, only the tick-clamping math runs."""
        t0 = time.perf_counter()
        sweep_edge = self.cfg.sweep_edge_ticks
        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks

        fair_a, sigma_a = self.fair_a()
        fair_b, sigma_b = self.fair_b()
        in_flat = self._in_flatten_bias_window(sigma_a)

        new_live: Dict[str, _LiveThresholds] = {}
        for sym, fair, sigma in (("A", fair_a, sigma_a),
                                 ("B", fair_b, sigma_b)):
            tick = self.state[sym].tick
            # Normal sweep thresholds (same formula as in slow precompute).
            lift_to = ((int(math.floor(fair - sweep_edge))) // tick) * tick
            hit_to_raw = int(math.ceil(fair + sweep_edge))
            hit_to = ((hit_to_raw + tick - 1) // tick) * tick

            # Flatten-bias variants — relaxed in the position-reducing
            # direction, floored at flatten_min_edge_ticks (must still
            # beat taker fee).
            relaxed = max(flat_min, sweep_edge - flat_reduction)
            lift_to_flat = ((int(math.floor(fair - relaxed))) // tick) * tick
            hit_to_flat_raw = int(math.ceil(fair + relaxed))
            hit_to_flat = (((hit_to_flat_raw + tick - 1) // tick) * tick)

            new_live[sym] = _LiveThresholds(
                sym=sym, fair=fair, sigma=sigma,
                lift_to=lift_to, hit_to=hit_to,
                lift_to_flat=lift_to_flat, hit_to_flat=hit_to_flat,
                in_flatten_window=in_flat, gen=self._live_gen + 1)

        with self._live_lock:
            self._live_thresholds = new_live
            self._live_gen += 1
            self._fast_precompute_run_count += 1
            self._last_fast_precompute_us = (
                time.perf_counter() - t0) * 1e6

    def _get_snipe_thresholds(
        self, sym: str, position: int
    ) -> Tuple[Optional[int], Optional[int], float, bool]:
        """Effective (lift_to, hit_to, fair, in_flatten_window) for the
        given symbol given current position. Applies flatten-bias only
        in the direction that reduces |position|.

        Returns (None, None, fair, in_flat) if the live table is empty
        (e.g. fast loop hasn't ticked yet) — callers should fall back.
        """
        with self._live_lock:
            lt = self._live_thresholds.get(sym)
        if lt is None:
            return None, None, 0.0, False
        lift_to = lt.lift_to
        hit_to = lt.hit_to
        if lt.in_flatten_window:
            # Relax only the direction that flattens.
            if position < 0:
                lift_to = lt.lift_to_flat   # raise — buy at worse asks
            if position > 0:
                hit_to = lt.hit_to_flat     # lower — sell at worse bids
        return lift_to, hit_to, lt.fair, lt.in_flatten_window

    def _build_precompute(self) -> None:
        """For every plausible next reveal value v, stage all 4 quotes +
        both sweep thresholds (per symbol)."""
        t0 = time.perf_counter()
        cycle_at_start = self._reveal_count
        if self._n_remaining() <= 0:
            with self._precompute_lock:
                self._precompute = {}
                self._precompute_for_cycle = cycle_at_start
            return

        post_snapshot = dict(self.posterior.posterior)
        running_base = self._running_sum
        n_rem_after = self._n_remaining() - 1
        # Snapshot positions so all rows are computed against a single
        # inventory state. They may shift between build and apply, but
        # the MM loop will re-skew on the next refresh.
        pos_a = self.state["A"].position
        pos_b = self.state["B"].position
        sweep_edge = self.cfg.sweep_edge_ticks
        tick_a = self.state["A"].tick
        tick_b = self.state["B"].tick

        eps = 1e-9
        new_table: Dict[int, _Scenario] = {}
        for v in self._possible_x_values():
            # Bayesian update: P(a,w | v) ∝ P(a,w) * 1/(w+1) if v∈[a,a+w]
            new_post: Dict[Tuple[int, int], float] = {}
            total = 0.0
            marg = 0.0
            for (a, w), p in post_snapshot.items():
                if v < a - eps or v > a + w + eps:
                    continue
                lik = 1.0 / (w + 1)
                marg += p * lik
                np_new = p / (w + 1)
                new_post[(a, w)] = np_new
                total += np_new
            if total <= 0 or marg < self.cfg.precompute_min_prob:
                continue
            new_post = {k: x / total for k, x in new_post.items()}

            # E[settle_A | v], Var via law-of-total-variance
            running_after = running_base + v
            e_inner = 0.0
            e2_inner = 0.0
            e_var = 0.0
            ex = 0.0
            ex2 = 0.0
            for (a, w), p in new_post.items():
                mean_x = a + w / 2.0
                var_x = w * (w + 2) / 12.0
                inner_mean = n_rem_after * mean_x
                inner_var = n_rem_after * var_x
                e_inner += p * inner_mean
                e2_inner += p * inner_mean * inner_mean
                e_var += p * inner_var
                ex += p * mean_x
                ex2 += p * (var_x + mean_x * mean_x)
            var_a = e_var + (e2_inner - e_inner * e_inner)
            fair_a = running_after + e_inner
            sigma_a = math.sqrt(max(0.0, var_a))
            fair_b = ex
            sigma_b = math.sqrt(max(0.0, ex2 - ex * ex))

            # Passive quotes
            bid_a, ask_a = self._quote_prices("A", fair_a, pos_a, "normal")
            bid_b, ask_b = self._quote_prices("B", fair_b, pos_b, "normal")

            # Sweep thresholds — lift any ask ≤ fair − sweep_edge,
            # hit any bid ≥ fair + sweep_edge. Quantized to tick.
            lift_a = int(math.floor(fair_a - sweep_edge))
            hit_a = int(math.ceil(fair_a + sweep_edge))
            lift_b = int(math.floor(fair_b - sweep_edge))
            hit_b = int(math.ceil(fair_b + sweep_edge))
            # Quantize to tick (floor for lift, ceil for hit so we never
            # cross our own threshold accidentally).
            lift_a = (lift_a // tick_a) * tick_a
            hit_a = ((hit_a + tick_a - 1) // tick_a) * tick_a
            lift_b = (lift_b // tick_b) * tick_b
            hit_b = ((hit_b + tick_b - 1) // tick_b) * tick_b

            new_table[v] = _Scenario(
                value=v, prob=marg,
                fair_a=fair_a, sigma_a=sigma_a,
                fair_b=fair_b, sigma_b=sigma_b,
                bid_a=bid_a, ask_a=ask_a,
                bid_b=bid_b, ask_b=ask_b,
                lift_to_a=lift_a, hit_to_a=hit_a,
                lift_to_b=lift_b, hit_to_b=hit_b,
            )

        # Only commit if the posterior didn't advance during compute.
        if self._reveal_count != cycle_at_start:
            return
        with self._precompute_lock:
            self._precompute = new_table
            self._precompute_for_cycle = cycle_at_start
            self._precompute_run_count += 1
            self._last_precompute_us = (time.perf_counter() - t0) * 1e6

    # ==================================================================
    # HOT PATH on reveal: dict lookup → parallel IOC sweep → apply quotes
    # ==================================================================
    def on_reveal(self, value) -> None:
        try:
            v = int(round(float(value)))
        except Exception:
            print(f"[v18 ON_REVEAL] bad value {value!r}")
            return

        # 1. O(1) precompute lookup BEFORE posterior update.
        with self._precompute_lock:
            scen = self._precompute.get(v)

        # 2. Posterior update + counters under single lock.
        with self.lock:
            self.posterior.update(value)
            self._reveal_count = len(self.posterior.reveals)
            self._running_sum = float(sum(self.posterior.reveals))
            self._last_reveal_t = time.time()
            self._bump_posterior_gen()

        # 2b. Flip endgame flag if this was the LAST reveal — fair_A is
        # now an exact constant, switch to burst sweep mode.
        self._maybe_activate_endgame()

        if scen is None:
            # Cache miss — fall back to live recompute (rare).
            print(f"[v18 MISS] no precompute for v={v}; live refresh")
            self._refresh_mm_quotes()
            self._precompute_request.set()
            return

        # 3. Parallel IOC sweep using precomputed lift_to / hit_to.
        ioc_a = self._sweep_post_reveal("A", scen.lift_to_a, scen.hit_to_a,
                                        scen.fair_a)
        ioc_b = self._sweep_post_reveal("B", scen.lift_to_b, scen.hit_to_b,
                                        scen.fair_b)

        # 4. Apply pre-staged passive quotes (parallel modify or fresh post).
        self._apply_quote("A", scen.bid_a, scen.ask_a)
        self._apply_quote("B", scen.bid_b, scen.ask_b)

        # 5. Cross-symbol arb pass. The reveal is on the private feed; the
        # public book is ~15ms behind. Any laggard mispricing that
        # survived steps 3-4 (or only became visible at the new posterior)
        # gets picked off here while competitors are still reading the
        # old book.
        try:
            ioc_arb = self._try_cross_arb("reveal")
        except Exception as e:
            print(f"[v18 ARB-ERR reveal] {type(e).__name__}: {e}")
            ioc_arb = 0

        print(f"[v18 HOT] v={v} fair_a={scen.fair_a:.1f} fair_b={scen.fair_b:.2f}  "
              f"A=[{scen.bid_a},{scen.ask_a}] lift/hit=[{scen.lift_to_a},{scen.hit_to_a}]  "
              f"B=[{scen.bid_b},{scen.ask_b}] lift/hit=[{scen.lift_to_b},{scen.hit_to_b}]  "
              f"ioc_a={ioc_a} ioc_b={ioc_b} arb={ioc_arb}")

        # 6. Trigger precompute rebuild for the NEW posterior.
        self._precompute_request.set()

    def _sweep_post_reveal(self, sym: str, lift_to: int, hit_to: int,
                           fair: float) -> int:
        """Snap the book and fire IOCs against laggard prices using the
        precomputed lift_to / hit_to thresholds. Returns count of orders
        fired."""
        if self._in_lockout():
            return 0
        s = self.state[sym]
        book = s.book
        if not book:
            return 0
        max_slice = self.cfg.sweep_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        fired = 0

        # BUY asks priced <= lift_to (positive edge).
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px > lift_to:
                    break  # sorted ascending
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, px):
                    taken += want
                    room_buy -= want
                    fired += 1
                    print(f"[v18 SWP:{sym}] BUY {want}@{px}  "
                          f"fair={fair:.1f} lift_to={lift_to}")
                else:
                    break

        # SELL bids priced >= hit_to (positive edge).
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px < hit_to:
                    break  # sorted descending
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, px):
                    taken += want
                    room_sell -= want
                    fired += 1
                    print(f"[v18 SWP:{sym}] SELL {want}@{px}  "
                          f"fair={fair:.1f} hit_to={hit_to}")
                else:
                    break

        if fired:
            self._reveal_ioc_count += fired
        return fired

    # ==================================================================
    # MM refresh loop (between reveals)
    # ==================================================================
    def _mm_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_mm_quotes()
            except Exception as e:
                print(f"[v18 MM-ERR] {type(e).__name__}: {e}")
            self._mm_wake.wait(timeout=self.cfg.mm_refresh_sec)
            self._mm_wake.clear()
            if self._stop.is_set():
                return

    def _refresh_mm_quotes(self) -> None:
        if not self._can_post():
            return
        # In endgame mode A's settlement is known — don't repost MM
        # quotes on A (they'd just be picked off by other snipers).
        # Skip the loop entirely if both symbols would be excluded.
        if self._endgame_active:
            return
        now = time.time()
        if (self.cfg.mm_min_interval_sec > 0
                and (now - self._last_mm_refresh_t)
                < self.cfg.mm_min_interval_sec):
            return
        self._last_mm_refresh_t = now
        in_park = self._in_park_window()
        for sym in self.SYMS:
            try:
                if sym == "A":
                    fair, _ = self.fair_a()
                else:
                    fair, _ = self.fair_b()
                pos = self.state[sym].position
                if in_park:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "park")
                else:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "normal")
                    bid_px, ask_px = self._maybe_penny(
                        sym, fair, pos, bid_px, ask_px)
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v18 MM:{sym}-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # Park loop — tight ~50ms tick, parks at T_reveal − park_sec exactly
    # ==================================================================
    def _park_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._park_tick()
            except Exception as e:
                print(f"[v18 PARK-ERR] {type(e).__name__}: {e}")
            self._stop.wait(0.05)

    def _park_tick(self) -> None:
        if not self._can_post():
            return
        if not self._in_park_window():
            return
        # Park ONCE per reveal index — don't re-park on every tick.
        if self._last_park_reveal_idx == self._reveal_count:
            return
        self._last_park_reveal_idx = self._reveal_count
        # Apply park quotes for both symbols. This goes through the same
        # _apply_quote path as everything else, so modify-replace is
        # used and the order survives the window at deep price.
        for sym in self.SYMS:
            try:
                if sym == "A":
                    fair, _ = self.fair_a()
                else:
                    fair, _ = self.fair_b()
                pos = self.state[sym].position
                bid_px, ask_px = self._quote_prices(sym, fair, pos, "park")
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v18 PARK:{sym}-ERR] {type(e).__name__}: {e}")
        nxt = self._next_reveal_at()
        eta = (nxt - time.time()) if nxt is not None else float("nan")
        print(f"[v18 PARK] parked all sides, reveal in {eta:.2f}s")

    # ==================================================================
    # Endgame burst loop — after last reveal, A's settlement is KNOWN.
    # Walk full book depth at 25Hz and snipe every mispriced level.
    # ==================================================================
    def _endgame_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if (self.cfg.endgame_enabled
                        and self._endgame_active
                        and self.phase == "running"):
                    self._endgame_tick()
            except Exception as e:
                print(f"[v18 ENDGAME-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.endgame_tick_sec)

    def _endgame_tick(self) -> None:
        """One pass through both books, fire IOCs on anything mispriced.

        After last reveal, posterior collapses → fair_A = running_sum
        exact, fair_B = E[X] is no longer in play (B settled per reveal).
        We focus the burst on A and cancel our own resting quotes so they
        can't be picked off by competitors during the burst.
        """
        if self._in_lockout():
            return
        if self._tokens_available() < self.cfg.endgame_min_tokens:
            return

        # A: fair is exact. Cancel any of our resting quotes that aren't
        # the BBO improvement — they could only get filled at adverse
        # prices now that fair is known.
        fair_a = self._running_sum  # by definition after last reveal
        edge = self.cfg.endgame_edge_ticks_a
        max_levels = self.cfg.endgame_max_levels
        max_slice = self.cfg.endgame_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks

        s = self.state["A"]
        book = s.book
        if not book:
            return
        fired = 0

        # BUY asks priced ≤ running_sum − edge
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            for lvl in (book.get("asks") or [])[:max_levels]:
                if room_buy <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if fair_a - px < edge:
                    break
                if max_dist > 0 and abs(px - fair_a) > max_dist:
                    continue
                want = min(max_slice, room_buy, size)
                if self._ioc("A", "buy", want, px):
                    room_buy -= want
                    fired += 1
                    print(f"[v18 ENDGAME:A] BUY {want}@{px}  "
                          f"settle={fair_a:.0f} edge={fair_a - px:.1f}t")

        # SELL bids priced ≥ running_sum + edge
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            for lvl in (book.get("bids") or [])[:max_levels]:
                if room_sell <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px - fair_a < edge:
                    break
                if max_dist > 0 and abs(px - fair_a) > max_dist:
                    continue
                want = min(max_slice, room_sell, size)
                if self._ioc("A", "sell", want, px):
                    room_sell -= want
                    fired += 1
                    print(f"[v18 ENDGAME:A] SELL {want}@{px}  "
                          f"settle={fair_a:.0f} edge={px - fair_a:.1f}t")

        if fired:
            self._endgame_count += fired

    def _maybe_activate_endgame(self) -> None:
        """Flip the endgame flag on the LAST reveal."""
        if self._endgame_active:
            return
        if not self.cfg.endgame_enabled:
            return
        thresh = self.cfg.endgame_activate_at_remaining
        if self._n_remaining() <= thresh:
            self._endgame_active = True
            self._endgame_pos_a_at_entry = self.state["A"].position
            # Cancel our own resting orders on A — we don't want to be
            # picked off at adverse prices now that settlement is known.
            try:
                self.c.cancel_all(symbol="A")
                self.state["A"].resting["bid"].clear()
                self.state["A"].resting["ask"].clear()
            except Exception as e:
                print(f"[v18 ENDGAME] cancel_all A failed: {e}")
            print(f"[v18 ENDGAME] ACTIVE  rs={self._running_sum:.0f}  "
                  f"pos_a={self._endgame_pos_a_at_entry}  "
                  f"tick={self.cfg.endgame_tick_sec*1000:.0f}ms  "
                  f"edge={self.cfg.endgame_edge_ticks_a}t  "
                  f"depth={self.cfg.endgame_max_levels}")

    # ==================================================================
    # Connection pre-warm — saturate the pool with cheap GETs so the
    # first POST after an idle stretch doesn't eat a TCP handshake.
    # ==================================================================
    def _prewarm_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._prewarm_tick()
            except Exception as e:
                print(f"[v18 PREWARM-ERR] {type(e).__name__}: {e}")
            self._stop.wait(0.2)

    def _prewarm_tick(self) -> None:
        if not self.cfg.prewarm_enabled:
            return
        if self.phase != "running":
            return
        if self._in_lockout():
            return
        now = time.time()
        # Periodic idle keep-alive.
        do_periodic = (
            (now - self._last_prewarm_t) >= self.cfg.prewarm_interval_sec)
        # Burst right before reveal — confirm pool health for the IOC
        # fan-out about to fire.
        nxt = self._next_reveal_at()
        in_burst_window = (
            nxt is not None
            and 0.0 <= (nxt - now) <= self.cfg.prewarm_before_reveal_sec
            and self._prewarm_before_reveal_idx != self._reveal_count)
        if not (do_periodic or in_burst_window):
            return
        if self._tokens_available() < 2.0:
            # Don't burn rate-limit budget on warm-ups when bucket is low.
            return
        # Cheapest authenticated GET. Doesn't count as a strategy-side
        # write (no _register_send) — SDK bucket still gates it.
        try:
            self.c.tokens_available()  # local check, free
            # Use my_orders('A') as the warm-up — single tiny response
            # that exercises the same Session/keep-alive path as POSTs.
            self.c.my_orders(symbol="A")
            self._last_prewarm_t = now
            if in_burst_window:
                self._prewarm_before_reveal_idx = self._reveal_count
        except Exception:
            pass  # warm-up is best-effort

    # ==================================================================
    # Quote application — modify-replace fast path with multi-order safety
    # ==================================================================
    def _resting_qty(self, sym: str, side: str) -> int:
        return sum(int(e.get("qty") or 0)
                   for e in self.state[sym].resting[side].values())

    def _primary_resting(self, sym: str, side: str) -> Optional[dict]:
        entries = self.state[sym].resting[side]
        if not entries:
            return None
        return entries[max(entries.keys())]

    def _quote_size(self, sym: str, side: str) -> int:
        s = self.state[sym]
        base = self.cfg.size_a if sym == "A" else self.cfg.size_b
        pad = self.cfg.mm_inventory_safety_pad
        same_side_out = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        if side == "bid":
            headroom = ((s.position_limit - pad)
                        - s.position - same_side_out - in_flight)
        else:
            headroom = ((s.position_limit - pad)
                        + s.position - same_side_out - in_flight)
        return max(0, min(base, headroom + same_side_out))

    def _tokens_available(self) -> float:
        fn = getattr(self.c, "tokens_available", None)
        if fn is None:
            return float("inf")
        try:
            return float(fn())
        except Exception:
            return float("inf")

    def _in_lockout(self) -> bool:
        return time.monotonic() < self._lockout_until

    def _can_post(self) -> bool:
        """Master gate for any order-creating REST call (POST/MODIFY).
        False if game isn't in trading phase OR we've already seen a
        terminal 403 (game ended). Cancels can still go through during
        non-running phase via _cancel() so flatten cleanup works."""
        if self._post_terminal_blocked:
            return False
        if self.phase != "running":
            return False
        return True

    @staticmethod
    def _http_status_and_body(e: Exception) -> Tuple[Optional[int], str]:
        """Best-effort extraction of (status, body) from a requests
        HTTPError. Returns (None, '') if e isn't an HTTPError or has
        no .response attached."""
        resp = getattr(e, "response", None)
        if resp is None:
            return None, ""
        status = getattr(resp, "status_code", None)
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            body = ""
        return status, body

    def _handle_forbidden(self, ctx: str, sym: str, side: str,
                          target_px: Optional[int],
                          e: Exception) -> None:
        """Central 403 handler. Three things:
          1. Log the response body once so we can see WHY (auth /
             trading_closed / position / etc.).
          2. Re-fetch game_state to confirm phase. If server says we're
             NOT running, set the terminal block immediately.
          3. Count 403s in a sliding window; trip the circuit breaker
             if too many even after the phase recheck says we're fine
             (something else is broken — auth, position, etc.).
        """
        status, body = self._http_status_and_body(e)
        self._forbidden_count += 1
        now = time.monotonic()
        with self._forbidden_log_lock:
            self._forbidden_log.append(now)
            cutoff = now - self.cfg.forbidden_circuit_window_sec
            while (self._forbidden_log
                   and self._forbidden_log[0] < cutoff):
                self._forbidden_log.popleft()
            in_window = len(self._forbidden_log)

        # Log diagnostic info on first 403 of a streak.
        if self._forbidden_count == 1 or self._forbidden_count % 10 == 0:
            print(f"[v18 FORBIDDEN:{ctx}:{sym}/{side}@{target_px}] "
                  f"status={status} body={body!r} "
                  f"count={self._forbidden_count} "
                  f"in_window={in_window}/"
                  f"{self.cfg.forbidden_circuit_max}")

        # Throttled authoritative phase re-check. If the server says
        # we're not running, set the terminal block so subsequent sends
        # short-circuit silently.
        if (now - self._last_phase_recheck_t
                >= self.cfg.forbidden_phase_recheck_throttle_sec):
            self._last_phase_recheck_t = now
            try:
                gs = self.c.game_state()
                server_phase = gs.get("phase")
                if server_phase != "running":
                    if not self._post_terminal_blocked:
                        print(f"[v18 FORBIDDEN] server phase="
                              f"{server_phase!r} (local was "
                              f"{self.phase!r}) → setting terminal block")
                    self.phase = server_phase
                    self._post_terminal_blocked = True
            except Exception as e2:
                print(f"[v18 FORBIDDEN] phase recheck failed: "
                      f"{type(e2).__name__}: {e2}")

        # Circuit breaker: too many 403s in window even though we
        # believe we should be allowed → hard-block sends.
        if (in_window >= self.cfg.forbidden_circuit_max
                and not self._post_terminal_blocked):
            print(f"[v18 FORBIDDEN] circuit breaker tripped "
                  f"({in_window} 403s in "
                  f"{self.cfg.forbidden_circuit_window_sec:.0f}s) → "
                  f"blocking sends until phase transition")
            self._post_terminal_blocked = True

    def _can_send_now(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.cfg.sends_window_sec
        with self._send_log_lock:
            while self._send_log and self._send_log[0] < cutoff:
                self._send_log.popleft()
            return len(self._send_log) < self.cfg.max_sends_per_second

    def _register_send(self) -> None:
        now = time.monotonic()
        cutoff = now - self.cfg.sends_window_sec
        with self._send_log_lock:
            while self._send_log and self._send_log[0] < cutoff:
                self._send_log.popleft()
            self._send_log.append(now)

    def on_reject_event(self, msg: dict) -> None:
        if (msg.get("reason") or "").lower() != "rate_limited":
            return
        retry_ms = msg.get("retry_after_ms")
        try:
            retry_ms = float(retry_ms) if retry_ms is not None else 0.0
        except Exception:
            retry_ms = 0.0
        if retry_ms <= 0:
            retry_ms = self.cfg.lockout_min_pad_ms
        retry_ms = max(self.cfg.lockout_min_pad_ms,
                       min(self.cfg.lockout_max_pad_ms, retry_ms))
        new_until = time.monotonic() + retry_ms / 1000.0
        if new_until > self._lockout_until:
            self._lockout_until = new_until
            self._lockout_count += 1
            print(f"[v18 LOCKOUT] op={msg.get('op')!r} sym={msg.get('symbol')!r} "
                  f"retry_ms={retry_ms:.0f} count={self._lockout_count}")

    def _make_coid(self) -> str:
        self._coid_counter += 1
        return f"{self.cfg.client_order_id_prefix}-{self._coid_counter}"

    def _can_send(self, sym: str, side: str, qty: int,
                  is_modify: bool = False, primary_qty: int = 0) -> bool:
        s = self.state[sym]
        cap = s.position_limit - self.cfg.mm_inventory_safety_pad
        resting = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        add = max(0, qty - primary_qty) if is_modify else qty
        if side == "bid":
            worst = s.position + resting + in_flight + add
            if worst > cap:
                return False
        else:
            worst = s.position - resting - in_flight - add
            if worst < -cap:
                return False
        return True

    def _apply_quote(self, sym: str, bid_px: Optional[int],
                     ask_px: Optional[int]) -> None:
        s = self.state[sym]
        with self.lock:
            bid_sz = self._quote_size(sym, "bid") if bid_px is not None else 0
            ask_sz = self._quote_size(sym, "ask") if ask_px is not None else 0
            self._apply_side(sym, "bid",
                             bid_px if bid_sz > 0 else None, bid_sz)
            self._apply_side(sym, "ask",
                             ask_px if ask_sz > 0 else None, ask_sz)

    def _apply_side(self, sym: str, side: str,
                    target_px: Optional[int], target_qty: int) -> None:
        if self._in_lockout():
            return
        s = self.state[sym]
        side_orders = s.resting[side]
        primary = self._primary_resting(sym, side)

        # Cancel-only branch
        if target_px is None or target_qty <= 0:
            if (len(side_orders) >= 2
                    and self._tokens_available()
                    < self.cfg.bulk_cancel_min_tokens):
                return
            for oid in list(side_orders.keys()):
                if not self._cancel(sym, oid):
                    return
                side_orders.pop(oid, None)
            return

        # Drain orphans before any send.
        if primary is not None:
            orphans = [oid for oid in side_orders.keys()
                       if oid != primary["order_id"]]
            if (len(orphans) >= 2
                    and self._tokens_available()
                    < self.cfg.bulk_cancel_min_tokens):
                return
            for oid in orphans:
                if not self._cancel(sym, oid):
                    return
                side_orders.pop(oid, None)
            primary = self._primary_resting(sym, side)

        # Modify-replace fast path
        if primary is not None and self.cfg.use_modify_replace:
            same_px = (abs(primary["price"] - target_px)
                       < self.cfg.quote_min_change_ticks)
            same_qty = primary["qty"] == target_qty
            if same_px and same_qty:
                return
            if not self._can_post():
                return
            primary_qty = int(primary.get("qty") or 0)
            if not self._can_send(sym, side, target_qty,
                                  is_modify=True, primary_qty=primary_qty):
                return
            if self._tokens_available() < self.cfg.mm_send_min_tokens:
                return
            if not self._can_send_now():
                self._sends_deferred += 1
                return
            self._register_send()
            old_oid = primary["order_id"]
            net_add = max(0, target_qty - primary_qty)
            s.in_flight[side] += net_add
            try:
                res = self.c.modify(
                    old_oid,
                    price=target_px if not same_px else None,
                    qty=target_qty if not same_qty else None)
                order = (res or {}).get("order") or {}
                new_oid = (order.get("order_id") or order.get("id")
                           or old_oid)
                new_qty = order.get("qty", target_qty)
                if new_oid != old_oid:
                    side_orders.pop(old_oid, None)
                side_orders[new_oid] = {
                    "order_id": new_oid,
                    "price": target_px,
                    "qty": int(new_qty),
                    "added_t": time.time(),
                }
                self._modify_count += 1
                return
            except Exception as e:
                msg = str(e).lower()
                is_403 = "403" in msg or "forbidden" in msg
                terminal = (any(x in msg for x in
                                ("404", "not found", "no such", "gone"))
                            or "400" in msg or "bad request" in msg
                            or is_403)
                if is_403:
                    self._handle_forbidden(
                        "MODIFY", sym, side, target_px, e)
                    side_orders.pop(old_oid, None)
                elif terminal:
                    side_orders.pop(old_oid, None)
                else:
                    print(f"[v18 MODIFY:{sym}/{side}@{target_px}x"
                          f"{target_qty}] {type(e).__name__}: {e}")
                    return
            finally:
                s.in_flight[side] = max(
                    0, s.in_flight[side] - net_add)
        elif primary is not None:
            if not self._cancel(sym, primary["order_id"]):
                return
            side_orders.pop(primary["order_id"], None)

        # Post fresh
        if target_px < self.state[sym].tick:
            return
        if not self._can_post():
            return
        backoff_t = self._post_backoff_until.get((sym, side), 0.0)
        if backoff_t and time.monotonic() < backoff_t:
            return
        for oid in list(side_orders.keys()):
            if self._cancel(sym, oid):
                side_orders.pop(oid, None)
            else:
                return
        if not self._can_send(sym, side, target_qty):
            return
        if self._tokens_available() < self.cfg.mm_send_min_tokens:
            return
        # Duplicate-price guard
        for entry in side_orders.values():
            if int(entry.get("price", 0)) == int(target_px):
                return
        if not self._can_send_now():
            self._sends_deferred += 1
            return
        self._register_send()
        fn = self.c.buy if side == "bid" else self.c.sell
        s.in_flight[side] += target_qty
        try:
            res = fn(sym, target_px, target_qty,
                     client_order_id=self._make_coid())
            res_obj = res or {}
            nested = res_obj.get("order") or {}
            oid = (res_obj.get("order_id") or res_obj.get("id")
                   or nested.get("order_id") or nested.get("id"))
            if oid is not None:
                side_orders[oid] = {
                    "order_id": oid,
                    "price": target_px,
                    "qty": target_qty,
                    "added_t": time.time(),
                }
            self._post_count += 1
            self._post_backoff_until.pop((sym, side), None)
        except Exception as e:
            msg = str(e).lower()
            is_403 = "403" in msg or "forbidden" in msg
            terminal = ("400" in msg or "bad request" in msg or is_403)
            if is_403:
                self._handle_forbidden("POST", sym, side, target_px, e)
            elif terminal:
                already = (self._post_backoff_until.get(
                    (sym, side), 0.0) > time.monotonic())
                self._post_backoff_until[(sym, side)] = (
                    time.monotonic() + self.cfg.post_backoff_sec)
                if not already:
                    print(f"[v18 POST:{sym}/{side}@{target_px}] "
                          f"{type(e).__name__}: {e} → backoff "
                          f"{self.cfg.post_backoff_sec}s")
            elif "limit" not in msg and "position" not in msg:
                print(f"[v18 POST:{sym}/{side}@{target_px}] "
                      f"{type(e).__name__}: {e}")
        finally:
            s.in_flight[side] = max(0, s.in_flight[side] - target_qty)

    def _cancel(self, sym: str, order_id) -> bool:
        if self._in_lockout():
            return False
        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        self._register_send()
        try:
            self.c.cancel(order_id)
            return True
        except Exception as e:
            msg = str(e).lower()
            if (any(x in msg for x in ("404", "not found", "no such", "gone"))
                    or "400" in msg or "bad request" in msg
                    or "403" in msg or "forbidden" in msg):
                return True
            print(f"[v18 CANCEL:{sym} oid={order_id}] "
                  f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # IOC sniping helper (used by sweep + inter-sweep)
    # ==================================================================
    def _ioc(self, sym: str, side: str, qty: int, price: int) -> bool:
        if qty <= 0 or self._in_lockout():
            return False
        if not self._can_post():
            return False
        max_dist = self.cfg.max_ioc_distance_ticks
        if max_dist > 0:
            fair = (self.fair_a()[0] if sym == "A" else self.fair_b()[0])
            if abs(price - fair) > max_dist:
                return False
        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        s = self.state[sym]
        cap_side = "bid" if side == "buy" else "ask"
        if not self._can_send(sym, cap_side, qty):
            return False
        self._register_send()
        if side == "buy" and (s.position + qty) > s.position_limit:
            qty = s.position_limit - s.position
            if qty <= 0:
                return False
        if side == "sell" and (s.position - qty) < -s.position_limit:
            qty = s.position + s.position_limit
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
                print(f"[v18 IOC:{sym}/{side}@{price}x{qty}] "
                      f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # Inter-reveal stale-quote sweep — fires on book_event
    # ==================================================================
    def _maybe_inter_sweep(self, sym: str) -> None:
        """Inter-reveal stale-quote sweep. Reads cached live thresholds
        from the FAST precompute loop (which already applied flatten-
        bias) — falls back to fair_a/fair_b if the cache is empty.

        Inter-sweep uses a wider `inter_sweep_edge_ticks` (3t default)
        than the post-reveal sweep — between reveals the book is mostly
        in equilibrium, so a 3t mispricing is a confident stale-quoter.
        """
        if not self.cfg.inter_sweep_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        now = time.time()
        if (now - s.last_inter_sweep_t) < self.cfg.inter_sweep_throttle_sec:
            return
        s.last_inter_sweep_t = now

        # Use live thresholds (flatten-aware) as baseline, then re-derive
        # the wider inter-sweep cutoff. Live cache: O(1).
        live_lift, live_hit, live_fair, in_flat = self._get_snipe_thresholds(
            sym, s.position)
        if live_fair == 0.0:
            # Fast loop hasn't ticked yet — direct compute.
            if sym == "A":
                live_fair, _ = self.fair_a()
            else:
                live_fair, _ = self.fair_b()
        fair = live_fair
        book = s.book
        if not book:
            return
        edge = self.cfg.inter_sweep_edge_ticks
        # In flatten window, reduce the edge on the flatten side.
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
        max_slice = self.cfg.inter_sweep_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        fired = 0

        # BUY asks below fair − edge_buy.
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
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
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, px):
                    taken += want
                    room_buy -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position < 0) else "INTER"
                    print(f"[v18 {tag}:{sym}] BUY {want}@{px} "
                          f"fair={fair:.1f} edge={fair - px:.1f}t "
                          f"pos={s.position}")
                else:
                    break

        # SELL bids above fair + edge_sell.
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
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
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, px):
                    taken += want
                    room_sell -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position > 0) else "INTER"
                    print(f"[v18 {tag}:{sym}] SELL {want}@{px} "
                          f"fair={fair:.1f} edge={px - fair:.1f}t "
                          f"pos={s.position}")
                else:
                    break
        if fired:
            self._inter_sweep_count += fired

    # ==================================================================
    # Dime-defense / BBO-claim — react to inside book changes
    # ==================================================================
    def _maybe_dime_defense(self, sym: str) -> None:
        """Per-side, on every book event, ensure we're sitting AT the
        inside BBO whenever stepping there still leaves >= min_edge vs
        fair. Three sub-cases per side:

          1. Dimed:  we have resting, competitor is strictly inside us
                     → modify/post to best ± tick.
          2. Absent: no resting on this side (fresh after fill, or before
                     MM refresh) → post at best ± tick if profitable.
          3. Outside: we have resting BELOW (or above for ask) the inside
                     → step in if min_edge holds. Catches cases where the
                     inside tightened without anyone explicitly diming us.

        Skipped during the pre-reveal park window — we WANT to be deep
        then. Cases 1/2/3 all share the per-symbol throttle so a v=1
        dime war is bounded; the throttle also caps REST traffic.
        """
        if not self.cfg.dime_defense_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        now = time.time()
        if (now - s.last_dime_defense_t) < self.cfg.dime_defense_throttle_sec:
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

        if sym == "A":
            fair, _ = self.fair_a()
            min_edge = self.cfg.penny_min_edge_a
        else:
            fair, _ = self.fair_b()
            min_edge = self.cfg.penny_min_edge_b
        tick = s.tick
        max_step_dist = self.cfg.penny_max_step_ticks * tick
        fired = False

        # --- Bid side ---
        my_best_bid = None
        for entry in s.resting["bid"].values():
            px = int(entry.get("price") or 0)
            if my_best_bid is None or px > my_best_bid:
                my_best_bid = px
        # Default target: step 1t inside the inside. If we're already AT
        # or above the inside, no action — we're already BBO.
        if my_best_bid is None or my_best_bid < best_bid:
            target_bid = best_bid + tick
            # Edge floor (preserves maker profit after fees) + no cross +
            # cap on step distance (so a one-side collapse can't drag us
            # too far in).
            if (target_bid <= fair - min_edge
                    and target_bid < best_ask
                    and (my_best_bid is None
                         or (target_bid - my_best_bid) <= max_step_dist)):
                qty = self._quote_size(sym, "bid")
                if qty > 0:
                    case = "absent" if my_best_bid is None else "dimed"
                    print(f"[v18 BBO:{sym}/bid {case}] best={best_bid}"
                          f"(mine={my_best_bid}) → step to {target_bid}"
                          f" fair={fair:.1f}")
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
            if (target_ask >= fair + min_edge
                    and target_ask > best_bid
                    and (my_best_ask is None
                         or (my_best_ask - target_ask) <= max_step_dist)):
                qty = self._quote_size(sym, "ask")
                if qty > 0:
                    case = "absent" if my_best_ask is None else "dimed"
                    print(f"[v18 BBO:{sym}/ask {case}] best={best_ask}"
                          f"(mine={my_best_ask}) → step to {target_ask}"
                          f" fair={fair:.1f}")
                    self._apply_side(sym, "ask", target_ask, qty)
                    fired = True

        if fired:
            s.last_dime_defense_t = now

    # ==================================================================
    # Cross-symbol arb + private-feed (15ms) exploit
    # ==================================================================
    def _try_cross_arb(self, source: str) -> int:
        """Scan BOTH books and snipe any level past `cross_arb_edge_ticks`
        from fair. Walks `cross_arb_levels` deep on each side.

        The 15ms private-feed advantage: when called from on_fill_event
        or on_reveal (private events with no public-feed delay), our view
        of the new world is fresh but competitors are still pricing off
        a stale public book for ~15ms. Their stale liquidity is the
        cheapest it'll ever be — take it.

        Bypasses the inter_sweep_throttle (this is a private-event hook,
        not a book-flap reaction) but still honors:
          * server lockout cutoff (_in_lockout)
          * sliding-window rate cap (_can_send_now via _ioc)
          * max_ioc_distance trap filter
          * inventory pad

        `source` ∈ {"fill", "reveal", "manual"} — for log telemetry.
        """
        if not self.cfg.cross_arb_enabled:
            return 0
        if not self._can_post():
            return 0
        if self._in_lockout():
            return 0
        if self._in_park_window():
            return 0  # don't add risk in the 0.8s before reveal

        # Read live thresholds (FAST precompute) — already includes any
        # flatten-bias relaxation per direction.
        base_edge = self.cfg.cross_arb_edge_ticks
        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks
        max_slice = self.cfg.cross_arb_max_slice
        depth = max(1, self.cfg.cross_arb_levels)
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        fired_total = 0

        with self._live_lock:
            live_a = self._live_thresholds.get("A")
            live_b = self._live_thresholds.get("B")

        for sym, live in (("A", live_a), ("B", live_b)):
            s = self.state[sym]
            book = s.book
            if not book:
                continue

            # Determine effective edge per direction. If live cache is
            # populated and we're in a flatten window, relax on the
            # position-reducing side.
            if live is not None:
                fair = live.fair
                in_flat = live.in_flatten_window
            else:
                fair, _ = (self.fair_a() if sym == "A" else self.fair_b())
                in_flat = False
            edge_buy = base_edge
            edge_sell = base_edge
            if in_flat:
                if s.position < 0:
                    edge_buy = max(flat_min, base_edge - flat_reduction)
                if s.position > 0:
                    edge_sell = max(flat_min, base_edge - flat_reduction)

            # BUY mispriced asks (price ≤ fair − edge_buy), depth levels.
            room_buy = (s.position_limit - pad) - s.position
            if room_buy > 0:
                taken = 0
                for lvl in (book.get("asks") or [])[:depth]:
                    if room_buy <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if fair - px < edge_buy:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    want = min(max_slice - taken, room_buy, size)
                    if want <= 0:
                        break
                    if self._ioc(sym, "buy", want, px):
                        taken += want
                        room_buy -= want
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position < 0)
                               else f"ARB({source})")
                        print(f"[v18 {tag}:{sym}] BUY {want}@{px}  "
                              f"fair={fair:.2f} edge={fair - px:.2f}t "
                              f"pos={s.position}")
                    else:
                        break

            # SELL mispriced bids (price ≥ fair + edge_sell), depth levels.
            room_sell = s.position - (-s.position_limit + pad)
            if room_sell > 0:
                taken = 0
                for lvl in (book.get("bids") or [])[:depth]:
                    if room_sell <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if px - fair < edge_sell:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    want = min(max_slice - taken, room_sell, size)
                    if want <= 0:
                        break
                    if self._ioc(sym, "sell", want, px):
                        taken += want
                        room_sell -= want
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position > 0)
                               else f"ARB({source})")
                        print(f"[v18 {tag}:{sym}] SELL {want}@{px}  "
                              f"fair={fair:.2f} edge={px - fair:.2f}t "
                              f"pos={s.position}")
                    else:
                        break

        if fired_total:
            self._cross_arb_count += fired_total
        return fired_total

    # ==================================================================
    # Reconcile (orphan cleanup)
    # ==================================================================
    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.phase == "running":
                    self._reconcile_once()
            except Exception as e:
                print(f"[v18 RECON-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.reconcile_check_sec)

    def _reconcile_once(self) -> None:
        if self._in_lockout():
            return
        if self._tokens_available() < self.cfg.reconcile_min_tokens:
            return
        for sym in self.SYMS:
            if not self._can_send_now():
                self._sends_deferred += 1
                continue
            self._register_send()
            try:
                orders = self.c.my_orders(symbol=sym)
            except Exception as e:
                msg = str(e).lower()
                if "rate" not in msg:
                    print(f"[v18 RECON:{sym}] my_orders: "
                          f"{type(e).__name__}: {e}")
                continue
            server_oids = {int(o.get("order_id") or o.get("id") or -1): o
                           for o in (orders or [])
                           if (o.get("order_id") or o.get("id")) is not None}
            s = self.state[sym]
            now = time.time()
            min_age = self.cfg.reconcile_min_age_sec
            with self.lock:
                # Prune ghost entries
                for side in ("bid", "ask"):
                    for oid in list(s.resting[side].keys()):
                        if oid in server_oids:
                            continue
                        e = s.resting[side][oid]
                        if (now - float(e.get("added_t", 0))) >= min_age:
                            s.resting[side].pop(oid, None)
                # Cancel server-side orphans tagged with our prefix
                tracked = set(s.resting["bid"]) | set(s.resting["ask"])
                prefix = self.cfg.client_order_id_prefix
                respect = self.cfg.reconcile_respect_manual_orders
                orphan_oids = []
                for oid, o in server_oids.items():
                    if oid in tracked:
                        continue
                    if respect:
                        coid = o.get("client_order_id")
                        if coid is None or not str(coid).startswith(prefix):
                            continue
                    orphan_oids.append(oid)
                total = len(orphan_oids)
                bulk_thr = self.cfg.reconcile_bulk_cancel_threshold
                if (bulk_thr > 0 and total >= bulk_thr
                        and self._can_send_now()):
                    self._register_send()
                    try:
                        self.c.cancel_all(symbol=sym)
                        for side in ("bid", "ask"):
                            s.resting[side].clear()
                        print(f"[v18 RECON:{sym}] BULK cancel_all "
                              f"({total} orphans)")
                        continue
                    except Exception as e:
                        print(f"[v18 RECON:{sym}] cancel_all failed: {e}")
                cap = max(1, self.cfg.reconcile_max_cancels_per_tick)
                cancelled = 0
                for oid in orphan_oids[:cap]:
                    if not self._cancel(sym, oid):
                        break
                    cancelled += 1
                # Only log when above the noise threshold or when we
                # had to cap (capping hints at a real leak worth seeing).
                capped = total > cap
                if total >= self.cfg.reconcile_print_min or capped:
                    suffix = (f" (capped at {cap}/{total})"
                              if capped else "")
                    print(f"[v18 RECON:{sym}] cancelled {cancelled} "
                          f"orphan(s){suffix}")

    # ==================================================================
    # Book helpers
    # ==================================================================
    def _book_best(self, sym: str, side: str) -> Optional[Tuple[int, int]]:
        book = self.state[sym].book
        if not book:
            return None
        lvls = book.get(side) or []
        if not lvls:
            return None
        return (lvls[0]["price"], int(lvls[0].get("qty") or 0))

    # ==================================================================
    # WS event handlers
    # ==================================================================
    def on_book_event(self, msg: dict) -> None:
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        s = self.state[sym]
        s.book = {
            "bids": list(msg.get("bids") or []),
            "asks": list(msg.get("asks") or []),
        }
        s.book_t = time.time()
        # Penny/dime + inter-reveal stale sweep both react here. Both
        # are no-ops during the park window.
        self._maybe_inter_sweep(sym)
        # Reclaim BBO if a competitor stepped inside us. Runs on EVERY
        # book event (own throttle) so we don't sit dimed for a full
        # mm_refresh_sec window.
        self._maybe_dime_defense(sym)

    def on_fill_event(self, msg: dict) -> None:
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        s = self.state[sym]
        side = msg.get("side")
        qty = int(msg.get("qty") or 0)
        if qty <= 0 or side not in ("buy", "sell"):
            return
        with self.lock:
            s.position += qty if side == "buy" else -qty
        if msg.get("liquidity") == "maker":
            ord_id = msg.get("order_id")
            for kind in ("bid", "ask"):
                entry = s.resting[kind].get(ord_id)
                if entry is not None:
                    new_qty = max(0, int(entry.get("qty") or 0) - qty)
                    if new_qty == 0:
                        s.resting[kind].pop(ord_id, None)
                    else:
                        entry["qty"] = new_qty
                    break
        # 15ms private-feed exploit: the print hasn't propagated to the
        # public WS feed yet, so competitors are still seeing the
        # pre-fill book. Scan both A and B for arb edges at fresh fairs
        # before they reprice. Runs BEFORE the defensive-cancel block so
        # the IOC bursts go out first; cancels are second-order.
        try:
            self._try_cross_arb("fill")
        except Exception as e:
            print(f"[v18 ARB-ERR fill] {type(e).__name__}: {e}")
        # Defensive cancel if we hit the pad zone.
        pad = self.cfg.mm_inventory_safety_pad
        if s.position >= s.position_limit - pad:
            self._defensive_cancel_side(sym, "bid")
        if s.position <= -(s.position_limit - pad):
            self._defensive_cancel_side(sym, "ask")
        self._emergency_flatten_if_needed(sym)
        self._mm_wake.set()

    def _defensive_cancel_side(self, sym: str, side: str) -> None:
        s = self.state[sym]
        if not s.resting[side]:
            return
        if self._in_lockout():
            return
        if (len(s.resting[side]) >= 3
                and self._tokens_available()
                < self.cfg.bulk_cancel_min_tokens):
            return
        for oid in list(s.resting[side].keys()):
            if self._cancel(sym, oid):
                s.resting[side].pop(oid, None)

    def _emergency_flatten_if_needed(self, sym: str) -> None:
        s = self.state[sym]
        limit = s.position_limit
        trip = int(limit * self.cfg.emergency_flatten_factor)
        if abs(s.position) <= trip:
            return
        if s.resting["bid"] or s.resting["ask"]:
            try:
                self.c.cancel_all(symbol=sym)
                s.resting["bid"].clear()
                s.resting["ask"].clear()
            except Exception as e:
                print(f"[v18 EMG cancel_all:{sym}] "
                      f"{type(e).__name__}: {e}")
        if not self._can_post():
            return
        if s.position > trip:
            excess = s.position - (limit - self.cfg.mm_inventory_safety_pad)
            best = self._book_best(sym, "bids")
            if best is not None and excess > 0:
                px, sz = best
                try:
                    self.c.sell_ioc(sym, px, min(excess, sz))
                except Exception as e:
                    msg = str(e).lower()
                    if not (("403" in msg or "forbidden" in msg)
                            and self.phase != "running"):
                        print(f"[v18 EMG sell:{sym}] "
                              f"{type(e).__name__}: {e}")
                print(f"[v18 EMG:{sym}] pos={s.position}/lim={limit} "
                      f"IOC sell {min(excess, sz)}@{px}")
        else:
            excess = -s.position - (limit - self.cfg.mm_inventory_safety_pad)
            best = self._book_best(sym, "asks")
            if best is not None and excess > 0:
                px, sz = best
                try:
                    self.c.buy_ioc(sym, px, min(excess, sz))
                except Exception as e:
                    msg = str(e).lower()
                    if not (("403" in msg or "forbidden" in msg)
                            and self.phase != "running"):
                        print(f"[v18 EMG buy:{sym}] "
                              f"{type(e).__name__}: {e}")
                print(f"[v18 EMG:{sym}] pos={s.position}/lim={limit} "
                      f"IOC buy {min(excess, sz)}@{px}")

    def on_trade(self, msg: dict) -> None:
        pass

    def on_quote_event(self, msg: dict) -> None:
        pass

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        with self.lock:
            prev_phase = self.phase
            self.phase = phase
            if phase == "running":
                self._phase_running_t = time.time()
                self.posterior.reset(reveals or [])
                self._reveal_count = len(self.posterior.reveals)
                self._running_sum = float(sum(self.posterior.reveals))
                self._last_reveal_t = None
                self._last_park_reveal_idx = -1
                self._endgame_active = False  # reset on phase enter
                # Clear sticky 403 block — new round, server accepts orders.
                if self._post_terminal_blocked:
                    print(f"[v18 POST-TERMINAL] cleared "
                          f"(prev_phase={prev_phase!r}, count="
                          f"{self._terminal_403_count}, "
                          f"forbidden_total={self._forbidden_count})")
                self._post_terminal_blocked = False
                self._terminal_403_count = 0
                self._forbidden_count = 0
                with self._forbidden_log_lock:
                    self._forbidden_log.clear()
                self._last_phase_recheck_t = 0.0
                self._bump_posterior_gen()
                self._precompute_request.set()
                # If we joined mid-round AFTER the last reveal, flip
                # endgame immediately so the burst loop picks up.
                self._maybe_activate_endgame()
            elif phase in ("completed", "settling", "settled"):
                self._phase_running_t = None
                self._endgame_active = False
                # Pre-emptively block all POSTs so we don't race a 403.
                self._post_terminal_blocked = True
                for sym in self.SYMS:
                    for kind in ("bid", "ask"):
                        self.state[sym].resting[kind] = {}

    # ==================================================================
    # Flatten + stop
    # ==================================================================
    def flatten(self) -> None:
        self._stop.set()
        self._mm_wake.set()
        self._precompute_request.set()
        for sym in self.SYMS:
            try:
                self.c.cancel_all(symbol=sym)
            except Exception as e:
                print(f"[v18 FLATTEN cancel_all:{sym}] "
                      f"{type(e).__name__}: {e}")
            s = self.state[sym]
            for kind in ("bid", "ask"):
                s.resting[kind] = {}
        for sym in self.SYMS:
            s = self.state[sym]
            if s.position == 0:
                continue
            side = "sell" if s.position > 0 else "buy"
            qty = abs(s.position)
            best = self._book_best(
                sym, "bids" if side == "sell" else "asks")
            if best is None:
                continue
            px = best[0]
            self._ioc(sym, side, qty, px)
            print(f"[v18 FLATTEN:{sym}] {side} {qty}@{px}")
        try:
            self._ioc_executor.shutdown(wait=False)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        self._mm_wake.set()
        self._precompute_request.set()


# ===========================================================================
# Standalone runner — prefer run_combined18.py
# ===========================================================================
def main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")
    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} (a, w) pairs.")
    strat = Strategy(c, post)

    def on_reveal(msg):
        print(f"REVEAL #{msg.get('index')} = {msg.get('value')}  "
              f"running_sum={msg.get('running_sum')}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(f"FILL {msg['symbol']} {msg['side']:>4s} "
              f"{msg['qty']}@{msg['price']}  "
              f"liq={msg.get('liquidity')}  cp={msg.get('counterparty')}")
        strat.on_fill_event(msg)

    def on_game_state(msg):
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_message(msg):
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = strat.on_trade
    c.on_book = strat.on_book_event
    c.on_game_state = on_game_state
    c.on_message = on_message
    c.on_reject = strat.on_reject_event
    c.start()

    print("\nv18 standalone. s=status, p=precompute, f=flatten, q=quit.\n")
    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fa, sa = strat.fair_a()
                fb, sb = strat.fair_b()
                print(f"  phase={strat.phase}  k={strat._reveal_count}/"
                      f"{strat.n_total}  rs={strat._running_sum:.0f}")
                print(f"  A: pos={strat.state['A'].position}  "
                      f"fair={fa:.1f}+/-{sa:.1f}")
                print(f"  B: pos={strat.state['B'].position}  "
                      f"fair={fb:.2f}+/-{sb:.2f}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precompute.items())
                print(f"  {len(items)} scenarios:")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.prob:.3f}  "
                          f"A=[{sc.bid_a},{sc.ask_a}]/"
                          f"[{sc.lift_to_a},{sc.hit_to_a}]  "
                          f"B=[{sc.bid_b},{sc.ask_b}]/"
                          f"[{sc.lift_to_b},{sc.hit_to_b}]")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown {cmd!r}; try s/p/f/q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten error: {e}")


if __name__ == "__main__":
    main()
