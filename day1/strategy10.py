"""Day 1 live trading strategy v10 — precompute architecture (self-contained).

This file is the consolidated single-file successor to strategy{7,8,9}.py.
All v7/v8/v9/v10 behavior is folded into one Strategy class so the hot path
(on_reveal, on_quote_event, on_trade) doesn't pay any super() chain cost.

DESIGN — what makes v10 fast at the moments that matter:

  1. PrecomputedScenario: snapshot of what to do if the next reveal equals a
     particular value v. Stores the new posterior (already reweighted), the
     new fair/sigma, the market-making bid_px/ask_px, AND the IOC bounds
     (lift_to_px = max ask we'd IOC-buy; hit_to_px = min bid we'd IOC-sell).

  2. Background `_precompute_worker`: triggered by an Event after every
     reveal and ~`precompute_lead_sec` seconds before each predicted reveal.
     For every reveal value v with marginal P(X=v) >= `precompute_min_prob`,
     computes the scenario. Cached in `_precomputed: Dict[int, ...]`.

  3. Fast-path `on_reveal`: dict lookup → atomic posterior swap → post the
     pre-computed quotes → walk top of book and IOC any level inside the
     pre-computed lift/hit bounds. NO posterior update, NO fair recompute,
     NO Bayesian math in the hot path. Total cost: O(book_depth).

  4. Between-reveal MM (maximised aggression):
       - Default v7 maker quotes with edge = max(min_edge, edge_per_sigma * sigma)
       - Penny/dime BBO whenever doing so keeps per-fill margin >= tight_floor_edge
       - tight_penny_min_reveals = 1 (penny from the very first reveal)
       - Pre-reveal-window upsize (qty=5) ~1.5s before each reveal when consensus confirms
       - No-edge pull: if expected edge < `no_edge_pull_ticks`, both sides None
       - Adverse-fill skew: after a maker fill, the hit side widens for a decay window

  5. FAST scheduler loop (20ms tick) — purpose: keep the trade-execution
     hot path nearly free of synchronous network/compute.
       - Pre-reveal cancel at `nxt - pre_reveal_cancel_sec` (0.3s)
       - Pre-reveal repost at `nxt - pre_reveal_post_sec` (1.5s)
       - Precompute prefetch trigger at `nxt - precompute_lead_sec` (4s)
       - Book pre-warm from `nxt - book_prewarm_lead_sec` (1.0s) onward,
         refreshing `_book_cache` every fast tick. on_reveal reads the
         cached book for its IOC sweep and skips the REST round-trip
         entirely (saves ~10-50ms of latency at the most time-critical
         moment).
       - JIT theo-freshness guarantee at `nxt - jit_theo_refresh_sec` (0.5s):
         if `_precompute_for_cycle != current_cycle`, run `_do_precompute()`
         SYNCHRONOUSLY in the scheduler so the fast on_reveal path is
         guaranteed to hit a fresh table. ~19ms cost, fires at most once
         per cycle.

  6. SLOW housekeeping loop (2s tick) — purpose: keep the precomputed
     theos and cached quote prices fresh so the fast loop never has to
     do Bayesian math itself.
       - Refresh `_current_theos` (fair, sigma, bid_px, ask_px for the
         CURRENT posterior) so inter-reveal MM quoting can read them
         directly via `get_cached_current_theos()` instead of recomputing.
       - Periodic position reconcile vs REST (sanity check).
       - Posterior-staleness watchdog: if precompute is >=2 cycles behind,
         force a synchronous rebuild via `_ensure_theos_fresh(blocking=True)`.

  EXECUTION FLOW AT A REVEAL (T=0 = reveal arrives):
       T-4.0s   async precompute prefetch fires (~19ms in worker thread)
       T-1.5s   pre-reveal repost (last MM update before reveal)
       T-1.0s   book pre-warm begins (fast loop refreshes book every 20ms)
       T-0.5s   JIT synchronous rebuild if precompute still stale
       T-0.3s   pre-reveal cancel of all resting orders
       T=0      on_reveal: O(1) dict lookup -> atomic posterior swap ->
                post precomputed quotes -> IOC-sweep against cached book
                (NO REST book fetch, NO Bayesian math, NO super() dispatch)

  7. v9 SAFETY (kept): reveal-math guard (K>=1), drift-cancel after stale fair,
     opening-round quote clamp, tighter inv_hard_kill_frac=0.4.
     Reveal danger zone is DISABLED in v10 — precomputed scenarios let us
     react safely the instant the reveal lands; the danger zone was a v9
     workaround for slow recompute.

  8. THEO FRESHNESS — theos (the per-value precomputed scenarios) are
     re-derived whenever the posterior changes (after each reveal) AND
     re-verified synchronously 0.5s before each predicted reveal. This means
     the table on_reveal looks at is ALWAYS for the right cycle; if the
     async worker missed its deadline, the scheduler runs the recompute
     inline. Worst case the fast-path on_reveal also rebuilds inline on a
     dict-miss before falling through to the v7 slow path.

Run:
    python day1/run_combined10.py
    python day1/strategy10.py            # standalone, no probe
"""
from __future__ import annotations

import math
import os
import random
import statistics
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sdk.client import GameClient  # noqa: E402


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"

# Informed trader id (set to None / "" to disable). run_combined10 also
# accepts --informed-trader=<NAME> to override at launch.
INFORMED_TRADER_ID: Optional[str] = ""


# ---------------------------------------------------------------------------
# Posterior over (a, w)
# ---------------------------------------------------------------------------
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8    # a = floor(LogN(mu, sigma))
W_LOGN_MU, W_LOGN_SIGMA = 1.7, 0.9    # w = 1 + floor(LogN(mu, sigma))
N_PRIOR_SIM = 1_000_000


class Posterior:
    """Discrete joint posterior over (a, w) given reveals X_i ~ Uniform{a, ..., a+w}."""

    def __init__(
        self,
        a_mu: float = A_LOGN_MU,
        a_sigma: float = A_LOGN_SIGMA,
        w_mu: float = W_LOGN_MU,
        w_sigma: float = W_LOGN_SIGMA,
        n_sim: int = N_PRIOR_SIM,
    ):
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for _ in range(n_sim):
            a_v = int(math.floor(random.lognormvariate(a_mu, a_sigma)))
            w_v = 1 + int(math.floor(random.lognormvariate(w_mu, w_sigma)))
            counts[(a_v, w_v)] += 1
        total = sum(counts.values())
        self.prior: dict[tuple[int, int], float] = {k: v / total for k, v in counts.items()}
        self.posterior: dict[tuple[int, int], float] = dict(self.prior)
        self.reveals: list[float] = []

    def reset(self, reveals: Optional[list[float]] = None) -> None:
        self.posterior = dict(self.prior)
        self.reveals = []
        for x in reveals or []:
            self.update(x)

    def update(self, x: float) -> None:
        self.reveals.append(float(x))
        new: dict[tuple[int, int], float] = {}
        total = 0.0
        eps = 1e-9
        for (a, w), p in self.posterior.items():
            b = a + w
            if x < a - eps or x > b + eps:
                continue
            new_p = p / (w + 1)
            new[(a, w)] = new_p
            total += new_p
        if total <= 0:
            print(f"WARN: reveal {x} outside posterior support; ignoring update")
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def mean_x(self) -> float:
        return sum(p * (a + w / 2.0) for (a, w), p in self.posterior.items())

    def predict_settle(self, running_sum: float, n_remaining: int) -> tuple[float, float]:
        if n_remaining <= 0:
            return float(running_sum), 0.0
        e_inner = 0.0
        e2_inner = 0.0
        e_var = 0.0
        for (a, w), p in self.posterior.items():
            inner_mean = n_remaining * (a + w / 2.0)
            inner_var = n_remaining * (w * (w + 2)) / 12.0
            e_inner += p * inner_mean
            e2_inner += p * inner_mean * inner_mean
            e_var += p * inner_var
        var_total = e_var + (e2_inner - e_inner * e_inner)
        return running_sum + e_inner, math.sqrt(max(var_total, 0.0))


# ---------------------------------------------------------------------------
# PrecomputedScenario: cached "if next reveal == v, do this" plan
# ---------------------------------------------------------------------------
@dataclass
class PrecomputedScenario:
    value: int
    posterior_snapshot: Dict[Tuple[int, int], float]
    fair: float
    sigma: float
    bid_px: Optional[int]
    ask_px: Optional[int]
    lift_to_px: Optional[int]
    hit_to_px: Optional[int]
    marginal_prob: float = 0.0


# ---------------------------------------------------------------------------
# Strategy: unified v7+v8+v9+v10 (no class hierarchy → no super() overhead)
# ---------------------------------------------------------------------------
class Strategy:
    def __init__(self, client: GameClient, posterior: Posterior,
                 symbol: str = "A",
                 informed_trader_id: Optional[str] = None):
        self.c = client
        self.symbol = symbol
        self.posterior = posterior
        self.lock = threading.RLock()

        _iti = (informed_trader_id if informed_trader_id is not None
                else INFORMED_TRADER_ID)
        self.informed_trader_id: Optional[str] = _iti or None

        gs = client.game_state()
        self.duration = gs["duration"]
        self.reveal_interval = gs["reveal_interval"]
        self.n_total = self.duration // self.reveal_interval
        instr = gs["instruments"][symbol]
        self.tick = instr["tick_size"]
        self.position_limit = instr["position_limit"]
        fees = gs.get("fees", {})
        self.maker_fee = fees.get("maker_per_lot", 0.5)
        self.taker_fee = fees.get("taker_per_lot", 0.5)

        self.posterior.reset(gs.get("reveals") or [])
        self.position = int(client.positions()["positions"].get(symbol, 0))
        self.phase: Optional[str] = gs.get("phase")

        # ----- throttle / cache -----
        self._last_quote_event_t: float = 0.0
        self.quote_event_throttle_sec: float = 0.05
        self._posterior_gen: int = 0
        self._fair_sigma_cache: Optional[tuple[int, float, float]] = None
        self.resting: dict[str, Optional[dict]] = {"bid": None, "ask": None}
        self._last_modify_t: dict[int, float] = {}
        self.min_modify_interval_sec: float = 0.25
        self.tokens_low_threshold: float = 3.0
        self._last_maker_apply_t: float = 0.0

        # ----- informed-strike model (v2 carry-forward) -----
        self._informed_strikes_bid: float = 0.0
        self._informed_strikes_ask: float = 0.0
        self._informed_strikes_last_t: float = time.time()

        # ----- core MM knobs -----
        self.quote_qty = 2
        self.min_edge = 1.0
        self.edge_per_sigma = 0.25
        self.skew_per_unit = 0.20
        self.snipe_buffer_sigma = 0.40
        self.snipe_min_edge = 2.0           # v8: raised 1.0 -> 2.0
        self.snipe_book_depth = 10
        self.snipe_max_qty_per_level = 10
        self.max_snipes_per_round = 40       # v8: 120 -> 40 (passive intent)
        self._snipe_count_this_round = 0

        # Penny/dime knobs
        self.tight_quoting_enabled = True
        self.tight_floor_edge = 1.0
        self.tight_step_inside_ticks = 1
        self.tight_penny_sigma_max = 10.0
        self.tight_penny_min_reveals = 1     # v10: was 2 in v7; allow K=1 pennying

        # Book cache
        self._book_cache: Optional[dict] = None
        self._book_cache_t: float = 0.0
        self._book_cache_max_age_sec: float = 1.0

        # Private-feed market signal
        self._last_fill_px: float = 0.0
        self._last_fill_t: float = 0.0
        self._private_signal_max_age_sec: float = 2.0

        # Pre-reveal MM
        self.min_reveals_to_quote = 1
        self.min_reveals_to_snipe = 1
        self.pre_reveal_quote_qty = 1
        self.pre_reveal_min_edge = 4.0
        self.pre_reveal_skew_disabled = True
        self.pre_reveal_warmup_sec = 2.0
        self.pre_reveal_pull_disagreement = 10.0
        self.empty_side_edge = 15.0
        self._phase_running_t: Optional[float] = None

        # Sigma-scaled position cap
        self.max_pos_target_sigma = 2.0
        self.max_pos_floor = 10
        self.max_pos_target_sigma_normal = 3.0
        self.max_pos_floor_normal = 15

        # Market-mid anchor
        self.market_anchor_disagreement_min = 5.0
        self.snipe_max_disagreement = 3.0    # v8: 5.0 -> 3.0
        self.snipe_disagreement_directional = False

        # Variance-scaled quote SIZE
        self.full_qty_sigma_max = 3.0
        self.mid_round_quote_qty = 1
        self.quote_qty_normal = 5
        self.mid_round_quote_qty_normal = 2

        # Inventory dampener
        self.inv_dampen_threshold_frac = 0.40
        self.inv_dampen_factor = 0.0
        self.inv_hard_kill_frac = 0.4        # v9: 0.55 -> 0.4

        # Snipe adverse extra edge
        self.snipe_adverse_extra_edge = 1.5

        # Post-final
        self.quote_after_final_reveal = False
        self.snipe_after_final_reveal = True

        # Maker cadence (v2)
        self.maker_reprice_interval_by_k: list[tuple[int, float]] = [
            (3, 12.0),
            (7, 8.0),
            (99, 4.0),
        ]
        self.informed_counterparties: set[str] = set()
        self.informed_strike_decay_per_sec: float = math.log(2) / 60.0
        self.informed_strike_edge_per_strike: float = 0.5
        self.informed_strike_max: float = 4.0
        self.skip_pre_reveal: bool = True
        self.snipe_position_buffer = 8       # v8: 5 -> 8

        # Informed-trader state (v4)
        self.informed_cooloff_sec: float = 15.0
        self._informed_cooloff_until: dict[str, float] = {"bid": 0.0, "ask": 0.0}
        self._informed_last_t: float = 0.0
        self._informed_last_their_side: Optional[str] = None
        self._informed_last_px: Optional[float] = None
        self._informed_fair_lo: Optional[float] = None
        self._informed_fair_hi: Optional[float] = None
        self._informed_total_qty: int = 0
        self._informed_fill_count: int = 0

        # Public trade tape (v6)
        self.trade_window_sec: float = 30.0
        self._max_recent_trades: int = 1000
        self._recent_trades: deque[tuple[float, float, int, str]] = deque(
            maxlen=self._max_recent_trades)
        self._vwap_num: float = 0.0
        self._vwap_den: int = 0
        self._imb_buy_q: int = 0
        self._imb_sell_q: int = 0

        # Per-CP signed flow (v5)
        self._cp_net_flow: dict[str, int] = defaultdict(int)
        self.cp_cap_threshold: int = 70
        self.cp_near_cap_strike_decay: float = 0.5

        # Pre-reveal VWAP-blended fair (v6)
        self.pre_reveal_mm_enabled: bool = True
        self.min_trade_vol_pre_reveal: int = 3
        self.pre_reveal_vwap_weight: float = 0.7
        self.pre_reveal_max_pos: int = 5

        # v7: don't fight the tape
        self.vwap_clamp_max_k: int = 3
        self.vwap_clamp_min_volume: int = 3
        self.vwap_side_guard_ticks: int = 1
        self.snipe_vwap_buffer: float = 1.0
        self.partial_fill_cool_off_sec: float = 3.0
        self._partial_fill_cool_off_until: dict[str, float] = {"bid": 0.0, "ask": 0.0}

        # ===== v8: pre-reveal-window scheduler + reveal-math guard =====
        self._last_reveal_t: Optional[float] = None
        self._pre_reveal_last_serviced_idx: int = -1
        self._pre_reveal_last_cancelled_idx: int = -1
        self.pre_reveal_post_sec: float = 1.5
        # Pre-reveal cancel lead time. Cancel-ack round-trip is ~150-200ms,
        # so cancel at T-0.9s lands by T-0.7s — leaving a clean book for the
        # full blackout window. Logs (224229/224419/224557) showed 96% of
        # blackout-window maker hits happened between cancel-issue and ack;
        # widening this from 0.6s -> 0.9s gives the ack time to complete
        # BEFORE the blackout opens. Costs ~6% additional MM downtime per
        # reveal cycle (5s) — accepted to eliminate stale-quote hit-bait.
        self.pre_reveal_cancel_sec: float = 0.9
        self.pre_reveal_post_qty: int = 5
        self.pre_reveal_post_enabled: bool = True
        # Quote blackout: stop posting NEW MM quotes (any source: step(),
        # _apply_target_quotes, on_quote_event reactions) within this many
        # seconds before the next reveal. Set slightly LARGER than
        # pre_reveal_cancel_sec so it activates *before* the scheduler's
        # cancel slot — no post can sneak in between blackout opening and
        # cancel landing. With cancel at T-0.9s and blackout at T-0.95s,
        # any in-flight quote is cancelled before reveal AND no new quote
        # gets posted in the dead zone.
        self.pre_reveal_quote_blackout_sec: float = 0.95
        # Token-budget reservation: in the same window, refuse any non-
        # essential REST call (extra modifies, redundant cancels, book
        # fetches) so the post-reveal parallel-IOC burst has a full set of
        # tokens. Reserve this many tokens for the reveal moment. Bucket
        # capacity is 20; reserving 7 leaves ~13 for normal MM work.
        self.pre_reveal_token_reserve: float = 7.0
        # Reveal-math guard
        self.reveal_math_min_k: int = 1      # v9: 2 -> 1 (was missing K=1 leak)
        self.reveal_math_guard_ticks: float = 4.0
        self.reveal_math_guard_enabled: bool = True
        self.reveal_math_k1_widening: float = 2.0  # v9

        # ===== v9: reveal danger zone + drift cancel + opening clamp =====
        self.reveal_danger_zone_sec: float = 2.0
        self.reveal_danger_zone_enabled: bool = False  # v10: disabled
        self.drift_cancel_ticks: float = 3.0
        self.drift_cancel_enabled: bool = True
        self._last_quote_fair_eff: Optional[float] = None
        self.drift_cancel_min_interval: float = 0.5
        self._last_drift_cancel_t: float = 0.0
        try:
            mean0, _ = posterior.predict_settle(
                running_sum=0.0, n_remaining=self.n_total)
            self._prior_E_settle: float = float(mean0)
        except Exception:
            self._prior_E_settle = 27.0
        self.opening_clamp_ticks: int = 1
        self.opening_clamp_enabled: bool = True
        self._sched_log_seq: int = 0

        # ===== v10: precompute architecture =====
        self._precomputed: Dict[int, PrecomputedScenario] = {}
        self._precompute_lock = threading.RLock()
        self._precompute_for_cycle: int = -1
        self._precompute_request = threading.Event()
        self._last_precompute_us: float = 0.0
        self._precompute_run_count: int = 0
        self._precompute_prefetched_cycle: int = -1
        # JIT freshness state — guarantees theos are right for the next reveal
        self._jit_refresh_done_for_cycle: int = -1
        self.jit_theo_refresh_sec: float = 0.5  # synchronous rebuild deadline

        # Book pre-warm: fast loop refreshes the book cache during the reveal
        # window so on_reveal's IOC sweep doesn't pay a synchronous REST
        # round-trip. Pre-warm starts `book_prewarm_lead_sec` before the
        # predicted reveal and runs on every fast tick (20ms) until the
        # reveal fires.
        # Pre-warm only during the last 700ms (down from 1s) — saves tokens
        # outside the critical window. Max age 0.1s (down from 0.2s) so the
        # cache is at most 100ms stale at T+0; this prevents the fallback to
        # a synchronous REST in _fast_post_reveal_sweep (which would add
        # 30-50ms to the IOC fire). 700ms/100ms = ~7 refreshes per cycle.
        self.book_prewarm_lead_sec: float = 0.7
        self.book_prewarm_max_age_sec: float = 0.1
        self._last_book_prewarm_t: float = 0.0

        # Current-state theos cache: fair/sigma/bid_px/ask_px for the CURRENT
        # posterior (not next-reveal scenarios). Maintained by the slow loop
        # so inter-reveal MM quoting doesn't need to recompute Bayesian fair.
        self._current_theos: Optional[Dict[str, float]] = None
        self._current_theos_cycle: int = -1
        self._current_theos_t: float = 0.0
        self.current_theos_max_age_sec: float = 2.5

        # Parallel IOC executor — 3 sequential IOCs at ~80ms each = 240ms
        # wall on the critical post-reveal path. Firing them concurrently
        # collapses to ~80ms wall (one RTT). Cap workers at 6 to cover the
        # max number of price levels we'd sweep in one reveal.
        self.ioc_parallel_workers: int = 6
        self._ioc_executor = ThreadPoolExecutor(
            max_workers=self.ioc_parallel_workers,
            thread_name_prefix="ioc-fire")
        # Knobs
        self.precompute_min_prob: float = 5e-3
        self.precompute_lead_sec: float = 4.0
        self.precompute_max_values: int = 40
        self.no_edge_pull_ticks: float = 0.5
        self.penny_aggressive: bool = True
        self.penny_min_edge_after: float = 1.0
        self.adverse_fill_skew_ticks: float = 1.5
        self.adverse_fill_decay_sec: float = 8.0
        self._last_adverse_fill_t: Dict[str, float] = {"bid": 0.0, "ask": 0.0}

        # ===== threads =====
        self._scheduler_stop = threading.Event()
        # Fast loop: 20ms tick — pre-reveal cancel/post, JIT theo refresh, book pre-warm.
        self._scheduler_tick_sec: float = 0.02
        self._scheduler_thread = threading.Thread(
            target=self._fast_scheduler_loop,
            name="fast-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        # Slow loop: 2s tick — periodic reconcile + freshness sanity checks.
        self.slow_housekeeping_interval_sec: float = 2.0
        self._slow_housekeeping_thread = threading.Thread(
            target=self._slow_housekeeping_loop,
            name="slow-housekeeping",
            daemon=True,
        )
        self._slow_housekeeping_thread.start()
        self._precompute_worker = threading.Thread(
            target=self._precompute_loop,
            name="precompute-worker",
            daemon=True,
        )
        self._precompute_worker.start()
        self._precompute_request.set()  # build the first table immediately

        self._warm_connections(self.ioc_parallel_workers)

    def _warm_connections(self, n: int) -> None:
        """Pre-open n HTTP connections in parallel so urllib3's pool has
        warm sockets ready for the IOC executor.

        Why: diagnose_latency (2026-05-19) showed the first REST call on
        a cold connection is ~1.5-2.5x slower than steady state (~32 ms
        vs ~13 ms p50). With ioc_parallel_workers=6 firing concurrently
        at reveal, we need 6 warm sockets so none pays the cold-TCP
        penalty on the most latency-critical action of the game.
        """
        t0 = time.perf_counter()
        try:
            list(self._ioc_executor.map(
                lambda _: self.c.game_state(), range(n)))
            dt_ms = (time.perf_counter() - t0) * 1000
            print(f"[WARM] {n} HTTP connections warmed in {dt_ms:.1f}ms")
        except Exception as e:
            print(f"[WARM] connection warm-up failed: {e}")

    # ----------------------------------------------------------------
    # Trivial helpers
    # ----------------------------------------------------------------

    def _running_sum(self) -> float:
        return sum(self.posterior.reveals)

    def _n_remaining(self) -> int:
        return max(self.n_total - len(self.posterior.reveals), 0)

    def _maker_reprice_interval(self) -> float:
        k = len(self.posterior.reveals)
        if k == 0:
            return float("inf")
        for k_max, interval in self.maker_reprice_interval_by_k:
            if k <= k_max:
                return interval
        return self.maker_reprice_interval_by_k[-1][1]

    def _decay_strikes(self) -> None:
        now = time.time()
        elapsed = now - self._informed_strikes_last_t
        if elapsed <= 0:
            return
        factor = math.exp(-elapsed * self.informed_strike_decay_per_sec)
        self._informed_strikes_bid *= factor
        self._informed_strikes_ask *= factor
        self._informed_strikes_last_t = now

    def _record_informed_fill(self, counterparty: Optional[str],
                              fill_side: str) -> None:
        if not counterparty or counterparty not in self.informed_counterparties:
            return
        self._decay_strikes()
        flow = self._cp_net_flow.get(counterparty, 0)
        strike_weight = (self.cp_near_cap_strike_decay
                         if abs(flow) >= self.cp_cap_threshold else 1.0)
        if fill_side == "buy":
            self._informed_strikes_bid = min(
                self.informed_strike_max,
                self._informed_strikes_bid + strike_weight)
        else:
            self._informed_strikes_ask = min(
                self.informed_strike_max,
                self._informed_strikes_ask + strike_weight)

    def _informed_extra_edge(self) -> tuple[float, float]:
        self._decay_strikes()
        return (self._informed_strikes_bid * self.informed_strike_edge_per_strike,
                self._informed_strikes_ask * self.informed_strike_edge_per_strike)

    def fair_and_sigma(self) -> tuple[float, float]:
        gen = self._posterior_gen
        cached = self._fair_sigma_cache
        if cached is not None and cached[0] == gen:
            return cached[1], cached[2]
        fair, sigma = self.posterior.predict_settle(
            self._running_sum(), self._n_remaining())
        self._fair_sigma_cache = (gen, fair, sigma)
        return fair, sigma

    # ----------------------------------------------------------------
    # VWAP / market-signal helpers
    # ----------------------------------------------------------------

    def _pre_reveal_v6_fair(self, prior_mean: float) -> Optional[float]:
        if not self.pre_reveal_mm_enabled:
            return None
        vwap = self._trade_vwap(window=self.trade_window_sec)
        if vwap is None:
            return None
        if self._vwap_den < self.min_trade_vol_pre_reveal:
            return None
        w = self.pre_reveal_vwap_weight
        return w * vwap + (1.0 - w) * prior_mean

    def _vwap_clamped_fair(self, fair: float) -> float:
        k = len(self.posterior.reveals)
        if k < 1 or k > self.vwap_clamp_max_k:
            return fair
        if self._vwap_den < self.vwap_clamp_min_volume:
            return fair
        vwap = self._trade_vwap(window=self.trade_window_sec)
        if vwap is None:
            return fair
        w = (self.vwap_clamp_max_k - k + 1) / (self.vwap_clamp_max_k + 1)
        return w * vwap + (1.0 - w) * fair

    def _vwap_for_side_guard(self) -> Optional[float]:
        if self._vwap_den < self.vwap_clamp_min_volume:
            return None
        return self._trade_vwap(window=self.trade_window_sec)

    def _max_position_for_sigma(self, sigma: float, *,
                                 relaxed: bool = False) -> int:
        if relaxed:
            target = self.max_pos_target_sigma_normal
            floor = self.max_pos_floor_normal
        else:
            target = self.max_pos_target_sigma
            floor = self.max_pos_floor
        if sigma <= target:
            return self.position_limit
        cap = int(self.position_limit * target / sigma)
        return max(floor, cap)

    def _refresh_book_cache_if_stale(self) -> None:
        if (self._book_cache is not None and
                time.time() - self._book_cache_t <= self._book_cache_max_age_sec):
            return
        try:
            self._book_cache = self.c.book(
                self.symbol, depth=self.snipe_book_depth)
            self._book_cache_t = time.time()
        except Exception:
            pass

    def _other_makers_present(self) -> bool:
        self._refresh_book_cache_if_stale()
        book = self._book_cache
        if book is None:
            return False
        if (time.time() - self._book_cache_t > self._book_cache_max_age_sec):
            return False

        def side_has_other(levels, our_px, our_qty):
            for lvl in levels:
                lvl_px = lvl.get("price")
                if lvl_px != our_px:
                    return True
                if our_qty is None:
                    return True
                if lvl.get("qty", 0) > our_qty:
                    return True
            return False

        our_bid = self.resting.get("bid")
        our_ask = self.resting.get("ask")
        our_bid_px = our_bid["price"] if our_bid else None
        our_bid_qty = our_bid["qty"] if our_bid else None
        our_ask_px = our_ask["price"] if our_ask else None
        our_ask_qty = our_ask["qty"] if our_ask else None

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        return (side_has_other(bids, our_bid_px, our_bid_qty) or
                side_has_other(asks, our_ask_px, our_ask_qty))

    def _recent_private_price(self) -> Optional[float]:
        if self._last_fill_t == 0.0:
            return None
        if (time.time() - self._last_fill_t > self._private_signal_max_age_sec):
            return None
        return self._last_fill_px

    def _effective_fair(self, fair: float) -> float:
        if (self._informed_fair_lo is not None
                and fair < self._informed_fair_lo):
            return self._informed_fair_lo
        if (self._informed_fair_hi is not None
                and fair > self._informed_fair_hi):
            return self._informed_fair_hi
        return fair

    def _handle_informed_fill(self, msg: dict) -> None:
        if not self.informed_trader_id:
            return
        counterparty = msg.get("counterparty")
        if counterparty != self.informed_trader_id:
            return
        our_side = msg.get("side")
        price = msg.get("price")
        qty = msg.get("qty", 0)
        if our_side not in ("buy", "sell") or price is None:
            return
        now = time.time()
        self._informed_last_t = now
        self._informed_last_px = float(price)
        self._informed_total_qty += int(qty)
        self._informed_fill_count += 1
        if our_side == "buy":
            self._informed_last_their_side = "sell"
            if (self._informed_fair_hi is None
                    or price < self._informed_fair_hi):
                self._informed_fair_hi = float(price)
            self._informed_cooloff_until["bid"] = now + self.informed_cooloff_sec
            try:
                self._safe_cancel("bid")
            except Exception:
                pass
            print(f"INFORMED  {counterparty} SOLD to us @ {price} "
                  f"(we bought {qty}). truth<={price}. "
                  f"cancel bid; cooloff {self.informed_cooloff_sec:.0f}s.")
        else:
            self._informed_last_their_side = "buy"
            if (self._informed_fair_lo is None
                    or price > self._informed_fair_lo):
                self._informed_fair_lo = float(price)
            self._informed_cooloff_until["ask"] = now + self.informed_cooloff_sec
            try:
                self._safe_cancel("ask")
            except Exception:
                pass
            print(f"INFORMED  {counterparty} BOUGHT from us @ {price} "
                  f"(we sold {qty}). truth>={price}. "
                  f"cancel ask; cooloff {self.informed_cooloff_sec:.0f}s.")

    def _market_mid_from_book(self) -> Optional[float]:
        book = self._book_cache
        if book is None:
            return None
        if time.time() - self._book_cache_t > self._book_cache_max_age_sec:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return (bids[0]["price"] + asks[0]["price"]) / 2.0

    def _expire_trades(self, now: float) -> None:
        cutoff = now - self.trade_window_sec
        tape = self._recent_trades
        while tape and tape[0][0] < cutoff:
            _, px_old, q_old, agg_old = tape.popleft()
            self._vwap_num -= px_old * q_old
            self._vwap_den -= q_old
            if agg_old == "buy":
                self._imb_buy_q -= q_old
            elif agg_old == "sell":
                self._imb_sell_q -= q_old

    def _trade_vwap(self, window: float = 30.0) -> Optional[float]:
        if window <= 0:
            return None
        now = time.time()
        with self.lock:
            self._expire_trades(now)
            if window >= self.trade_window_sec:
                if self._vwap_den <= 0:
                    return None
                return self._vwap_num / self._vwap_den
            cutoff = now - window
            num = 0.0
            den = 0
            for t, px, q, _ in self._recent_trades:
                if t < cutoff:
                    continue
                num += px * q
                den += q
            if den <= 0:
                return None
            return num / den

    def _aggressor_imbalance(self, window: float = 30.0) -> Optional[float]:
        if window <= 0:
            return None
        now = time.time()
        with self.lock:
            self._expire_trades(now)
            if window >= self.trade_window_sec:
                total = self._imb_buy_q + self._imb_sell_q
                if total <= 0:
                    return None
                return (self._imb_buy_q - self._imb_sell_q) / total
            cutoff = now - window
            buy_q = 0
            sell_q = 0
            for t, _, q, agg in self._recent_trades:
                if t < cutoff:
                    continue
                if agg == "buy":
                    buy_q += q
                elif agg == "sell":
                    sell_q += q
            total = buy_q + sell_q
            if total <= 0:
                return None
            return (buy_q - sell_q) / total

    def _market_signal(self) -> Optional[float]:
        book_mid = self._market_mid_from_book()
        vwap = self._trade_vwap(window=self.trade_window_sec)
        if book_mid is not None and vwap is not None:
            return 0.5 * book_mid + 0.5 * vwap
        if vwap is not None:
            return vwap
        return book_mid

    def reconcile_position(self) -> None:
        try:
            self.position = int(self.c.positions()["positions"].get(self.symbol, 0))
        except Exception as e:
            print(f"reconcile_position failed: {e}")

    # ----------------------------------------------------------------
    # v8: reveal-timing helpers
    # ----------------------------------------------------------------

    def _next_reveal_at(self) -> Optional[float]:
        if self._last_reveal_t is None:
            return None
        nxt = self._last_reveal_t + self.reveal_interval
        now = time.time()
        while nxt < now - 0.5:
            nxt += self.reveal_interval
        return nxt

    def _in_pre_reveal_window(self) -> bool:
        if not self.pre_reveal_post_enabled:
            return False
        nxt = self._next_reveal_at()
        if nxt is None:
            return False
        if self._n_remaining() <= 0:
            return False
        now = time.time()
        return (nxt - self.pre_reveal_post_sec) <= now < (nxt - self.pre_reveal_cancel_sec)

    def _in_quote_blackout(self) -> bool:
        """True if we are inside the pre-reveal quote-blackout window: any
        new MM quote posted now would still be on the book at T+0 and
        become hit-bait for competitor IOCs racing the reveal.
        """
        nxt = self._next_reveal_at()
        if nxt is None:
            return False
        if self._n_remaining() <= 0:
            return False
        return time.time() >= (nxt - self.pre_reveal_quote_blackout_sec)

    def _tokens_reserved_for_reveal(self) -> bool:
        """True if we are inside the pre-reveal window AND the REST token
        bucket is below the reserve threshold. In that state, non-essential
        REST work should yield so the parallel IOC burst at T+0 has tokens.
        """
        if not self._in_quote_blackout():
            return False
        try:
            tokens = self.c.tokens_available()
        except Exception:
            return False
        return tokens < self.pre_reveal_token_reserve

    # ----------------------------------------------------------------
    # v9: reveal-math guard + danger zone + drift cancel
    # ----------------------------------------------------------------

    def _in_reveal_danger_zone(self) -> bool:
        if not self.reveal_danger_zone_enabled:
            return False
        if self._n_remaining() <= 0:
            return False
        now = time.time()
        zone = self.reveal_danger_zone_sec
        if (self._last_reveal_t is not None
                and now <= self._last_reveal_t + zone):
            return True
        nxt = self._next_reveal_at()
        if nxt is not None and now >= nxt - zone:
            return True
        return False

    def _reveal_math_fair(self) -> Optional[float]:
        reveals = self.posterior.reveals
        k = len(reveals)
        if k < self.reveal_math_min_k:
            return None
        remaining = self._n_remaining()
        if remaining <= 0:
            return sum(reveals)
        return sum(reveals) + remaining * statistics.fmean(reveals)

    def _reveal_math_guard_width(self) -> float:
        k = len(self.posterior.reveals)
        if k == 1:
            return self.reveal_math_guard_ticks * self.reveal_math_k1_widening
        return self.reveal_math_guard_ticks

    def _reveal_math_blocks_buy(self, fair_eff: float) -> bool:
        if not self.reveal_math_guard_enabled:
            return False
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return fair_eff - rm > self._reveal_math_guard_width()

    def _reveal_math_blocks_sell(self, fair_eff: float) -> bool:
        if not self.reveal_math_guard_enabled:
            return False
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return rm - fair_eff > self._reveal_math_guard_width()

    def _maybe_drift_cancel(self) -> None:
        if not self.drift_cancel_enabled:
            return
        if self.phase != "running":
            return
        if self._last_quote_fair_eff is None:
            return
        now = time.time()
        if now - self._last_drift_cancel_t < self.drift_cancel_min_interval:
            return
        try:
            fair, _ = self.fair_and_sigma()
        except Exception:
            return
        fair_eff = self._effective_fair(fair)
        fair_eff = self._vwap_clamped_fair(fair_eff)
        if abs(fair_eff - self._last_quote_fair_eff) <= self.drift_cancel_ticks:
            return
        print(f"  v9 DRIFT-CANCEL: fair_eff {self._last_quote_fair_eff:.1f}"
              f" -> {fair_eff:.1f} (move "
              f"{fair_eff - self._last_quote_fair_eff:+.1f})")
        self._last_drift_cancel_t = now
        try:
            with self.lock:
                self.c.cancel_all()
                self.resting = {"bid": None, "ask": None}
            self._last_quote_fair_eff = None
            self.step()
        except Exception as e:
            print(f"  v9 drift-cancel failed: {e}")

    # ----------------------------------------------------------------
    # v10: adverse-fill skew (between-reveal MM)
    # ----------------------------------------------------------------

    def _adverse_skew(self, side: str, now: float) -> float:
        last_t = self._last_adverse_fill_t.get(side, 0.0)
        if last_t <= 0.0:
            return 0.0
        age = now - last_t
        if age >= self.adverse_fill_decay_sec:
            return 0.0
        return self.adverse_fill_skew_ticks * (
            1.0 - age / self.adverse_fill_decay_sec)

    # ----------------------------------------------------------------
    # desired_quotes (v7 + v9 opening clamp + v10 no-edge pull + adverse skew)
    # ----------------------------------------------------------------

    def desired_quotes(self) -> tuple[Optional[int], Optional[int], float, float]:
        fair, sigma = self.fair_and_sigma()
        fair_eff = self._effective_fair(fair)
        fair_eff = self._vwap_clamped_fair(fair_eff)
        pre_reveal_v6_active = False
        if len(self.posterior.reveals) == 0:
            v6_fair = self._pre_reveal_v6_fair(prior_mean=fair)
            if v6_fair is None:
                return None, None, fair, sigma
            fair_eff = v6_fair
            pre_reveal_v6_active = True
        if (not pre_reveal_v6_active and
                len(self.posterior.reveals) < self.min_reveals_to_quote):
            return None, None, fair, sigma
        if self._n_remaining() == 0 and not self.quote_after_final_reveal:
            return None, None, fair, sigma

        if not self._other_makers_present():
            return None, None, fair, sigma

        pre_reveal = len(self.posterior.reveals) == 0

        if pre_reveal and self._phase_running_t is not None:
            elapsed = time.time() - self._phase_running_t
            if elapsed < self.pre_reveal_warmup_sec:
                return None, None, fair, sigma

        pre_bids: list = []
        pre_asks: list = []
        if pre_reveal:
            book = self._book_cache
            book_stale = (book is None or
                          time.time() - self._book_cache_t
                          > self._book_cache_max_age_sec)
            if book_stale:
                return None, None, fair, sigma
            pre_bids = book.get("bids") or []
            pre_asks = book.get("asks") or []
            if not pre_bids and not pre_asks:
                return None, None, fair, sigma

        edge = max(self.min_edge, self.edge_per_sigma * sigma)
        if pre_reveal:
            edge = max(edge, self.pre_reveal_min_edge)

        # v10: no-edge pull — variance at zero edge is strictly negative EV.
        # Don't apply at K=0 (pre-reveal min_edge already widens us).
        if not pre_reveal and edge < self.no_edge_pull_ticks:
            return None, None, fair, sigma

        if pre_reveal and self.pre_reveal_skew_disabled:
            skew = 0.0
        else:
            skew = -self.position * self.skew_per_unit

        extra_bid, extra_ask = self._informed_extra_edge()

        # v10: adverse-fill skew. Widens the side that was recently hit.
        now = time.time()
        bid_adv = self._adverse_skew("bid", now)
        ask_adv = self._adverse_skew("ask", now)

        bid_px = int(math.floor(fair_eff - edge - extra_bid + skew - bid_adv))
        ask_px = int(math.ceil(fair_eff + edge + extra_ask + skew + ask_adv))

        if pre_reveal:
            if not pre_asks:
                ask_px = int(math.ceil(fair_eff + self.empty_side_edge + skew))
                if (pre_bids and
                        pre_bids[0]["price"] >
                        fair_eff + self.pre_reveal_pull_disagreement):
                    ask_px = None
            elif not pre_bids:
                bid_px = int(math.floor(fair_eff - self.empty_side_edge + skew))
                if (pre_asks and
                        pre_asks[0]["price"] <
                        fair_eff - self.pre_reveal_pull_disagreement):
                    bid_px = None

        market_mid = self._market_signal()
        biased = (market_mid is not None and
                  abs(fair_eff - market_mid) >= self.market_anchor_disagreement_min)

        if (self.tight_quoting_enabled and sigma <= self.tight_penny_sigma_max
                and not biased
                and len(self.posterior.reveals) >= self.tight_penny_min_reveals):
            bid_px, ask_px = self._apply_penny(bid_px, ask_px, fair_eff, skew)

        if biased:
            safe_bid = int(math.floor(market_mid - edge))
            safe_ask = int(math.ceil(market_mid + edge))
            bid_px = min(bid_px, safe_bid)
            ask_px = max(ask_px, safe_ask)

        if pre_reveal and market_mid is not None:
            if market_mid > fair_eff + self.pre_reveal_pull_disagreement:
                ask_px = None
            elif market_mid < fair_eff - self.pre_reveal_pull_disagreement:
                bid_px = None

        if self.vwap_side_guard_ticks > 0:
            guard_vwap = self._vwap_for_side_guard()
            if guard_vwap is not None:
                g = self.vwap_side_guard_ticks * self.tick
                bid_ceiling = int(math.floor(guard_vwap)) - g
                ask_floor = int(math.ceil(guard_vwap)) + g
                if bid_px is not None and bid_px > bid_ceiling:
                    bid_px = bid_ceiling
                if ask_px is not None and ask_px < ask_floor:
                    ask_px = ask_floor

        # v9: opening-round clamp (K=0 only).
        if (self.opening_clamp_enabled and len(self.posterior.reveals) == 0):
            clamp = self.opening_clamp_ticks
            bid_ceiling = int(math.floor(self._prior_E_settle)) + clamp
            ask_floor = int(math.ceil(self._prior_E_settle)) - clamp
            if bid_px is not None and bid_px > bid_ceiling:
                print(f"  v9 OPENING-CLAMP: bid {bid_px} -> {bid_ceiling} "
                      f"(prior_E_settle={self._prior_E_settle:.1f})")
                bid_px = bid_ceiling
            if ask_px is not None and ask_px < ask_floor:
                print(f"  v9 OPENING-CLAMP: ask {ask_px} -> {ask_floor} "
                      f"(prior_E_settle={self._prior_E_settle:.1f})")
                ask_px = ask_floor

        if (ask_px is not None and bid_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + self.tick

        bid_qty = self._current_quote_qty("bid")
        ask_qty = self._current_quote_qty("ask")
        relaxed = (market_mid is not None and not biased)
        if pre_reveal_v6_active:
            max_pos = self.pre_reveal_max_pos
        else:
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        if bid_px is not None and self.position + bid_qty > max_pos:
            bid_px = None
        if ask_px is not None and self.position - ask_qty < -max_pos:
            ask_px = None

        kill_thresh = int(max_pos * self.inv_hard_kill_frac)
        if self.position > kill_thresh and bid_px is not None:
            bid_px = None
        if self.position < -kill_thresh and ask_px is not None:
            ask_px = None

        if (bid_px is not None and
                now < self._informed_cooloff_until.get("bid", 0.0)):
            bid_px = None
        if (ask_px is not None and
                now < self._informed_cooloff_until.get("ask", 0.0)):
            ask_px = None

        if (bid_px is not None and
                now < self._partial_fill_cool_off_until.get("bid", 0.0)):
            bid_px = None
        if (ask_px is not None and
                now < self._partial_fill_cool_off_until.get("ask", 0.0)):
            ask_px = None

        # No-cross guard against the cached inside book.
        book = self._book_cache
        if (book is not None
                and time.time() - self._book_cache_t
                <= self._book_cache_max_age_sec):
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if ask_px is not None and bids:
                best_bid = bids[0].get("price")
                if best_bid is not None and ask_px <= best_bid:
                    ask_px = None
            if bid_px is not None and asks:
                best_ask = asks[0].get("price")
                if best_ask is not None and bid_px >= best_ask:
                    bid_px = None

        # Drift anchor for v9 drift cancel.
        if bid_px is not None or ask_px is not None:
            self._last_quote_fair_eff = fair_eff

        return bid_px, ask_px, fair, sigma

    def _apply_penny(self, bid_px: int, ask_px: int, fair: float,
                     skew: float) -> tuple[int, int]:
        book = self._book_cache
        if book is None:
            return bid_px, ask_px
        if time.time() - self._book_cache_t > self._book_cache_max_age_sec:
            return bid_px, ask_px

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        step = self.tight_step_inside_ticks * self.tick

        if bids:
            market_best_bid = bids[0]["price"]
            our_bid = self.resting.get("bid")
            already_top = our_bid is not None and our_bid["price"] >= market_best_bid
            if not already_top:
                penny_bid = market_best_bid + step
                max_safe_bid = int(math.floor(fair + skew - self.tight_floor_edge))
                if penny_bid > bid_px and penny_bid <= max_safe_bid:
                    bid_px = penny_bid
                elif market_best_bid > bid_px and market_best_bid <= max_safe_bid:
                    bid_px = market_best_bid

        if asks:
            market_best_ask = asks[0]["price"]
            our_ask = self.resting.get("ask")
            already_top = our_ask is not None and our_ask["price"] <= market_best_ask
            if not already_top:
                penny_ask = market_best_ask - step
                min_safe_ask = int(math.ceil(fair + skew + self.tight_floor_edge))
                if penny_ask < ask_px and penny_ask >= min_safe_ask:
                    ask_px = penny_ask
                elif market_best_ask < ask_px and market_best_ask >= min_safe_ask:
                    ask_px = market_best_ask

        return bid_px, ask_px

    def _apply_target_quotes(self, bid_px: Optional[int],
                              ask_px: Optional[int]) -> None:
        # Pre-reveal blackout: new quotes posted in the last
        # pre_reveal_quote_blackout_sec become hit-bait for competitor IOCs
        # at T+0. Cancels of EXISTING quotes still flow through (we want
        # to be flat at reveal); only NEW posts/reprices are suppressed.
        in_blackout = self._in_quote_blackout()
        if in_blackout:
            self._safe_cancel("bid")
            self._safe_cancel("ask")
            return
        cur_ask = self.resting["ask"]["price"] if self.resting["ask"] else None
        cur_bid = self.resting["bid"]["price"] if self.resting["bid"] else None
        bid_would_cross = (bid_px is not None and cur_ask is not None
                           and bid_px >= cur_ask)
        ask_would_cross = (ask_px is not None and cur_bid is not None
                           and ask_px <= cur_bid)
        if bid_would_cross:
            self._safe_cancel("ask")
            cur_ask = None
        if ask_would_cross:
            self._safe_cancel("bid")
            cur_bid = None
        if bid_px is None:
            self._safe_cancel("bid")
        else:
            self._reprice("bid", bid_px)
        if ask_px is None:
            self._safe_cancel("ask")
        else:
            self._reprice("ask", ask_px)

    # ----------------------------------------------------------------
    # Order plumbing
    # ----------------------------------------------------------------

    def _record_fill_from_trades(self, side: str, trades: list[dict]) -> int:
        filled = sum(t["qty"] for t in trades)
        if filled:
            self.position += filled if side == "buy" else -filled
            last_px = None
            for t in trades:
                if t.get("qty"):
                    last_px = t.get("price")
            if last_px is not None:
                self._last_fill_px = float(last_px)
                self._last_fill_t = time.time()
        return filled

    def _safe_cancel(self, side: str) -> None:
        rest = self.resting[side]
        if rest is None:
            return
        try:
            self.c.cancel(rest["order_id"])
        except Exception:
            pass
        self.resting[side] = None

    def _current_quote_qty(self, side: Optional[str] = None) -> int:
        """Active quote size with v7 sigma/relax tiers + v8 pre-reveal-window upsize."""
        if len(self.posterior.reveals) == 0:
            return self.pre_reveal_quote_qty
        fair, sigma = self.fair_and_sigma()
        market_mid = self._market_signal()
        relaxed = (market_mid is not None and
                   abs(fair - market_mid) <
                   self.market_anchor_disagreement_min)
        if sigma > self.full_qty_sigma_max:
            base = (self.mid_round_quote_qty_normal
                    if relaxed else self.mid_round_quote_qty)
        else:
            base = self.quote_qty_normal if relaxed else self.quote_qty

        # v8: pre-reveal-window upsize when consensus confirms.
        if (self.pre_reveal_post_enabled and self._in_pre_reveal_window()
                and market_mid is not None
                and abs(fair - market_mid) < self.market_anchor_disagreement_min):
            base = max(base, self.pre_reveal_post_qty)

        if side is not None:
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
            threshold = int(max_pos * self.inv_dampen_threshold_frac)
            adding = (side == "bid" and self.position > threshold) or \
                     (side == "ask" and self.position < -threshold)
            if adding:
                base = max(1, int(round(base * self.inv_dampen_factor)))
        return base

    def _post(self, side: str, price: int) -> None:
        method = self.c.buy if side == "bid" else self.c.sell
        sgn_side = "buy" if side == "bid" else "sell"
        qty = self._current_quote_qty(side)
        try:
            r = method(self.symbol, price=price, qty=qty)
        except Exception as e:
            self.resting[side] = None
            print(f"  post {side} @ {price} failed: {e}")
            return
        o = r["order"]
        self._record_fill_from_trades(sgn_side, r.get("trades", []))
        if o["status"] in ("open", "partial"):
            self.resting[side] = {
                "order_id": o["order_id"],
                "price": o["price"],
                "qty": o["remaining"],
            }
        else:
            self.resting[side] = None

    def _reprice(self, side: str, want_px: int) -> None:
        rest = self.resting[side]
        if rest is None:
            self._post(side, want_px)
            return
        qty = self._current_quote_qty(side)
        if rest["price"] == want_px and rest["qty"] <= qty:
            return

        order_id = rest["order_id"]
        now = time.time()
        last = self._last_modify_t.get(order_id, 0.0)
        if now - last < self.min_modify_interval_sec:
            return

        try:
            tokens = self.c.tokens_available()
        except AttributeError:
            tokens = self.tokens_low_threshold + 1.0
        if tokens < self.tokens_low_threshold:
            return

        sgn_side = "buy" if side == "bid" else "sell"
        try:
            res = self.c.modify(order_id, price=want_px, qty=qty)
        except Exception:
            self._safe_cancel(side)
            self._post(side, want_px)
            return
        o = res["order"]
        self._record_fill_from_trades(sgn_side, res.get("trades", []))
        if o["status"] in ("open", "partial"):
            new_id = o["order_id"]
            self.resting[side] = {
                "order_id": new_id,
                "price": o["price"],
                "qty": o["remaining"],
            }
            self._last_modify_t[new_id] = now
            if new_id != order_id:
                self._last_modify_t.pop(order_id, None)
        else:
            self.resting[side] = None
            self._last_modify_t.pop(order_id, None)

    # ----------------------------------------------------------------
    # Sniping (with v8 reveal-math guard + v9 danger zone)
    # ----------------------------------------------------------------

    def maybe_snipe(self, fair: float, sigma: float) -> bool:
        if self._in_reveal_danger_zone():
            now = time.time()
            nxt = self._next_reveal_at()
            rel_nxt = (f"{nxt - now:+.2f}s" if nxt is not None else "-")
            rel_last = (f"{now - self._last_reveal_t:+.2f}s"
                        if self._last_reveal_t is not None else "-")
            print(f"  v9 SNIPE-SKIP: in reveal danger zone "
                  f"(next={rel_nxt} last={rel_last})")
            return False
        if self.reveal_math_guard_enabled:
            fair_eff = self._effective_fair(fair)
            fair_eff = self._vwap_clamped_fair(fair_eff)
            block_buy = self._reveal_math_blocks_buy(fair_eff)
            block_sell = self._reveal_math_blocks_sell(fair_eff)
            if block_buy and block_sell:
                return False
            if block_buy or block_sell:
                rm = self._reveal_math_fair()
                print(f"  v8 reveal-math gate: fair_eff={fair_eff:.1f} "
                      f"reveal_math={rm:.1f} block_buy={block_buy} "
                      f"block_sell={block_sell} -> skip snipe")
                return False

        if len(self.posterior.reveals) < self.min_reveals_to_snipe:
            return False
        if self._n_remaining() == 0 and not self.snipe_after_final_reveal:
            return False
        if (self._n_remaining() > 0 and
                self._snipe_count_this_round >= self.max_snipes_per_round):
            return False
        try:
            book = self.c.book(self.symbol, depth=self.snipe_book_depth)
            self._book_cache = book
            self._book_cache_t = time.time()
        except Exception:
            return False

        fair_eff = self._effective_fair(fair)
        fair_eff = self._vwap_clamped_fair(fair_eff)
        snipe_vwap = self._vwap_for_side_guard()
        market_mid = self._market_signal()
        block_buy = False
        block_sell = False
        if self._n_remaining() > 0 and market_mid is not None:
            diff = fair_eff - market_mid
            if abs(diff) > self.snipe_max_disagreement:
                if self.snipe_disagreement_directional:
                    if diff < 0:
                        block_buy = True
                    else:
                        block_sell = True
                else:
                    return False
        if block_buy and block_sell:
            return False

        our_ask_px = self.resting["ask"]["price"] if self.resting["ask"] else None
        our_bid_px = self.resting["bid"]["price"] if self.resting["bid"] else None

        if self._n_remaining() == 0:
            edge_required_buy = self.taker_fee
            edge_required_sell = self.taker_fee
        else:
            base_edge = self.taker_fee + max(
                self.snipe_min_edge, self.snipe_buffer_sigma * sigma)
            max_pos_for_threshold = self._max_position_for_sigma(
                sigma, relaxed=(market_mid is not None and
                                abs(fair_eff - market_mid) <
                                self.market_anchor_disagreement_min))
            threshold = int(max_pos_for_threshold * self.inv_dampen_threshold_frac)
            edge_required_buy = base_edge + (
                self.snipe_adverse_extra_edge if self.position > threshold else 0.0)
            edge_required_sell = base_edge + (
                self.snipe_adverse_extra_edge if self.position < -threshold else 0.0)
        snipe_cap = self.snipe_max_qty_per_level
        relaxed = (market_mid is not None and
                   abs(fair_eff - market_mid) <
                   self.market_anchor_disagreement_min)
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        snipe_max_pos = min(max_pos,
                            self.position_limit - self.snipe_position_buffer)
        took_any = False

        if not block_buy:
            for level in book.get("asks") or []:
                if our_ask_px is not None and level["price"] == our_ask_px:
                    continue
                mispricing = fair_eff - level["price"]
                if mispricing <= edge_required_buy:
                    break
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap < level["price"] - self.snipe_vwap_buffer):
                    break
                headroom = snipe_max_pos - self.position
                if headroom <= 0:
                    break
                qty = min(level["qty"], headroom, snipe_cap)
                if qty <= 0:
                    break
                try:
                    res = self.c.buy_ioc(self.symbol, price=level["price"], qty=qty)
                except Exception:
                    break
                filled = self._record_fill_from_trades("buy", res.get("trades", []))
                if filled:
                    took_any = True
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  SNIPE buy  {filled} @ {level['price']}  "
                          f"fair={fair_eff:.1f}  edge={mispricing:.1f}")
                else:
                    break

        if not block_sell:
            for level in book.get("bids") or []:
                if our_bid_px is not None and level["price"] == our_bid_px:
                    continue
                mispricing = level["price"] - fair_eff
                if mispricing <= edge_required_sell:
                    break
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap > level["price"] + self.snipe_vwap_buffer):
                    break
                headroom = snipe_max_pos + self.position
                if headroom <= 0:
                    break
                qty = min(level["qty"], headroom, snipe_cap)
                if qty <= 0:
                    break
                try:
                    res = self.c.sell_ioc(self.symbol, price=level["price"], qty=qty)
                except Exception:
                    break
                filled = self._record_fill_from_trades("sell", res.get("trades", []))
                if filled:
                    took_any = True
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  SNIPE sell {filled} @ {level['price']}  "
                          f"fair={fair_eff:.1f}  edge={mispricing:.1f}")
                else:
                    break

        return took_any

    def _try_direct_snipe(self, msg: dict) -> bool:
        if self._in_reveal_danger_zone():
            return False
        side = msg.get("side")
        price = msg.get("price")
        qty_avail = msg.get("qty")
        if side not in ("buy", "sell") or price is None or not qty_avail:
            return False

        with self.lock:
            if self.phase != "running":
                return False
            if len(self.posterior.reveals) < self.min_reveals_to_snipe:
                return False
            if self._n_remaining() == 0 and not self.snipe_after_final_reveal:
                return False
            if (self._n_remaining() > 0 and
                    self._snipe_count_this_round >= self.max_snipes_per_round):
                return False

            our_rest = self.resting.get("bid" if side == "buy" else "ask")
            if our_rest is not None and our_rest["price"] == price:
                return False

            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return False

            fair_eff = self._effective_fair(fair)
            fair_eff = self._vwap_clamped_fair(fair_eff)
            snipe_vwap = self._vwap_for_side_guard()

            # v8: reveal-math directional guard (per-side).
            if self.reveal_math_guard_enabled:
                if side == "buy" and self._reveal_math_blocks_sell(fair_eff):
                    return False
                if side == "sell" and self._reveal_math_blocks_buy(fair_eff):
                    return False

            market_mid = self._market_signal()
            if self._n_remaining() > 0:
                if market_mid is None:
                    return False
                diff = fair_eff - market_mid
                if abs(diff) > self.snipe_max_disagreement:
                    if not self.snipe_disagreement_directional:
                        return False
                    if diff < 0 and side == "sell":
                        return False
                    if diff > 0 and side == "buy":
                        return False

            snipe_cap = self.snipe_max_qty_per_level
            relaxed = (market_mid is not None and
                       abs(fair_eff - market_mid) <
                       self.market_anchor_disagreement_min)
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
            snipe_max_pos = min(
                max_pos,
                self.position_limit - self.snipe_position_buffer)
            threshold = int(max_pos * self.inv_dampen_threshold_frac)

            if self._n_remaining() == 0:
                edge_required_buy = self.taker_fee
                edge_required_sell = self.taker_fee
            else:
                base_edge = (self.taker_fee +
                             max(self.snipe_min_edge,
                                 self.snipe_buffer_sigma * sigma))
                edge_required_buy = base_edge + (
                    self.snipe_adverse_extra_edge
                    if self.position > threshold else 0.0)
                edge_required_sell = base_edge + (
                    self.snipe_adverse_extra_edge
                    if self.position < -threshold else 0.0)

            if side == "buy":
                mispricing = price - fair_eff
                if mispricing <= edge_required_sell:
                    return False
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap > price + self.snipe_vwap_buffer):
                    return False
                headroom = snipe_max_pos + self.position
                if headroom <= 0:
                    return False
                qty = min(qty_avail, headroom, snipe_cap)
                if qty <= 0:
                    return False
                try:
                    res = self.c.sell_ioc(self.symbol, price=price, qty=qty)
                except Exception:
                    return False
                filled = self._record_fill_from_trades("sell",
                                                      res.get("trades", []))
                if filled:
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  DIRECT-SNIPE sell {filled} @ {price}  "
                          f"fair={fair_eff:.1f}  edge={mispricing:.1f}")
                return bool(filled)
            else:
                mispricing = fair_eff - price
                if mispricing <= edge_required_buy:
                    return False
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap < price - self.snipe_vwap_buffer):
                    return False
                headroom = snipe_max_pos - self.position
                if headroom <= 0:
                    return False
                qty = min(qty_avail, headroom, snipe_cap)
                if qty <= 0:
                    return False
                try:
                    res = self.c.buy_ioc(self.symbol, price=price, qty=qty)
                except Exception:
                    return False
                filled = self._record_fill_from_trades("buy",
                                                      res.get("trades", []))
                if filled:
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  DIRECT-SNIPE buy  {filled} @ {price}  "
                          f"fair={fair_eff:.1f}  edge={mispricing:.1f}")
                return bool(filled)

    # ----------------------------------------------------------------
    # Top-level step
    # ----------------------------------------------------------------

    def step(self, *, reconcile: bool = False) -> None:
        with self.lock:
            phase = self.phase
            if phase != "running":
                if phase == "settled":
                    self.resting = {"bid": None, "ask": None}
                return

            # Quote blackout: any new posts here would just be no-op'd by
            # _apply_target_quotes anyway. Skip the whole step (saves the
            # token bucket from being drained on now-useless reconcile/
            # desired_quotes/REST work right before the reveal IOC burst).
            if self._in_quote_blackout():
                return

            pre_reveal = (len(self.posterior.reveals) == 0)

            if reconcile:
                self.reconcile_position()

            fair, sigma = self.fair_and_sigma()
            if not pre_reveal:
                self.maybe_snipe(fair, sigma)

            bid_px, ask_px, fair, sigma = self.desired_quotes()
            self._apply_target_quotes(bid_px, ask_px)
            self._last_maker_apply_t = time.time()

            print(
                f"QUOTE  fv={fair:6.1f} +/-{sigma:4.1f}  pos={self.position:+4d}  "
                f"bid={bid_px}  ask={ask_px}  k={len(self.posterior.reveals)}/{self.n_total}"
            )

    # ----------------------------------------------------------------
    # FAST-PATH on_reveal (v10): dict lookup → atomic swap → post → IOC sweep
    # ----------------------------------------------------------------

    def on_reveal(self, value: float) -> None:
        """Race-optimized reveal handler. Order of operations is engineered to
        win the post-reveal trade race against other bots:

          1. Atomic local posterior swap (no I/O) — ~50us
          2. **FIRE IOCs FIRST** against the pre-warmed book using
             precomputed lift/hit bounds. This is the race-winning action.
          3. Then post the new MM quotes (less time-critical).
          4. cancel_all is INTENTIONALLY OMITTED here — the fast scheduler
             already cancels at T-pre_reveal_cancel_sec, and posting new
             quotes will replace any survivors via modify_replace.

        Total path target: <5ms wall + 1 IOC-RTT (~80ms minimum at this venue).
        """
        t_start = time.perf_counter()
        # Anchor the next-reveal predictor to the ACTUAL arrival time so jitter
        # and per-game drift don't accumulate across cycles.
        self._last_reveal_t = time.time()

        int_value = int(value)
        with self._precompute_lock:
            scenario = self._precomputed.get(int_value)
            cached_cycle = self._precompute_for_cycle
        current_cycle = len(self.posterior.reveals)

        # Stale cycle? Try an inline JIT rebuild before giving up on fast path.
        if cached_cycle != current_cycle:
            print(f"[FAST-MISS-STALE] value={int_value} "
                  f"cached_cycle={cached_cycle} current_cycle={current_cycle} "
                  f"-> inline JIT rebuild")
            t0 = time.perf_counter()
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[FAST-MISS-STALE] inline rebuild error: {e!r}")
            print(f"[FAST-MISS-STALE] inline rebuild took "
                  f"{(time.perf_counter()-t0)*1000:.1f}ms")
            with self._precompute_lock:
                scenario = self._precomputed.get(int_value)
                cached_cycle = self._precompute_for_cycle

        if scenario is None or cached_cycle != current_cycle:
            print(f"[FAST-MISS] value={int_value} cached_cycle={cached_cycle} "
                  f"current_cycle={current_cycle} "
                  f"have_scenario={scenario is not None} -> slow path")
            self._on_reveal_slow(value)
            self._precompute_request.set()
            self._precompute_prefetched_cycle = -1
            self._jit_refresh_done_for_cycle = -1
            return

        # ---- STAGE 1: local-only posterior swap (no network I/O) ----
        with self.lock:
            self.posterior.posterior = dict(scenario.posterior_snapshot)
            self.posterior.reveals.append(float(value))
            self._posterior_gen += 1
            self._fair_sigma_cache = None
            self._last_maker_apply_t = 0.0
            self._last_quote_fair_eff = None
            # Resting orders have already been cancelled at T-pre_reveal_cancel_sec
            # by the fast scheduler; assume they are gone.
            self.resting = {"bid": None, "ask": None}
            n_remaining_after = self.n_total - len(self.posterior.reveals)

        t_local_done = time.perf_counter()

        # ---- STAGE 2: FIRE IOCs FIRST (race-winning action) ----
        sweep_count = 0
        if n_remaining_after >= 0 and (
                scenario.lift_to_px is not None
                or scenario.hit_to_px is not None):
            # Hold the lock only briefly inside _fast_post_reveal_sweep so the
            # quote-event handler doesn't block while we hammer IOCs.
            with self.lock:
                sweep_count = self._fast_post_reveal_sweep(scenario)
        t_sweep_done = time.perf_counter()

        # ---- STAGE 3: post new MM quotes (only after race is won) ----
        bid_px, ask_px = scenario.bid_px, scenario.ask_px
        if bid_px is not None or ask_px is not None:
            with self.lock:
                self._apply_target_quotes(bid_px, ask_px)
                self._last_quote_fair_eff = scenario.fair
        t_quotes_done = time.perf_counter()

        elapsed_us = (t_quotes_done - t_start) * 1e6
        local_us = (t_local_done - t_start) * 1e6
        sweep_us = (t_sweep_done - t_local_done) * 1e6
        quote_us = (t_quotes_done - t_sweep_done) * 1e6
        print(f"[FAST #{int_value}] cycle={current_cycle+1}/{self.n_total} "
              f"fair={scenario.fair:.1f}+/-{scenario.sigma:.1f} "
              f"bid={bid_px} ask={ask_px} "
              f"lift_to={scenario.lift_to_px} hit_to={scenario.hit_to_px} "
              f"sweeps={sweep_count} "
              f"t_local={local_us:.0f}us "
              f"t_sweep={sweep_us:.0f}us "
              f"t_quote={quote_us:.0f}us "
              f"total={elapsed_us:.0f}us")

        self._precompute_request.set()
        self._precompute_prefetched_cycle = -1

        if n_remaining_after == 0 and self.snipe_after_final_reveal:
            running_sum = float(sum(self.posterior.reveals))
            self._sweep_at_settle(running_sum=running_sum)

    def _on_reveal_slow(self, value: float) -> None:
        """The v7 reveal handler — Bayesian update + reconcile step. Only fires
        when the precompute table doesn't have the revealed value cached.
        """
        with self.lock:
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.posterior.update(value)
            self._posterior_gen += 1
            self._last_maker_apply_t = 0.0
            k = len(self.posterior.reveals)
            running = sum(self.posterior.reveals)
            is_final = (k >= self.n_total)
            if is_final:
                print(f"FINAL  all {k}/{self.n_total} reveals in. "
                      f"settle={int(running)}  passive quotes -> cancel; sweeping book.")

        # v9 logging.
        now = time.time()
        nxt_predicted = self._next_reveal_at()
        actual_vs_predicted = None
        if nxt_predicted is not None:
            actual_vs_predicted = now - nxt_predicted
        print(f"[SCHED] REVEAL value={value} "
              f"cycle_was={len(self.posterior.reveals)-1} "
              f"actual_vs_predicted="
              f"{f'{actual_vs_predicted:+.2f}s' if actual_vs_predicted is not None else '-'} "
              f"last_cancelled_idx={self._pre_reveal_last_cancelled_idx} "
              f"last_serviced_idx={self._pre_reveal_last_serviced_idx}")

        self.step(reconcile=True)
        if is_final and self.snipe_after_final_reveal:
            self._sweep_at_settle(
                running_sum=float(sum(self.posterior.reveals)))

    def _fast_post_reveal_sweep(self, scenario: PrecomputedScenario) -> int:
        """Walk top of book and IOC any level inside the precomputed
        lift_to_px / hit_to_px bounds. Caller holds self.lock.

        PARALLEL FIRE: builds the full IOC plan upfront (capping cumulative
        qty by full headroom), then submits all IOCs to the parallel
        executor at once. 3 sequential IOCs at ~80ms each = 240ms wall;
        parallel = ~80ms wall.

        Uses the fast-loop pre-warmed `_book_cache` if it's <200ms old to
        avoid a synchronous REST round-trip in the hot path.
        """
        now = time.time()
        cache_age = now - self._book_cache_t
        book: Optional[dict] = None
        used_cache = False
        if (self._book_cache is not None
                and cache_age <= self.book_prewarm_max_age_sec):
            book = self._book_cache
            used_cache = True
        else:
            try:
                book = self.c.book(self.symbol, depth=self.snipe_book_depth)
                self._book_cache = book
                self._book_cache_t = time.time()
            except Exception:
                return 0
        if book is None:
            return 0
        max_pos = self._max_position_for_sigma(scenario.sigma, relaxed=True)
        snipe_max_pos = min(
            max_pos,
            self.position_limit - self.snipe_position_buffer)
        snipe_cap = self.snipe_max_qty_per_level

        # ----- Build plan: cap cumulative qty by full headroom so parallel
        # fires can't exceed position limit even if every IOC fully fills.
        buy_plans: List[Tuple[int, int]] = []
        if scenario.lift_to_px is not None:
            remaining = snipe_max_pos - self.position
            for lvl in book.get("asks") or []:
                if lvl["price"] > scenario.lift_to_px:
                    break
                if remaining <= 0:
                    break
                qty = min(lvl["qty"], remaining, snipe_cap)
                if qty <= 0:
                    break
                buy_plans.append((lvl["price"], qty))
                remaining -= qty

        sell_plans: List[Tuple[int, int]] = []
        if scenario.hit_to_px is not None:
            remaining = snipe_max_pos + self.position
            for lvl in book.get("bids") or []:
                if lvl["price"] < scenario.hit_to_px:
                    break
                if remaining <= 0:
                    break
                qty = min(lvl["qty"], remaining, snipe_cap)
                if qty <= 0:
                    break
                sell_plans.append((lvl["price"], qty))
                remaining -= qty

        n_planned = len(buy_plans) + len(sell_plans)
        if n_planned == 0:
            print(f"  [FAST-SWEEP] book_source="
                  f"{'cache' if used_cache else 'fresh'} "
                  f"age={cache_age*1000:.0f}ms sweeps=0 (no levels in range)")
            return 0

        # ----- Submit all IOCs in parallel. requests.Session is thread-safe.
        t_submit = time.perf_counter()
        futures: List[Tuple[str, int, int, "Future"]] = []
        for px, qty in buy_plans:
            fut = self._ioc_executor.submit(
                self.c.buy_ioc, self.symbol, price=px, qty=qty)
            futures.append(("buy", px, qty, fut))
        for px, qty in sell_plans:
            fut = self._ioc_executor.submit(
                self.c.sell_ioc, self.symbol, price=px, qty=qty)
            futures.append(("sell", px, qty, fut))

        # ----- Collect results.
        sweep_count = 0
        for side, px, qty, fut in futures:
            try:
                res = fut.result(timeout=2.0)
            except Exception as e:
                print(f"  [FAST-SWEEP] {side} @ {px} q={qty} failed: {e!r}")
                continue
            filled = self._record_fill_from_trades(side, res.get("trades", []))
            if filled:
                sweep_count += 1
                self._snipe_count_this_round += 1
                edge = (scenario.fair - px) if side == "buy" else (px - scenario.fair)
                print(f"  [FAST-SWEEP] {side} {filled} @ {px} "
                      f"fair={scenario.fair:.1f} edge={edge:+.1f}")
        wall_ms = (time.perf_counter() - t_submit) * 1000
        print(f"  [FAST-SWEEP] book_source="
              f"{'cache' if used_cache else 'fresh'} "
              f"age={cache_age*1000:.0f}ms planned={n_planned} "
              f"sweeps={sweep_count} parallel_wall={wall_ms:.0f}ms")
        return sweep_count

    def _sweep_at_settle(self, running_sum: float) -> None:
        for _ in range(6):
            with self.lock:
                took = self.maybe_snipe(running_sum, 0.0)
            if not took:
                return
            time.sleep(0.05)

    # ----------------------------------------------------------------
    # Other event handlers
    # ----------------------------------------------------------------

    def on_fill_event(self, msg: dict) -> None:
        # v10: adverse-fill skew tracking (very first thing -- no lock needed).
        liq = msg.get("liquidity")
        side = msg.get("side")
        if liq == "maker" and side in ("buy", "sell"):
            # side=="buy" means our BID was hit -> remember "bid"
            self._last_adverse_fill_t[
                "bid" if side == "buy" else "ask"] = time.time()

        matched = False
        with self.lock:
            side = msg["side"]
            qty = msg["qty"]
            order_id = msg["order_id"]
            fill_px = msg.get("price")
            if fill_px is not None:
                self._last_fill_px = float(fill_px)
                self._last_fill_t = time.time()
            cp_id = msg.get("counterparty")
            if cp_id:
                if side == "buy":
                    self._cp_net_flow[cp_id] -= qty
                elif side == "sell":
                    self._cp_net_flow[cp_id] += qty
            if msg.get("liquidity") == "maker":
                self._record_informed_fill(msg.get("counterparty"), side)
            self._handle_informed_fill(msg)
            for key, expected_side in (("bid", "buy"), ("ask", "sell")):
                rest = self.resting[key]
                if rest is not None and rest["order_id"] == order_id and side == expected_side:
                    rest["qty"] -= qty
                    if side == "buy":
                        self.position += qty
                    else:
                        self.position -= qty
                    if (rest["qty"] > 0
                            and msg.get("liquidity") == "maker"
                            and self.partial_fill_cool_off_sec > 0):
                        self._partial_fill_cool_off_until[key] = (
                            time.time() + self.partial_fill_cool_off_sec)
                    if rest["qty"] <= 0:
                        self.resting[key] = None
                    matched = True
                    break
        self.step(reconcile=not matched)

    def on_quote_event(self, msg: dict) -> None:
        if self.phase != "running":
            return
        k = len(self.posterior.reveals)
        pre_reveal = (k == 0)

        if not pre_reveal and msg.get("type") == "quote_add":
            self._try_direct_snipe(msg)

        now = time.time()
        do_book_scan = (not pre_reveal and
                        (now - self._last_quote_event_t >=
                         self.quote_event_throttle_sec))
        if pre_reveal:
            do_maker_reprice = (now - self._last_maker_apply_t >= 1.0)
        else:
            do_maker_reprice = (now - self._last_maker_apply_t >=
                                self._maker_reprice_interval())

        if not do_book_scan and not do_maker_reprice:
            # v9 drift-cancel still runs even when nothing else fires.
            self._maybe_drift_cancel()
            return

        if do_book_scan:
            self._last_quote_event_t = now

        with self.lock:
            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return
            if do_book_scan and k >= self.min_reveals_to_snipe:
                self.maybe_snipe(fair, sigma)
            if do_maker_reprice:
                if pre_reveal or k >= self.min_reveals_to_quote:
                    bid_px, ask_px, _, _ = self.desired_quotes()
                    self._apply_target_quotes(bid_px, ask_px)
                    self._last_maker_apply_t = now

        self._maybe_drift_cancel()

    def on_book_event(self, msg: dict) -> None:
        """Feed the WS book snapshot straight into the hot-path cache.

        Why: diagnose_latency runs (2026-05-19) showed ~3% of REST /book
        calls spike to 90-184 ms (vs ~13 ms p50), and reveal->first-book
        on the WS push is ~1 ms. Subscribing strategy to WS book pushes
        keeps _book_cache fresh asynchronously, so _fast_post_reveal_sweep
        never has to fall through to the synchronous REST path on a stale
        cache. The format matches REST /book (bids/asks arrays), so
        downstream consumers don't change.
        """
        if msg.get("symbol") not in (None, self.symbol):
            return
        self._book_cache = msg
        self._book_cache_t = time.time()

    def on_trade(self, msg: dict) -> None:
        """v6 trade tape + v9 drift-cancel hook. on_trade fires for every
        public trade — we only consume our own symbol."""
        if msg.get("symbol") != self.symbol:
            return
        try:
            price = float(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        if qty <= 0:
            return
        aggressor = msg.get("aggressor")
        if aggressor not in ("buy", "sell"):
            aggressor = ""
        now = time.time()
        with self.lock:
            self._expire_trades(now)
            if len(self._recent_trades) >= self._max_recent_trades:
                _, px_old, q_old, agg_old = self._recent_trades[0]
                self._vwap_num -= px_old * q_old
                self._vwap_den -= q_old
                if agg_old == "buy":
                    self._imb_buy_q -= q_old
                elif agg_old == "sell":
                    self._imb_sell_q -= q_old
            self._recent_trades.append((now, price, qty, aggressor))
            self._vwap_num += price * qty
            self._vwap_den += qty
            if aggressor == "buy":
                self._imb_buy_q += qty
            elif aggressor == "sell":
                self._imb_sell_q += qty

        self._maybe_drift_cancel()

    def on_phase_change(self, phase: Optional[str], reveals: list[float]) -> None:
        with self.lock:
            self.phase = phase
            if phase == "running":
                try:
                    self.c.cancel_all()
                    print("CANCEL_ALL on phase->running")
                except Exception as e:
                    print(f"cancel_all on phase change failed: {e}")
                self.posterior.reset(reveals)
                self._posterior_gen += 1
                self._fair_sigma_cache = None
                self.resting = {"bid": None, "ask": None}
                self._snipe_count_this_round = 0
                self._informed_strikes_bid = 0.0
                self._informed_strikes_ask = 0.0
                self._informed_strikes_last_t = time.time()
                self._last_maker_apply_t = 0.0
                self._informed_cooloff_until = {"bid": 0.0, "ask": 0.0}
                self._informed_last_t = 0.0
                self._informed_last_their_side = None
                self._informed_last_px = None
                self._informed_fair_lo = None
                self._informed_fair_hi = None
                self._informed_total_qty = 0
                self._informed_fill_count = 0
                self._partial_fill_cool_off_until = {"bid": 0.0, "ask": 0.0}
                self._recent_trades.clear()
                self._vwap_num = 0.0
                self._vwap_den = 0
                self._imb_buy_q = 0
                self._imb_sell_q = 0
                self._cp_net_flow = defaultdict(int)
                self._phase_running_t = time.time()
                # v8
                self._last_reveal_t = None
                self._pre_reveal_last_serviced_idx = -1
                self._pre_reveal_last_cancelled_idx = -1
                # v9
                self._last_quote_fair_eff = None
                self._last_drift_cancel_t = 0.0
                # v10
                self._precompute_prefetched_cycle = -1
                self._jit_refresh_done_for_cycle = -1
                self._current_theos = None
                self._current_theos_cycle = -1
                self._current_theos_t = 0.0
                self._last_book_prewarm_t = 0.0
                self._last_adverse_fill_t = {"bid": 0.0, "ask": 0.0}
                self._precompute_request.set()  # rebuild for the new round
                self.reconcile_position()
        if phase == "running":
            self.step()

    def flatten(self) -> None:
        try:
            self._scheduler_stop.set()
        except Exception:
            pass
        try:
            self._ioc_executor.shutdown(wait=False)
        except Exception:
            pass
        with self.lock:
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.reconcile_position()
            pos = self.position
            if pos > 0:
                try:
                    res = self.c.sell_market(self.symbol, qty=pos)
                    self._record_fill_from_trades("sell", res.get("trades", []))
                except Exception as e:
                    print(f"flatten sell failed: {e}")
            elif pos < 0:
                try:
                    res = self.c.buy_market(self.symbol, qty=-pos)
                    self._record_fill_from_trades("buy", res.get("trades", []))
                except Exception as e:
                    print(f"flatten buy failed: {e}")
            print(f"FLAT  pos={self.position}")

    # ----------------------------------------------------------------
    # Fast scheduler (v8 pre-reveal MM + v9 watchdog + v10 precompute prefetch + book pre-warm)
    # ----------------------------------------------------------------

    def _fast_scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self._fast_scheduler_tick()
            except Exception as e:
                print(f"fast_scheduler tick error: {e}")
            self._scheduler_stop.wait(self._scheduler_tick_sec)

    def _fast_scheduler_tick(self) -> None:
        if self.phase != "running":
            return
        if self._n_remaining() <= 0:
            return
        nxt = self._next_reveal_at()
        if nxt is None:
            return
        now = time.time()
        cycle_idx = len(self.posterior.reveals)
        cancel_at = nxt - self.pre_reveal_cancel_sec
        post_at = nxt - self.pre_reveal_post_sec

        # Pre-reveal cancel (v9: unconditional cancel_all for safety).
        if now >= cancel_at and self._pre_reveal_last_cancelled_idx < cycle_idx:
            self._pre_reveal_last_cancelled_idx = cycle_idx
            self._sched_log_seq += 1
            try:
                with self.lock:
                    self.c.cancel_all()
                    self.resting = {"bid": None, "ask": None}
                print(f"[SCHED #{self._sched_log_seq}] PRE-REVEAL-CANCEL "
                      f"cycle={cycle_idx} reveal_in={nxt - now:.2f}s")
            except Exception as e:
                print(f"[SCHED #{self._sched_log_seq}] cancel error: {e}")
        elif (now >= post_at and now < cancel_at
                and self._pre_reveal_last_serviced_idx < cycle_idx):
            self._pre_reveal_last_serviced_idx = cycle_idx
            self._sched_log_seq += 1
            print(f"[SCHED #{self._sched_log_seq}] PRE-REVEAL-POST cycle={cycle_idx}"
                  f" reveal_in={nxt - now:.2f}s "
                  f"pos={self.position} "
                  f"k={cycle_idx}/{self.n_total}")
            try:
                self.step()
            except Exception as e:
                print(f"[SCHED #{self._sched_log_seq}] post error: {e}")

        # v10: precompute prefetch (async, independent of cancel/post gates).
        if self._precompute_prefetched_cycle < cycle_idx:
            if now >= nxt - self.precompute_lead_sec:
                self._precompute_prefetched_cycle = cycle_idx
                self._precompute_request.set()
                print(f"[SCHED] precompute-prefetch fired cycle={cycle_idx} "
                      f"reveal_in={nxt - now:.2f}s")

        # v10: book pre-warm. From ~1s before reveal until the reveal fires,
        # refresh the book cache on every fast tick so on_reveal's IOC sweep
        # can read it directly instead of paying a REST round-trip.
        if now >= nxt - self.book_prewarm_lead_sec:
            age = now - self._book_cache_t
            if age >= self.book_prewarm_max_age_sec:
                try:
                    self._book_cache = self.c.book(
                        self.symbol, depth=self.snipe_book_depth)
                    self._book_cache_t = time.time()
                    self._last_book_prewarm_t = self._book_cache_t
                except Exception:
                    pass

        # v10: JIT theo-freshness guarantee. ~0.5s before the predicted
        # reveal, if the precompute table is not for the current cycle,
        # rebuild it SYNCHRONOUSLY so the fast on_reveal path is guaranteed
        # to hit a fresh table. Fires at most once per cycle.
        if (self._jit_refresh_done_for_cycle < cycle_idx
                and now >= nxt - self.jit_theo_refresh_sec):
            self._jit_refresh_done_for_cycle = cycle_idx
            with self._precompute_lock:
                cached_cycle = self._precompute_for_cycle
                cached_n = len(self._precomputed)
            if cached_cycle != cycle_idx:
                self._sched_log_seq += 1
                print(f"[SCHED #{self._sched_log_seq}] JIT-THEO-REFRESH "
                      f"cycle={cycle_idx} reveal_in={nxt - now:.2f}s "
                      f"(cached_cycle={cached_cycle}, n={cached_n}) "
                      f"-> running synchronous rebuild")
                t0 = time.perf_counter()
                try:
                    self._do_precompute()
                except Exception as e:
                    print(f"[SCHED #{self._sched_log_seq}] JIT rebuild "
                          f"error: {e!r}")
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                print(f"[SCHED #{self._sched_log_seq}] JIT-THEO-REFRESH "
                      f"done in {elapsed_ms:.1f}ms")

    # ----------------------------------------------------------------
    # v10: precompute worker
    # ----------------------------------------------------------------

    def _precompute_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            triggered = self._precompute_request.wait(timeout=0.5)
            if self._scheduler_stop.is_set():
                break
            if not triggered:
                continue
            self._precompute_request.clear()
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[PRECOMPUTE] error: {e!r}")

    def _ensure_theos_fresh(self, blocking: bool = True) -> bool:
        """Guarantee the precompute table is for the current cycle.
        If stale and blocking=True, rebuilds synchronously in this thread
        and returns True iff the table is fresh on return. If blocking=False,
        just triggers an async rebuild and returns False on stale.
        """
        current_cycle = len(self.posterior.reveals)
        with self._precompute_lock:
            if self._precompute_for_cycle == current_cycle:
                return True
        if not blocking:
            self._precompute_request.set()
            return False
        try:
            self._do_precompute()
        except Exception as e:
            print(f"[ENSURE-THEOS] sync rebuild error: {e!r}")
            return False
        with self._precompute_lock:
            return self._precompute_for_cycle == current_cycle

    # ----------------------------------------------------------------
    # Slow housekeeping loop (2s tick): non-time-critical sanity checks.
    # ----------------------------------------------------------------

    def _slow_housekeeping_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self._slow_housekeeping_tick()
            except Exception as e:
                print(f"slow_housekeeping tick error: {e}")
            self._scheduler_stop.wait(self.slow_housekeeping_interval_sec)

    def _slow_housekeeping_tick(self) -> None:
        if self.phase != "running":
            return
        # Periodic REST position reconcile — cheap drift check that catches
        # any divergence between local fill accounting and the exchange.
        try:
            rest_pos = int(
                self.c.positions()["positions"].get(self.symbol, 0))
        except Exception:
            return
        if rest_pos != self.position:
            print(f"[HOUSEKEEP] position drift: local={self.position} "
                  f"rest={rest_pos} -> reconciling")
            with self.lock:
                self.position = rest_pos
        # Posterior-staleness warning: precompute multiple cycles behind
        # means both prefetch and JIT missed.
        cycle_idx = len(self.posterior.reveals)
        with self._precompute_lock:
            cached_cycle = self._precompute_for_cycle
        if (cached_cycle >= 0 and cycle_idx - cached_cycle >= 2
                and self._n_remaining() > 0):
            print(f"[HOUSEKEEP] WARN precompute stale by "
                  f"{cycle_idx - cached_cycle} cycles "
                  f"(cycle={cycle_idx} cached={cached_cycle}) "
                  f"-> forcing rebuild")
            self._ensure_theos_fresh(blocking=True)
        # Refresh current-state theos so between-reveal MM quoting can read
        # fair/sigma/bid_px/ask_px without recomputing Bayesian math.
        self._refresh_current_theos()

    def _refresh_current_theos(self) -> None:
        """Recompute current-state theos: fair, sigma, and the maker quote
        prices the inter-reveal MM step would post right now. Stored in
        `_current_theos` so callers can read instantly.
        """
        try:
            fair, sigma = self.fair_and_sigma()
            bid_px, ask_px, _, _ = self.desired_quotes()
        except Exception as e:
            print(f"[CURRENT-THEOS] refresh error: {e!r}")
            return
        cycle = len(self.posterior.reveals)
        self._current_theos = {
            "fair": float(fair),
            "sigma": float(sigma),
            "bid_px": bid_px,
            "ask_px": ask_px,
        }
        self._current_theos_cycle = cycle
        self._current_theos_t = time.time()

    def get_cached_current_theos(self) -> Optional[Dict[str, float]]:
        """Return the current-state theos if they're for the right cycle and
        fresh enough (<current_theos_max_age_sec). Otherwise None.
        """
        if self._current_theos is None:
            return None
        if self._current_theos_cycle != len(self.posterior.reveals):
            return None
        if time.time() - self._current_theos_t > self.current_theos_max_age_sec:
            return None
        return self._current_theos

    def _do_precompute(self) -> None:
        t_start = time.perf_counter()
        with self.lock:
            if self.phase != "running":
                return
            snapshot = dict(self.posterior.posterior)
            reveals_so_far = list(self.posterior.reveals)
        running_sum = sum(reveals_so_far)
        n_remaining = self.n_total - len(reveals_so_far)
        cycle_idx = len(reveals_so_far)

        if n_remaining <= 0:
            return
        if not snapshot:
            return

        marginal: Dict[int, float] = defaultdict(float)
        for (a, w), p in snapshot.items():
            mass = p / (w + 1)
            for v in range(a, a + w + 1):
                marginal[v] += mass

        sorted_vals = sorted(marginal.items(), key=lambda kv: -kv[1])
        sorted_vals = [(v, p) for v, p in sorted_vals
                       if p >= self.precompute_min_prob]
        sorted_vals = sorted_vals[:self.precompute_max_values]

        new_precomputed: Dict[int, PrecomputedScenario] = {}
        for v, prob in sorted_vals:
            scenario = self._build_scenario(
                snapshot, v, prob, running_sum, n_remaining)
            if scenario is not None:
                new_precomputed[v] = scenario

        with self._precompute_lock:
            self._precomputed = new_precomputed
            self._precompute_for_cycle = cycle_idx
        self._last_precompute_us = (time.perf_counter() - t_start) * 1e6
        self._precompute_run_count += 1
        vals_sorted = sorted(new_precomputed.keys())
        print(f"[PRECOMPUTE #{self._precompute_run_count}] "
              f"cycle={cycle_idx} k={cycle_idx}/{self.n_total} "
              f"n_scenarios={len(new_precomputed)} "
              f"values={vals_sorted[:15]}"
              f"{'...' if len(vals_sorted) > 15 else ''} "
              f"elapsed={self._last_precompute_us:.0f}us")

    def _build_scenario(
        self,
        prior_post: Dict[Tuple[int, int], float],
        v: int,
        prob: float,
        running_sum: float,
        n_remaining: int,
    ) -> Optional[PrecomputedScenario]:
        # Reweight posterior for "next reveal == v".
        new_post: Dict[Tuple[int, int], float] = {}
        total = 0.0
        for (a, w), p in prior_post.items():
            if a <= v <= a + w:
                new_p = p / (w + 1)
                new_post[(a, w)] = new_p
                total += new_p
        if total <= 0:
            return None
        for k in new_post:
            new_post[k] /= total

        new_remaining = n_remaining - 1
        new_running = running_sum + v
        if new_remaining <= 0:
            fair = float(new_running)
            sigma = 0.0
        else:
            e_inner = 0.0
            e2_inner = 0.0
            e_var = 0.0
            for (a, w), p in new_post.items():
                inner_mean = new_remaining * (a + w / 2.0)
                inner_var = new_remaining * (w * (w + 2)) / 12.0
                e_inner += p * inner_mean
                e2_inner += p * inner_mean * inner_mean
                e_var += p * inner_var
            var_total = e_var + (e2_inner - e_inner * e_inner)
            fair = new_running + e_inner
            sigma = math.sqrt(max(var_total, 0.0))

        edge = max(self.min_edge, self.edge_per_sigma * sigma)
        if edge < self.no_edge_pull_ticks:
            bid_px: Optional[int] = None
            ask_px: Optional[int] = None
        else:
            bid_px = int(math.floor(fair - edge))
            ask_px = int(math.ceil(fair + edge))

        base_edge = self.taker_fee + max(
            self.snipe_min_edge, self.snipe_buffer_sigma * sigma)
        lift_to_px: Optional[int] = int(math.floor(fair - base_edge))
        hit_to_px: Optional[int] = int(math.ceil(fair + base_edge))

        return PrecomputedScenario(
            value=v,
            posterior_snapshot=new_post,
            fair=fair,
            sigma=sigma,
            bid_px=bid_px,
            ask_px=ask_px,
            lift_to_px=lift_to_px,
            hit_to_px=hit_to_px,
            marginal_prob=prob,
        )


# ---------------------------------------------------------------------------
# Standalone runner. Prefer run_combined10 for the full WS + probe setup.
# ---------------------------------------------------------------------------
def main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")
    mean0, std0 = post.predict_settle(running_sum=0, n_remaining=10)
    print(f"Prior settle estimate (N=10, k=0): {mean0:.1f} +/- {std0:.1f}")

    strat = Strategy(c, post, symbol="A")
    print(f"v10: reveal_interval={strat.reveal_interval}s  "
          f"duration={strat.duration}s  n_total={strat.n_total}  "
          f"position_limit={strat.position_limit}  tick={strat.tick}")
    print(f"v10: precompute_min_prob={strat.precompute_min_prob}  "
          f"lead={strat.precompute_lead_sec}s  "
          f"max_values={strat.precompute_max_values}  "
          f"no_edge_pull={strat.no_edge_pull_ticks}ticks  "
          f"adverse_skew={strat.adverse_fill_skew_ticks}ticks "
          f"decay={strat.adverse_fill_decay_sec}s")

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  "
              f"running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(
            f"FILL   {msg['side']:>4s} {msg['qty']} @ {msg['price']}  "
            f"liq={msg.get('liquidity')} vs {msg.get('counterparty')}"
        )
        strat.on_fill_event(msg)

    def on_trade(msg):
        strat.on_trade(msg)

    def on_book(msg):
        pass

    def on_game_state(msg):
        phase = msg.get("phase")
        reveals = msg.get("reveals") or []
        print(f"STATE  phase={phase}  reveals={len(reveals)}")
        strat.on_phase_change(phase, reveals)

    def on_settlement(msg):
        print(f"SETTLE prices={msg.get('prices')}  pnl={msg.get('pnl')}")

    def on_ack(msg):
        pass

    def on_message(msg):
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = on_trade
    c.on_book = on_book
    c.on_game_state = on_game_state
    c.on_settlement = on_settlement
    c.on_ack = on_ack
    c.on_message = on_message

    c.start()

    if c.game_state().get("phase") == "running":
        strat.step(reconcile=True)

    print("\nBot started (v10).")
    print("Commands: 's' = status, 'p' = precompute table, "
          "'f' = flatten, 'q' = quit.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                in_window = strat._in_pre_reveal_window()
                nxt = strat._next_reveal_at()
                nxt_in = (nxt - time.time()) if nxt is not None else None
                with strat._precompute_lock:
                    n_scen = len(strat._precomputed)
                    pcycle = strat._precompute_for_cycle
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"k={len(strat.posterior.reveals)}/{strat.n_total}  "
                      f"precompute={n_scen}@cycle{pcycle} "
                      f"({strat._last_precompute_us:.0f}us)  "
                      f"pre_reveal_window={in_window}  next_reveal_in="
                      f"{(f'{nxt_in:.2f}s' if nxt_in is not None else '-')}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precomputed.items())
                    pcycle = strat._precompute_for_cycle
                print(f"  precompute table (cycle={pcycle}, n={len(items)}):")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.marginal_prob:.3f}  "
                          f"fair={sc.fair:6.1f}+/-{sc.sigma:4.1f}  "
                          f"bid={sc.bid_px} ask={sc.ask_px}  "
                          f"lift<={sc.lift_to_px} hit>={sc.hit_to_px}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown command {cmd!r}; try 's', 'p', 'f', 'q'")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Flattening and exiting...")
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten on exit failed: {e}")


if __name__ == "__main__":
    main()
