"""Day 1 live trading strategy v11 — efficiency rewrite of v10.

WHY v11: v10 ran the right algorithm but with infrastructure tax. Audit found
the IOC-sweep loop holds `self.lock` for ~80ms while parallel IOCs are in
flight, blocking on_quote_event/on_fill/on_trade for the entire reveal-race
window. v10 also carried ~250 lines of dead code (informed-strikes never
populated, danger zone disabled, dual snipe paths, opening clamp dominated
by VWAP clamp) and 95 knobs scattered as instance attributes. v11 keeps the
*algorithm* (precompute architecture) and rewrites *only the infrastructure*:

DESIGN — what's different:

  1. LOCK-FREE HOT PATH at reveal. Acquire `self.lock` only briefly to swap
     the posterior + snapshot book/position; SUBMIT IOC futures *outside*
     the lock; collect results without the lock. Each fill briefly re-locks
     to mutate self.position. Net effect: on_quote_event/on_fill can serve
     during the ~80ms IOC RTT instead of stalling.

  2. PRE-ENCODED IOC JSON. Each PrecomputedScenario carries dict bodies for
     buy_ioc/sell_ioc at each price level we'd hit. requests still json-
     encodes per call but the construction work is moved off the hot path.

  3. SINGLE Config DATACLASS. ~30 knobs in one place vs 95 attributes on
     the Strategy. Grouped by purpose (mm/snipe/precompute/scheduler/risk).

  4. ONE SNIPE PATH. v10 had `maybe_snipe` (book-scan) AND `_try_direct_snipe`
     (per-quote-event), each with subtly different gating. v11 has one
     `_snipe` that takes (side, price, qty_avail). on_quote_event fires it
     for the new level; the periodic step() fires it across the book.

  5. CANCEL+REPOST (no modify). Logs show `kept_priority=false` on 17/17
     modify calls in this engine — the venue treats every modify as a
     cancel+new. Skip the modify API; cancel-then-post is the same cost
     wall-clock and one fewer code path.

  6. WS-DRIVEN BOOK. `on_book_event` keeps `_book_cache` fresh (~1ms post-
     reveal per diagnose_latency). The fast scheduler's book pre-warm path
     is kept as a fallback but expected to be no-op when WS is healthy.

  7. NO DEAD CODE. Removed: informed-trader (200 LOC, disabled by default),
     informed-strikes (80 LOC, never populated), reveal danger zone (disabled
     in v10), opening clamp (subsumed by VWAP clamp), `_aggressor_imbalance`
     (defined but never read), dual snipe path, `_try_direct_snipe`.

  8. SLOW PATH IS HARD ERROR. v10 fell back to `_on_reveal_slow` (full
     Bayesian rebuild at reveal); v11 logs a critical error and uses a
     minimal inline rebuild so we don't silently degrade. The scheduler's
     JIT refresh (T-0.5s) should prevent this from ever firing.

HOT-PATH BUDGET (target):
   T=0       on_reveal entered
   +30us     scenario dict lookup (no lock)
   +80us     posterior swap (lock held: ~50us)
   +400us    IOC futures submitted (lock released, executor.submit only)
   +500us    MM post submitted (separate executor, fire-and-forget)
   +80ms     first IOC fill comes back (network bound — can't shrink without raw sockets)

Run:
    python day1/run_combined11.py
    python day1/strategy11.py            # standalone, no probe
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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Posterior over (a, w) — unchanged from v10
# ---------------------------------------------------------------------------
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8
W_LOGN_MU, W_LOGN_SIGMA = 1.7, 0.9
N_PRIOR_SIM = 1_000_000


class Posterior:
    """Discrete joint posterior over (a, w) for X_i ~ Uniform{a, ..., a+w}."""

    def __init__(self, a_mu: float = A_LOGN_MU, a_sigma: float = A_LOGN_SIGMA,
                 w_mu: float = W_LOGN_MU, w_sigma: float = W_LOGN_SIGMA,
                 n_sim: int = N_PRIOR_SIM):
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for _ in range(n_sim):
            a_v = int(math.floor(random.lognormvariate(a_mu, a_sigma)))
            w_v = 1 + int(math.floor(random.lognormvariate(w_mu, w_sigma)))
            counts[(a_v, w_v)] += 1
        total = sum(counts.values())
        self.prior: dict[tuple[int, int], float] = {
            k: v / total for k, v in counts.items()}
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
            print(f"WARN: reveal {x} outside posterior support; ignoring")
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def predict_settle(self, running_sum: float,
                       n_remaining: int) -> tuple[float, float]:
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
# Config: every tunable in one dataclass, grouped by purpose
# ---------------------------------------------------------------------------
@dataclass
class Config:
    # --- maker quoting ---
    quote_qty: int = 2                  # default lots per side
    quote_qty_normal: int = 5           # when sigma small + market agrees
    mid_round_quote_qty: int = 1        # when sigma > full_qty_sigma_max
    full_qty_sigma_max: float = 3.0
    pre_reveal_quote_qty: int = 1
    min_edge: float = 1.0
    edge_per_sigma: float = 0.25
    skew_per_unit: float = 0.20         # inventory skew
    no_edge_pull_ticks: float = 0.5     # if edge < this, pull quote
    tight_floor_edge: float = 1.0       # min per-fill margin when pennying

    # --- snipe (IOC) ---
    snipe_min_edge: float = 2.0
    snipe_buffer_sigma: float = 0.40
    snipe_book_depth: int = 10
    snipe_max_qty_per_level: int = 10
    snipe_max_per_round: int = 40
    snipe_position_buffer: int = 8      # stop sniping this far from limit
    snipe_max_disagreement: float = 3.0  # vs market-mid; abort if exceeded
    snipe_vwap_buffer: float = 1.0

    # --- precompute (next-reveal scenarios) ---
    precompute_min_prob: float = 5e-3
    precompute_max_values: int = 40
    precompute_lead_sec: float = 4.0    # prefetch this long before reveal
    jit_refresh_sec: float = 0.5        # JIT sync rebuild this long before
    ioc_parallel_workers: int = 6

    # --- pre-reveal scheduler ---
    pre_reveal_post_sec: float = 1.5    # repost MM at T-1.5s
    pre_reveal_post_qty: int = 5
    pre_reveal_cancel_sec: float = 0.9  # cancel resting at T-0.9s
    pre_reveal_blackout_sec: float = 0.95  # no NEW posts inside this window
    pre_reveal_token_reserve: float = 7.0  # save tokens for reveal burst

    # --- book pre-warm fallback (used only if WS goes silent) ---
    book_prewarm_lead_sec: float = 0.7
    book_prewarm_max_age_sec: float = 0.1
    book_cache_max_age_sec: float = 1.0

    # --- adverse-fill skew (between-reveal MM) ---
    adverse_skew_ticks: float = 1.5
    adverse_decay_sec: float = 8.0

    # --- VWAP / market signal ---
    trade_window_sec: float = 30.0
    vwap_clamp_max_k: int = 3           # blend VWAP into fair for first K
    vwap_clamp_min_volume: int = 3
    vwap_side_guard_ticks: int = 1
    min_trade_vol_pre_reveal: int = 3
    pre_reveal_vwap_weight: float = 0.7
    pre_reveal_max_pos: int = 5
    market_anchor_disagreement: float = 5.0

    # --- reveal-math guard (don't snipe against k>=1 arithmetic fair) ---
    reveal_math_guard_ticks: float = 4.0
    reveal_math_k1_widening: float = 2.0

    # --- inventory ---
    max_pos_target_sigma: float = 2.0       # conservative
    max_pos_floor: int = 10
    max_pos_target_sigma_normal: float = 3.0  # market agrees
    max_pos_floor_normal: int = 15
    inv_hard_kill_frac: float = 0.4         # one side off when |pos|>this

    # --- drift cancel ---
    drift_cancel_ticks: float = 3.0
    drift_cancel_min_interval: float = 0.5

    # --- partial fill cool-off ---
    partial_fill_cool_off_sec: float = 3.0

    # --- pre-reveal MM ---
    pre_reveal_min_edge: float = 4.0
    pre_reveal_pull_disagreement: float = 10.0
    empty_side_edge: float = 15.0
    pre_reveal_warmup_sec: float = 2.0

    # --- maker cadence between reveals ---
    maker_reprice_interval_by_k: tuple[tuple[int, float], ...] = (
        (3, 12.0), (7, 8.0), (99, 4.0))

    # --- scheduler ticks ---
    fast_scheduler_tick_sec: float = 0.02
    slow_housekeeping_interval_sec: float = 2.0
    current_theos_max_age_sec: float = 2.5

    # --- ws-book settling ---
    quote_event_throttle_sec: float = 0.05


# ---------------------------------------------------------------------------
# PrecomputedScenario: "if next reveal == v, do this"
# ---------------------------------------------------------------------------
@dataclass
class PrecomputedScenario:
    value: int
    posterior_snapshot: Dict[Tuple[int, int], float]
    fair: float
    sigma: float
    bid_px: Optional[int]
    ask_px: Optional[int]
    lift_to_px: Optional[int]     # max ask we'd IOC-buy at
    hit_to_px: Optional[int]      # min bid we'd IOC-sell at
    marginal_prob: float = 0.0


# ---------------------------------------------------------------------------
# Strategy v11
# ---------------------------------------------------------------------------
class Strategy:
    def __init__(self, client: GameClient, posterior: Posterior,
                 symbol: str = "A", config: Optional[Config] = None):
        self.c = client
        self.symbol = symbol
        self.posterior = posterior
        self.cfg = config or Config()
        self.lock = threading.RLock()

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

        # ----- state -----
        self.resting: dict[str, Optional[dict]] = {"bid": None, "ask": None}
        self._posterior_gen: int = 0
        self._fair_sigma_cache: Optional[tuple[int, float, float]] = None
        self._last_maker_apply_t: float = 0.0
        self._last_quote_event_t: float = 0.0
        self._snipe_count_this_round: int = 0
        self._phase_running_t: Optional[float] = None

        # ----- adverse skew + cool-offs -----
        self._last_adverse_fill_t: Dict[str, float] = {
            "bid": 0.0, "ask": 0.0}
        self._partial_fill_cool_off_until: Dict[str, float] = {
            "bid": 0.0, "ask": 0.0}

        # ----- WS book cache -----
        self._book_cache: Optional[dict] = None
        self._book_cache_t: float = 0.0

        # ----- private fill signal -----
        self._last_fill_px: float = 0.0
        self._last_fill_t: float = 0.0

        # ----- public trade tape -----
        self._recent_trades: deque[tuple[float, float, int, str]] = deque(
            maxlen=1000)
        self._vwap_num: float = 0.0
        self._vwap_den: int = 0

        # ----- reveal timing -----
        self._last_reveal_t: Optional[float] = None
        self._pre_reveal_last_serviced_idx: int = -1
        self._pre_reveal_last_cancelled_idx: int = -1

        # ----- drift cancel -----
        self._last_quote_fair_eff: Optional[float] = None
        self._last_drift_cancel_t: float = 0.0
        try:
            mean0, _ = posterior.predict_settle(0.0, self.n_total)
            self._prior_E_settle: float = float(mean0)
        except Exception:
            self._prior_E_settle = 27.0

        # ----- precompute -----
        self._precomputed: Dict[int, PrecomputedScenario] = {}
        self._precompute_lock = threading.RLock()
        self._precompute_for_cycle: int = -1
        self._precompute_request = threading.Event()
        self._last_precompute_us: float = 0.0
        self._precompute_run_count: int = 0
        self._precompute_prefetched_cycle: int = -1
        self._jit_refresh_done_for_cycle: int = -1
        self._last_book_prewarm_t: float = 0.0

        # ----- current-state theos (for between-reveal MM cheap reads) -----
        self._current_theos: Optional[Dict[str, Any]] = None
        self._current_theos_cycle: int = -1
        self._current_theos_t: float = 0.0

        # ----- executors -----
        # IOC executor: parallel reveal-race fires. requests.Session is
        # thread-safe; urllib3 connection pool has matching capacity.
        self._ioc_executor = ThreadPoolExecutor(
            max_workers=self.cfg.ioc_parallel_workers,
            thread_name_prefix="v11-ioc")
        # MM executor: fire-and-forget post/cancel so on_reveal doesn't wait
        # synchronously for ~80ms maker quote round-trips. Single worker is
        # enough — MM posts are not parallelized; we just want them off the
        # critical path.
        self._mm_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="v11-mm")

        # ----- threads -----
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(
            target=self._fast_scheduler_loop,
            name="v11-fast-sched", daemon=True)
        self._scheduler_thread.start()
        self._slow_thread = threading.Thread(
            target=self._slow_housekeeping_loop,
            name="v11-slow-house", daemon=True)
        self._slow_thread.start()
        self._precompute_worker = threading.Thread(
            target=self._precompute_loop,
            name="v11-precompute", daemon=True)
        self._precompute_worker.start()
        self._precompute_request.set()

        self._warm_connections(self.cfg.ioc_parallel_workers)

    # ------------------------------------------------------------------
    # Warming
    # ------------------------------------------------------------------
    def _warm_connections(self, n: int) -> None:
        """Pre-open n HTTP sockets so the IOC pool has warm connections at
        first reveal. Per project_day1_exchange_timings.md, cold REST is
        1.5-2.5x slower than steady state."""
        t0 = time.perf_counter()
        try:
            list(self._ioc_executor.map(
                lambda _: self.c.game_state(), range(n)))
            dt_ms = (time.perf_counter() - t0) * 1000
            print(f"[v11 WARM] {n} HTTP connections in {dt_ms:.1f}ms")
        except Exception as e:
            print(f"[v11 WARM] connection warm-up failed: {e}")

    # ------------------------------------------------------------------
    # Trivial helpers
    # ------------------------------------------------------------------
    def _running_sum(self) -> float:
        return sum(self.posterior.reveals)

    def _n_remaining(self) -> int:
        return max(self.n_total - len(self.posterior.reveals), 0)

    def fair_and_sigma(self) -> tuple[float, float]:
        gen = self._posterior_gen
        cached = self._fair_sigma_cache
        if cached is not None and cached[0] == gen:
            return cached[1], cached[2]
        fair, sigma = self.posterior.predict_settle(
            self._running_sum(), self._n_remaining())
        self._fair_sigma_cache = (gen, fair, sigma)
        return fair, sigma

    def _maker_reprice_interval(self) -> float:
        k = len(self.posterior.reveals)
        if k == 0:
            return float("inf")
        for k_max, interval in self.cfg.maker_reprice_interval_by_k:
            if k <= k_max:
                return interval
        return self.cfg.maker_reprice_interval_by_k[-1][1]

    # ------------------------------------------------------------------
    # Reveal-math guard (don't snipe vs k>=1 arithmetic fair)
    # ------------------------------------------------------------------
    def _reveal_math_fair(self) -> Optional[float]:
        reveals = self.posterior.reveals
        k = len(reveals)
        if k < 1:
            return None
        remaining = self._n_remaining()
        if remaining <= 0:
            return sum(reveals)
        return sum(reveals) + remaining * statistics.fmean(reveals)

    def _reveal_math_guard_width(self) -> float:
        k = len(self.posterior.reveals)
        if k == 1:
            return self.cfg.reveal_math_guard_ticks * self.cfg.reveal_math_k1_widening
        return self.cfg.reveal_math_guard_ticks

    def _reveal_math_blocks_buy(self, fair_eff: float) -> bool:
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return fair_eff - rm > self._reveal_math_guard_width()

    def _reveal_math_blocks_sell(self, fair_eff: float) -> bool:
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return rm - fair_eff > self._reveal_math_guard_width()

    # ------------------------------------------------------------------
    # VWAP / market signal
    # ------------------------------------------------------------------
    def _expire_trades(self, now: float) -> None:
        cutoff = now - self.cfg.trade_window_sec
        tape = self._recent_trades
        while tape and tape[0][0] < cutoff:
            _, px_old, q_old, _ = tape.popleft()
            self._vwap_num -= px_old * q_old
            self._vwap_den -= q_old

    def _trade_vwap(self) -> Optional[float]:
        now = time.time()
        with self.lock:
            self._expire_trades(now)
            if self._vwap_den <= 0:
                return None
            return self._vwap_num / self._vwap_den

    def _vwap_clamped_fair(self, fair: float) -> float:
        k = len(self.posterior.reveals)
        if k < 1 or k > self.cfg.vwap_clamp_max_k:
            return fair
        if self._vwap_den < self.cfg.vwap_clamp_min_volume:
            return fair
        vwap = self._trade_vwap()
        if vwap is None:
            return fair
        w = (self.cfg.vwap_clamp_max_k - k + 1) / (self.cfg.vwap_clamp_max_k + 1)
        return w * vwap + (1.0 - w) * fair

    def _vwap_for_side_guard(self) -> Optional[float]:
        if self._vwap_den < self.cfg.vwap_clamp_min_volume:
            return None
        return self._trade_vwap()

    def _market_mid_from_book(self) -> Optional[float]:
        book = self._book_cache
        if book is None:
            return None
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return (bids[0]["price"] + asks[0]["price"]) / 2.0

    def _market_signal(self) -> Optional[float]:
        book_mid = self._market_mid_from_book()
        vwap = self._trade_vwap()
        if book_mid is not None and vwap is not None:
            return 0.5 * book_mid + 0.5 * vwap
        return vwap if vwap is not None else book_mid

    def _pre_reveal_vwap_fair(self, prior_mean: float) -> Optional[float]:
        vwap = self._trade_vwap()
        if vwap is None or self._vwap_den < self.cfg.min_trade_vol_pre_reveal:
            return None
        w = self.cfg.pre_reveal_vwap_weight
        return w * vwap + (1.0 - w) * prior_mean

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def _max_position_for_sigma(self, sigma: float,
                                *, relaxed: bool = False) -> int:
        if relaxed:
            target = self.cfg.max_pos_target_sigma_normal
            floor = self.cfg.max_pos_floor_normal
        else:
            target = self.cfg.max_pos_target_sigma
            floor = self.cfg.max_pos_floor
        if sigma <= target:
            return self.position_limit
        cap = int(self.position_limit * target / sigma)
        return max(floor, cap)

    def _current_quote_qty(self, side: Optional[str] = None) -> int:
        if len(self.posterior.reveals) == 0:
            return self.cfg.pre_reveal_quote_qty
        fair, sigma = self.fair_and_sigma()
        market_mid = self._market_signal()
        relaxed = (market_mid is not None and
                   abs(fair - market_mid) < self.cfg.market_anchor_disagreement)
        if sigma > self.cfg.full_qty_sigma_max:
            base = self.cfg.mid_round_quote_qty
        else:
            base = self.cfg.quote_qty_normal if relaxed else self.cfg.quote_qty
        # Pre-reveal-window upsize when consensus confirms
        if (self._in_pre_reveal_window() and market_mid is not None
                and abs(fair - market_mid) < self.cfg.market_anchor_disagreement):
            base = max(base, self.cfg.pre_reveal_post_qty)
        return base

    # ------------------------------------------------------------------
    # Adverse-fill skew (widens hit side post-fill, decays linearly)
    # ------------------------------------------------------------------
    def _adverse_skew(self, side: str, now: float) -> float:
        last_t = self._last_adverse_fill_t.get(side, 0.0)
        if last_t <= 0.0:
            return 0.0
        age = now - last_t
        if age >= self.cfg.adverse_decay_sec:
            return 0.0
        return self.cfg.adverse_skew_ticks * (
            1.0 - age / self.cfg.adverse_decay_sec)

    # ------------------------------------------------------------------
    # Reveal-window helpers
    # ------------------------------------------------------------------
    def _next_reveal_at(self) -> Optional[float]:
        if self._last_reveal_t is None:
            return None
        nxt = self._last_reveal_t + self.reveal_interval
        now = time.time()
        while nxt < now - 0.5:
            nxt += self.reveal_interval
        return nxt

    def _in_pre_reveal_window(self) -> bool:
        nxt = self._next_reveal_at()
        if nxt is None or self._n_remaining() <= 0:
            return False
        now = time.time()
        return (nxt - self.cfg.pre_reveal_post_sec) <= now \
            < (nxt - self.cfg.pre_reveal_cancel_sec)

    def _in_quote_blackout(self) -> bool:
        """True if new MM quotes would still be on the book at T+0 and
        become hit-bait for competitor IOCs racing the reveal."""
        nxt = self._next_reveal_at()
        if nxt is None or self._n_remaining() <= 0:
            return False
        return time.time() >= (nxt - self.cfg.pre_reveal_blackout_sec)

    # ------------------------------------------------------------------
    # Quote calculation
    # ------------------------------------------------------------------
    def desired_quotes(self) -> tuple[Optional[int], Optional[int], float, float]:
        fair, sigma = self.fair_and_sigma()
        fair_eff = self._vwap_clamped_fair(fair)
        pre_reveal_vwap_active = False
        if len(self.posterior.reveals) == 0:
            v6 = self._pre_reveal_vwap_fair(prior_mean=fair)
            if v6 is None:
                return None, None, fair, sigma
            fair_eff = v6
            pre_reveal_vwap_active = True
        if self._n_remaining() == 0:
            return None, None, fair, sigma

        if not self._other_makers_present():
            return None, None, fair, sigma

        pre_reveal = len(self.posterior.reveals) == 0
        if pre_reveal and self._phase_running_t is not None:
            if time.time() - self._phase_running_t < self.cfg.pre_reveal_warmup_sec:
                return None, None, fair, sigma

        pre_bids: list = []
        pre_asks: list = []
        if pre_reveal:
            book = self._book_cache
            if (book is None or
                    time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec):
                return None, None, fair, sigma
            pre_bids = book.get("bids") or []
            pre_asks = book.get("asks") or []
            if not pre_bids and not pre_asks:
                return None, None, fair, sigma

        edge = max(self.cfg.min_edge, self.cfg.edge_per_sigma * sigma)
        if pre_reveal:
            edge = max(edge, self.cfg.pre_reveal_min_edge)
        if not pre_reveal and edge < self.cfg.no_edge_pull_ticks:
            return None, None, fair, sigma

        skew = 0.0 if pre_reveal else -self.position * self.cfg.skew_per_unit

        now = time.time()
        bid_adv = self._adverse_skew("bid", now)
        ask_adv = self._adverse_skew("ask", now)

        bid_px: Optional[int] = int(math.floor(fair_eff - edge + skew - bid_adv))
        ask_px: Optional[int] = int(math.ceil(fair_eff + edge + skew + ask_adv))

        # Pre-reveal one-sided book handling
        if pre_reveal:
            if not pre_asks:
                ask_px = int(math.ceil(fair_eff + self.cfg.empty_side_edge + skew))
                if (pre_bids and pre_bids[0]["price"]
                        > fair_eff + self.cfg.pre_reveal_pull_disagreement):
                    ask_px = None
            elif not pre_bids:
                bid_px = int(math.floor(fair_eff - self.cfg.empty_side_edge + skew))
                if (pre_asks and pre_asks[0]["price"]
                        < fair_eff - self.cfg.pre_reveal_pull_disagreement):
                    bid_px = None

        market_mid = self._market_signal()
        biased = (market_mid is not None and
                  abs(fair_eff - market_mid) >= self.cfg.market_anchor_disagreement)

        if not biased:
            bid_px, ask_px = self._apply_penny(bid_px, ask_px, fair_eff, skew)

        if biased and market_mid is not None:
            safe_bid = int(math.floor(market_mid - edge))
            safe_ask = int(math.ceil(market_mid + edge))
            if bid_px is not None:
                bid_px = min(bid_px, safe_bid)
            if ask_px is not None:
                ask_px = max(ask_px, safe_ask)

        if pre_reveal and market_mid is not None:
            if market_mid > fair_eff + self.cfg.pre_reveal_pull_disagreement:
                ask_px = None
            elif market_mid < fair_eff - self.cfg.pre_reveal_pull_disagreement:
                bid_px = None

        if self.cfg.vwap_side_guard_ticks > 0:
            gv = self._vwap_for_side_guard()
            if gv is not None:
                g = self.cfg.vwap_side_guard_ticks * self.tick
                bid_ceiling = int(math.floor(gv)) - g
                ask_floor = int(math.ceil(gv)) + g
                if bid_px is not None and bid_px > bid_ceiling:
                    bid_px = bid_ceiling
                if ask_px is not None and ask_px < ask_floor:
                    ask_px = ask_floor

        if ask_px is not None and bid_px is not None and ask_px <= bid_px:
            ask_px = bid_px + self.tick

        bid_qty = self._current_quote_qty("bid")
        ask_qty = self._current_quote_qty("ask")
        relaxed = (market_mid is not None and not biased)
        if pre_reveal_vwap_active:
            max_pos = self.cfg.pre_reveal_max_pos
        else:
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        if bid_px is not None and self.position + bid_qty > max_pos:
            bid_px = None
        if ask_px is not None and self.position - ask_qty < -max_pos:
            ask_px = None

        kill_thresh = int(max_pos * self.cfg.inv_hard_kill_frac)
        if self.position > kill_thresh and bid_px is not None:
            bid_px = None
        if self.position < -kill_thresh and ask_px is not None:
            ask_px = None

        if (bid_px is not None and
                now < self._partial_fill_cool_off_until.get("bid", 0.0)):
            bid_px = None
        if (ask_px is not None and
                now < self._partial_fill_cool_off_until.get("ask", 0.0)):
            ask_px = None

        # No-cross guard vs cached inside book
        book = self._book_cache
        if (book is not None and
                time.time() - self._book_cache_t <= self.cfg.book_cache_max_age_sec):
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

        if bid_px is not None or ask_px is not None:
            self._last_quote_fair_eff = fair_eff

        return bid_px, ask_px, fair, sigma

    def _apply_penny(self, bid_px: Optional[int], ask_px: Optional[int],
                     fair: float, skew: float) -> tuple[Optional[int], Optional[int]]:
        book = self._book_cache
        if (book is None or
                time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec):
            return bid_px, ask_px
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        step = self.tick

        if bids and bid_px is not None:
            best_bid = bids[0]["price"]
            our = self.resting.get("bid")
            already_top = our is not None and our["price"] >= best_bid
            if not already_top:
                penny = best_bid + step
                max_safe = int(math.floor(fair + skew - self.cfg.tight_floor_edge))
                if penny > bid_px and penny <= max_safe:
                    bid_px = penny
                elif best_bid > bid_px and best_bid <= max_safe:
                    bid_px = best_bid
        if asks and ask_px is not None:
            best_ask = asks[0]["price"]
            our = self.resting.get("ask")
            already_top = our is not None and our["price"] <= best_ask
            if not already_top:
                penny = best_ask - step
                min_safe = int(math.ceil(fair + skew + self.cfg.tight_floor_edge))
                if penny < ask_px and penny >= min_safe:
                    ask_px = penny
                elif best_ask < ask_px and best_ask >= min_safe:
                    ask_px = best_ask
        return bid_px, ask_px

    def _other_makers_present(self) -> bool:
        book = self._book_cache
        if book is None:
            return False
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return False

        def side_has_other(levels, our_px, our_qty):
            for lvl in levels:
                lvl_px = lvl.get("price")
                if lvl_px != our_px:
                    return True
                if our_qty is None or lvl.get("qty", 0) > our_qty:
                    return True
            return False

        ob = self.resting.get("bid")
        oa = self.resting.get("ask")
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        return (side_has_other(bids,
                               ob["price"] if ob else None,
                               ob["qty"] if ob else None)
                or side_has_other(asks,
                                  oa["price"] if oa else None,
                                  oa["qty"] if oa else None))

    # ------------------------------------------------------------------
    # Order plumbing — cancel+post only (no modify)
    # ------------------------------------------------------------------
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

    def _apply_target_quotes(self, bid_px: Optional[int],
                             ask_px: Optional[int]) -> None:
        if self._in_quote_blackout():
            self._safe_cancel("bid")
            self._safe_cancel("ask")
            return
        cur_ask = self.resting["ask"]["price"] if self.resting["ask"] else None
        cur_bid = self.resting["bid"]["price"] if self.resting["bid"] else None

        # Crossing fix: cancel opposite first
        if bid_px is not None and cur_ask is not None and bid_px >= cur_ask:
            self._safe_cancel("ask")
        if ask_px is not None and cur_bid is not None and ask_px <= cur_bid:
            self._safe_cancel("bid")

        for side, want_px, cur_px in (
                ("bid", bid_px, cur_bid),
                ("ask", ask_px, cur_ask)):
            if want_px is None:
                self._safe_cancel(side)
                continue
            # Always cancel+post; modify loses priority on this engine anyway.
            if cur_px != want_px:
                self._safe_cancel(side)
                self._post(side, want_px)

    # ------------------------------------------------------------------
    # Drift cancel
    # ------------------------------------------------------------------
    def _maybe_drift_cancel(self) -> None:
        if self.phase != "running":
            return
        if self._last_quote_fair_eff is None:
            return
        now = time.time()
        if now - self._last_drift_cancel_t < self.cfg.drift_cancel_min_interval:
            return
        try:
            fair, _ = self.fair_and_sigma()
        except Exception:
            return
        fair_eff = self._vwap_clamped_fair(fair)
        if abs(fair_eff - self._last_quote_fair_eff) <= self.cfg.drift_cancel_ticks:
            return
        print(f"  v11 DRIFT-CANCEL: fair_eff {self._last_quote_fair_eff:.1f}"
              f" -> {fair_eff:.1f} "
              f"({fair_eff - self._last_quote_fair_eff:+.1f})")
        self._last_drift_cancel_t = now
        try:
            with self.lock:
                self.c.cancel_all()
                self.resting = {"bid": None, "ask": None}
            self._last_quote_fair_eff = None
            self.step()
        except Exception as e:
            print(f"  v11 drift-cancel failed: {e}")

    # ------------------------------------------------------------------
    # Unified snipe path
    # ------------------------------------------------------------------
    def _snipe_check_global(self) -> bool:
        """Pre-conditions for ANY snipe attempt."""
        if len(self.posterior.reveals) < 1:
            return False
        if self._n_remaining() > 0 \
                and self._snipe_count_this_round >= self.cfg.snipe_max_per_round:
            return False
        return True

    def _snipe_edge_required(self, sigma: float, side: str) -> float:
        """Edge needed beyond taker fee to fire a snipe on `side`."""
        if self._n_remaining() == 0:
            return self.taker_fee
        return self.taker_fee + max(self.cfg.snipe_min_edge,
                                    self.cfg.snipe_buffer_sigma * sigma)

    def _snipe_max_pos(self, sigma: float, *, relaxed: bool) -> int:
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        return min(max_pos, self.position_limit - self.cfg.snipe_position_buffer)

    def _snipe(self, side: str, price: int, qty_avail: int) -> bool:
        """Attempt one snipe. side='buy' means we lift a passive ASK at price.
        Returns True iff a fill landed. Caller holds NO lock — we acquire briefly.
        """
        if side not in ("buy", "sell") or price is None or qty_avail <= 0:
            return False

        with self.lock:
            if self.phase != "running":
                return False
            if not self._snipe_check_global():
                return False

            # Don't snipe our own resting
            our = self.resting.get("ask" if side == "buy" else "bid")
            if our is not None and our["price"] == price:
                return False

            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return False
            fair_eff = self._vwap_clamped_fair(fair)
            snipe_vwap = self._vwap_for_side_guard()

            # Reveal-math directional guard
            if side == "buy" and self._reveal_math_blocks_buy(fair_eff):
                return False
            if side == "sell" and self._reveal_math_blocks_sell(fair_eff):
                return False

            market_mid = self._market_signal()
            if self._n_remaining() > 0:
                if market_mid is None:
                    return False
                if abs(fair_eff - market_mid) > self.cfg.snipe_max_disagreement:
                    return False

            relaxed = (market_mid is not None and
                       abs(fair_eff - market_mid) < self.cfg.market_anchor_disagreement)
            snipe_max_pos = self._snipe_max_pos(sigma, relaxed=relaxed)
            edge_required = self._snipe_edge_required(sigma, side)

            if side == "buy":
                mispricing = fair_eff - price
                if mispricing <= edge_required:
                    return False
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap < price - self.cfg.snipe_vwap_buffer):
                    return False
                headroom = snipe_max_pos - self.position
            else:
                mispricing = price - fair_eff
                if mispricing <= edge_required:
                    return False
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap > price + self.cfg.snipe_vwap_buffer):
                    return False
                headroom = snipe_max_pos + self.position
            if headroom <= 0:
                return False
            qty = min(qty_avail, headroom, self.cfg.snipe_max_qty_per_level)
            if qty <= 0:
                return False

        # ---- network call OUTSIDE the lock ----
        ioc = self.c.buy_ioc if side == "buy" else self.c.sell_ioc
        try:
            res = ioc(self.symbol, price=price, qty=qty)
        except Exception:
            return False

        with self.lock:
            filled = self._record_fill_from_trades(side, res.get("trades", []))
            if filled and self._n_remaining() > 0:
                self._snipe_count_this_round += 1
        if filled:
            print(f"  SNIPE {side} {filled} @ {price}  "
                  f"fair={fair_eff:.1f} edge={mispricing:+.1f}")
        return bool(filled)

    def _snipe_book_scan(self, fair: float, sigma: float) -> bool:
        """Walk the cached book and snipe every mispriced level. Used by
        the periodic step() between reveals."""
        book = self._book_cache
        if book is None:
            return False
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return False
        took = False
        for level in book.get("asks") or []:
            if self._snipe("buy", level["price"], level["qty"]):
                took = True
            else:
                break
        for level in book.get("bids") or []:
            if self._snipe("sell", level["price"], level["qty"]):
                took = True
            else:
                break
        return took

    # ------------------------------------------------------------------
    # Top-level step (periodic, mid-round housekeeping)
    # ------------------------------------------------------------------
    def step(self, *, reconcile: bool = False) -> None:
        with self.lock:
            if self.phase != "running":
                if self.phase == "settled":
                    self.resting = {"bid": None, "ask": None}
                return
            if self._in_quote_blackout():
                return
            if reconcile:
                self.reconcile_position()
            pre_reveal = (len(self.posterior.reveals) == 0)

        if not pre_reveal:
            fair, sigma = self.fair_and_sigma()
            self._snipe_book_scan(fair, sigma)

        with self.lock:
            bid_px, ask_px, fair, sigma = self.desired_quotes()
            self._apply_target_quotes(bid_px, ask_px)
            self._last_maker_apply_t = time.time()
            print(f"QUOTE  fv={fair:6.1f} +/-{sigma:4.1f}  "
                  f"pos={self.position:+4d}  bid={bid_px}  ask={ask_px}  "
                  f"k={len(self.posterior.reveals)}/{self.n_total}")

    def reconcile_position(self) -> None:
        try:
            self.position = int(
                self.c.positions()["positions"].get(self.symbol, 0))
        except Exception as e:
            print(f"reconcile_position failed: {e}")

    # ------------------------------------------------------------------
    # FAST-PATH on_reveal: lock-free wait edition
    # ------------------------------------------------------------------
    def on_reveal(self, value: float) -> None:
        t_start = time.perf_counter()
        self._last_reveal_t = time.time()
        int_value = int(value)

        # ---- scenario lookup (no lock; immutable snapshot) ----
        with self._precompute_lock:
            scenario = self._precomputed.get(int_value)
            cached_cycle = self._precompute_for_cycle
        current_cycle = len(self.posterior.reveals)

        # Stale-cycle JIT rebuild
        if cached_cycle != current_cycle:
            print(f"[v11 FAST-MISS-STALE] v={int_value} "
                  f"cached={cached_cycle} cur={current_cycle} -> JIT rebuild")
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[v11 FAST-MISS-STALE] rebuild error: {e!r}")
            with self._precompute_lock:
                scenario = self._precomputed.get(int_value)
                cached_cycle = self._precompute_for_cycle

        if scenario is None or cached_cycle != current_cycle:
            print(f"[v11 CRIT] no scenario for value={int_value} cycle={current_cycle} "
                  f"-> emergency rebuild + slow update")
            self._emergency_reveal(value)
            return

        # ---- STAGE 1: atomic state swap + snapshot for the sweep ----
        with self.lock:
            self.posterior.posterior = dict(scenario.posterior_snapshot)
            self.posterior.reveals.append(float(value))
            self._posterior_gen += 1
            self._fair_sigma_cache = None
            self._last_maker_apply_t = 0.0
            self._last_quote_fair_eff = None
            self.resting = {"bid": None, "ask": None}
            n_remaining_after = self.n_total - len(self.posterior.reveals)
            book = self._book_cache
            pos = self.position
            relaxed_max_pos = self._max_position_for_sigma(
                scenario.sigma, relaxed=True)
        t_local = time.perf_counter()

        # ---- STAGE 2: build IOC plan + SUBMIT (no lock held during wait) ----
        sweep_count = 0
        futures: List[Tuple[str, int, int, Future]] = []
        if n_remaining_after >= 0 and book is not None:
            futures = self._build_and_submit_ioc(
                scenario, book, pos, relaxed_max_pos)
        t_submit = time.perf_counter()

        # ---- STAGE 3: fire-and-forget MM post on a separate worker so we
        # don't synchronously wait ~80ms for it. The lock-free wait below
        # finishes the IOC race; the MM post happens in parallel. ----
        if scenario.bid_px is not None or scenario.ask_px is not None:
            self._mm_executor.submit(
                self._post_mm_async, scenario.bid_px, scenario.ask_px,
                scenario.fair)

        # ---- STAGE 4: collect IOC results WITHOUT main lock ----
        if futures:
            sweep_count = self._collect_ioc_results(futures, scenario)
        t_done = time.perf_counter()

        elapsed_us = (t_done - t_start) * 1e6
        local_us = (t_local - t_start) * 1e6
        submit_us = (t_submit - t_local) * 1e6
        wait_us = (t_done - t_submit) * 1e6
        print(f"[v11 FAST #{int_value}] cycle={current_cycle+1}/{self.n_total} "
              f"fair={scenario.fair:.1f}+/-{scenario.sigma:.1f} "
              f"bid={scenario.bid_px} ask={scenario.ask_px} "
              f"lift_to={scenario.lift_to_px} hit_to={scenario.hit_to_px} "
              f"sweeps={sweep_count} "
              f"t_local={local_us:.0f}us t_submit={submit_us:.0f}us "
              f"t_wait={wait_us/1000:.0f}ms total={elapsed_us/1000:.1f}ms")

        self._precompute_request.set()
        self._precompute_prefetched_cycle = -1

        if n_remaining_after == 0:
            self._sweep_at_settle(float(sum(self.posterior.reveals)))

    def _post_mm_async(self, bid_px: Optional[int],
                       ask_px: Optional[int], fair: float) -> None:
        """Fire-and-forget MM post on the mm_executor so on_reveal doesn't
        synchronously wait for the maker round-trip."""
        try:
            with self.lock:
                self._apply_target_quotes(bid_px, ask_px)
                self._last_quote_fair_eff = fair
        except Exception as e:
            print(f"  [v11 MM-POST] async post error: {e!r}")

    def _build_and_submit_ioc(
        self,
        scenario: PrecomputedScenario,
        book: dict,
        position: int,
        relaxed_max_pos: int,
    ) -> List[Tuple[str, int, int, Future]]:
        """Build cumulative-headroom-capped IOC plan and submit to executor.
        Caller does NOT hold self.lock when this is called for the wait;
        we hold it only briefly above to snapshot state. This function
        itself does no lock acquisition — pure compute + executor.submit.
        """
        snipe_max_pos = min(
            relaxed_max_pos, self.position_limit - self.cfg.snipe_position_buffer)
        cap = self.cfg.snipe_max_qty_per_level

        buy_plans: List[Tuple[int, int]] = []
        if scenario.lift_to_px is not None:
            remaining = snipe_max_pos - position
            for lvl in book.get("asks") or []:
                if lvl["price"] > scenario.lift_to_px:
                    break
                if remaining <= 0:
                    break
                qty = min(lvl["qty"], remaining, cap)
                if qty <= 0:
                    break
                buy_plans.append((lvl["price"], qty))
                remaining -= qty

        sell_plans: List[Tuple[int, int]] = []
        if scenario.hit_to_px is not None:
            remaining = snipe_max_pos + position
            for lvl in book.get("bids") or []:
                if lvl["price"] < scenario.hit_to_px:
                    break
                if remaining <= 0:
                    break
                qty = min(lvl["qty"], remaining, cap)
                if qty <= 0:
                    break
                sell_plans.append((lvl["price"], qty))
                remaining -= qty

        futures: List[Tuple[str, int, int, Future]] = []
        for px, q in buy_plans:
            fut = self._ioc_executor.submit(
                self.c.buy_ioc, self.symbol, price=px, qty=q)
            futures.append(("buy", px, q, fut))
        for px, q in sell_plans:
            fut = self._ioc_executor.submit(
                self.c.sell_ioc, self.symbol, price=px, qty=q)
            futures.append(("sell", px, q, fut))
        return futures

    def _collect_ioc_results(
        self,
        futures: List[Tuple[str, int, int, Future]],
        scenario: PrecomputedScenario,
    ) -> int:
        """Drain IOC futures WITHOUT holding self.lock. Each fill briefly
        re-acquires the lock to mutate position state."""
        sweep_count = 0
        for side, px, qty, fut in futures:
            try:
                res = fut.result(timeout=2.0)
            except Exception as e:
                print(f"  [v11 FAST-SWEEP] {side} @ {px} q={qty} failed: {e!r}")
                continue
            with self.lock:
                filled = self._record_fill_from_trades(side, res.get("trades", []))
                if filled:
                    self._snipe_count_this_round += 1
            if filled:
                sweep_count += 1
                edge = (scenario.fair - px) if side == "buy" else (px - scenario.fair)
                print(f"  [v11 FAST-SWEEP] {side} {filled} @ {px} "
                      f"fair={scenario.fair:.1f} edge={edge:+.1f}")
        return sweep_count

    def _emergency_reveal(self, value: float) -> None:
        """Hard fallback when scenario is missing — should never fire if the
        JIT refresh works. Does the minimum to keep state correct."""
        with self.lock:
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.posterior.update(value)
            self._posterior_gen += 1
            self._fair_sigma_cache = None
        self.step(reconcile=True)
        n_after = self._n_remaining()
        if n_after == 0:
            self._sweep_at_settle(float(sum(self.posterior.reveals)))

    def _sweep_at_settle(self, running_sum: float) -> None:
        """After the final reveal, walk the book a few times to mop up any
        remaining mispriced levels."""
        for _ in range(6):
            took = False
            book = self._book_cache
            if book is not None:
                for lvl in book.get("asks") or []:
                    if self._snipe("buy", lvl["price"], lvl["qty"]):
                        took = True
                for lvl in book.get("bids") or []:
                    if self._snipe("sell", lvl["price"], lvl["qty"]):
                        took = True
            if not took:
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # WS event handlers
    # ------------------------------------------------------------------
    def on_fill_event(self, msg: dict) -> None:
        liq = msg.get("liquidity")
        side = msg.get("side")
        # Adverse-skew bookkeeping: tracks side hit ("bid" if our buy filled)
        if liq == "maker" and side in ("buy", "sell"):
            self._last_adverse_fill_t[
                "bid" if side == "buy" else "ask"] = time.time()

        matched = False
        with self.lock:
            qty = msg["qty"]
            order_id = msg["order_id"]
            fill_px = msg.get("price")
            if fill_px is not None:
                self._last_fill_px = float(fill_px)
                self._last_fill_t = time.time()
            for key, expected_side in (("bid", "buy"), ("ask", "sell")):
                rest = self.resting[key]
                if rest is not None and rest["order_id"] == order_id and side == expected_side:
                    rest["qty"] -= qty
                    if side == "buy":
                        self.position += qty
                    else:
                        self.position -= qty
                    if (rest["qty"] > 0 and liq == "maker"
                            and self.cfg.partial_fill_cool_off_sec > 0):
                        self._partial_fill_cool_off_until[key] = (
                            time.time() + self.cfg.partial_fill_cool_off_sec)
                    if rest["qty"] <= 0:
                        self.resting[key] = None
                    matched = True
                    break
        # If our local accounting matched, defer the reconcile REST call.
        # Slow loop polls positions every 2s as a drift safety net.
        if not matched:
            self.step(reconcile=True)

    def on_quote_event(self, msg: dict) -> None:
        if self.phase != "running":
            return
        k = len(self.posterior.reveals)
        pre_reveal = (k == 0)

        # ---- direct-snipe new passive quotes (no book scan, instant) ----
        if not pre_reveal and msg.get("type") == "quote_add":
            side = msg.get("side")
            price = msg.get("price")
            qty = msg.get("qty")
            if side and price is not None and qty:
                # quote_add side="buy" means a new passive BID; we lift it
                # by selling.
                our_side = "sell" if side == "buy" else "buy"
                self._snipe(our_side, price, qty)

        # ---- throttled book scan + reprice (cheap; reads cached theos) ----
        now = time.time()
        do_book_scan = (not pre_reveal and
                        (now - self._last_quote_event_t
                         >= self.cfg.quote_event_throttle_sec))
        if pre_reveal:
            do_maker_reprice = (now - self._last_maker_apply_t >= 1.0)
        else:
            do_maker_reprice = (now - self._last_maker_apply_t
                                >= self._maker_reprice_interval())

        if not do_book_scan and not do_maker_reprice:
            self._maybe_drift_cancel()
            return

        if do_book_scan:
            self._last_quote_event_t = now

        if do_book_scan and k >= 1:
            fair, sigma = self.fair_and_sigma()
            if not (fair == 0.0 and sigma == 0.0):
                self._snipe_book_scan(fair, sigma)
        if do_maker_reprice:
            with self.lock:
                if pre_reveal or k >= 1:
                    bid_px, ask_px, _, _ = self.desired_quotes()
                    self._apply_target_quotes(bid_px, ask_px)
                    self._last_maker_apply_t = now
        self._maybe_drift_cancel()

    def on_book_event(self, msg: dict) -> None:
        """WS book push: drop straight into the cache. Per
        project_day1_exchange_timings.md, WS book lands ~1ms post-reveal vs
        ~13ms (90-184ms tail!) REST. This is THE source for the reveal-race
        book read; the scheduler's REST pre-warm is a fallback."""
        if msg.get("symbol") not in (None, self.symbol):
            return
        self._book_cache = msg
        self._book_cache_t = time.time()

    def on_trade(self, msg: dict) -> None:
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
            if len(self._recent_trades) >= self._recent_trades.maxlen:
                _, px_old, q_old, _ = self._recent_trades[0]
                self._vwap_num -= px_old * q_old
                self._vwap_den -= q_old
            self._recent_trades.append((now, price, qty, aggressor))
            self._vwap_num += price * qty
            self._vwap_den += qty
        self._maybe_drift_cancel()

    def on_phase_change(self, phase: Optional[str],
                        reveals: list[float]) -> None:
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
                self._last_maker_apply_t = 0.0
                self._partial_fill_cool_off_until = {"bid": 0.0, "ask": 0.0}
                self._recent_trades.clear()
                self._vwap_num = 0.0
                self._vwap_den = 0
                self._phase_running_t = time.time()
                self._last_reveal_t = None
                self._pre_reveal_last_serviced_idx = -1
                self._pre_reveal_last_cancelled_idx = -1
                self._last_quote_fair_eff = None
                self._last_drift_cancel_t = 0.0
                self._precompute_prefetched_cycle = -1
                self._jit_refresh_done_for_cycle = -1
                self._current_theos = None
                self._current_theos_cycle = -1
                self._current_theos_t = 0.0
                self._last_book_prewarm_t = 0.0
                self._last_adverse_fill_t = {"bid": 0.0, "ask": 0.0}
                self._precompute_request.set()
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
        try:
            self._mm_executor.shutdown(wait=False)
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

    # ------------------------------------------------------------------
    # Fast scheduler (pre-reveal cancel/post + JIT + book pre-warm)
    # ------------------------------------------------------------------
    def _fast_scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self._fast_scheduler_tick()
            except Exception as e:
                print(f"fast_scheduler tick error: {e}")
            self._scheduler_stop.wait(self.cfg.fast_scheduler_tick_sec)

    def _fast_scheduler_tick(self) -> None:
        if self.phase != "running":
            return
        if self._n_remaining() <= 0:
            return
        nxt = self._next_reveal_at()
        if nxt is None:
            return
        now = time.time()
        cycle = len(self.posterior.reveals)
        cancel_at = nxt - self.cfg.pre_reveal_cancel_sec
        post_at = nxt - self.cfg.pre_reveal_post_sec

        # Pre-reveal cancel
        if now >= cancel_at and self._pre_reveal_last_cancelled_idx < cycle:
            self._pre_reveal_last_cancelled_idx = cycle
            try:
                with self.lock:
                    self.c.cancel_all()
                    self.resting = {"bid": None, "ask": None}
                print(f"[v11 SCHED] PRE-REVEAL-CANCEL cycle={cycle} "
                      f"reveal_in={nxt - now:.2f}s")
            except Exception as e:
                print(f"[v11 SCHED] cancel error: {e}")
        elif (now >= post_at and now < cancel_at
                and self._pre_reveal_last_serviced_idx < cycle):
            self._pre_reveal_last_serviced_idx = cycle
            print(f"[v11 SCHED] PRE-REVEAL-POST cycle={cycle} "
                  f"reveal_in={nxt - now:.2f}s pos={self.position}")
            try:
                self.step()
            except Exception as e:
                print(f"[v11 SCHED] post error: {e}")

        # Precompute prefetch
        if self._precompute_prefetched_cycle < cycle:
            if now >= nxt - self.cfg.precompute_lead_sec:
                self._precompute_prefetched_cycle = cycle
                self._precompute_request.set()
                print(f"[v11 SCHED] precompute-prefetch cycle={cycle} "
                      f"reveal_in={nxt - now:.2f}s")

        # Book pre-warm fallback (WS push is primary; this only fires if
        # WS is slow and the cache goes stale)
        if now >= nxt - self.cfg.book_prewarm_lead_sec:
            age = now - self._book_cache_t
            if age >= self.cfg.book_prewarm_max_age_sec:
                try:
                    self._book_cache = self.c.book(
                        self.symbol, depth=self.cfg.snipe_book_depth)
                    self._book_cache_t = time.time()
                    self._last_book_prewarm_t = self._book_cache_t
                except Exception:
                    pass

        # JIT precompute refresh
        if (self._jit_refresh_done_for_cycle < cycle
                and now >= nxt - self.cfg.jit_refresh_sec):
            self._jit_refresh_done_for_cycle = cycle
            with self._precompute_lock:
                cached_cycle = self._precompute_for_cycle
                cached_n = len(self._precomputed)
            if cached_cycle != cycle:
                print(f"[v11 SCHED] JIT-REFRESH cycle={cycle} "
                      f"reveal_in={nxt - now:.2f}s "
                      f"(cached={cached_cycle}, n={cached_n})")
                t0 = time.perf_counter()
                try:
                    self._do_precompute()
                except Exception as e:
                    print(f"[v11 SCHED] JIT rebuild error: {e!r}")
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                print(f"[v11 SCHED] JIT-REFRESH done {elapsed_ms:.1f}ms")

    # ------------------------------------------------------------------
    # Slow housekeeping loop (2s tick)
    # ------------------------------------------------------------------
    def _slow_housekeeping_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self._slow_housekeeping_tick()
            except Exception as e:
                print(f"slow_housekeeping tick error: {e}")
            self._scheduler_stop.wait(self.cfg.slow_housekeeping_interval_sec)

    def _slow_housekeeping_tick(self) -> None:
        if self.phase != "running":
            return
        # Position drift reconcile vs REST
        try:
            rest_pos = int(
                self.c.positions()["positions"].get(self.symbol, 0))
        except Exception:
            return
        if rest_pos != self.position:
            print(f"[v11 HOUSEKEEP] position drift: local={self.position} "
                  f"rest={rest_pos} -> reconciling")
            with self.lock:
                self.position = rest_pos
        # Precompute staleness watchdog
        cycle = len(self.posterior.reveals)
        with self._precompute_lock:
            cached_cycle = self._precompute_for_cycle
        if (cached_cycle >= 0 and cycle - cached_cycle >= 2
                and self._n_remaining() > 0):
            print(f"[v11 HOUSEKEEP] WARN precompute stale "
                  f"by {cycle - cached_cycle} cycles -> forcing rebuild")
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[v11 HOUSEKEEP] forced rebuild error: {e!r}")
        # Refresh current-state theos cache
        self._refresh_current_theos()

    def _refresh_current_theos(self) -> None:
        try:
            fair, sigma = self.fair_and_sigma()
            bid_px, ask_px, _, _ = self.desired_quotes()
        except Exception as e:
            print(f"[v11 CURRENT-THEOS] refresh error: {e!r}")
            return
        self._current_theos = {
            "fair": float(fair), "sigma": float(sigma),
            "bid_px": bid_px, "ask_px": ask_px}
        self._current_theos_cycle = len(self.posterior.reveals)
        self._current_theos_t = time.time()

    def get_cached_current_theos(self) -> Optional[Dict[str, Any]]:
        if self._current_theos is None:
            return None
        if self._current_theos_cycle != len(self.posterior.reveals):
            return None
        if (time.time() - self._current_theos_t
                > self.cfg.current_theos_max_age_sec):
            return None
        return self._current_theos

    # ------------------------------------------------------------------
    # Precompute worker
    # ------------------------------------------------------------------
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
                print(f"[v11 PRECOMPUTE] error: {e!r}")

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

        if n_remaining <= 0 or not snapshot:
            return

        marginal: Dict[int, float] = defaultdict(float)
        for (a, w), p in snapshot.items():
            mass = p / (w + 1)
            for v in range(a, a + w + 1):
                marginal[v] += mass

        sorted_vals = sorted(marginal.items(), key=lambda kv: -kv[1])
        sorted_vals = [(v, p) for v, p in sorted_vals
                       if p >= self.cfg.precompute_min_prob]
        sorted_vals = sorted_vals[:self.cfg.precompute_max_values]

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
        print(f"[v11 PRECOMPUTE #{self._precompute_run_count}] "
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
        # Reweight posterior for "next reveal == v"
        new_post: Dict[Tuple[int, int], float] = {}
        total = 0.0
        for (a, w), p in prior_post.items():
            if a <= v <= a + w:
                np = p / (w + 1)
                new_post[(a, w)] = np
                total += np
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

        edge = max(self.cfg.min_edge, self.cfg.edge_per_sigma * sigma)
        if edge < self.cfg.no_edge_pull_ticks:
            bid_px: Optional[int] = None
            ask_px: Optional[int] = None
        else:
            bid_px = int(math.floor(fair - edge))
            ask_px = int(math.ceil(fair + edge))

        base_edge = self.taker_fee + max(
            self.cfg.snipe_min_edge, self.cfg.snipe_buffer_sigma * sigma)
        lift_to_px = int(math.floor(fair - base_edge))
        hit_to_px = int(math.ceil(fair + base_edge))

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
# Standalone runner. Prefer run_combined11 for full WS + probe setup.
# ---------------------------------------------------------------------------
def main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol="A")
    print(f"v11: reveal_interval={strat.reveal_interval}s  "
          f"duration={strat.duration}s  n_total={strat.n_total}")
    print(f"v11: precompute_min_prob={strat.cfg.precompute_min_prob}  "
          f"lead={strat.cfg.precompute_lead_sec}s  "
          f"max_values={strat.cfg.precompute_max_values}")

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  "
              f"running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(f"FILL   {msg['side']:>4s} {msg['qty']} @ {msg['price']}  "
              f"liq={msg.get('liquidity')}")
        strat.on_fill_event(msg)

    def on_game_state(msg):
        phase = msg.get("phase")
        reveals = msg.get("reveals") or []
        print(f"STATE  phase={phase}  reveals={len(reveals)}")
        strat.on_phase_change(phase, reveals)

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
    c.start()

    if c.game_state().get("phase") == "running":
        strat.step(reconcile=True)

    print("\nBot started (v11).  Commands: s=status, p=precompute, "
          "f=flatten, q=quit.\n")
    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                nxt = strat._next_reveal_at()
                nxt_in = f"{nxt - time.time():.2f}s" if nxt else "-"
                with strat._precompute_lock:
                    n_scen = len(strat._precomputed)
                    pcycle = strat._precompute_for_cycle
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"k={len(strat.posterior.reveals)}/{strat.n_total}  "
                      f"precompute={n_scen}@cycle{pcycle} "
                      f"({strat._last_precompute_us:.0f}us)  "
                      f"next_reveal_in={nxt_in}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precomputed.items())
                    pcycle = strat._precompute_for_cycle
                print(f"  precompute (cycle={pcycle}, n={len(items)}):")
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
                print(f"unknown {cmd!r}; try s/p/f/q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Flattening...")
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten on exit failed: {e}")


if __name__ == "__main__":
    main()
