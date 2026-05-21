"""Day 1 live trading strategy v17 — dual-symbol passive MM (A and B)
with reveal-time precompute hot path and cross-market arbitrage.

Design pillars:
  (1) Market-make BOTH A and B.
  (2) Skew quotes by inventory so position naturally mean-reverts.
  (3) Widen / pull quotes around each reveal to dodge adverse selection.
  (4) Pre-compute post-reveal quotes for every possible next reveal
      value, so on_reveal is a dict lookup + apply (no live recompute).

  fair_A = running_sum + n_remaining * E[X | posterior]   (settle = sum)
  fair_B = E[X | posterior]                                (settle = X_n)

Both share the same posterior over (a, w), so a single precompute pass
generates per-symbol scenarios in one sweep.

Components:

  A. PRECOMPUTE HOT PATH. For each possible next reveal value v,
     pre-build (fair_A, fair_B, bid_A, ask_A, bid_B, ask_B) using
     the post-update posterior. on_reveal becomes:
        entry = self._precompute.get(value)
        if entry: apply_quotes(A=entry.bid_a/ask_a, B=entry.bid_b/ask_b)

  B. CROSS-MARKET ARB. Slow loop computes implied_X from each market:
        impl_X_from_A = (mid_A - running_sum) / n_remaining
        impl_X_from_B = mid_B
     If |impl_X_from_A - impl_X_from_B| >= arb_min_edge_ticks, we
     IOC-take both legs (sell expensive, buy cheap) — locking the spread.

  C. STALE-QUOTE SWEEPER on B only (A has too many informed snipers).
     Background scan: if any resting price is >= stale_attack_min_edge_t
     from our fair_eff with size >= stale_attack_min_size, IOC-sweep.

  D. INVENTORY SKEW. Quote midpoint shifts toward flat by
     (position / skew_full_flip) * half_width. At |position| ==
     skew_full_flip the quote fully flips: ask becomes very aggressive,
     bid backs off — automatic mean-reversion well before pos cap.

  E. PRE-REVEAL WIDENING / PULL. In the last pre_reveal_lead_sec before
     each scheduled reveal, multiply half_width by pre_reveal_widen_mult,
     or pull the quote entirely, so informed snipers (VALKC etc.) can't
     pick us off cheap.

  F. POSTERIOR-PROFITABLE-QUOTE GUARD. Even with skew, if the resulting
     bid > fair_eff (we'd be buying above fair) or ask < fair_eff, skip
     posting that side. Prevents skew from creating a -EV quote during
     extreme inventory imbalance.

  G. MULTI-ORDER SAFETY. resting[side] tracks ALL our live orders
     keyed by order_id, not a single slot. Before any fresh post we
     cancel every known orphan on that side. A reconciliation thread
     polls /api/orders and IOC-flattens if pos exceeds the cap.

DESIGN:
  * Shared Posterior across A and B.
  * Per-symbol state (position, book cache, multi-order resting set).
  * Four threads: precompute, MM refresh, arb, stale sweep + reconcile.
  * Single _ioc/_post path per symbol with hard ±position_limit guard.

Run:
    python day1/run_combined17.py
    python day1/strategy17.py
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
for _p in (_PARENT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk.client import GameClient  # noqa: E402
from strategy12 import (  # noqa: E402, F401
    URL, API_KEY, N_PRIOR_SIM, Posterior, _install_nodelay,
)


# ===========================================================================
# Config — passive MM with reveal-time precompute + arb safety rails
# ===========================================================================
@dataclass
class Config:
    # ---- MM widths/sizes ----
    # A is summed over reveals so its tick variance is larger -> width=8t.
    # B settles on last reveal alone -> tighter width=4t is profitable.
    quote_width_a: int = 8
    quote_width_b: int = 4
    size_a: int = 5
    size_b: int = 5
    # Inventory skew: quote midpoint shifts (position / skew_full_flip)
    # half-widths toward flat. Larger flip = more inventory tolerance.
    # Aligned with SEAN bot's published values (A=50, B=100) which our
    # log analysis confirmed are well-tuned for the underlying volatility.
    skew_full_flip_a: int = 50
    skew_full_flip_b: int = 100

    # ---- pre-reveal widening ----
    # Lead bumped 1.0 -> 2.0 -> 2.5: log analysis on 122506 showed fills
    # at t_reveal - 1.5s where VALKO lifted our asks while pull was meant
    # to be active. Bumping to 2.5s ensures the pull lands before any
    # WS/REST dispatch lag — even if cancel hits a 3s rate-limit lockout
    # for one cycle, we still have a buffer.
    pre_reveal_lead_sec: float = 2.5
    pre_reveal_widen_mult: float = 5.0
    # Default ON for BOTH symbols after v17/025222: B's PnL was -6745
    # driven by CVALK and informed CPs lifting our asks pre-reveal.
    # The "B accumulates profitably" hypothesis was specific to earlier
    # log windows; under pressure from informed snipers, B loses too.
    pre_reveal_pull_a: bool = True
    pre_reveal_pull_b: bool = True
    # NEW: when we have NO reveals yet (k=0), the posterior is the raw
    # prior, so E[X] is highly biased relative to whatever true (a,w) the
    # round drew. Always use the widened spread until first reveal.
    no_info_widen_mult: float = 8.0

    # ---- slow MM refresh ----
    # Logs 122433/122506/122647 showed 70+ rate_limited rejects PER ROUND
    # (each = 3s lockout). At 1.0s with 2 sides × 2 syms × (cancel+post)
    # we burst ~8 req/s baseline, hitting the 20 req/s ceiling whenever
    # arb + reconcile + reveal hot-path overlap. Bumped 1.5→2.0 after the
    # dashboard webapp started sharing the same API key: manual writes
    # plus our 18 req/s ceiling needed extra headroom to avoid rejecting
    # the user's manual orders.
    mm_refresh_sec: float = 2.0
    # Deadband on price changes. Was 1 (any 1-tick fair drift requoted).
    # Bumped to 2 so a fair drift of ±1t no longer fires a modify. At
    # ~0.5-1 fill/s during steady-state, inventory-skew moves are <1t
    # per fill so this cuts modify churn substantially. Adverse-CP
    # widening (2x half-width) and reveal hot-path still re-quote because
    # both move price by 4+ ticks.
    quote_min_change_ticks: int = 2

    # ---- precompute control ----
    precompute_max_x: int = 60
    precompute_min_prob: float = 1e-4

    # ---- cross-market arbitrage ----
    # ENABLED. Risk-free spread when |impl_X_from_A - impl_X_from_B| is
    # large. Edge threshold + position pad keep us out of noisy regimes
    # (mid_A and mid_B are both estimators of X; small diffs are noise,
    # not edge). The check cadence is intentionally slow (1.0s) to stay
    # under the rate-limit budget shared with MM + reconcile.
    arb_enabled: bool = True
    arb_min_edge_ticks: float = 4.0     # bumped from 3 — noise filter
    arb_max_slice_a: int = 5            # smaller -> safer per-trigger
    arb_max_slice_b: int = 5
    arb_check_sec: float = 2.0          # 1.0→1.5→2.0 to share budget
                                         # with dashboard manual writes
    arb_pos_pad: int = 10               # stay further from the limit

    # ---- stale-quote sweeper (per symbol) ----
    # DISABLED on A. In v17/game1 the "rich bids" we swept were posted by
    # informed CPs who knew true X was much higher than our prior-driven
    # fair. Sweeping them = buying low, settling lower. Keep enabled on B
    # only (where the underlying is bounded and informed snipers are rarer).
    stale_attack_enabled: bool = False
    stale_attack_check_sec: float = 1.0
    stale_attack_min_edge_ticks: float = 4.0
    stale_attack_min_size: int = 5
    stale_attack_max_slice: int = 10
    stale_attack_position_pad: int = 5

    # ---- network / executor ----
    ioc_parallel_workers: int = 4

    # ---- safety ----
    skip_posting_when_negative_ev: bool = True
    # Stay this far inside the position limit when sizing MM quotes.
    # Server does NOT enforce instrument.position_limit (logs show pos
    # reaching +200 vs reported limit=50 with zero position-limit
    # rejects), so this pad is our PRIMARY soft cap. The hard cap below
    # triggers an emergency flatten when even this gets blown.
    mm_inventory_safety_pad: int = 15
    # Hard cap on |pos| as a multiple of position_limit. When exceeded,
    # cancel_all + IOC flatten back to (limit - pad). v17 game logs
    # 074834 / 075543 hit pos=+206 because the single-slot resting
    # tracker forgot about orphan orders; this is the bulletproof
    # backstop in case that ever recurs.
    emergency_flatten_factor: float = 1.0  # i.e. trip when |pos| > limit

    # ---- order reconciliation ----
    # Periodically poll /api/orders to (1) prune our tracker of oids the
    # server no longer holds, and (2) cancel any oids the server holds
    # that aren't in our tracker (true orphans, e.g. from server-side
    # modify that returned a new oid we didn't see). 2.0→3.0→5.0 to drop
    # per-round REST GET load AND give the dashboard webapp room to write
    # manual orders without triggering rate_limited.
    reconcile_check_sec: float = 3.0
    # Max orphan cancels reconcile will fire in a single tick. The
    # previous logic let one tick burst N cancels — observed pattern:
    # 20 orphan cancels in 270ms in v17/145320, which saturated the
    # server's 1-second window and triggered cascading rate_limits.
    # At cap=8/tick with reconcile_check_sec=3s, a backlog of N orphans
    # clears in (N/8)*3s seconds. Tuned up from 5/5s after observing
    # orphan accumulation faster than drain rate (5/11→5/13→5/14→5/15
    # in v17/200715). The per-side cap (_can_send) already counts orphan
    # qty against capacity, so an uncleared orphan can't make us double-
    # quote — but it does block modify-replace and cause REST churn.
    reconcile_max_cancels_per_tick: int = 8
    # When orphan backlog exceeds this many per symbol, escalate to a
    # bulk cancel_all(symbol). Single REST call replaces N individual
    # cancels — used as a circuit breaker when the per-tick cap can't
    # keep up. Set to 0 to disable. Caveat: cancel_all also kills
    # dashboard webapp orders on the same API key, so the threshold is
    # set high (only fires when accumulation is clearly pathological).
    reconcile_bulk_cancel_threshold: int = 20
    # Don't prune freshly-added entries: a race between our send and the
    # server indexing the order can briefly show "missing" on my_orders.
    # If we pruned aggressively, the next MM cycle would re-post (no
    # primary) and we'd double-quote. Skip entries younger than this.
    # Bug recurred in v17/122647: pos breached 54 vs limit=50 because
    # 452986 was pruned by reconcile, then 453011 was posted at the same
    # price 34, both filled by VALKO in 38ms.
    reconcile_min_age_sec: float = 3.0

    # ---- latency exploit ----
    # Use the PATCH /api/order/{id} endpoint to replace a resting quote in
    # a single round-trip (vs cancel + post = 2 round-trips). Halves
    # per-update latency AND halves rate-limit budget consumption. Modify
    # also keeps queue priority for pure qty-down at the same price.
    use_modify_replace: bool = True

    # ---- adverse-fill widening ----
    # When a known hostile counterparty (VALKO/VALKC/VALKB on A, VALKE
    # excluded as profitable) hits one of our quotes, widen that side's
    # half-width for the next `adverse_widen_decay_sec` window. Stops
    # the "race to the bottom" loop visible in log 122506 where VALKO
    # lifted our A asks 8+ times in a row, each fill skewing our next
    # ask lower and inviting the next hit. Multiplier applies on top of
    # inventory skew and pre-reveal widening.
    adverse_cps: tuple = ("VALKO", "VALKC", "VALKB", "DVALK", "CVALK")
    adverse_widen_mult: float = 2.0
    adverse_widen_decay_sec: float = 4.0
    adverse_min_hits: int = 1  # widen after the FIRST hostile fill

    # ---- rate-budget guards ----
    # The SDK throttles to 18 req/s with a 20-token burst bucket. Below
    # these token counts, skip the corresponding write so the bucket
    # has headroom for reveal hot-path posts and fill-driven defensive
    # cancels (which can't be skipped). Tuned so MM gets ~14 tokens
    # reserved, arb backs off at 6, reconcile at 8. These also reserve
    # budget for the dashboard webapp's manual writes (same API key).
    arb_min_tokens: float = 6.0
    reconcile_min_tokens: float = 8.0
    # MM refresh is the most frequent caller; we gate the inner
    # POST/MODIFY path on this threshold rather than the whole refresh,
    # because cancels (pre-reveal pull, defensive cleanups) must still
    # land even under rate pressure.
    mm_send_min_tokens: float = 4.0
    # Token threshold below which we skip *bulk* cancel loops (orphan
    # cleanup in _apply_side / _reconcile_once / on_fill_event defensive
    # cleanup). 69 of 107 rejects in v17/141232 were cancels — the SDK's
    # 20-token burst capacity let multiple orphan loops fire 5-8 cancels
    # within 100ms, and once the server's per-second window was
    # saturated each subsequent cancel got rate_limited. Single critical
    # cancels (modify-disabled primary swap) stay ungated.
    bulk_cancel_min_tokens: float = 6.0

    # ---- server-lockout backoff ----
    # The server returns `reject {reason: rate_limited, retry_after_ms: N}`
    # and ENTERS A LOCKOUT WINDOW (typically 3000ms) during which every
    # request is rejected. The SDK's local token bucket knows nothing
    # about this, so without an explicit backoff the strategy keeps
    # firing for the full lockout — turning a single rate burst into a
    # ~30-reject cascade (observed pattern in v17/141232). on_reject_event
    # below captures retry_after_ms and pushes a process-wide cooldown
    # that gates every send. Floor of 100ms = "definitely don't burst
    # past this regardless of what the server said".
    lockout_min_pad_ms: float = 100.0
    # Cap on the cooldown we'll apply from a single reject — protects
    # against a malformed payload claiming a huge value.
    lockout_max_pad_ms: float = 3500.0

    # ---- IOC sweep on reveal (latency exploit) ----
    # The fast hot-path on reveal pre-stages PASSIVE quotes. But laggard
    # CPs frequently leave stale GTC orders on the book that don't update
    # in time. We snap the book at reveal and IOC any level with enough
    # edge (price has clearly moved past it). Combined with the precompute
    # IF/ELSE this gives us first-mover IOCs against laggards within ~1ms
    # of the reveal landing on the WS thread.
    ioc_on_reveal_enabled: bool = True
    ioc_on_reveal_edge_ticks: float = 2.0
    ioc_on_reveal_max_slice: int = 10
    ioc_on_reveal_position_pad: int = 5

    # ---- wide-quoter trap filter ----
    # User noted other traders posting "in the thousands at volume" —
    # massive bid/ask quotes at extreme prices designed to lure IOC
    # sweepers into bad fills. Refuse to IOC at prices more than
    # max_ioc_distance_ticks from current fair. Applies to arb, stale
    # sweep, AND the new reveal IOC sweep — anywhere _ioc() is called.
    max_ioc_distance_ticks: float = 50.0

    # ---- MM loop throttle ----
    # `_mm_wake` is set on every fill (on_fill_event line 1629). Without
    # this throttle, fill bursts re-fire the MM loop every 80-200ms,
    # turning the slow 2s loop into a hot loop (v17/145320: 23 GTC posts
    # in 1.3s = 4 posts × 6 cycles in 1.3s = 1 cycle per ~215ms). Cap to
    # one refresh per N seconds; coalesces fast-fire fills into a single
    # re-quote without losing low-latency response on the FIRST fill.
    mm_min_interval_sec: float = 0.5

    # ---- post backoff on 400/403 ----
    # When a fresh post returns 400 (Bad Request — e.g. negative price)
    # or 403 (Forbidden — account/cash issue), back off that sym/side
    # for this long instead of re-posting the same guaranteed-reject
    # order every MM cycle. Logs from 165410 had 8+ identical 403 errors
    # in a row; the backoff collapses that to one error then quiet.
    post_backoff_sec: float = 5.0

    # ---- pre-reveal park-not-cancel ----
    # Old behavior: pre-reveal pull returns (None,None) from
    # _passive_quote → _apply_side enters cancel-only branch → primary
    # gone. Then post-reveal MM cycle finds no primary and POSTS FRESH
    # (line 1143). Result: every reveal cycle = cancel + post per side
    # per sym = 8 sends, and modify NEVER fires (observed in 145320:
    # 785 posts + 692 cancels + 0 modify_acks).
    # New behavior: park the quote at fair ± park_offset_ticks (deep,
    # won't fill). Order stays alive → the next MM cycle MODIFIES it
    # back into market. Halves the per-reveal REST cost when enabled.
    pre_reveal_use_park: bool = True
    pre_reveal_park_offset_ticks: int = 30

    # ---- strategy-level sliding-window rate limit ----
    # The SDK's token bucket has a 20-token BURST capacity. That means
    # five threads (mm + arb + reconcile + WS + IOC executor) can drain
    # 20 tokens in <50ms, packing 20 sends into a single server-side
    # 1-second window. Observed in v17/145320: reconcile fired 20
    # cancels in 270ms, then 13 more got rate_limited.
    #
    # This hard cap is enforced at strategy level: every REST send is
    # registered, and any path that would push the trailing-1s count
    # above max_sends_per_second skips. Cap is set well under the
    # server's 20/s to leave headroom for the dashboard webapp's manual
    # writes and any in-flight requests we haven't seen the ack for.
    max_sends_per_second: float = 15.0
    sends_window_sec: float = 1.0

    # ---- dashboard / manual-order coexistence ----
    # The dashboard webapp at day1/web/ shares this API key. Anything
    # placed via the dashboard appears in our `my_orders` response. We
    # tag every v17 order with `client_order_id = "{prefix}-{N}"` so
    # reconcile can DISTINGUISH our orphans (safe to cancel) from manual
    # orders (must leave alone). Without this, reconcile would happily
    # cancel any GTC order the user posts via the dashboard.
    client_order_id_prefix: str = "v17"
    # When True, reconcile only cancels server-side orders whose coid
    # starts with `client_order_id_prefix`. Set False to revert to the
    # legacy "cancel any oid we don't know" behavior (single-user mode).
    reconcile_respect_manual_orders: bool = True

    # ---- micro-optimization knobs (low-risk latency) ----
    # When the strategy posts a brand-new quote on a side that has no
    # resting order (e.g. first MM cycle, or after a complete fill), we
    # skip the modify path entirely. This is already correct, but we
    # also avoid string formatting in the success path of _apply_side
    # for a small CPU win on the hot reveal path.
    log_every_modify: bool = False


# ===========================================================================
# Per-symbol mutable state
# ===========================================================================
@dataclass
class _SymState:
    sym: str
    position: int = 0
    position_limit: int = 100
    tick: int = 1
    book: Optional[dict] = None
    book_t: float = 0.0
    # resting[side] -> {order_id: {"order_id":..., "price":..., "qty":...}}.
    # Dict (not single slot) because modify-then-fresh-post races, server
    # 404s on retry, etc. can leave multiple live orders on the same side.
    # Forgetting any of them caused the +206 blowout (log 074834): the
    # single-slot tracker stopped quoting at pos=35 (correct) but 10
    # orphaned bids were still live on the book and got swept.
    resting: Dict[str, Dict[int, dict]] = field(
        default_factory=lambda: {"bid": {}, "ask": {}})
    last_quote_apply_t: float = 0.0
    # In-flight: qty of orders sent on this side that haven't completed
    # round-trip yet (no REST response back, OR REST done but WS hasn't
    # confirmed disposition). Acts as a conservative cap so concurrent
    # MM + fill-driven re-quote can't double-post into the limit.
    in_flight: Dict[str, int] = field(
        default_factory=lambda: {"bid": 0, "ask": 0})


# ===========================================================================
# Precomputed scenario for a given next reveal value
# ===========================================================================
@dataclass
class _Scenario:
    value: int
    prob: float
    fair_a: float
    sigma_a: float
    fair_b: float
    sigma_b: float
    bid_a: Optional[int]
    ask_a: Optional[int]
    bid_b: Optional[int]
    ask_b: Optional[int]


# ===========================================================================
# Strategy v17
# ===========================================================================
class Strategy:
    """Dual-symbol passive MM with reveal-time precompute + cross-arb."""

    SYMS = ("A", "B")

    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[Config] = None):
        self.c = client
        self.posterior = posterior
        self.cfg = config or Config()
        self.lock = threading.RLock()

        # ---- TCP_NODELAY + connection pool ----
        ok = _install_nodelay(client, pool_maxsize=max(
            8, self.cfg.ioc_parallel_workers + 2))
        print(f"[v17 NODELAY] installed={ok}")

        # ---- bootstrap from game_state ----
        gs = client.game_state()
        self.duration = gs["duration"]
        self.reveal_interval = gs["reveal_interval"]
        self.n_total = self.duration // self.reveal_interval
        fees = gs.get("fees", {})
        self.maker_fee = fees.get("maker_per_lot", 0.5)
        self.taker_fee = fees.get("taker_per_lot", 0.5)

        # per-symbol state
        self.state: Dict[str, _SymState] = {}
        for sym in self.SYMS:
            instr = (gs.get("instruments") or {}).get(sym) or {}
            self.state[sym] = _SymState(
                sym=sym,
                position=0,
                position_limit=int(instr.get("position_limit") or 100),
                tick=int(instr.get("tick_size") or 1),
            )

        # initial positions
        try:
            ps = (client.positions() or {}).get("positions") or {}
            for sym in self.SYMS:
                self.state[sym].position = int(ps.get(sym, 0))
        except Exception as e:
            print(f"[v17 INIT-POSITIONS] failed: {e}")

        # phase + posterior sync to current reveals
        self.phase: Optional[str] = gs.get("phase")
        self.posterior.reset(gs.get("reveals") or [])
        self._reveal_count = len(self.posterior.reveals)
        self._running_sum = float(sum(self.posterior.reveals))
        self._phase_running_t: Optional[float] = (
            time.time() if self.phase == "running" else None)
        self._last_reveal_t: Optional[float] = None

        # ---- caches ----
        self._fair_a_cache: Optional[Tuple[int, float, float]] = None
        self._fair_b_cache: Optional[Tuple[int, float, float]] = None
        self._posterior_gen: int = 0  # bumped on each posterior change

        # ---- precompute ----
        self._precompute: Dict[int, _Scenario] = {}
        self._precompute_lock = threading.RLock()
        self._precompute_for_cycle: int = -1  # k after which this was built
        self._precompute_request = threading.Event()
        self._precompute_run_count: int = 0
        self._last_precompute_us: float = 0.0

        # ---- request counters (visible via `s` status) ----
        self._modify_count: int = 0
        self._post_count: int = 0
        # Monotonic counter for client_order_id tagging. Lets reconcile
        # distinguish v17 orders from manual orders posted by the dashboard
        # webapp (day1/web/) that shares this API key.
        self._coid_counter: int = 0
        # Server-lockout cutoff (monotonic seconds). Set by
        # on_reject_event when a rate_limited reject arrives. While
        # monotonic() < this, every REST-firing path bails out early.
        self._lockout_until: float = 0.0
        self._lockout_count: int = 0
        # Strategy-level sliding-window send log. Every successful REST
        # send pushes time.monotonic(); _can_send_now prunes >1s old and
        # refuses when count >= max_sends_per_second. Hard cap on top of
        # the SDK bucket — the bucket's 20-token burst can pack many
        # sends into one server-side 1-second window; this can't.
        self._send_log: deque = deque(maxlen=64)
        self._send_log_lock = threading.Lock()
        self._sends_deferred: int = 0
        # MM-loop min-interval throttle. fills set _mm_wake which can
        # re-fire the MM loop every <200ms during a fill burst; this
        # cap coalesces a burst into a single re-quote.
        self._last_mm_refresh_t: float = 0.0
        # Reveal-IOC counter (visible via `s` status).
        self._reveal_ioc_count: int = 0
        # Per-(sym,side) post backoff: when a fresh-post returns 400 or
        # 403, post on that sym/side is skipped for post_backoff_sec to
        # prevent spamming the server with the same guaranteed-reject
        # order every MM cycle. Cleared on successful post.
        self._post_backoff_until: Dict[Tuple[str, str], float] = {}

        # ---- adverse-fill tracker ----
        # Maps (sym, side) -> [(t, cp), ...] of recent hostile-CP fills.
        # Widening applies when any entry is within decay window of now.
        self._adverse_fills: Dict[Tuple[str, str], deque] = {
            (sym, side): deque(maxlen=16)
            for sym in self.SYMS for side in ("bid", "ask")
        }

        # ---- executors ----
        self._ioc_executor = ThreadPoolExecutor(
            max_workers=self.cfg.ioc_parallel_workers,
            thread_name_prefix="v17-ioc")

        # ---- threads ----
        self._stop = threading.Event()
        # MM wake event: position-changing events (fills) set this so the MM
        # loop doesn't sit out the rest of its mm_refresh_sec window holding
        # stale skew. _refresh_mm_quotes is also still called synchronously
        # from on_fill_event for minimum latency on the WS thread itself.
        self._mm_wake = threading.Event()
        self._th_precompute = threading.Thread(
            target=self._precompute_loop, daemon=True, name="v17-precomp")
        self._th_mm = threading.Thread(
            target=self._mm_refresh_loop, daemon=True, name="v17-mm")
        self._th_arb = threading.Thread(
            target=self._arb_loop, daemon=True, name="v17-arb")
        self._th_stale = threading.Thread(
            target=self._stale_loop, daemon=True, name="v17-stale")
        self._th_reconcile = threading.Thread(
            target=self._reconcile_loop, daemon=True, name="v17-recon")
        self._th_precompute.start()
        self._th_mm.start()
        self._th_arb.start()
        self._th_stale.start()
        self._th_reconcile.start()
        self._precompute_request.set()

        # banner
        print(f"[v17] phase={self.phase}  n_total={self.n_total}  "
              f"reveal_interval={self.reveal_interval}s")
        for sym in self.SYMS:
            s = self.state[sym]
            print(f"[v17 SYM:{sym}] pos_limit={s.position_limit}  "
                  f"tick={s.tick}  pos={s.position}")
        print(f"[v17 MM] A: width={self.cfg.quote_width_a}t "
              f"size={self.cfg.size_a} skew_flip={self.cfg.skew_full_flip_a}  "
              f"B: width={self.cfg.quote_width_b}t "
              f"size={self.cfg.size_b} skew_flip={self.cfg.skew_full_flip_b}")
        print(f"[v17 PRE_REVEAL] lead={self.cfg.pre_reveal_lead_sec}s  "
              f"widen={self.cfg.pre_reveal_widen_mult}x")
        print(f"[v17 ARB] enabled={self.cfg.arb_enabled}  "
              f"min_edge={self.cfg.arb_min_edge_ticks}t  "
              f"slice A={self.cfg.arb_max_slice_a}/B={self.cfg.arb_max_slice_b}")
        print(f"[v17 STALE] enabled={self.cfg.stale_attack_enabled}  "
              f"min_edge={self.cfg.stale_attack_min_edge_ticks}t  "
              f"min_size={self.cfg.stale_attack_min_size}")

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
        """E[settle_A], sigma."""
        cached = self._fair_a_cache
        if cached and cached[0] == self._posterior_gen:
            return cached[1], cached[2]
        n_rem = self._n_remaining()
        fair, sigma = self.posterior.predict_settle(self._running_sum, n_rem)
        self._fair_a_cache = (self._posterior_gen, fair, sigma)
        return fair, sigma

    def fair_b(self) -> Tuple[float, float]:
        """E[X], sigma — B settles at X_n_total."""
        cached = self._fair_b_cache
        if cached and cached[0] == self._posterior_gen:
            return cached[1], cached[2]
        ex, ex2 = 0.0, 0.0
        for (a, w), p in self.posterior.posterior.items():
            mean_x = a + w / 2.0
            # E[X^2 | a,w] = Var[X|a,w] + (E[X|a,w])^2
            #            = w*(w+2)/12 + mean_x^2
            var_x = w * (w + 2) / 12.0
            ex += p * mean_x
            ex2 += p * (var_x + mean_x * mean_x)
        var_total = max(0.0, ex2 - ex * ex)
        sigma = math.sqrt(var_total)
        self._fair_b_cache = (self._posterior_gen, ex, sigma)
        return ex, sigma

    # ==================================================================
    # Passive quote computation (inventory skew + pre-reveal widen/pull)
    # ==================================================================
    def _possible_x_values(self) -> List[int]:
        """Integer X values with nonzero posterior probability."""
        seen: dict[int, float] = defaultdict(float)
        for (a, w), p in self.posterior.posterior.items():
            for v in range(a, a + w + 1):
                seen[v] += p / (w + 1)
        # Filter low-prob and cap
        out = [v for v, p in seen.items()
               if p >= self.cfg.precompute_min_prob
               and v <= self.cfg.precompute_max_x and v >= 0]
        out.sort()
        return out

    def _passive_quote(self, sym: str, fair: float, position: int,
                       in_pre_reveal: bool) -> Tuple[Optional[int], Optional[int]]:
        """Passive quotes: fair ± half_width with inventory skew.

        Returns (bid_px, ask_px). Either side may be None if -EV after skew
        or if we're inside the inventory safety pad. Pre-reveal pull is
        applied per-symbol via pre_reveal_pull_a / pre_reveal_pull_b.

        Adverse-CP widening: if a hostile counterparty has lifted us on a
        side within the last `adverse_widen_decay_sec`, that side's
        half-width gets multiplied by `adverse_widen_mult`. Breaks the
        race-to-the-bottom loop visible in v17/122506 logs (VALKO lifted
        A asks 8+ times in a row, each pushing our next ask lower).
        """
        s = self.state[sym]
        if sym == "A":
            width = self.cfg.quote_width_a
            flip = max(1, self.cfg.skew_full_flip_a)
            pull = self.cfg.pre_reveal_pull_a
        else:
            width = self.cfg.quote_width_b
            flip = max(1, self.cfg.skew_full_flip_b)
            pull = self.cfg.pre_reveal_pull_b
        # Hard pull: in pre-reveal, drop both sides for this symbol entirely.
        if in_pre_reveal and pull:
            if self.cfg.pre_reveal_use_park:
                # Park quote far from market: order stays alive (post-reveal
                # MM cycle can MODIFY it back instead of cancel+post),
                # halving REST cost. Park price is far enough from fair
                # that snipers can't lift it profitably.
                tick = s.tick
                off = self.cfg.pre_reveal_park_offset_ticks * tick
                park_bid = ((int(round(fair - off))) // tick) * tick
                park_ask = ((int(round(fair + off))) // tick) * tick
                # B has small fair (~8-10): fair-30 lands at -20 which
                # the server rejects with 400. Floor parked bid at tick
                # (lowest legal price). If the resulting spread is too
                # narrow to be a real park, pull that side instead.
                if park_bid < tick:
                    park_bid = None
                if park_ask is not None and park_bid is not None \
                        and park_ask <= park_bid:
                    park_ask = park_bid + tick
                # Honor inventory pad on the parked quote — never park a
                # bid above the limit-pad boundary or an ask below it.
                pad = self.cfg.mm_inventory_safety_pad
                if position >= s.position_limit - pad:
                    park_bid = None
                if position <= -s.position_limit + pad:
                    park_ask = None
                return park_bid, park_ask
            return None, None
        half = width / 2.0
        # k=0 has the strongest informational disadvantage (only the prior).
        # Use the widest spread until the first reveal, regardless of timing.
        if self._reveal_count == 0:
            half *= self.cfg.no_info_widen_mult
        elif in_pre_reveal:
            half *= self.cfg.pre_reveal_widen_mult
        # Side-asymmetric widening: bid and ask can have different half-widths
        # based on which side was recently lifted by a hostile CP.
        half_bid = half * self._adverse_widen_factor(sym, "bid")
        half_ask = half * self._adverse_widen_factor(sym, "ask")
        # Skew: long position -> push both bid and ask DOWN (sells more
        # likely, buys less likely -> mean-revert to flat).
        skew = (position / flip) * half
        bid_px = int(round(fair - half_bid - skew))
        ask_px = int(round(fair + half_ask - skew))
        tick = s.tick
        bid_px = (bid_px // tick) * tick
        ask_px = (ask_px // tick) * tick
        if ask_px <= bid_px:
            ask_px = bid_px + tick
        if self.cfg.skip_posting_when_negative_ev:
            if bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None
        # Hard floor: server rejects price <= 0 with HTTP 400 Bad Request.
        # Skew + low-fair B can produce negative bids — pull that side
        # entirely rather than burn tokens on guaranteed-reject posts.
        if bid_px is not None and bid_px < tick:
            bid_px = None
        # Inventory safety pad: stop the quote on the side that would push
        # us further into the limit BEFORE hitting the wall, so concurrent
        # fills + cancel latency can't blow past the cap.
        pad = self.cfg.mm_inventory_safety_pad
        if position >= s.position_limit - pad:
            bid_px = None
        if position <= -s.position_limit + pad:
            ask_px = None
        return bid_px, ask_px

    def _adverse_widen_factor(self, sym: str, side: str) -> float:
        """Multiplier on half-width if hostile CPs hit (sym, side) recently.

        side is "bid" or "ask". Returns adverse_widen_mult when at least
        `adverse_min_hits` hostile fills are within the decay window, else
        1.0. Decays smoothly: when only one stale entry remains, factor
        drops back to 1.0.
        """
        cfg = self.cfg
        if cfg.adverse_widen_mult <= 1.0:
            return 1.0
        dq = self._adverse_fills.get((sym, side))
        if not dq:
            return 1.0
        now = time.time()
        cutoff = now - cfg.adverse_widen_decay_sec
        # Drop stale entries (left side).
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        if len(dq) >= cfg.adverse_min_hits:
            return cfg.adverse_widen_mult
        return 1.0

    def _in_pre_reveal_window(self) -> bool:
        nxt = self._next_reveal_at()
        if nxt is None:
            return False
        now = time.time()
        # Pre-reveal window covers (nxt - lead) <= t <= nxt + grace. The
        # "overdue" branch (now > nxt but no reveal yet) is critical:
        # v17/025222 R#3 fired 5s late and our window had already expired,
        # so the pull deactivated and A asks got sniped for −340. Treat
        # any time after the scheduled reveal as "still pre-reveal" until
        # the actual reveal lands.
        lead = self.cfg.pre_reveal_lead_sec
        return (nxt - now) < lead or now >= nxt

    def _next_reveal_at(self) -> Optional[float]:
        if self.phase != "running" or self._phase_running_t is None:
            return None
        if self._reveal_count >= self.n_total:
            return None
        # Anchor on the LAST actual reveal time when we have one: schedule
        # drift means phase_running_t + k*interval can be seconds wrong
        # by reveal #3+. Falling back to the schedule only when k=0.
        if self._last_reveal_t is not None:
            return self._last_reveal_t + self.reveal_interval
        return (self._phase_running_t
                + (self._reveal_count + 1) * self.reveal_interval)

    # ==================================================================
    # Precompute (slow loop)
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
                print(f"[v17 PRECOMP-ERR] {e!r}")

    def _build_precompute(self) -> None:
        t0 = time.perf_counter()
        cycle_at_start = self._reveal_count
        if self._n_remaining() <= 0:
            with self._precompute_lock:
                self._precompute = {}
                self._precompute_for_cycle = cycle_at_start
            return
        post_snapshot = dict(self.posterior.posterior)
        running_sum_base = self._running_sum
        n_rem_after = self._n_remaining() - 1
        in_pre = self._in_pre_reveal_window()  # next reveal will already
        # have fired, so post-reveal we're NOT pre-reveal of NEXT reveal

        new_table: Dict[int, _Scenario] = {}
        eps = 1e-9
        for v in self._possible_x_values():
            # Conditional posterior P(a,w | x=v) ∝ P(a,w) / (w+1) if v in [a,a+w]
            new_post: dict[tuple[int, int], float] = {}
            total = 0.0
            marg = 0.0
            for (a, w), p in post_snapshot.items():
                if v < a - eps or v > a + w + eps:
                    continue
                lik = 1.0 / (w + 1)
                joint = p * lik
                marg += joint
                np_new = p / (w + 1)
                new_post[(a, w)] = np_new
                total += np_new
            if total <= 0 or marg < self.cfg.precompute_min_prob:
                continue
            new_post = {k: v_ / total for k, v_ in new_post.items()}
            # E[settle_A | v]
            running_after = running_sum_base + v
            e_inner = 0.0; e2_inner = 0.0; e_var = 0.0
            ex = 0.0; ex2 = 0.0
            for (a, w), p in new_post.items():
                mean_x = a + w / 2.0
                var_x = w * (w + 2) / 12.0
                inner_mean = n_rem_after * mean_x
                inner_var = n_rem_after * var_x
                e_inner += p * inner_mean
                e2_inner += p * inner_mean * inner_mean
                e_var += p * inner_var
                # B
                ex += p * mean_x
                ex2 += p * (var_x + mean_x * mean_x)
            var_a = e_var + (e2_inner - e_inner * e_inner)
            fair_a = running_after + e_inner
            sigma_a = math.sqrt(max(0.0, var_a))
            fair_b = ex
            sigma_b = math.sqrt(max(0.0, ex2 - ex * ex))
            # Position-skewed quotes for BOTH symbols using current pos.
            # IMPORTANT: scenarios are applied at REVEAL time, at which
            # point we are between reveals (NOT in pre-reveal pull of
            # the NEXT reveal). Always pass in_pre=False. Previously
            # passed `in_pre` which baked parked prices into scenarios
            # when precompute ran during a pre-reveal window — the hot
            # path then posted parked prices (e.g. B bid @ -20 → server
            # 400 Bad Request).
            bid_a, ask_a = self._passive_quote(
                "A", fair_a, self.state["A"].position, in_pre_reveal=False)
            bid_b, ask_b = self._passive_quote(
                "B", fair_b, self.state["B"].position, in_pre_reveal=False)
            new_table[v] = _Scenario(
                value=v, prob=marg,
                fair_a=fair_a, sigma_a=sigma_a,
                fair_b=fair_b, sigma_b=sigma_b,
                bid_a=bid_a, ask_a=ask_a,
                bid_b=bid_b, ask_b=ask_b,
            )
        # Commit only if posterior didn't move under us
        if self._reveal_count != cycle_at_start:
            return
        with self._precompute_lock:
            self._precompute = new_table
            self._precompute_for_cycle = cycle_at_start
            self._precompute_run_count += 1
            self._last_precompute_us = (time.perf_counter() - t0) * 1e6

    # ==================================================================
    # HOT PATH: on_reveal — fast dict lookup + apply for both symbols
    # ==================================================================
    def on_reveal(self, value) -> None:
        """Fast path: O(1) dict lookup, apply pre-staged quotes for A & B."""
        try:
            v = int(round(float(value)))
        except Exception:
            print(f"[v17 ON_REVEAL] bad value {value!r}")
            return
        # Pull scenario BEFORE posterior update — table is keyed by the
        # value of the reveal we just received.
        with self._precompute_lock:
            entry = self._precompute.get(v)

        # Update posterior + counters (single lock)
        with self.lock:
            self.posterior.update(value)
            self._reveal_count = len(self.posterior.reveals)
            self._running_sum = float(sum(self.posterior.reveals))
            self._last_reveal_t = time.time()
            self._bump_posterior_gen()

        if entry is not None:
            # FAST IF/ELSE on reveal:
            # (1) IOC sweep laggard book orders FIRST, before any other
            #     trader's book update lands. Precomputed fair_a/fair_b
            #     mean zero math on the hot path — just snap book and
            #     fire IOCs at any level past the edge threshold.
            ioc_a = ioc_b = 0
            if self.cfg.ioc_on_reveal_enabled:
                ioc_a = self._ioc_sweep_on_reveal("A", entry.fair_a)
                ioc_b = self._ioc_sweep_on_reveal("B", entry.fair_b)
            # (2) Apply pre-staged passive quotes at new fair.
            self._apply_quote("A", entry.bid_a, entry.ask_a)
            self._apply_quote("B", entry.bid_b, entry.ask_b)
            print(f"[v17 HOT] v={v} fair_a={entry.fair_a:.1f}+/-{entry.sigma_a:.1f} "
                  f"fair_b={entry.fair_b:.2f}+/-{entry.sigma_b:.2f}  "
                  f"A=[{entry.bid_a},{entry.ask_a}] B=[{entry.bid_b},{entry.ask_b}]  "
                  f"prob={entry.prob:.3f}  ioc_a={ioc_a} ioc_b={ioc_b}")
        else:
            # Fallback: recompute live (this should be rare)
            print(f"[v17 MISS] no precompute for v={v}; recomputing live")
            self._refresh_mm_quotes()

        # Request next precompute (posterior has changed)
        self._precompute_request.set()

    # ==================================================================
    # SLOW LOOP: MM quote refresh (continuous re-quote with current fair)
    # ==================================================================
    def _mm_refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_mm_quotes()
            except Exception as e:
                print(f"[v17 MM-REFRESH-ERR] {e!r}")
            # Wait the full mm_refresh_sec OR until a fill / event wakes us.
            # Either way clear the wake so the next iteration can be triggered.
            self._mm_wake.wait(timeout=self.cfg.mm_refresh_sec)
            self._mm_wake.clear()
            if self._stop.is_set():
                return

    def _refresh_mm_quotes(self) -> None:
        if self.phase != "running":
            return
        # Throttle: coalesce fill-burst-driven wakes into one re-quote.
        now = time.time()
        min_iv = self.cfg.mm_min_interval_sec
        if min_iv > 0 and (now - self._last_mm_refresh_t) < min_iv:
            return
        self._last_mm_refresh_t = now
        in_pre = self._in_pre_reveal_window()
        for sym in self.SYMS:
            try:
                if sym == "A":
                    fair, _ = self.fair_a()
                else:
                    fair, _ = self.fair_b()
                pos = self.state[sym].position
                bid_px, ask_px = self._passive_quote(sym, fair, pos, in_pre)
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v17 MM:{sym}-ERR] {e!r}")

    # ==================================================================
    # REVEAL IOC SWEEP — fire IOCs at lagging book orders on reveal
    # ==================================================================
    def _ioc_sweep_on_reveal(self, sym: str, fair: float) -> int:
        """On reveal, snap the book and IOC any laggard level whose
        price is now clearly stale vs the new fair. Filtered:
          - edge >= ioc_on_reveal_edge_ticks (real edge, not noise)
          - distance <= max_ioc_distance_ticks (skip wide-quoter traps)

        Returns number of IOCs fired. Bounded by ioc_on_reveal_max_slice
        per side. Position-padded by ioc_on_reveal_position_pad.
        """
        if self._in_lockout():
            return 0
        s = self.state[sym]
        book = s.book
        if not book:
            return 0
        edge = self.cfg.ioc_on_reveal_edge_ticks
        max_dist = self.cfg.max_ioc_distance_ticks
        max_slice = self.cfg.ioc_on_reveal_max_slice
        pad = self.cfg.ioc_on_reveal_position_pad
        fired = 0
        # asks below fair -> we BUY
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
                gap = fair - px
                if gap < edge:
                    break  # asks sorted ascending; nothing better below
                if max_dist > 0 and gap > max_dist:
                    # wide-quoter trap: skip but check shallower levels
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, px):
                    taken += want
                    room_buy -= want
                    fired += 1
                    print(f"[v17 REVEAL-IOC:{sym}] BUY {want}@{px} "
                          f"fair={fair:.1f} gap={gap:.1f}t")
                else:
                    break  # rate-gated; stop trying this tick
        # bids above fair -> we SELL
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
                gap = px - fair
                if gap < edge:
                    break  # bids sorted descending; nothing better above
                if max_dist > 0 and gap > max_dist:
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, px):
                    taken += want
                    room_sell -= want
                    fired += 1
                    print(f"[v17 REVEAL-IOC:{sym}] SELL {want}@{px} "
                          f"fair={fair:.1f} gap={gap:.1f}t")
                else:
                    break
        if fired:
            self._reveal_ioc_count += fired
        return fired

    # ==================================================================
    # Quote application — uses modify() to replace cancel+post (LATENCY EXPLOIT)
    # ==================================================================
    def _resting_qty(self, sym: str, side: str) -> int:
        """Sum live qty across ALL tracked orders on this side."""
        return sum(int(e.get("qty") or 0)
                   for e in self.state[sym].resting[side].values())

    def _primary_resting(self, sym: str, side: str) -> Optional[dict]:
        """Most-recent order on this side (highest oid), or None."""
        entries = self.state[sym].resting[side]
        if not entries:
            return None
        oid = max(entries.keys())
        return entries[oid]

    def _quote_size(self, sym: str, side: str) -> int:
        """Order size that respects strict inventory cap incl. in-flight.

        Conservative: ALL resting orders on the side (including orphans)
        and ALL in-flight sends can fill in full. Bid headroom =
        (limit - pad) - pos - sum(resting_bid_qty) - in_flight_bid_qty.
        Returns 0 when no headroom remains; caller must skip posting.
        """
        s = self.state[sym]
        base = self.cfg.size_a if sym == "A" else self.cfg.size_b
        pad = self.cfg.mm_inventory_safety_pad
        same_side_outstanding = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        if side == "bid":
            headroom = ((s.position_limit - pad)
                        - s.position - same_side_outstanding - in_flight)
        else:
            headroom = ((s.position_limit - pad)
                        + s.position - same_side_outstanding - in_flight)
        # We can post up to `headroom` extra; existing resting already
        # contributes same_side_outstanding which the modify path retains.
        return max(0, min(base, headroom + same_side_outstanding))

    def _tokens_available(self) -> float:
        """Snapshot of REST tokens free in the client bucket.

        Returns +inf if the client doesn't expose `tokens_available()`
        (stub clients, older SDKs). That way the gate is a no-op when
        the metric isn't available rather than blocking all writes.
        """
        fn = getattr(self.c, "tokens_available", None)
        if fn is None:
            return float("inf")
        try:
            return float(fn())
        except Exception:
            return float("inf")

    def _in_lockout(self) -> bool:
        """True while the server's rate_limit lockout window is still
        active. Caller MUST skip its REST send when this returns True —
        the server will reject everything during the window, and even
        local cancels just burn the SDK bucket for nothing."""
        return time.monotonic() < self._lockout_until

    def _can_send_now(self) -> bool:
        """Sliding-window rate-limit check. Returns True iff registering
        a send now would NOT push us past max_sends_per_second over the
        trailing 1.0s. Cheaper than acquiring the SDK bucket and gives a
        hard burst cap the bucket can't enforce."""
        now = time.monotonic()
        cutoff = now - self.cfg.sends_window_sec
        with self._send_log_lock:
            while self._send_log and self._send_log[0] < cutoff:
                self._send_log.popleft()
            return len(self._send_log) < self.cfg.max_sends_per_second

    def _register_send(self) -> None:
        """Record that a REST send is about to be made (or just was).
        Call BEFORE the actual SDK call so concurrent callers see the
        updated count and decide to defer rather than pile on."""
        now = time.monotonic()
        cutoff = now - self.cfg.sends_window_sec
        with self._send_log_lock:
            while self._send_log and self._send_log[0] < cutoff:
                self._send_log.popleft()
            self._send_log.append(now)

    def on_reject_event(self, msg: dict) -> None:
        """Server-side reject hook. When the server says rate_limited,
        it has entered a 3-second lockout — record the cutoff so every
        REST-firing path can bail out instead of piling on more rejects.

        Without this, a single rate burst (8 orders, ~25ms) used to
        trigger a 470-680ms cascade of cancel rejects because the SDK
        bucket happily handed out tokens that the server then rejected.
        """
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
        # Take the LATER of the existing and new cutoff — overlapping
        # rejects shouldn't shorten the window.
        if new_until > self._lockout_until:
            self._lockout_until = new_until
            self._lockout_count += 1
            op = msg.get("op") or "?"
            sym = msg.get("symbol") or ""
            print(f"[v17 LOCKOUT] op={op} sym={sym} "
                  f"retry_ms={retry_ms:.0f} count={self._lockout_count}")

    def _make_coid(self) -> str:
        """Unique client_order_id tagged with our prefix.

        Reconcile inspects the prefix on server-side orders to distinguish
        ones WE posted (safe to cancel as orphans) from ones the dashboard
        webapp posted (must leave alone). Format: "{prefix}-{N}" with N
        strictly monotonic, so collisions across restarts are impossible
        within a single round.
        """
        self._coid_counter += 1
        return f"{self.cfg.client_order_id_prefix}-{self._coid_counter}"

    def _can_send(self, sym: str, side: str, qty: int,
                  is_modify: bool = False,
                  primary_qty: int = 0) -> bool:
        """Hard cap — refuse to send when worst-case fill of every tracked
        resting order on this side + every in-flight order on this side +
        this new send could push |position| past (limit - pad).

        Robust against stale tracking: position is always accurate (it's
        the integral of fills), so this gates against the absolute floor
        regardless of how stale our resting view is. Returns True iff
        safe to send.

        For modify, primary_qty is already counted in resting_qty, so the
        net additional exposure is max(0, target_qty - primary_qty).
        """
        s = self.state[sym]
        cap = s.position_limit - self.cfg.mm_inventory_safety_pad
        resting = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        add = max(0, qty - primary_qty) if is_modify else qty
        if side == "bid":
            worst = s.position + resting + in_flight + add
            if worst > cap:
                print(f"[v17 CAP:{sym}/bid] BLOCK qty={qty} "
                      f"pos={s.position} rest={resting} inf={in_flight} "
                      f"add={add} > cap={cap}")
                return False
        else:  # ask
            worst = s.position - resting - in_flight - add
            if worst < -cap:
                print(f"[v17 CAP:{sym}/ask] BLOCK qty={qty} "
                      f"pos={s.position} rest={resting} inf={in_flight} "
                      f"add={add} < -cap={-cap}")
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
            s.last_quote_apply_t = time.time()

    def _apply_side(self, sym: str, side: str,
                    target_px: Optional[int], target_qty: int) -> None:
        """One-call update: modify when possible, else cancel+post.

        Multi-order safety: resting[side] is dict[oid -> entry]. Any
        orphans (extra oids beyond the primary) are cancelled BEFORE any
        new send. This is what prevents the +206 blowout — even if a
        modify silently creates a new oid while the old stays alive, the
        next _apply_side call cleans the old one up before posting more.
        """
        # Server lockout: every cancel/post would just stack more rejects
        # on the existing 3s lockout. Reconcile catches up after the
        # window closes. emergency_flatten_if_needed bypasses this gate
        # because it's the hard backstop.
        if self._in_lockout():
            return
        s = self.state[sym]
        side_orders = s.resting[side]
        primary = self._primary_resting(sym, side)

        # ---- cancel-only branch ----
        if target_px is None or target_qty <= 0:
            # When 2+ orders sit on this side, the loop bursts cancels at
            # ~5ms each. Defer when the bucket is low — the next MM cycle
            # will retry, and pre-reveal pull explicitly gates by
            # _in_lockout above for the same reason.
            if (len(side_orders) >= 2
                    and self._tokens_available()
                    < self.cfg.bulk_cancel_min_tokens):
                return
            for oid in list(side_orders.keys()):
                if not self._cancel(sym, oid):
                    # Rate-window full or lockout — leave remaining orders
                    # tracked so we don't lose them. Next MM cycle retries.
                    return
                side_orders.pop(oid, None)
            return

        # ---- prune orphans BEFORE any send ----
        # Any oid that isn't the primary is an orphan we want gone before
        # adding more exposure on this side.
        if primary is not None:
            orphan_oids = [oid for oid in side_orders.keys()
                           if oid != primary["order_id"]]
            # Bulk orphan cleanup: skip when bucket is low. Worst case
            # the orphan stays one more MM cycle (2.0s) — _can_send
            # already counts orphan qty against capacity, so we won't
            # double-quote.
            if (len(orphan_oids) >= 2
                    and self._tokens_available()
                    < self.cfg.bulk_cancel_min_tokens):
                return
            for oid in orphan_oids:
                if not self._cancel(sym, oid):
                    return
                side_orders.pop(oid, None)
            # primary may have been popped if _cancel saw it as gone — refresh
            primary = self._primary_resting(sym, side)

        # ---- modify path (single live order, latency exploit) ----
        if primary is not None and self.cfg.use_modify_replace:
            same_px = (abs(primary["price"] - target_px)
                       < self.cfg.quote_min_change_ticks)
            same_qty = primary["qty"] == target_qty
            if same_px and same_qty:
                return  # nothing changed
            # Hard cap: refuse to bump qty if it would breach the limit.
            primary_qty = int(primary.get("qty") or 0)
            if not self._can_send(sym, side, target_qty,
                                  is_modify=True, primary_qty=primary_qty):
                return
            # Rate-budget gate: skip a non-critical modify when the
            # bucket is low. Cancels (target_px=None branch above) are
            # NOT gated because they reduce risk. Reveal hot-path and
            # adverse-CP widening still re-quote because they move price
            # by 4+ ticks — well past the deadband.
            if self._tokens_available() < self.cfg.mm_send_min_tokens:
                return
            # Hard sliding-window cap: skip if we'd exceed 15/s.
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
                new_oid = (order.get("order_id")
                           or order.get("id")
                           or old_oid)
                new_qty = order.get("qty", target_qty)
                # Swap entries: drop old, insert new (may be same oid).
                if new_oid != old_oid:
                    side_orders.pop(old_oid, None)
                side_orders[new_oid] = {
                    "order_id": new_oid,
                    "price": target_px,
                    "qty": int(new_qty),
                    "added_t": time.time(),
                }
                self._modify_count += 1
                if self.cfg.log_every_modify:
                    kp = (res or {}).get("kept_priority")
                    print(f"[v17 MODIFY:{sym}/{side}] "
                          f"{old_oid}->{new_oid} "
                          f"px={target_px} qty={new_qty} kept={kp}")
                return
            except Exception as e:
                msg = str(e).lower()
                is_gone = any(x in msg for x in ("404", "not found",
                                                  "no such", "gone"))
                # 400 Bad Request: server rejected the modify usually
                # because the order is in a terminal state (already
                # cancelled, already filled, or otherwise unmodifiable).
                # 403 Forbidden: account-state error (cash/margin) or
                # the order belongs to a different session. Either way
                # retrying won't help and the order is unmodifiable.
                # Treat both the same as gone — drop tracking, fall
                # through. Leaving them tracked perpetuates orphan
                # churn because reconcile then tries to cancel an oid
                # that's not actually on the server.
                is_bad = ("400" in msg or "bad request" in msg
                           or "403" in msg or "forbidden" in msg)
                if is_gone or is_bad:
                    side_orders.pop(old_oid, None)
                else:
                    # Rate-limit / other transient. Order may still be live.
                    # DON'T fall through (would add another live order on
                    # the same side).
                    print(f"[v17 MODIFY:{sym}/{side}@{target_px}x"
                          f"{target_qty}] {type(e).__name__}: {e}")
                    return
            finally:
                s.in_flight[side] = max(
                    0, s.in_flight[side] - net_add)
        elif primary is not None:
            # Modify disabled. Cancel primary; only post fresh if it died.
            if not self._cancel(sym, primary["order_id"]):
                return
            side_orders.pop(primary["order_id"], None)

        # ---- post fresh ----
        # Hard min-price guard: server returns 400 Bad Request on price<=0.
        # _passive_quote already filters but defense-in-depth here catches
        # any other caller (precompute hot path, future call sites).
        if target_px < self.state[sym].tick:
            return
        # Per-(sym,side) post backoff: skip posting when a recent post
        # returned 400/403 — server-side state hasn't changed yet so the
        # next post would just reject too.
        backoff_t = self._post_backoff_until.get((sym, side), 0.0)
        if backoff_t and time.monotonic() < backoff_t:
            return
        # At this point side_orders should be empty (orphans + primary
        # all cancelled). Belt-and-braces: cancel anything still sitting.
        for oid in list(side_orders.keys()):
            if self._cancel(sym, oid):
                side_orders.pop(oid, None)
            else:
                # A still-live untracked order means posting now would
                # double our exposure. Skip this cycle.
                return
        # Hard cap re-check before send. _quote_size already enforced this
        # under normal conditions, but reconcile pruning, race conditions,
        # or arb/stale paths can leave stale state — this is the floor.
        if not self._can_send(sym, side, target_qty):
            return
        # Rate-budget gate (same justification as the modify path).
        if self._tokens_available() < self.cfg.mm_send_min_tokens:
            return
        # Duplicate-price guard: never post two live orders at the same
        # price on the same side. Even if cancellation appears successful
        # above, server lag can leave a phantom briefly; refusing to post
        # at the same price (and waiting one MM cycle) breaks the case
        # that caused the v17/122647 breach (452986 + 453011 both @ 34).
        for entry in side_orders.values():
            if int(entry.get("price", 0)) == int(target_px):
                if self.cfg.log_every_modify:
                    print(f"[v17 DUP-PX:{sym}/{side}@{target_px}] "
                          f"already tracked, skipping post")
                return
        # Hard sliding-window cap before fresh post.
        if not self._can_send_now():
            self._sends_deferred += 1
            return
        self._register_send()
        fn = self.c.buy if side == "bid" else self.c.sell
        s.in_flight[side] += target_qty
        try:
            res = fn(sym, target_px, target_qty,
                     client_order_id=self._make_coid())
            # Server may return either {"order_id": N, ...} (flat) or
            # {"order": {"order_id": N, ...}, "trades": [...]} (nested,
            # same shape as modify). Check both so we never lose track
            # of an order we just posted — untracked = orphan next tick.
            res_obj = res or {}
            nested = res_obj.get("order") or {}
            oid = (res_obj.get("order_id")
                   or res_obj.get("id")
                   or nested.get("order_id")
                   or nested.get("id"))
            if oid is not None:
                side_orders[oid] = {
                    "order_id": oid,
                    "price": target_px,
                    "qty": target_qty,
                    "added_t": time.time(),
                }
            elif self.cfg.log_every_modify:
                print(f"[v17 POST:{sym}/{side}@{target_px}] "
                      f"oid missing in response, will be orphaned: "
                      f"{res_obj}")
            self._post_count += 1
            # Successful post clears any prior backoff on this side.
            self._post_backoff_until.pop((sym, side), None)
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            # 400 / 403: server-side terminal reject. Arm backoff so the
            # next N MM cycles don't re-post the same guaranteed-reject
            # order. Log ONCE per backoff window instead of every cycle.
            is_terminal = ("400" in low or "bad request" in low
                            or "403" in low or "forbidden" in low)
            if is_terminal:
                already_backed_off = (
                    self._post_backoff_until.get((sym, side), 0.0)
                    > time.monotonic())
                self._post_backoff_until[(sym, side)] = (
                    time.monotonic() + self.cfg.post_backoff_sec)
                if not already_backed_off:
                    print(f"[v17 POST:{sym}/{side}@{target_px}] "
                          f"{type(e).__name__}: {e} -> backoff "
                          f"{self.cfg.post_backoff_sec}s")
            elif "limit" not in low and "position" not in low:
                print(f"[v17 POST:{sym}/{side}@{target_px}] "
                      f"{type(e).__name__}: {e}")
        finally:
            s.in_flight[side] = max(0, s.in_flight[side] - target_qty)

    def _cancel(self, sym: str, order_id) -> bool:
        """Cancel a resting order. Returns True if the server confirmed the
        order is no longer live (success OR already-gone). Returns False on
        rate-limit or unexpected error — caller MUST keep tracking the
        order, because the server still has it and it can still fill.

        v17 bug: prior code silently swallowed rate_limited and let the
        caller clear resting[side]=None. The order kept filling and we
        lost track. Crystal-clear post-mortem in 024941/025222.
        """
        # During a server lockout every cancel is rejected; report False
        # so the caller keeps tracking the order and we don't burn a
        # bucket token on a guaranteed-reject. Emergency flatten bypasses
        # this check.
        if self._in_lockout():
            return False
        # Sliding-window rate cap. Refusing here causes the caller to
        # keep the order tracked; the next loop iteration retries.
        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        self._register_send()
        try:
            self.c.cancel(order_id)
            return True
        except Exception as e:
            msg = str(e).lower()
            # 404 / gone / no-such — server doesn't have it, safe to clear.
            # 400 Bad Request — already-cancelled or terminal-state.
            # 403 Forbidden — account-state issue; cancel won't succeed.
            # All three: drop the order from tracking. The alternative is
            # perpetually retrying an order we can't actually cancel.
            if (any(x in msg for x in ("404", "not found",
                                       "no such", "gone"))
                    or "400" in msg or "bad request" in msg
                    or "403" in msg or "forbidden" in msg):
                return True
            # Rate-limit, network error, etc. — order is probably still live.
            print(f"[v17 CANCEL:{sym} oid={order_id}] "
                  f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # IOC sniping (used by arb + stale sweeper)
    # ==================================================================
    def _ioc(self, sym: str, side: str, qty: int, price: int) -> bool:
        """Single IOC at price. Returns True if order was accepted.

        IOCs add immediate position (best-case full take). Arb and stale
        sweeper paths route through here, so the hard cap check below is
        the same gate _apply_side uses for passive posts. This is the
        backstop that prevents arb/stale from bypassing the pad — they
        used to do their own cap math which double-counted differently
        from MM and could push past the limit.
        """
        if qty <= 0:
            return False
        if self._in_lockout():
            return False
        # Wide-quoter trap filter: extreme prices (typically posted by
        # adversaries trying to bait IOC sweepers) get refused before
        # we burn a token. Applies to arb, stale, and reveal-IOC paths.
        max_dist = self.cfg.max_ioc_distance_ticks
        if max_dist > 0:
            fair = (self.fair_a()[0] if sym == "A" else self.fair_b()[0])
            if abs(price - fair) > max_dist:
                if self.cfg.log_every_modify:
                    print(f"[v17 IOC-DIST-BLOCK:{sym}/{side}@{price}] "
                          f"|px-fair|={abs(price-fair):.1f} > {max_dist}t")
                return False
        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        s = self.state[sym]
        # Convert buy/sell -> bid/ask for capacity bookkeeping.
        cap_side = "bid" if side == "buy" else "ask"
        if not self._can_send(sym, cap_side, qty):
            return False
        self._register_send()
        # Hard position guard (legacy belt-and-braces — _can_send is the
        # tighter check, but keep this as the absolute server-cap check
        # in case can_send is ever bypassed via a future code path).
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
            msg = str(e)
            if "limit" not in msg.lower() and "position" not in msg.lower():
                print(f"[v17 IOC:{sym}/{side}@{price}x{qty}] "
                      f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # ARB LOOP: cross-market when implied_X diverges
    # ==================================================================
    def _arb_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.cfg.arb_enabled:
                    self._arb_step()
            except Exception as e:
                print(f"[v17 ARB-ERR] {e!r}")
            self._stop.wait(self.cfg.arb_check_sec)

    def _arb_step(self) -> None:
        if self.phase != "running":
            return
        if self._n_remaining() < 1:
            return
        # Server lockout — arb IOCs would just stack rejects.
        if self._in_lockout():
            return
        # Rate-budget gate: arb fires up to 2 IOCs back-to-back. Skip
        # this opportunity when the bucket is low so reveal/fill paths
        # don't get rate_limited mid-burst.
        if self._tokens_available() < self.cfg.arb_min_tokens:
            return
        mid_a = self._book_mid("A")
        mid_b = self._book_mid("B")
        if mid_a is None or mid_b is None:
            return
        # Implied X from each side
        n_rem = self._n_remaining()
        impl_a = (mid_a - self._running_sum) / n_rem
        impl_b = mid_b
        diff = impl_a - impl_b  # +ve: A says X higher than B says
        if abs(diff) < self.cfg.arb_min_edge_ticks:
            return
        slice_a = self.cfg.arb_max_slice_a
        slice_b = self.cfg.arb_max_slice_b
        pad = self.cfg.arb_pos_pad
        sa, sb = self.state["A"], self.state["B"]
        if diff > 0:
            # A market priced too high vs B -> sell A, buy B
            sell_a = min(slice_a, max(0, (sa.position - (-sa.position_limit + pad))))
            buy_b = min(slice_b, max(0, ((sb.position_limit - pad) - sb.position)))
            best_bid_a = self._book_best("A", "bids")
            best_ask_b = self._book_best("B", "asks")
            if sell_a > 0 and best_bid_a is not None:
                # IOC at best bid (we sell)
                px, sz = best_bid_a
                self._ioc("A", "sell", min(sell_a, sz), px)
            if buy_b > 0 and best_ask_b is not None:
                px, sz = best_ask_b
                self._ioc("B", "buy", min(buy_b, sz), px)
            print(f"[v17 ARB] sell A / buy B  diff={diff:+.2f}t  "
                  f"sell_a={sell_a} buy_b={buy_b}")
        else:
            # A priced too low vs B -> buy A, sell B
            buy_a = min(slice_a, max(0, ((sa.position_limit - pad) - sa.position)))
            sell_b = min(slice_b, max(0, (sb.position - (-sb.position_limit + pad))))
            best_ask_a = self._book_best("A", "asks")
            best_bid_b = self._book_best("B", "bids")
            if buy_a > 0 and best_ask_a is not None:
                px, sz = best_ask_a
                self._ioc("A", "buy", min(buy_a, sz), px)
            if sell_b > 0 and best_bid_b is not None:
                px, sz = best_bid_b
                self._ioc("B", "sell", min(sell_b, sz), px)
            print(f"[v17 ARB] buy A / sell B  diff={diff:+.2f}t  "
                  f"buy_a={buy_a} sell_b={sell_b}")

    # ==================================================================
    # STALE SWEEPER (per symbol)
    # ==================================================================
    def _stale_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.cfg.stale_attack_enabled:
                    for sym in self.SYMS:
                        self._stale_step(sym)
            except Exception as e:
                print(f"[v17 STALE-ERR] {e!r}")
            self._stop.wait(self.cfg.stale_attack_check_sec)

    def _stale_step(self, sym: str) -> int:
        if self.phase != "running":
            return 0
        s = self.state[sym]
        book = s.book
        if book is None:
            return 0
        if sym == "A":
            fair, _ = self.fair_a()
        else:
            fair, _ = self.fair_b()
        min_edge = self.cfg.stale_attack_min_edge_ticks
        min_size = self.cfg.stale_attack_min_size
        max_slice = self.cfg.stale_attack_max_slice
        pad = self.cfg.stale_attack_position_pad
        taken = 0
        # asks below fair (we BUY)
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    if px is None:
                        continue
                    continue
                if fair - px < min_edge:
                    break
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, px):
                    taken += want
                    room_buy -= want
                    print(f"[v17 STALE:{sym}] BUY {want}@{px}  "
                          f"fair={fair:.1f} edge={fair - px:.1f}t")
        # bids above fair (we SELL)
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    if px is None:
                        continue
                    continue
                if px - fair < min_edge:
                    break
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, px):
                    taken += want
                    room_sell -= want
                    print(f"[v17 STALE:{sym}] SELL {want}@{px}  "
                          f"fair={fair:.1f} edge={px - fair:.1f}t")
        return taken

    # ==================================================================
    # Reconciliation loop — sync tracker with server's view of our orders
    # ==================================================================
    def _reconcile_loop(self) -> None:
        """Periodically reconcile our resting[] tracker with the server's
        actual list of our open orders. Two failure modes this fixes:

          1. We have an oid the server doesn't (already filled or
             cancelled but we missed the WS event) -> remove from tracker
             so `_quote_size` doesn't double-count phantom exposure.

          2. Server has an oid we don't (orphan from a modify path that
             returned a new oid we didn't see, or a partial-fill chain
             that confused us) -> CANCEL it. This is the bulletproof
             cleanup against the +206 blowout class of failure.
        """
        while not self._stop.is_set():
            try:
                if self.phase == "running":
                    self._reconcile_once()
            except Exception as e:
                print(f"[v17 RECON-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.reconcile_check_sec)

    def _reconcile_once(self) -> None:
        # Server lockout: skip reconcile entirely. my_orders + any
        # orphan cancels would all be rejected. Reconcile is a cleanup
        # task, not a critical path — a 3-5s slip is harmless.
        if self._in_lockout():
            return
        # Rate-budget gate: reconcile costs 2 GETs + up to N cancels per
        # tick. Defer to the next interval when the bucket is low —
        # orphans are bounded by ongoing MM activity, so a 5-10s slip
        # is harmless.
        if self._tokens_available() < self.cfg.reconcile_min_tokens:
            return
        # Get server's view per symbol (one REST call each).
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
                    print(f"[v17 RECON:{sym}] my_orders: "
                          f"{type(e).__name__}: {e}")
                continue
            server_oids = {int(o.get("order_id") or o.get("id") or -1): o
                           for o in (orders or [])
                           if (o.get("order_id") or o.get("id")) is not None}
            s = self.state[sym]
            now = time.time()
            min_age = self.cfg.reconcile_min_age_sec
            with self.lock:
                # (1) prune our tracker — but only entries that are old
                # enough that the server has had time to index them. A
                # freshly-posted order may briefly not appear in my_orders
                # due to eventual consistency; pruning it would cause the
                # next MM cycle to post a duplicate at the same price.
                for side in ("bid", "ask"):
                    for oid in list(s.resting[side].keys()):
                        if oid in server_oids:
                            continue
                        entry = s.resting[side][oid]
                        age = now - float(entry.get("added_t", 0))
                        if age >= min_age:
                            s.resting[side].pop(oid, None)
                # (2) cancel server-side orphans (oids the server has but
                # we don't track on either side). When
                # reconcile_respect_manual_orders is True, only cancel
                # orphans whose client_order_id starts with our prefix —
                # everything else is presumed to belong to the dashboard
                # webapp (day1/web/) which shares this API key.
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
                if (len(orphan_oids) >= 2
                        and self._tokens_available()
                        < self.cfg.bulk_cancel_min_tokens):
                    print(f"[v17 RECON:{sym}] deferring {len(orphan_oids)} "
                          f"orphan cancels (low tokens)")
                    continue
                # Per-tick cap on orphan cancels. Reconcile runs every 5s
                # so a 20-orphan backlog clears in 4 ticks. Without this
                # cap, reconcile fired 20 cancels in 270ms (v17/145320)
                # and saturated the server's 1-second window. The cap
                # plus per-iteration _can_send_now check inside _cancel
                # double-protects against bursts.
                cap = max(1, int(self.cfg.reconcile_max_cancels_per_tick))
                total_orphans = len(orphan_oids)
                bulk_thr = self.cfg.reconcile_bulk_cancel_threshold
                # Circuit breaker: pathological orphan backlog → one
                # cancel_all(symbol) drains everything (orphans AND any
                # of our own tracked orders, which the next MM cycle
                # will re-post). Costs 1 REST call instead of N.
                if (bulk_thr > 0 and total_orphans >= bulk_thr
                        and self._can_send_now()):
                    self._register_send()
                    try:
                        self.c.cancel_all(symbol=sym)
                        # Wipe local tracker; MM cycle will re-quote.
                        for side in ("bid", "ask"):
                            s.resting[side].clear()
                        print(f"[v17 RECON:{sym}] BULK cancel_all fired "
                              f"({total_orphans} orphans >= {bulk_thr})")
                        continue
                    except Exception as e:
                        # Fall through to per-oid cap path.
                        print(f"[v17 RECON:{sym}] cancel_all failed: "
                              f"{type(e).__name__}: {e}")
                if total_orphans > cap:
                    orphan_oids = orphan_oids[:cap]
                cancelled = 0
                for oid in orphan_oids:
                    if not self._cancel(sym, oid):
                        # rate gate triggered mid-loop; stop and resume
                        # next tick rather than spinning on rejects.
                        break
                    cancelled += 1
                # Collapse N per-oid messages into one summary line so
                # the orphan storm doesn't drown the log.
                if total_orphans:
                    suffix = (f" (capped at {cap}/{total_orphans})"
                              if total_orphans > cap else "")
                    print(f"[v17 RECON:{sym}] cancelled {cancelled} "
                          f"orphan(s){suffix}")

    # ==================================================================
    # Book helpers
    # ==================================================================
    def _book_mid(self, sym: str) -> Optional[float]:
        book = self.state[sym].book
        if not book:
            return None
        bb = book.get("bids") or []
        ba = book.get("asks") or []
        if not bb or not ba:
            return None
        return (bb[0]["price"] + ba[0]["price"]) / 2.0

    def _book_best(self, sym: str, side: str) -> Optional[Tuple[int, int]]:
        book = self.state[sym].book
        if not book:
            return None
        lvls = book.get(side) or []
        if not lvls:
            return None
        return (lvls[0]["price"], int(lvls[0].get("qty") or 0))

    # ==================================================================
    # Event handlers (called by GameClient WS callbacks)
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
        # If our maker order matched here, decrement / remove that oid.
        liq = msg.get("liquidity")
        if liq == "maker":
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
            # Adverse-CP tracking: a maker fill from a known taker is an
            # adverse-selection event. side=="buy" means we BOUGHT (our
            # bid was lifted by their sell), side=="sell" means we SOLD
            # (our ask was lifted by their buy). Record under the side
            # of OUR order that filled.
            cp = (msg.get("counterparty") or "").upper()
            if cp and cp in self.cfg.adverse_cps:
                our_side = "bid" if side == "buy" else "ask"
                self._adverse_fills[(sym, our_side)].append(
                    (time.time(), cp))

        # Defensive immediate cancel of the WRONG-side resting whenever a
        # fill pushed us into the pad zone. v17/024941 walked from +44 to
        # +109 lots in 2.7s while we were rate-limited; cancelling here
        # breaks the chain even if the MM loop hasn't run yet.
        # During server lockout the _cancel calls would all be rejected;
        # _cancel returns False and we leave tracking intact so the next
        # MM cycle (post-lockout) cleans up.
        pad = self.cfg.mm_inventory_safety_pad
        if s.position >= s.position_limit - pad:
            self._defensive_cancel_side(sym, "bid")
        if s.position <= -(s.position_limit - pad):
            self._defensive_cancel_side(sym, "ask")

        # HARD BACKSTOP: if we somehow exceeded the position limit, IOC
        # flatten back to (limit - pad). This is the last line of defense
        # against the +206 blowout class of failure.
        self._emergency_flatten_if_needed(sym)

        # Wake the MM loop (coalesces multiple fills into one requote).
        # We used to call _refresh_mm_quotes synchronously here, but that
        # fanned out 4-8 REST calls per fill — at 7+ fills/sec we burst
        # past the 20/s server cap (186 rejects in v17/025222).
        self._mm_wake.set()

    def _defensive_cancel_side(self, sym: str, side: str) -> None:
        """Cancel every tracked order on `side` for `sym`. Used by the
        on_fill_event pad-zone defense.

        Two orders or fewer: per-oid cancels (precise, doesn't touch
        manual dashboard orders on the same symbol/side).

        Three or more orders: skip when in lockout or low tokens — the
        next MM cycle will retry, and orphans don't increase position
        beyond what _can_send already counts. Without this guard, a
        big pad-zone breach would burst N cancels in <100ms and start
        the next lockout cascade.
        """
        s = self.state[sym]
        side_orders = s.resting[side]
        if not side_orders:
            return
        if self._in_lockout():
            # Every cancel would be rejected; leave tracking intact.
            return
        if (len(side_orders) >= 3
                and self._tokens_available()
                < self.cfg.bulk_cancel_min_tokens):
            return
        for oid in list(side_orders.keys()):
            if self._cancel(sym, oid):
                side_orders.pop(oid, None)

    def _emergency_flatten_if_needed(self, sym: str) -> None:
        """If |pos| has overrun the position_limit, IOC back to the pad
        boundary. Cancels all resting orders on the side that contributed
        to the overrun so they can't keep filling.

        Bypasses _in_lockout and bulk-cancel token gates: position is
        OVER LIMIT, every reject we eat trying to flatten is acceptable
        risk relative to letting the breach grow.
        """
        s = self.state[sym]
        limit = s.position_limit
        trip = int(limit * self.cfg.emergency_flatten_factor)
        if abs(s.position) <= trip:
            return
        # Batch-cancel via cancel_all: 1 token instead of N. Also cancels
        # manual dashboard orders on this symbol; acceptable because the
        # position is over the limit and the user's manual order would
        # likely add risk.
        if s.resting["bid"] or s.resting["ask"]:
            try:
                self.c.cancel_all(symbol=sym)
                s.resting["bid"].clear()
                s.resting["ask"].clear()
            except Exception as e:
                print(f"[v17 EMERGENCY cancel_all:{sym}] "
                      f"{type(e).__name__}: {e}")
        if s.position > trip:
            excess = s.position - (limit - self.cfg.mm_inventory_safety_pad)
            best = self._book_best(sym, "bids")
            if best is not None and excess > 0:
                px, sz = best
                # Bypass _ioc's lockout gate: emergency is critical.
                try:
                    self.c.sell_ioc(sym, px, min(excess, sz))
                except Exception as e:
                    print(f"[v17 EMERGENCY IOC sell:{sym}] "
                          f"{type(e).__name__}: {e}")
                print(f"[v17 EMERGENCY:{sym}] pos={s.position}/lim={limit}  "
                      f"IOC sell {min(excess, sz)}@{px}")
        else:
            excess = -s.position - (limit - self.cfg.mm_inventory_safety_pad)
            best = self._book_best(sym, "asks")
            if best is not None and excess > 0:
                px, sz = best
                try:
                    self.c.buy_ioc(sym, px, min(excess, sz))
                except Exception as e:
                    print(f"[v17 EMERGENCY IOC buy:{sym}] "
                          f"{type(e).__name__}: {e}")
                print(f"[v17 EMERGENCY:{sym}] pos={s.position}/lim={limit}  "
                      f"IOC buy {min(excess, sz)}@{px}")

    def on_trade(self, msg: dict) -> None:
        # We don't use the public tape directly for now.
        pass

    def on_quote_event(self, msg: dict) -> None:
        # Book event handler covers this; nothing extra.
        pass

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        with self.lock:
            self.phase = phase
            if phase == "running":
                self._phase_running_t = time.time()
                self.posterior.reset(reveals or [])
                self._reveal_count = len(self.posterior.reveals)
                self._running_sum = float(sum(self.posterior.reveals))
                self._bump_posterior_gen()
                self._precompute_request.set()
            elif phase in ("completed", "settling"):
                self._phase_running_t = None
                # Drop tracker on phase exit; orders are cancelled by
                # the server at round end. Keep positions for settlement.
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
        # One-shot cancel-all per symbol (1 REST call vs 2 per-order cancels).
        for sym in self.SYMS:
            try:
                self.c.cancel_all(symbol=sym)
            except Exception as e:
                print(f"[v17 FLATTEN cancel_all:{sym}] {type(e).__name__}: {e}")
            s = self.state[sym]
            for kind in ("bid", "ask"):
                s.resting[kind] = {}
        # IOC the positions to flat
        for sym in self.SYMS:
            s = self.state[sym]
            if s.position == 0:
                continue
            side = "sell" if s.position > 0 else "buy"
            qty = abs(s.position)
            best = self._book_best(sym, "bids" if side == "sell" else "asks")
            if best is None:
                continue
            px = best[0]
            self._ioc(sym, side, qty, px)
            print(f"[v17 FLATTEN:{sym}] {side} {qty}@{px}")

    def stop(self) -> None:
        self._stop.set()
        self._mm_wake.set()
        self._precompute_request.set()


# ===========================================================================
# Standalone runner. Prefer run_combined17.
# ===========================================================================
def main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")
    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} (a,w) pairs.")

    strat = Strategy(c, post)

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  "
              f"running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(f"FILL {msg['symbol']} {msg['side']:>4s} {msg['qty']}@{msg['price']}  "
              f"liq={msg.get('liquidity')}  cp={msg.get('counterparty')}")
        strat.on_fill_event(msg)

    def on_game_state(msg):
        ph = msg.get("phase")
        rev = msg.get("reveals") or []
        print(f"STATE phase={ph}  reveals={len(rev)}")
        strat.on_phase_change(ph, rev)

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

    print("\nv17 standalone. s=status, p=precompute, f=flatten, q=quit.\n")
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
                with strat._precompute_lock:
                    print(f"  precompute: {len(strat._precompute)} scenarios "
                          f"@ cycle {strat._precompute_for_cycle}  "
                          f"({strat._last_precompute_us/1000:.1f}ms)")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precompute.items())
                print(f"  {len(items)} scenarios:")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.prob:.3f}  "
                          f"A=[{sc.bid_a},{sc.ask_a}]@{sc.fair_a:.1f}  "
                          f"B=[{sc.bid_b},{sc.ask_b}]@{sc.fair_b:.2f}")
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
