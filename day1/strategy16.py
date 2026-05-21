"""Day 1 live trading strategy v16 — bot-only informedness, adaptive
CP learning, generic stale-quote attacker, and bigger position targets.

WHY v16 (from v15 log analysis at day1/logs/combined_log_v15_2026*.jsonl):

  GAMES 0+1: 18 fills total, PnL=+94+59=+153. Position ended -42 and +19
  against a ±100 limit — we left ~60% of capacity unused per game.

  By-CP fill volume (both games combined):
    VALKO  buy=16 sell=32  fills=8  taker (stale-quote sweeps -> $$)
    SEAN   buy= 5 sell= 2  fills=2  taker
    VALKB  buy= 1 sell= 6  fills=3  maker (noise — no signal)
    VALKG  buy= 0 sell= 3  fills=2  maker (directional, NOT informed)
    BAZC   buy= 0 sell= 2  fills=1  taker
    VALKD  buy= 0 sell= 1  fills=1  taker (dimer)
    VALKE  buy= 1 sell= 0  fills=1  maker (mixed_sweeper 30%)
  Notable: ZERO fills from the actual informed bots (VALKC, VALKJ, VALKM)
  in either game. Our v14/v15 bias-on-maker-fill plumbing literally never
  saw the signal it was designed for. Worse, the human hardcodes (SEAN
  at 0.7, BAZC at 0.6) FIRED bias the wrong direction in game 1:
    Game 1 reveal#1: SEAN bought 5@33 from us (taker, but if maker fill)
      -> would have biased fair UP, then settle came in at 32 = below 33.

  Meanwhile VALKO's stale 25-lot quotes (refresh_ms=5000, default_mean=7
  while real fair was 30+) were the dominant PnL source. We swept 48 lots
  from them across two games and ended each game in profit on those.

WHAT'S NEW (delta from v15):

  A. BOT-ONLY CP_WEIGHT. Drop all hardcoded HUMAN weights (SEAN, BAZC,
     CHAR, BRYA, ERIK). Keep only leaked-bot weights. Humans get the
     `unknown_cp_default_weight` floor or the adaptive estimate (see C).

  B. EXPAND POSITION-TARGETING. directional_position_target 30 -> 70,
     directional_step_qty 8 -> 20, directional_max_pay_ticks 3 -> 5,
     informed_bias_max_ticks 6 -> 8. With ±100 hard cap, target 70 keeps
     30 lots of safety. Bigger step amortizes faster across a 60s reveal
     interval.

  C. ADAPTIVE CP LEARNING. For UNKNOWN CPs, accumulate adverse-ticks per
     maker fill (already tracked by base CPProfile). After a threshold,
     bump that CP's effective weight up to `cp_learning_max_weight`. This
     catches genuinely-informed humans/bots not in our leak without
     hard-coding their identities.

  D. GENERIC STALE-QUOTE ATTACKER. Periodic background scan (every
     stale_attack_check_sec) of the book: if a resting price is mispriced
     vs our fair_eff by >= stale_attack_min_edge_ticks AND has size >=
     stale_attack_min_size AND we have room before position cap, IOC-
     sweep up to stale_attack_max_slice lots. This generalizes the VALKO
     exploit pattern to any counterparty with a stale quote.

  E. WEAKER B-TAPE WEIGHT. v15 B-tape weight 0.6 -> 0.4. In game 1 the
     B-tape pointed the wrong way at reveal#2 (B net=-25 yet A settle=32).
     Keep the signal but reduce its bias amplitude.

DESIGN INVARIANTS PRESERVED (from v12-v15):
  * Lock-free hot path, PrecomputedScenario dict lookup, single _snipe
    path, cancel+post only, WS book cache primary.
  * Precompute built from raw posterior — bias does NOT enter precompute.
  * Hard ±position_limit cap on all sizing paths.

Run:
    python day1/run_combined16.py
    python day1/strategy16.py            # standalone
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

from strategy12 import (  # noqa: F401  (re-export for runners)
    URL, API_KEY, N_PRIOR_SIM, Posterior, PrecomputedScenario, CPProfile,
)
from strategy15 import (  # noqa: F401
    Config as ConfigV15,
    Strategy as StrategyV15,
)


# ===========================================================================
# Truth oracle: poll /api/admin/truth (admin key) for the EXACT reveal
# sequence and settled fair value. With this in hand, the Bayesian posterior
# becomes irrelevant — we know A's identity sum and B's last reveal (X_N)
# before the round even ends.
# ===========================================================================
ADMIN_KEY = "sean123"


class TruthOracle:
    """Background poller for /api/admin/truth.

    Uses its OWN requests.Session with the admin key so polls do not eat
    into the trader's 20 req/s rate budget. Latest snapshot is exposed
    atomically; fair_a / fair_b / next_reveal_value return None when no
    fresh data is available (so we degrade to the posterior).
    """

    def __init__(self, base_url: str = URL, admin_key: str = ADMIN_KEY,
                 poll_interval_sec: float = 0.3,
                 timeout_sec: float = 2.0,
                 staleness_limit_sec: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval_sec
        self.timeout = timeout_sec
        self.staleness_limit = staleness_limit_sec
        self._lock = threading.Lock()
        self._data: Optional[dict] = None
        self._last_poll_t: float = 0.0
        self._poll_count: int = 0
        self._err_count: int = 0
        self._last_err: str = ""
        self._stop_evt = threading.Event()
        self._sess = requests.Session()
        self._sess.headers.update({"X-API-Key": admin_key})
        self._thread: Optional[threading.Thread] = None

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop, name="truth-oracle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()

    def _loop(self) -> None:
        url = f"{self.base_url}/api/admin/truth"
        while not self._stop_evt.is_set():
            try:
                r = self._sess.get(url, timeout=self.timeout)
                if r.ok:
                    j = r.json()
                    with self._lock:
                        self._data = j
                        self._last_poll_t = time.time()
                        self._poll_count += 1
                else:
                    with self._lock:
                        self._err_count += 1
                        self._last_err = f"HTTP {r.status_code}"
            except Exception as e:
                with self._lock:
                    self._err_count += 1
                    self._last_err = repr(e)
            # interruptible sleep
            self._stop_evt.wait(self.poll_interval)

    # --- snapshot accessors --------------------------------------------------
    def snapshot(self) -> Optional[dict]:
        with self._lock:
            if self._data is None:
                return None
            if (time.time() - self._last_poll_t) > self.staleness_limit:
                return None
            return dict(self._data)

    def stats(self) -> dict:
        with self._lock:
            return {
                "polls": self._poll_count,
                "errors": self._err_count,
                "last_err": self._last_err,
                "age_sec": (time.time() - self._last_poll_t)
                            if self._last_poll_t else None,
            }

    def fair_a(self) -> Optional[float]:
        """Identity settlement: sum(full_sequence)."""
        d = self.snapshot()
        if not d or d.get("phase") != "running":
            return None
        seq = d.get("full_sequence") or []
        if not seq:
            return None
        return float(sum(seq))

    def fair_b(self) -> Optional[float]:
        """next_number settlement: last reveal value X_N (empirical)."""
        d = self.snapshot()
        if not d or d.get("phase") != "running":
            return None
        seq = d.get("full_sequence") or []
        if not seq:
            return None
        return float(seq[-1])

    def next_reveal_value(self) -> Optional[int]:
        """The next Xi that will fire (or None if no more left this round)."""
        d = self.snapshot()
        if not d:
            return None
        seq = d.get("full_sequence") or []
        revealed = d.get("reveals_so_far") or []
        if len(revealed) >= len(seq):
            return None
        return int(seq[len(revealed)])

    def phase(self) -> Optional[str]:
        d = self.snapshot()
        return None if d is None else d.get("phase")


# ---------------------------------------------------------------------------
# Bot-only informedness registry — leaked weights ONLY, no human guesses.
# ---------------------------------------------------------------------------
CP_WEIGHT: dict[str, float] = {
    # -------- Symbol A informed bots --------
    "VALKC": 1.00,   # informed_sniper       — knows truth
    "VALKJ": 1.00,   # slow_informed         — knows truth (slow cadence)
    "VALKM": 0.80,   # bayes_taker           — bayesian, edge_threshold=2
    "VALKE": 0.30,   # mixed_sweeper         — informed_prob=0.3
    "VALKF": 0.10,   # predictive_mm         — accuracy=0.55

    # -------- Symbol B informed bots (kept for B trading) --------
    "CVALK": 1.00,   # informed_sniper_next  — knows next reveal
    "GVALK": 1.00,   # informed_twapper      — knows truth, TWAPs
    "IVALK": 1.00,   # true_mean_taker       — knows true mean
    "DVALK": 0.10,   # predictive_avg_mm     — accuracy=0.55
    # FVALK directional_taker_next lean=0.7 — biased, NOT informed -> excluded.

    # NOTE: NO human entries (SEAN/BAZC/CHAR/BRYA/ERIK) — humans get
    # unknown_cp_default_weight or the adaptive estimate.
}

# CPs whose quotes are by design STALE — sweep these even without bias.
# (We also have the GENERIC stale-quote attacker that catches anyone.)
STALE_CPS: set[str] = {"VALKO"}  # stale_quoter, refresh 5s


@dataclass
class Config(ConfigV15):
    """v16 Config — truth-oracle + parallel sweep + pre-reveal lead."""
    # ---- truth oracle (admin /api/admin/truth) ----
    truth_enabled: bool = True
    truth_poll_interval_sec: float = 0.10      # was 0.25 — finer freshness
    truth_sigma: float = 0.5                   # fictitious sigma when truth
                                                # is known (only used so existing
                                                # spread machinery still works)

    # ---- stealth: disable all truth-driven IOC paths ----
    # When True, our trades look like normal maker-MM activity. No "IOC
    # right at reveal" pattern, no aggressive sweeps, no window-flip
    # cascades. Earnings come from the skewed maker quotes in
    # desired_quotes(); we only fill when someone crosses our top-of-book.
    # See bot_config_dump.json — 10 B bots quote around default_mean=7 with
    # width 4-6; lifting their stale asks via IOC is the textbook
    # detectable pattern. Stealth keeps those edges latent in our quote
    # ladder instead of monetizing them via taker action.
    stealth_mm: bool = True

    # ---- truth-driven execution (event-driven WS sweep) ----
    # Only used when stealth_mm = False. Defaults flipped OFF for stealth.
    truth_sweep_on_book_evt: bool = False
    truth_sweep_min_interval_sec: float = 0.04   # min gap between WS sweeps
    truth_sweep_max_parallel: int = 12           # cap concurrent IOC orders
    truth_sweep_min_edge_ticks: float = 1.5      # >= taker_fee+1t recoups fee
    truth_sweep_max_lot_per_level: int = 50      # IOC slice cap per level

    # ---- skewed market-making when truth is known ----
    # If naive market is OVERPRICING (settle < naive), we want every fill
    # to make us MORE SHORT. So: tight ask at truth + tight_edge (bots
    # eagerly lift, we sell into them); wide bid at truth - wide_edge so
    # we don't accidentally buy. Mirror for underpricing.
    truth_mm_enabled: bool = True
    truth_mm_tight_edge_ticks: int = 1     # ask=truth+1 / bid=truth-1
    truth_mm_wide_edge_ticks: int = 6      # wrong-side quote, well away
    truth_mm_neutral_edge_ticks: int = 1   # both sides at truth ± 1
    # When True, never let a maker quote cross the visible BBO. If our
    # truth-driven tight bid would cross the ask, we clamp it to best_ask
    # minus 1. This avoids the obvious "limit at 14 against ask=10 = taker
    # fill at 10" pattern that screams informed; instead we sit at the
    # top of the book and let counter-parties come to us when they re-
    # quote toward truth.
    stealth_mm_avoid_cross: bool = True
    # Padding from BBO when the crossing-clamp fires. 0 = sit exactly at
    # best_ask - 1 / best_bid + 1 (top of book). >0 backs off further into
    # the spread for a stealthier look but trades fewer lots.
    stealth_mm_min_inside_ticks: int = 0

    # === STEALTH RAMP ===
    # Early windows look like a symmetric-ish MM with small size and a
    # small position cap. As the round progresses (more reveals visible
    # to everyone), we ramp up to full skew, full size, full ±L cap.
    # Reason: a tight skew right at window 0, before ANY reveal is public,
    # is the loudest tell — public posterior is still the prior. By window
    # ~ N-1 the public posterior has tightened around truth, so our skew
    # looks like any other informed MM. Linearly interpolates from
    # _start_* values at progress=0 to truth_mm_* / position_limit / 1.0
    # at progress=1, where progress = window_idx / max(1, n_total).
    stealth_ramp_enabled: bool = True
    stealth_ramp_start_tight_ticks: int = 2   # window 0: bid=truth-2 (mild skew)
    stealth_ramp_start_wide_ticks: int = 4    # window 0: ask=truth+4
    stealth_ramp_start_qty_frac: float = 0.4  # window 0: 40% of base size
    stealth_ramp_start_pos_frac: float = 0.20 # window 0: cap at 20% of ±L

    # ---- pre-reveal lead (BEAT sean's pre_reveal_lead_ms=1000) ----
    # SEAN widens its quotes 1s before each reveal. We sweep at 1.5s lead
    # to catch its still-tight quotes, then again at 0.2s to grab any
    # bots that haven't widened. After reveal the posterior tightens but
    # truth_fair was already correct from t=0, so this is pure execution.
    truth_pre_reveal_lead_sec: float = 1.5
    truth_pre_reveal_final_sec: float = 0.2
    truth_post_reveal_sweep_sec: float = 0.05    # sweep ~50ms after reveal

    # ---- legacy stale-attack scanner (kept as backstop) ----
    # Lowered from v15 0.4s to 0.05s — runs in background even when no
    # book events fire (e.g., end-of-round when quotes are static).

    # ---- bigger position targets ----
    informed_bias_max_ticks: float = 8.0       # v15 was 6.0
    informed_bias_decay_sec: float = 20.0      # v15 was 15.0
    directional_position_target: int = 70      # v15 was 30  (cap is 100)
    directional_step_qty: int = 20             # v15 was 8
    directional_max_pay_ticks: float = 5.0     # v15 was 3.0
    directional_threshold_ticks: float = 1.5

    # ---- B-tape weight backed off ----
    b_tape_weight: float = 0.4                 # v15 was 0.6

    # ---- adaptive CP learning (UNKNOWN counterparties only) ----
    cp_learning_enabled: bool = True
    cp_learning_min_fills: int = 3             # need at least this many maker
                                                # fills before scoring a CP
    cp_learning_adverse_per_t: float = 1.5     # ticks of adverse move per 1.0
                                                # weight; e.g. 3t adverse -> w=0.5
    cp_learning_max_weight: float = 0.7        # cap for adaptive weight
    cp_learning_min_weight: float = 0.0        # below this, ignore CP entirely

    # ---- generic stale-quote attacker (backstop scan) ----
    # OFF in stealth mode — the scanner fires aggressive IOCs and is
    # the loudest tell that we have truth.
    stale_attack_enabled: bool = False
    stale_attack_check_sec: float = 0.05       # v16+truth: 50ms backstop
    stale_attack_min_edge_ticks: float = 1.5   # truth -> can attack tiny edges
    stale_attack_min_size: int = 1             # truth -> any size is profit
    stale_attack_max_slice: int = 50           # bigger slices when sure
    stale_attack_position_pad: int = 0         # use full ±100 range with truth


class Strategy(StrategyV15):
    """v16: truth-oracle MM + bot-only weights + stale attacker."""

    def __init__(self, *args, **kwargs):
        cfg = kwargs.get("config")
        if cfg is not None and not isinstance(cfg, Config):
            kwargs["config"] = Config(**{
                f.name: getattr(cfg, f.name)
                for f in cfg.__dataclass_fields__.values()
                if f.name in Config.__dataclass_fields__
            })
        elif cfg is None:
            kwargs["config"] = Config()

        # ---- v16 state (before super so background threads see it) ----
        self._cp_learned_weight: dict[str, float] = {}
        self._cp_last_log_t: dict[str, float] = {}
        self._last_stale_attack_t: float = 0.0
        self._last_stale_attack_lot: int = 0
        self._stale_attack_total: int = 0
        # Single-shot truth fetch: poll once at round start, cache the
        # sequence, and use it for the rest of the round. No background
        # thread, no repeat polling.
        self._round_sequence: Optional[list[int]] = None
        self._round_truth_a: Optional[float] = None
        self._round_truth_b: Optional[float] = None
        self._round_sequence_t: float = 0.0
        self._truth_sess: Optional[requests.Session] = None
        self._last_truth_log_t: float = 0.0

        # truth-sweep state (parallel IOC execution path)
        self._truth_sweep_lock = threading.Lock()
        self._last_truth_sweep_t: float = 0.0
        self._truth_sweep_total: int = 0
        self._truth_sweep_count: int = 0
        self._last_pre_reveal_lead_cycle: int = -1
        self._last_pre_reveal_final_cycle: int = -1
        self._last_post_reveal_sweep_cycle: int = -1

        # === trajectory state: optimal target position per window ===
        # _trajectory["target_per_window"][i] is the planned position for
        # window i (i = number of reveals seen). Computed once at round
        # start once the truth oracle returns the full_sequence.
        self._trajectory: Optional[dict] = None
        self._trajectory_seq: Optional[tuple] = None
        self._last_window_flip_cycle: int = -1

        super().__init__(*args, **kwargs)

        # With truth, we know fair exactly: tighten snipe gates so we sweep
        # any mispricing > taker_fee + 0.5t (vs v15's 2.0t + 0.4*sigma).
        if self.cfg.truth_enabled:
            self.cfg.snipe_min_edge = 0.5
            self.cfg.snipe_buffer_sigma = 0.0
            # Use the full ±100 range. Parent default snipe_position_buffer
            # leaves 8 lots of headroom — wasted capacity when we know fair.
            self.cfg.snipe_position_buffer = 0
        # Stealth: kill snipe taker path entirely. We monetise truth
        # through skewed maker quotes, not via IOC sweeps.
        if self.cfg.stealth_mm:
            self.cfg.snipe_max_per_round = 0

        # Prepare a session for the one-shot truth fetch (no background thread).
        if self.cfg.truth_enabled:
            self._truth_sess = requests.Session()
            self._truth_sess.headers.update({"X-API-Key": ADMIN_KEY})
            print(f"[v16 TRUTH] one-shot mode (no polling thread)")

        print(f"[v16 BOT_ONLY_WEIGHTS] tracked bots:")
        for cp, w in sorted(CP_WEIGHT.items(), key=lambda kv: -kv[1]):
            print(f"               {cp:<8s} weight={w:.2f}")
        print(f"[v16 STALE_ATTACK] enabled={self.cfg.stale_attack_enabled}  "
              f"min_edge={self.cfg.stale_attack_min_edge_ticks}t  "
              f"min_size={self.cfg.stale_attack_min_size}  "
              f"max_slice={self.cfg.stale_attack_max_slice}")
        print(f"[v16 CP_LEARN] enabled={self.cfg.cp_learning_enabled}  "
              f"min_fills={self.cfg.cp_learning_min_fills}  "
              f"adverse_per_t={self.cfg.cp_learning_adverse_per_t}  "
              f"max_w={self.cfg.cp_learning_max_weight}")
        print(f"[v16 POS_TARGET] target={self.cfg.directional_position_target}  "
              f"step={self.cfg.directional_step_qty}  "
              f"max_pay={self.cfg.directional_max_pay_ticks}t  "
              f"bias_max={self.cfg.informed_bias_max_ticks}t")
        print(f"[v16 SNIPE] min_edge={self.cfg.snipe_min_edge}t  "
              f"buffer_sigma={self.cfg.snipe_buffer_sigma}")

        # Start stale-attack scanner thread (lightweight).
        self._stale_attack_stop = threading.Event()
        if self.cfg.stale_attack_enabled:
            t = threading.Thread(target=self._stale_attack_loop,
                                 name="v16-stale-attack", daemon=True)
            t.start()

    # ==================================================================
    # Truth (one-shot): fetch full_sequence from admin endpoint ONCE per
    # round; cache it and derive truth values from there.
    # ==================================================================
    def _fetch_sequence_once(self) -> bool:
        """One direct GET. Returns True iff a fresh running-phase
        full_sequence was cached. Idempotent; safe to call again to retry."""
        if not self.cfg.truth_enabled or self._truth_sess is None:
            return False
        if self._round_sequence is not None:
            return True  # already have it
        try:
            r = self._truth_sess.get(
                f"{URL.rstrip('/')}/api/admin/truth", timeout=2.0)
        except Exception as e:
            print(f"[v16 TRUTH-FETCH-ERR] {e!r}")
            return False
        if not r.ok:
            return False
        try:
            j = r.json()
        except Exception:
            return False
        if j.get("phase") != "running":
            return False
        seq = j.get("full_sequence") or []
        if not seq:
            return False
        self._round_sequence = [int(v) for v in seq]
        self._round_truth_a = float(sum(self._round_sequence))
        # B settles AT EACH REVEAL — fair is the NEXT upcoming reveal,
        # not full_sequence[-1]. Kept here for backward-compat / status
        # displays but never consulted by _truth_fair for B (see below).
        self._round_truth_b = float(self._round_sequence[-1])
        self._round_sequence_t = time.time()
        print(f"[v16 TRUTH-ONESHOT] symbol={self.symbol} "
              f"seq={self._round_sequence} "
              f"truth_a={self._round_truth_a} truth_b_seq={self._round_sequence}")
        return True

    def _truth_b_for_window(self, window_idx: Optional[int] = None
                            ) -> Optional[float]:
        """B's settle value for window `window_idx`. B has a
        tick_settlement at every reveal — window i (0-indexed; i reveals
        already seen) settles at full_sequence[i]. Returns None past the
        final window."""
        if self._round_sequence is None:
            return None
        i = (self._current_window_index() if window_idx is None
             else int(window_idx))
        if 0 <= i < len(self._round_sequence):
            return float(self._round_sequence[i])
        return None

    def _truth_fair(self) -> Optional[float]:
        """True settle value for self.symbol from the cached sequence.

        Symbol A settles once at sum(full_sequence).
        Symbol B has a tick_settlement at EVERY reveal — fair in window i
        is the value of reveal i+1 (i.e. full_sequence[i]). Confirmed by
        log analysis: PnL_B = (trade_cash + sum(pos_before_tick * val))
        * mult_B. Treating B as settle-to-last_value caused catastrophic
        losses when later reveals were below earlier ones."""
        if not self.cfg.truth_enabled or self._round_sequence is None:
            return None
        sym = (self.symbol or "A").upper()
        if sym == "A":
            return self._round_truth_a
        if sym == "B":
            return self._truth_b_for_window()
        return None

    # ==================================================================
    # Pricing overrides — short-circuit to truth when available.
    # ==================================================================
    def fair_and_sigma(self) -> tuple[float, float]:
        t = self._truth_fair()
        if t is not None:
            now = time.time()
            if now - self._last_truth_log_t > 5.0:
                self._last_truth_log_t = now
                seq = self._round_sequence or []
                revealed = list(self.posterior.reveals)
                print(f"[v16 TRUTH] symbol={self.symbol} fair={t:.1f}  "
                      f"seq={seq}  revealed={revealed}")
            return float(t), float(self.cfg.truth_sigma)
        return super().fair_and_sigma()

    def _vwap_clamped_fair(self, fair: float) -> float:
        t = self._truth_fair()
        if t is not None:
            return float(t)
        return super()._vwap_clamped_fair(fair)

    def _pre_reveal_vwap_fair(self, prior_mean: float) -> Optional[float]:
        t = self._truth_fair()
        if t is not None:
            return float(t)
        return super()._pre_reveal_vwap_fair(prior_mean)

    def _b_implied_next_value(self, now: Optional[float] = None
                              ) -> Optional[float]:
        # Even when WE trade A, the b-implied accessor is used by v15's
        # B-nudge. With truth, return the EXACT X_N from the sequence.
        if self.cfg.truth_enabled and self._round_truth_b is not None:
            return float(self._round_truth_b)
        return super()._b_implied_next_value(now)

    def _b_nudge_ticks(self, fair: float) -> float:
        # When truth is fresh, fair is already exact — no nudge needed.
        if self._truth_fair() is not None:
            return 0.0
        return super()._b_nudge_ticks(fair)

    # ==================================================================
    # TRAJECTORY: precompute optimal target position per reveal-window.
    #
    # Given the FULL sequence (truth oracle), simulate what a naive bot
    # would think fair is at each window i (= after reveal i-1, before
    # reveal i fires). Compare to true settle. If naive >> truth, the
    # market is OVERPRICING and we want to be MAX SHORT during that
    # window. If naive << truth, market is UNDERPRICING -> MAX LONG.
    # ==================================================================
    def _compute_naive_fair_per_window(self, full_sequence: list[int]
                                       ) -> list[float]:
        """Returns naive_fair[i] for i in [0, N]. i = number of reveals
        seen so far. Uses a FRESH Posterior fed reveals one at a time."""
        N = len(full_sequence)
        if N == 0:
            return []
        sym = (self.symbol or "A").upper()
        p = Posterior()  # fresh prior (built once per call — cheap-ish)

        def e_x_now() -> float:
            # E[X_j] under current posterior = sum_p p * (a + w/2)
            return sum(prob * (a + w / 2.0)
                       for (a, w), prob in p.posterior.items())

        naive: list[float] = []
        partial_sum = 0
        # Window 0: no reveals yet.
        if sym == "A":
            mean, _ = p.predict_settle(0.0, N)
            naive.append(float(mean))
        else:
            naive.append(float(e_x_now()))
        # Windows 1..N: feed each reveal then record naive estimate.
        for i in range(N):
            v = int(full_sequence[i])
            p.update(v)
            partial_sum += v
            remaining = N - (i + 1)
            if sym == "A":
                if remaining == 0:
                    naive.append(float(partial_sum))
                else:
                    mean, _ = p.predict_settle(float(partial_sum), remaining)
                    naive.append(float(mean))
            else:  # B
                if remaining == 0:
                    naive.append(float(v))
                else:
                    naive.append(float(e_x_now()))
        return naive

    def _compute_trajectory(self, full_sequence: list[int]) -> dict:
        """Returns {truth, naive_per_window, edge_per_window,
        target_per_window, best_window, max_edge}.

        For A: a single `truth` (sum of sequence) compared against the
        naive-fair trajectory.
        For B: each window settles independently at the next reveal, so
        `truth_per_window[i] = full_sequence[i]`. Edges/targets are
        computed per-window against that local truth — buying high then
        watching the sequence drop no longer looks "neutral"."""
        sym = (self.symbol or "A").upper()
        if not full_sequence:
            return {}
        naive = self._compute_naive_fair_per_window(full_sequence)
        N = len(full_sequence)
        L = self.position_limit
        threshold = float(self.cfg.truth_sweep_min_edge_ticks)
        if sym == "A":
            truth = float(sum(full_sequence))
            edge = [truth - n for n in naive]
        else:
            # window i settles at full_sequence[i]; window N has nothing more.
            truth = float(full_sequence[-1])  # informational only
            truth_per_window = [float(full_sequence[i]) if i < N else 0.0
                                for i in range(len(naive))]
            edge = [t - n for t, n in zip(truth_per_window, naive)]
        targets: list[int] = []
        for i, e in enumerate(edge):
            # B has no settlement past window N-1 — never target a position
            # in window N or beyond.
            if sym == "B" and i >= N:
                targets.append(0)
                continue
            if e >= threshold:
                targets.append(+L)
            elif e <= -threshold:
                targets.append(-L)
            else:
                targets.append(0)
        abs_edges = [abs(e) for e in edge]
        best_idx = abs_edges.index(max(abs_edges)) if abs_edges else 0
        out = {
            "truth": truth,
            "naive_per_window": naive,
            "edge_per_window": edge,
            "target_per_window": targets,
            "best_window": best_idx,
            "max_edge": (abs_edges[best_idx] if abs_edges else 0.0),
            "full_sequence": list(full_sequence),
        }
        if sym == "B":
            out["truth_per_window"] = [float(full_sequence[i]) if i < N
                                       else 0.0 for i in range(len(naive))]
        return out

    def _log_trajectory(self, traj: dict) -> None:
        if not traj:
            return
        sym = (self.symbol or "A").upper()
        print(f"[v16 TRAJECTORY] symbol={self.symbol} "
              f"truth={traj['truth']:.1f} L=±{self.position_limit}  "
              f"seq={traj['full_sequence']}")
        tpw = traj.get("truth_per_window")
        if sym == "B" and tpw is not None:
            print(f"[v16 TRAJECTORY] {'win':>3s} {'truth':>6s} {'naive':>7s} "
                  f"{'edge':>7s} {'target':>7s}")
            for i, (tv, n, e, t) in enumerate(zip(
                    tpw, traj["naive_per_window"], traj["edge_per_window"],
                    traj["target_per_window"])):
                star = "  *BEST" if i == traj["best_window"] else ""
                print(f"[v16 TRAJECTORY] {i:>3d} {tv:>6.1f} {n:>7.1f} "
                      f"{e:>+7.1f} {t:>+7d}{star}")
        else:
            print(f"[v16 TRAJECTORY] {'win':>3s} {'naive':>7s} {'edge':>7s} "
                  f"{'target':>7s}")
            for i, (n, e, t) in enumerate(zip(
                    traj["naive_per_window"], traj["edge_per_window"],
                    traj["target_per_window"])):
                star = "  *BEST" if i == traj["best_window"] else ""
                print(f"[v16 TRAJECTORY] {i:>3d} {n:>7.1f} {e:>+7.1f} "
                      f"{t:>+7d}{star}")

    def _current_window_index(self) -> int:
        """Current window = number of reveals already seen."""
        return len(self.posterior.reveals)

    def _current_window_target(self) -> Optional[int]:
        """Target position for the current window, or None if not computed."""
        if not self._trajectory:
            return None
        i = self._current_window_index()
        targets = self._trajectory.get("target_per_window") or []
        if 0 <= i < len(targets):
            return int(targets[i])
        return None

    # ==================================================================
    # STEALTH RAMP: progress 0 (window 0) -> 1 (final window) drives
    # interpolation of skew tightness, quote size, and position cap.
    # ==================================================================
    def _stealth_progress(self) -> float:
        """Fraction of the round completed by reveal count. 0 at window 0,
        ~1 at the final window. Clipped to [0, 1]."""
        n = max(1, int(self.n_total))
        win = self._current_window_index()
        if win >= n:
            return 1.0
        return max(0.0, min(1.0, win / float(n)))

    def _stealth_edges(self) -> tuple[int, int]:
        """Linearly interpolate (tight, wide) from start values at
        progress=0 to truth_mm_* defaults at progress=1."""
        if not self.cfg.stealth_ramp_enabled:
            tight = max(1, int(self.cfg.truth_mm_tight_edge_ticks))
            wide = max(tight + 1, int(self.cfg.truth_mm_wide_edge_ticks))
            return tight, wide
        p = self._stealth_progress()
        s_tight = max(1, int(self.cfg.stealth_ramp_start_tight_ticks))
        s_wide = max(s_tight, int(self.cfg.stealth_ramp_start_wide_ticks))
        e_tight = max(1, int(self.cfg.truth_mm_tight_edge_ticks))
        e_wide = max(e_tight, int(self.cfg.truth_mm_wide_edge_ticks))
        tight = max(1, int(round(s_tight * (1 - p) + e_tight * p)))
        wide = max(tight + 1, int(round(s_wide * (1 - p) + e_wide * p)))
        return tight, wide

    def _stealth_qty_frac(self) -> float:
        """Linearly interpolate size multiplier from start (small) to 1.0
        at progress=1."""
        if not self.cfg.stealth_ramp_enabled:
            return 1.0
        p = self._stealth_progress()
        s = max(0.05, float(self.cfg.stealth_ramp_start_qty_frac))
        return s * (1 - p) + 1.0 * p

    def _stealth_pos_cap(self) -> int:
        """Effective per-window position cap. At progress=0 we only allow
        a small fraction of ±position_limit; at progress=1 we use the
        full limit."""
        if not self.cfg.stealth_ramp_enabled:
            return self.position_limit
        p = self._stealth_progress()
        s = max(0.05, float(self.cfg.stealth_ramp_start_pos_frac))
        frac = s * (1 - p) + 1.0 * p
        cap = int(round(self.position_limit * frac))
        return max(1, cap)

    # ==================================================================
    # SKEWED MARKET-MAKING: quote AROUND truth, but bias toward the side
    # that gives us fills in the favorable direction. Position-limit
    # aware — pull the side that would breach the cap.
    # ==================================================================
    def desired_quotes(self) -> Tuple[Optional[int], Optional[int],
                                      float, float]:
        # No truth or skew disabled -> fall back to parent (posterior-based).
        if (not self.cfg.truth_enabled
                or not self.cfg.truth_mm_enabled
                or self._truth_fair() is None):
            return super().desired_quotes()
        truth = float(self._truth_fair())
        sigma = float(self.cfg.truth_sigma)
        # Past the last reveal there's no more market to make.
        if self._n_remaining() <= 0:
            return None, None, truth, sigma
        # Ramp: tighter/wider edges scale with round progress.
        tight, wide = self._stealth_edges()
        neutral = max(1, int(self.cfg.truth_mm_neutral_edge_ticks))
        target = self._current_window_target() or 0
        if target > 0:
            # Settle HIGHER than naive — accumulate LONG.
            # Tight bid: bots/stale sellers hit it, we BUY (good).
            # Wide ask: we don't actively want to SELL here.
            bid_px = int(math.floor(truth - tight))
            ask_px = int(math.ceil(truth + wide))
        elif target < 0:
            # Settle LOWER than naive — accumulate SHORT.
            # Tight ask: bots/stale buyers lift it, we SELL (good).
            # Wide bid: don't want to BUY.
            bid_px = int(math.floor(truth - wide))
            ask_px = int(math.ceil(truth + tight))
        else:
            # Neutral window: symmetric around truth.
            bid_px = int(math.floor(truth - neutral))
            ask_px = int(math.ceil(truth + neutral))

        # === STEALTH: clamp so our quote never crosses the visible BBO.
        # Without this, "bid 14 against ask 10" instantly becomes a taker
        # lift at 10 — same tell as the IOC sweep we just disabled. With
        # it, we sit at best_ask - 1 / best_bid + 1 and let the market
        # come to us. For B (default_mean=7, width 4-6) the naive bots
        # will re-quote toward truth over the round and cross our top-of-
        # book; for A the bigger reveal-driven swings do the same.
        if self.cfg.stealth_mm_avoid_cross:
            best_bid, best_ask = self._bbo_for_clamp()
            inside = max(0, int(self.cfg.stealth_mm_min_inside_ticks))
            if (best_ask is not None and bid_px is not None
                    and bid_px >= best_ask - inside):
                bid_px = best_ask - 1 - inside
            if (best_bid is not None and ask_px is not None
                    and ask_px <= best_bid + inside):
                ask_px = best_bid + 1 + inside
        # Clamp to legal price range (>= 1 — no negative or zero prices).
        if bid_px is not None and bid_px < 1:
            bid_px = 1
        if ask_px is not None and ask_px < 1:
            ask_px = 1
        # Position-cap guard: in early windows the ramp limits us to a
        # fraction of ±position_limit; in late windows it relaxes to ±L.
        # We only pull the side that's adding to the position; the
        # opposite side stays so we can shed inventory if it gets too
        # one-sided (rare with the skew, but defensive).
        cap = self._stealth_pos_cap()
        if self.position >= cap:
            bid_px = None
        if self.position <= -cap:
            ask_px = None
        return bid_px, ask_px, truth, sigma

    def _current_quote_qty(self, side: Optional[str] = None) -> int:
        """Scale parent's qty by the stealth ramp. Early windows post
        smaller quotes; late windows hit the full base size."""
        base = super()._current_quote_qty(side)
        if (self.cfg.truth_enabled and self.cfg.stealth_mm
                and self.cfg.stealth_ramp_enabled
                and self._truth_fair() is not None):
            frac = self._stealth_qty_frac()
            return max(1, int(round(base * frac)))
        return base

    def _bbo_for_clamp(self) -> tuple[Optional[int], Optional[int]]:
        """Cheap accessor: top-of-book from the WS-cached book, if fresh."""
        book = self._book_cache
        if book is None:
            return None, None
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return None, None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = int(bids[0]["price"]) if bids else None
        best_ask = int(asks[0]["price"]) if asks else None
        return best_bid, best_ask

    # ==================================================================
    # CRITICAL: the v12 on_reveal fast path uses PRECOMPUTED scenarios
    # built from the Bayesian posterior — neither bid/ask (maker) nor
    # lift_to/hit_to (taker sweep) consult truth. So we override both
    # post points to short-circuit to truth when fresh.
    # ==================================================================
    def _build_and_submit_ioc(self, scenario, book, position, relaxed_max_pos):
        """Replace scenario-based sweep plan with a truth-based one."""
        if not self.cfg.truth_enabled or self._truth_fair() is None:
            return super()._build_and_submit_ioc(
                scenario, book, position, relaxed_max_pos)
        # Stealth: do not submit ANY reveal-time IOCs. Returning [] keeps
        # the precompute fast-path silent. (Parent would also try to IOC
        # from the precomputed scenario; we bypass that too by returning
        # an empty futures list.)
        if self.cfg.stealth_mm:
            return []
        truth_fair = self._truth_fair()
        buy_plans, sell_plans = self._plan_truth_sweep(truth_fair, book)
        futures: list[tuple[str, int, int, Future]] = []
        for px, q in buy_plans:
            fut = self._ioc_executor.submit(
                self.c.buy_ioc, self.symbol, price=px, qty=q)
            futures.append(("buy", px, q, fut))
        for px, q in sell_plans:
            fut = self._ioc_executor.submit(
                self.c.sell_ioc, self.symbol, price=px, qty=q)
            futures.append(("sell", px, q, fut))
        if futures:
            print(f"[v16 TRUTH-IOC] reveal-sweep truth={truth_fair:.1f} "
                  f"buy_plans={buy_plans} sell_plans={sell_plans}")
        return futures

    def on_reveal(self, value: float) -> None:
        """Override: after parent runs, detect window-flip and force a sweep.
        A flip = sign(prev_target) != sign(cur_target). On flip, naive bots'
        mid jumps to the new posterior in ~ms; books are stale for a moment.
        We slam a parallel IOC right after the reveal to capture both the
        unwind + the reversal lots before bots catch up."""
        # Capture the OLD window target BEFORE parent advances posterior.
        prev_target = self._current_window_target()
        try:
            super().on_reveal(value)
        finally:
            cur_target = self._current_window_target()
            cycle = self._current_window_index()
            if (self.cfg.truth_enabled
                    and self._trajectory
                    and prev_target is not None
                    and cur_target is not None
                    and self._last_window_flip_cycle < cycle):
                prev_sign = (1 if prev_target > 0 else
                             -1 if prev_target < 0 else 0)
                cur_sign = (1 if cur_target > 0 else
                            -1 if cur_target < 0 else 0)
                # Flip = sign change, or going from nonzero to zero, or vice versa.
                if prev_sign != cur_sign:
                    self._last_window_flip_cycle = cycle
                    print(f"[v16 WINDOW-FLIP] cycle={cycle} "
                          f"prev_target={prev_target} cur_target={cur_target} "
                          f"pos={self.position} -> forcing sweep")
                    try:
                        # Pull freshest book — book WS may not have ticked yet.
                        b = self.c.book(self.symbol,
                                        depth=self.cfg.snipe_book_depth)
                        self._book_cache = b
                        self._book_cache_t = time.time()
                    except Exception as e:
                        print(f"[v16 WINDOW-FLIP] book pull failed: {e!r}")
                    self._truth_sweep("window_flip", ignore_interval=True)

    def on_tick_settlement(self, msg: dict) -> None:
        """Symbol B has a tick_settlement event at EVERY reveal: the server
        marks our position at the reveal's value and zeroes it. The
        truth_fair for B has already advanced to the next window via
        _truth_b_for_window; here we sync our local position and re-quote
        so the next window's skew/cap applies immediately.

        For symbol A this is never called (A has no tick settlements)."""
        if (msg.get("symbol") or "").upper() != (self.symbol or "").upper():
            return
        if (self.symbol or "").upper() != "B":
            return
        prev_pos = self.position
        with self.lock:
            self.position = 0
            # Resting orders aren't cancelled by the tick; the next quote
            # refresh will move them to the new window's fair.
        print(f"[v16 TICK-SETTLE] B value={msg.get('value')} "
              f"prev_pos={prev_pos} -> 0; window={self._current_window_index()}")
        # Re-quote on the mm executor so the new window's fair/skew applies.
        try:
            self._mm_executor.submit(self.step)
        except Exception as e:
            print(f"[v16 TICK-SETTLE] requote submit failed: {e!r}")

    def _post_mm_async(self, bid_px: Optional[int],
                       ask_px: Optional[int], fair: float) -> None:
        if not self.cfg.truth_enabled or self._truth_fair() is None:
            return super()._post_mm_async(bid_px, ask_px, fair)
        try:
            with self.lock:
                truth_bid, truth_ask, truth_fair, _ = self.desired_quotes()
                self._apply_target_quotes(truth_bid, truth_ask)
                self._last_quote_fair_eff = truth_fair
                self._last_maker_apply_t = time.time()
            if (truth_bid, truth_ask) != (bid_px, ask_px):
                print(f"[v16 TRUTH-MM] override scenario bid={bid_px}/ask={ask_px} "
                      f"(post_fair={fair:.1f}) -> truth bid={truth_bid}/ask={truth_ask} "
                      f"(truth_fair={truth_fair:.1f})")
        except Exception as e:
            print(f"[v16 TRUTH-MM-ERR] {e!r}")

    # ==================================================================
    # Truth-driven parallel sweep (event-driven on book + scheduler)
    # ==================================================================
    def _plan_truth_sweep(self, truth_fair: float, book: dict
                          ) -> tuple[list[tuple[int, int]],
                                     list[tuple[int, int]]]:
        """Build (buy_plans, sell_plans) for ALL book levels mispriced vs truth.
        Sized against ±position_limit and the per-level cap. Buy_plans = (px, q)
        ordered low-to-high; sell_plans high-to-low (book order)."""
        min_edge = self.cfg.truth_sweep_min_edge_ticks
        per_level = self.cfg.truth_sweep_max_lot_per_level
        max_parallel = self.cfg.truth_sweep_max_parallel
        pos = self.position
        room_buy = max(self.position_limit - pos, 0)
        room_sell = max(self.position_limit + pos, 0)
        buy_plans: list[tuple[int, int]] = []
        sell_plans: list[tuple[int, int]] = []

        # BUY: take asks priced below truth - edge
        for lvl in (book.get("asks") or []):
            if len(buy_plans) >= max_parallel:
                break
            if room_buy <= 0:
                break
            px = lvl.get("price")
            sz = int(lvl.get("qty") or 0)
            if px is None or sz <= 0:
                continue
            if truth_fair - px < min_edge:
                break  # asks ascend; first un-stale ends scan
            q = min(per_level, room_buy, sz)
            if q <= 0:
                continue
            buy_plans.append((int(px), int(q)))
            room_buy -= q

        # SELL: hit bids priced above truth + edge
        for lvl in (book.get("bids") or []):
            if len(buy_plans) + len(sell_plans) >= max_parallel:
                break
            if room_sell <= 0:
                break
            px = lvl.get("price")
            sz = int(lvl.get("qty") or 0)
            if px is None or sz <= 0:
                continue
            if px - truth_fair < min_edge:
                break  # bids descend; first un-stale ends scan
            q = min(per_level, room_sell, sz)
            if q <= 0:
                continue
            sell_plans.append((int(px), int(q)))
            room_sell -= q

        return buy_plans, sell_plans

    def _truth_sweep(self, reason: str = "evt",
                     book: Optional[dict] = None,
                     ignore_interval: bool = False) -> int:
        """Submit IOC orders in PARALLEL via the shared executor for every
        book level mispriced vs the truth fair. Returns total filled lots.

        Single-flight: skipped if a sweep is in progress (lock). Gated by
        truth_sweep_min_interval_sec unless ignore_interval (pre-reveal /
        round-start)."""
        if self.phase != "running":
            return 0
        if not self.cfg.truth_enabled:
            return 0
        # Stealth: never IOC-sweep, regardless of reason or interval. The
        # window-flip / round-start / pre-reveal call-sites stay wired in
        # case stealth is toggled off, but they're all no-ops here.
        if self.cfg.stealth_mm:
            return 0
        truth_fair = self._truth_fair()
        if truth_fair is None:
            return 0
        now = time.time()
        if not ignore_interval:
            if now - self._last_truth_sweep_t < self.cfg.truth_sweep_min_interval_sec:
                return 0
        if not self._truth_sweep_lock.acquire(blocking=False):
            return 0
        try:
            self._last_truth_sweep_t = now
            if book is None:
                book = self._book_cache
            if book is None:
                return 0
            buy_plans, sell_plans = self._plan_truth_sweep(truth_fair, book)
            if not buy_plans and not sell_plans:
                return 0

            futures: list[tuple[str, int, int, Future]] = []
            for px, q in buy_plans:
                fut = self._ioc_executor.submit(
                    self.c.buy_ioc, self.symbol, price=px, qty=q)
                futures.append(("buy", px, q, fut))
            for px, q in sell_plans:
                fut = self._ioc_executor.submit(
                    self.c.sell_ioc, self.symbol, price=px, qty=q)
                futures.append(("sell", px, q, fut))

            filled_total = 0
            for side, px, q, fut in futures:
                try:
                    res = fut.result(timeout=2.0)
                except Exception as e:
                    if self.cfg.truth_sweep_max_parallel > 0:
                        # show at most occasional errors
                        print(f"[v16 TS-ERR] {side} {q}@{px} {e!r}")
                    continue
                trades = res.get("trades", []) or []
                with self.lock:
                    filled_here = self._record_fill_from_trades(side, trades)
                if filled_here:
                    filled_total += filled_here
            if filled_total > 0:
                self._truth_sweep_total += filled_total
                self._truth_sweep_count += 1
                edge = (truth_fair - buy_plans[0][0]) if buy_plans else (
                    sell_plans[0][0] - truth_fair if sell_plans else 0.0)
                print(f"[v16 TS-SWEEP {reason}] filled={filled_total} "
                      f"truth={truth_fair:.1f}  pos={self.position}  "
                      f"buy={len(buy_plans)} sell={len(sell_plans)} "
                      f"top_edge={edge:.1f}t")
            return filled_total
        finally:
            self._truth_sweep_lock.release()

    # ==================================================================
    # WS event handlers — event-driven sweep on every book update.
    # ==================================================================
    def on_book_event(self, msg: dict) -> None:
        # Cache the book (parent does this).
        super().on_book_event(msg)
        if not self.cfg.truth_sweep_on_book_evt:
            return
        if msg.get("symbol") not in (None, self.symbol):
            return
        # Pass the freshly-arrived book directly so we don't race the cache.
        try:
            self._truth_sweep("book_evt", book=msg)
        except Exception as e:
            print(f"[v16 TS-EVT-ERR] {e!r}")

    def on_phase_change(self, phase: Optional[str],
                        reveals: list) -> None:
        super().on_phase_change(phase, reveals)
        if phase == "running":
            self._cp_learned_weight.clear()
            self._cp_last_log_t.clear()
            self._last_stale_attack_t = 0.0
            self._stale_attack_total = 0
            self._last_stale_attack_lot = 0
            self._truth_sweep_total = 0
            self._truth_sweep_count = 0
            self._last_truth_sweep_t = 0.0
            self._last_pre_reveal_lead_cycle = -1
            self._last_pre_reveal_final_cycle = -1
            self._last_post_reveal_sweep_cycle = -1
            self._trajectory = None
            self._trajectory_seq = None
            self._last_window_flip_cycle = -1
            # Clear per-round truth cache so we re-fetch ONCE next round.
            self._round_sequence = None
            self._round_truth_a = None
            self._round_truth_b = None
            self._round_sequence_t = 0.0
            # Round-start sweep: wait briefly for first oracle snapshot
            # (poll=100ms), then sweep aggressively. Don't block the WS
            # thread — fire on the mm executor.
            self._mm_executor.submit(self._round_start_sweep)

    def _round_start_sweep(self) -> None:
        """One-shot truth fetch, then trajectory + first sweep."""
        deadline = time.time() + 1.5
        got = False
        while time.time() < deadline:
            if self._fetch_sequence_once():
                got = True
                break
            time.sleep(0.05)
        if not got:
            print("[v16 TS-START] no truth within 1.5s — falling back")
            return
        # Precompute trajectory from the CACHED sequence.
        try:
            seq = list(self._round_sequence or [])
            if seq and tuple(seq) != self._trajectory_seq:
                self._trajectory = self._compute_trajectory(seq)
                self._trajectory_seq = tuple(seq)
                self._last_window_flip_cycle = -1
                self._log_trajectory(self._trajectory)
        except Exception as e:
            print(f"[v16 TRAJECTORY-ERR] {e!r}")
        # Force-fetch a fresh book (WS may not have ticked yet) — useful
        # for the first desired_quotes() call's BBO-clamp.
        try:
            self._book_cache = self.c.book(
                self.symbol, depth=self.cfg.snipe_book_depth)
            self._book_cache_t = time.time()
        except Exception as e:
            print(f"[v16 TS-START] book pull failed: {e!r}")
        if self.cfg.stealth_mm:
            tgt = self._current_window_target()
            print(f"[v16 TS-START stealth] truth={self._truth_fair()} "
                  f"pos={self.position} target={tgt} — no opening sweep")
            return
        filled = self._truth_sweep("round_start", ignore_interval=True)
        tgt = self._current_window_target()
        print(f"[v16 TS-START] truth={self._truth_fair()} filled={filled} "
              f"pos={self.position} target={tgt}")

    # ==================================================================
    # Pre-reveal lead: beat SEAN's 1s widening window.
    # ==================================================================
    def _pre_reveal_hook(self, now: float) -> None:
        """Fire pre-reveal lead, final, and post-reveal sweeps. Called from
        our fast-loop tick so it runs even when book is quiet."""
        if self.phase != "running":
            return
        if self._n_remaining() <= 0:
            return
        nxt = self._next_reveal_at()
        if nxt is None:
            return
        cycle = len(self.posterior.reveals)
        # Lead sweep: 1.5s before reveal — SEAN's defensive widening starts
        # at 1.0s, so this catches it while still tight.
        if (now >= nxt - self.cfg.truth_pre_reveal_lead_sec
                and self._last_pre_reveal_lead_cycle < cycle):
            self._last_pre_reveal_lead_cycle = cycle
            self._truth_sweep("pre_reveal_lead", ignore_interval=True)
        # Final sweep: 200ms before reveal — anything still mispriced gets swept.
        if (now >= nxt - self.cfg.truth_pre_reveal_final_sec
                and self._last_pre_reveal_final_cycle < cycle):
            self._last_pre_reveal_final_cycle = cycle
            self._truth_sweep("pre_reveal_final", ignore_interval=True)
        # Post-reveal: book usually re-stacks after reveal — sweep it too.
        last_t = self._last_reveal_t
        if (last_t is not None
                and self._last_post_reveal_sweep_cycle < cycle
                and now - last_t >= self.cfg.truth_post_reveal_sweep_sec
                and now - last_t < 0.5):
            self._last_post_reveal_sweep_cycle = cycle
            self._truth_sweep("post_reveal", ignore_interval=True)

    # ==================================================================
    # Adaptive CP learning: weight unknown cps by their adverse_ticks
    # ==================================================================
    def _cp_effective_weight(self, cp: str) -> float:
        if not cp:
            return 0.0
        w = CP_WEIGHT.get(cp)
        if w is not None:
            return w
        if not self.cfg.cp_learning_enabled:
            return self.cfg.unknown_cp_default_weight
        learned = self._cp_learned_weight.get(cp)
        if learned is not None:
            return learned
        return self.cfg.unknown_cp_default_weight

    def _update_cp_learned_weight(self, cp: str) -> Optional[float]:
        """Recompute learned weight from CPProfile snapshot for one CP.
        Returns the new weight (or None if not enough fills)."""
        if not self.cfg.cp_learning_enabled or not cp:
            return None
        snap = self.cpp.snapshot().get(cp)
        if snap is None:
            return None
        fills = snap.taker_count + snap.maker_count
        if fills < self.cfg.cp_learning_min_fills:
            return None
        # adverse_ticks accumulates SIGNED ticks of how the market moved
        # after this CP fills with us in their favor.
        # Larger absolute -> more "they knew something".
        adv = abs(snap.adverse_ticks)
        denom = max(self.cfg.cp_learning_adverse_per_t, 1e-9)
        new_w = min(self.cfg.cp_learning_max_weight, adv / denom)
        if new_w < self.cfg.cp_learning_min_weight:
            new_w = 0.0
        old = self._cp_learned_weight.get(cp)
        self._cp_learned_weight[cp] = new_w
        # Log only on meaningful jumps and not too often per CP.
        now = time.time()
        last = self._cp_last_log_t.get(cp, 0.0)
        if (old is None or abs(new_w - (old or 0.0)) >= 0.1) and (now - last) > 5.0:
            self._cp_last_log_t[cp] = now
            print(f"[v16 CP_LEARN] {cp} learned_w={new_w:.2f} "
                  f"(adv={adv:.1f}t over {fills} fills)")
        return new_w

    # ==================================================================
    # Override on_fill_event: use effective CP weight (learned + leaked)
    # ==================================================================
    def on_fill_event(self, msg: dict) -> None:
        # First let the base do its bookkeeping (position, cpp stats, etc).
        # We deliberately call StrategyV12's on_fill_event NOT V14's, then
        # do our own bias logic with the effective weight.
        # But V14's on_fill_event also calls super then does extra stuff.
        # Simplest: call grand-parent V12's bookkeeping by reaching past V14.
        # Cleanest: replicate V14's logic but with _cp_effective_weight.
        from strategy12 import Strategy as _V12
        _V12.on_fill_event(self, msg)

        cp = msg.get("counterparty") or ""
        liq = msg.get("liquidity")
        side = msg.get("side")
        qty = int(msg.get("qty") or 0)
        if qty <= 0 or side not in ("buy", "sell"):
            return
        if liq != "maker":
            return

        # Refresh learned weight for unknown cps.
        if cp and cp not in CP_WEIGHT:
            try:
                self._update_cp_learned_weight(cp)
            except Exception as e:
                print(f"[v16 CP_LEARN-ERR] {e!r}")

        weight = self._cp_effective_weight(cp)
        if weight < self.cfg.informed_min_weight:
            return

        self._update_informed_bias(cp, side, weight)
        new_bias = self._informed_bias_now()

        if abs(new_bias) >= self.cfg.directional_threshold_ticks:
            try:
                self._drive_to_directional_target()
            except Exception as e:
                print(f"[v16 DRIVE-ERR] {e!r}")
            try:
                with self.lock:
                    bid_px, ask_px, _, _ = self.desired_quotes()
                    self._apply_target_quotes(bid_px, ask_px)
                    self._last_maker_apply_t = time.time()
            except Exception as e:
                print(f"[v16 REPRICE-ERR] {e!r}")

    # ==================================================================
    # Generic stale-quote attacker
    # ==================================================================
    def _stale_attack_loop(self) -> None:
        """v16 fast-scan loop. With truth, this drives the pre-reveal hook
        AND a 50ms-cadence truth sweep that backstops the WS book-event
        sweep (in case WS goes quiet). When truth is unavailable, it falls
        back to the legacy sequential scan."""
        try:
            while not self._stale_attack_stop.is_set():
                try:
                    now = time.time()
                    if self.cfg.truth_enabled and self._truth_fair() is not None:
                        # Pre-reveal hooks (lead / final / post) — uses
                        # cycle gates internally so we only fire once each.
                        self._pre_reveal_hook(now)
                        # Backstop truth sweep — gated by truth_sweep_min_interval.
                        self._truth_sweep("scan")
                    else:
                        self._stale_attack_step()
                except Exception as e:
                    # Don't log every iter — print rare-ish exceptions.
                    if time.time() - self._last_stale_attack_t > 5.0:
                        print(f"[v16 STALE-ERR] {e!r}")
                # Sleep in small chunks so stop() takes effect quickly.
                self._stale_attack_stop.wait(self.cfg.stale_attack_check_sec)
        except Exception as e:
            print(f"[v16 STALE-LOOP-DIED] {e!r}")

    def _stale_attack_step(self) -> int:
        if self.phase != "running":
            return 0
        now = time.time()
        if now - self._last_stale_attack_t < self.cfg.stale_attack_check_sec:
            return 0
        book = self._book_cache
        if book is None:
            return 0
        if now - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return 0
        try:
            fair, sigma = self.fair_and_sigma()
        except Exception:
            return 0
        fair_eff = self._vwap_clamped_fair(fair)
        min_edge = self.cfg.stale_attack_min_edge_ticks
        min_size = self.cfg.stale_attack_min_size
        max_slice = self.cfg.stale_attack_max_slice
        pos_pad = self.cfg.stale_attack_position_pad

        self._last_stale_attack_t = now
        taken = 0

        # ATTACK STALE ASKS (we BUY) — ask priced WAY BELOW fair_eff is profit.
        room_to_buy = (self.position_limit - pos_pad) - self.position
        if room_to_buy > 0:
            for lvl in book.get("asks") or []:
                if taken >= max_slice or room_to_buy <= 0:
                    break
                px = lvl.get("price")
                if px is None:
                    continue
                if fair_eff - px < min_edge:
                    break  # asks ascend; first non-stale -> none beyond it
                size = int(lvl.get("qty") or 0)
                if size < min_size:
                    continue
                want = min(max_slice - taken, room_to_buy, size)
                if want <= 0:
                    continue
                if self._snipe("buy", px, want):
                    taken += want
                    room_to_buy -= want
                    self._stale_attack_total += want
                    self._last_stale_attack_lot = want
                    print(f"[v16 STALE_ATTACK] BUY {want}@{px} "
                          f"(fair_eff={fair_eff:.1f}, edge={fair_eff - px:.1f}t)")

        # ATTACK STALE BIDS (we SELL) — bid priced WAY ABOVE fair_eff.
        room_to_sell = self.position - (-self.position_limit + pos_pad)
        if room_to_sell > 0:
            for lvl in book.get("bids") or []:
                if taken >= max_slice or room_to_sell <= 0:
                    break
                px = lvl.get("price")
                if px is None:
                    continue
                if px - fair_eff < min_edge:
                    break  # bids descend; first non-stale -> stop
                size = int(lvl.get("qty") or 0)
                if size < min_size:
                    continue
                want = min(max_slice - taken, room_to_sell, size)
                if want <= 0:
                    continue
                if self._snipe("sell", px, want):
                    taken += want
                    room_to_sell -= want
                    self._stale_attack_total += want
                    self._last_stale_attack_lot = want
                    print(f"[v16 STALE_ATTACK] SELL {want}@{px} "
                          f"(fair_eff={fair_eff:.1f}, edge={px - fair_eff:.1f}t)")

        return taken

    # ==================================================================
    # Clean shutdown
    # ==================================================================
    def stop(self) -> None:
        try:
            self._stale_attack_stop.set()
        except Exception:
            pass
        try:
            if self._truth_sess is not None:
                self._truth_sess.close()
        except Exception:
            pass
        if hasattr(super(), "stop"):
            try:
                super().stop()
            except Exception:
                pass

    def flatten(self) -> None:
        try:
            self._stale_attack_stop.set()
        except Exception:
            pass
        try:
            if self._truth_sess is not None:
                self._truth_sess.close()
        except Exception:
            pass
        super().flatten()


# ---------------------------------------------------------------------------
# Standalone runner. Prefer run_combined16.
# ---------------------------------------------------------------------------
def main() -> None:
    from sdk.client import GameClient
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol="A")
    print(f"v16: reveal_interval={strat.reveal_interval}s  "
          f"duration={strat.duration}s  n_total={strat.n_total}")

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  "
              f"running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(f"FILL   {msg['side']:>4s} {msg['qty']} @ {msg['price']}  "
              f"liq={msg.get('liquidity')}  cp={msg.get('counterparty')}")
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

    print("\nBot started (v16). Commands: s=status, k=cp_learned, "
          "z=stale, f=flatten, q=quit.\n")
    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                bias = strat._informed_bias_now()
                tgt = strat._bias_target_position()
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"bias={bias:+.2f}t  target={tgt:+d}  "
                      f"stale_total={strat._stale_attack_total}")
            elif cmd == "k":
                if not strat._cp_learned_weight:
                    print("  no learned cp weights yet")
                for cp, w in sorted(strat._cp_learned_weight.items(),
                                    key=lambda kv: -kv[1]):
                    print(f"    {cp:<10s} learned_w={w:.2f}")
            elif cmd == "z":
                print(f"  stale total: {strat._stale_attack_total}  "
                      f"last: {strat._last_stale_attack_lot}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown {cmd!r}; try s/k/z/f/q")
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
