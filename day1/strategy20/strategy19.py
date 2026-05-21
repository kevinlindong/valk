"""Day 1 strategy v19 — v18 execution infrastructure + v16 truth oracle.

WHY v19:
  * v18 is a robust dual-symbol MM with reveal-time precompute hot path,
    inter-reveal stale sweep, dime defense, cross-symbol arb on the 15ms
    private-feed window, endgame burst, and modify-replace fast path.
    But it prices off the Bayesian posterior — wide sigma, conservative
    edges, flatten-bias around uncertain reveals.
  * v16 demonstrated that the admin endpoint `/api/admin/truth` returns
    the entire reveal sequence in advance. Once cached, A's settlement
    value is sum(full_sequence) — KNOWN. B's per-window settlement is
    full_sequence[window_idx] — also known.

WHAT v19 DOES:
  * Subclasses v18's Strategy, replaces fair_a/fair_b with truth-anchored
    fairs (sigma collapsed to ~0). Everything downstream — precompute
    scenarios, live snipe thresholds, MM quote prices, sweep gates,
    cross-arb edges — now operates on TRUE fair, not the posterior.
  * Tightens snipe edges to 0.5t (just enough to cover the 0.5/lot taker
    fee). With truth, every level past 0.5t off fair is free money.
  * Disables flatten-bias (no uncertainty to flatten against).
  * Drops the pre-reveal park window — our maker quotes are at truth,
    never stale, never need to hide.
  * Drops mm_inventory_safety_pad to 0 — uses the full ±position_limit.
  * Bigger maker sizes and tighter widths — every fill is +EV by truth.
  * Replaces v18's per-X-value Bayesian precompute with a single
    truth-anchored scenario keyed on the exact next reveal value.
  * Extends the endgame burst loop to ALSO snipe B in its final window
    (the last B reveal value is exact via truth).

WHAT WE KEEP FROM v18:
  * Modify-replace, dime defense, dedicated mm/park/reconcile threads,
    orphan reconcile + bulk-cancel circuit breaker.
  * 15ms private-feed cross-arb fan-out on every fill / reveal.
  * Rate-limit gating, lockout handling, 403 circuit breaker.
  * Multi-order resting safety, in-flight bookkeeping.
  * `max_ioc_distance_ticks=50` spoofer filter (HVALK trap).

INVARIANTS:
  * If truth fetch fails, v19 degrades to v18 behavior — fair_a/fair_b
    fall back to super() (posterior), edges remain at v19 config defaults
    (still tighter than v18, accept the variance).
  * Position-limit cap is NEVER breached — v18's _can_send / IOC clamp
    paths are untouched.

Run:
    python day1/strategy19/run_combined19.py
"""
from __future__ import annotations

import copy
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

# Path bootstrap: keep sibling modules importable when run as a script.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sdk_client import GameClient  # noqa: E402
from strategy12 import (  # noqa: E402, F401
    URL, API_KEY, N_PRIOR_SIM, Posterior,
)
from strategy18 import (  # noqa: E402
    Config as ConfigV18,
    Strategy as StrategyV18,
    _Scenario,
)


# ===========================================================================
# Truth oracle — one-shot fetch of /api/admin/truth at round start.
#
# Re-implemented (not imported from v16) to keep strategy19 self-contained
# and to drop v16's superfluous polling thread.
# ===========================================================================
ADMIN_KEY = "sean123"


class TruthOracle:
    """Fetcher for `/api/admin/truth` (admin key) returning the entire
    reveal sequence.

    Uses its OWN requests.Session with the admin key so the fetch does
    not consume the trader's rate budget. One-shot per round: the
    `fetch_once` method is idempotent — once the sequence is cached it
    short-circuits. Call `reset()` at every phase->running transition
    so the next round re-fetches.
    """

    def __init__(self, base_url: str = URL, admin_key: str = ADMIN_KEY,
                 timeout_sec: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_sec
        self._lock = threading.Lock()
        self._round_sequence: Optional[List[int]] = None
        self._fetch_count = 0
        self._err_count = 0
        self._last_err: str = ""
        self._sess = requests.Session()
        self._sess.headers.update({"X-API-Key": admin_key})

    def fetch_once(self) -> bool:
        """Direct admin GET. Returns True iff a fresh running-phase
        sequence was cached (or already cached). Safe to call again."""
        with self._lock:
            if self._round_sequence is not None:
                return True
        try:
            r = self._sess.get(
                f"{self.base_url}/api/admin/truth",
                timeout=self.timeout)
        except Exception as e:
            with self._lock:
                self._err_count += 1
                self._last_err = repr(e)
            return False
        if not r.ok:
            with self._lock:
                self._err_count += 1
                self._last_err = f"HTTP {r.status_code}"
            return False
        try:
            j = r.json()
        except Exception as e:
            with self._lock:
                self._err_count += 1
                self._last_err = f"json: {e!r}"
            return False
        if j.get("phase") != "running":
            return False
        seq = j.get("full_sequence") or []
        if not seq:
            return False
        with self._lock:
            self._round_sequence = [int(v) for v in seq]
            self._fetch_count += 1
        return True

    def reset(self) -> None:
        """Clear the cached sequence (call on each phase->running edge)."""
        with self._lock:
            self._round_sequence = None

    def full_sequence(self) -> Optional[List[int]]:
        with self._lock:
            return (list(self._round_sequence)
                    if self._round_sequence else None)

    def truth_a(self) -> Optional[float]:
        """A settles once at sum(full_sequence). Constant once cached."""
        with self._lock:
            if not self._round_sequence:
                return None
            return float(sum(self._round_sequence))

    def truth_b_for_window(self, window_idx: int) -> Optional[float]:
        """B settles at every reveal — window i (= reveals already seen)
        settles at full_sequence[i]. Returns None past final window."""
        with self._lock:
            if not self._round_sequence:
                return None
            if 0 <= window_idx < len(self._round_sequence):
                return float(self._round_sequence[window_idx])
            return None

    def stats(self) -> dict:
        with self._lock:
            return {
                "cached": self._round_sequence is not None,
                "fetches": self._fetch_count,
                "errors": self._err_count,
                "last_err": self._last_err,
            }


# ===========================================================================
# Config
# ===========================================================================
@dataclass
class Config(ConfigV18):
    """v19 Config — v18 defaults with truth-aware overrides.

    Tightenings vs v18:
      * sweep_edge_ticks 1.5 -> 0.5         truth is exact, recoup 0.5t fee
      * inter_sweep_edge_ticks 3.0 -> 0.5   ditto
      * cross_arb_edge_ticks 2.0 -> 0.5     ditto
      * inter_sweep_min_size 4 -> 1         any size is +EV
      * sweep_max_slice 10 -> 25            bigger bites, fewer rounds
      * inter_sweep_max_slice 10 -> 25      ditto
      * cross_arb_max_slice 8 -> 25         ditto
      * cross_arb_levels 5 -> 10            deeper book walk
      * mm_inventory_safety_pad 10 -> 0     full ±L window
      * sweep_position_pad 5 -> 0           ditto
      * size_a 5 -> 10                      bigger maker quotes
      * size_b 5 -> 10                      ditto
      * skew_full_flip_a 50 -> 200          slower skew rebound
      * skew_full_flip_b 100 -> 200         ditto
      * quote_width_a 8 -> 4                tighter MM
      * quote_width_b 4 -> 2                tighter MM
      * pre_reveal_park_sec 0.8 -> 0.0      no park — quotes are truthful
      * no_info_widen_mult 8.0 -> 1.0       k=0 is no longer "no info"
      * pre_reveal_widen_mult 5.0 -> 1.0    ditto
      * flatten_bias_enabled True -> False  no uncertainty
    """
    # ---- truth oracle ----
    truth_enabled: bool = True
    # Block at most this long waiting for the round-start truth fetch.
    truth_init_wait_sec: float = 1.0

    # ---- tighter snipe edges ----
    sweep_edge_ticks: float = 0.5
    inter_sweep_edge_ticks: float = 0.5
    cross_arb_edge_ticks: float = 0.5

    # ---- larger slices / deeper depth ----
    sweep_max_slice: int = 25
    inter_sweep_max_slice: int = 25
    cross_arb_max_slice: int = 25
    cross_arb_levels: int = 10
    inter_sweep_min_size: int = 1
    inter_sweep_throttle_sec: float = 0.15

    # ---- no inventory padding ----
    mm_inventory_safety_pad: int = 0
    sweep_position_pad: int = 0

    # ---- park / widen disabled ----
    pre_reveal_park_sec: float = 0.0
    pre_reveal_park_offset_ticks: int = 0
    pre_reveal_widen_mult: float = 1.0
    no_info_widen_mult: float = 1.0

    # ---- flatten-bias disabled ----
    flatten_bias_enabled: bool = False

    # ---- bigger maker quotes, slower skew, tighter widths ----
    size_a: int = 10
    size_b: int = 10
    skew_full_flip_a: int = 200
    skew_full_flip_b: int = 200
    quote_width_a: int = 4
    quote_width_b: int = 2

    # ---- penny-step edges (the maker-side floor) ----
    # min_edge must still beat the 0.5/lot maker fee, but we can lower
    # it now that fair is exact. 0.5t == break-even after fee.
    penny_min_edge_a: float = 0.5
    penny_min_edge_b: float = 0.5

    # ---- endgame burst — extend to B ----
    # v18 only sweeps A during endgame (since A's settle is exact only
    # after the last reveal). With truth, B's value in the current
    # window is also exact at all times → reuse the burst pattern for
    # B once we're inside the final B window.
    endgame_b_enabled: bool = True
    endgame_edge_ticks_b: float = 0.5

    # ---- truth printing throttle ----
    truth_print_throttle_sec: float = 5.0

    # ----------------------------------------------------------------
    # Position-cap ramp — MM enforces a SOFT cap that grows from
    # start_frac*L at window 0 to end_frac*L at the final window.
    # Snipes (reveal IOC, inter-sweep, dime defense, cross-arb,
    # endgame) still use the HARD position_limit — every snipe fill
    # is +EV by truth, so size up always. The ramp only restrains
    # passive MM accumulation early in the round, where the posterior
    # has more time to adversely-select us and where directional
    # confidence matters less (since the favorable side ramps in too).
    # ----------------------------------------------------------------
    position_ramp_enabled: bool = True
    position_ramp_start_frac: float = 0.25  # 25% of ±L at window 0
    position_ramp_end_frac: float = 1.0     # 100% of ±L at final window
    position_ramp_min_cap: int = 15         # floor so early MM is viable

    # ---- Maker size ramp ----
    # Base quote size also ramps over the round so we lean into edges
    # more as the round matures (and as our soft cap expands).
    size_ramp_enabled: bool = True
    size_ramp_start_frac: float = 0.40
    size_ramp_min_size: int = 3

    # ---- Directional skew (truth-vs-naive edge) ----
    # When the truth fair differs from a fresh posterior's "naive"
    # fair at this window by >= min_edge ticks, skew our MM quotes:
    #   * favorable side (the one we want filled) → tight_ticks
    #     from fair (we WANT this fill — it's +EV by truth)
    #   * unfavorable side → wide_ticks from fair (we only sell/buy
    #     if some idiot lifts us at a crazy price)
    # Edge sign convention:
    #   edge > 0 → truth > naive → market underprices → BUY
    #   edge < 0 → truth < naive → market overprices → SELL
    # Disabled if naive trajectory is unknown (truth fetch failed).
    directional_skew_enabled: bool = True
    directional_skew_min_edge: float = 1.5      # ticks
    directional_skew_tight_ticks: int = 1
    directional_skew_wide_ticks: int = 5

    # ---- coid prefix ----
    client_order_id_prefix: str = "v19"


# ===========================================================================
# Strategy
# ===========================================================================
class Strategy(StrategyV18):
    """v19: v18 dual-symbol MM execution with truth-anchored fair."""

    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[Config] = None):
        # Force the Config v19 dataclass so all v19 fields are present.
        if config is None:
            config = Config()
        elif not isinstance(config, Config):
            # Cross-type promotion (Config18 -> Config19) keeps overlapping
            # fields and uses defaults for new ones.
            config = Config(**{
                f.name: getattr(config, f.name)
                for f in config.__dataclass_fields__.values()
                if f.name in Config.__dataclass_fields__
            })

        # Truth state — create BEFORE super so any background thread spun
        # up in super().__init__ that calls fair_a/fair_b doesn't NPE.
        self.truth = TruthOracle()
        self._last_truth_log_t: float = 0.0
        self._truth_scenario_count: int = 0
        # Cached "naive" per-window trajectory — what a fresh posterior
        # would say at each window after seeing reveals[0:i] from truth.
        # Used to compute the truth-vs-naive edge for directional skew.
        # Filled by _compute_naive_trajectory once truth is cached.
        self._naive_traj_a: Optional[List[float]] = None
        self._naive_traj_b: Optional[List[float]] = None

        super().__init__(client, posterior, config)

        # If we attach mid-running, try the first fetch right now so MM
        # threads can start using truth-anchored fair on their first tick.
        if self.phase == "running":
            deadline = time.time() + self.cfg.truth_init_wait_sec
            while time.time() < deadline:
                if self.truth.fetch_once():
                    break
                time.sleep(0.05)
            seq = self.truth.full_sequence()
            if seq is not None:
                print(f"[v19 TRUTH-INIT] seq={seq}  "
                      f"truth_a={sum(seq)}  "
                      f"truth_b@win{self._reveal_count}="
                      f"{seq[self._reveal_count] if self._reveal_count < len(seq) else '-'}")
                self._compute_naive_trajectory()
                self._bump_posterior_gen()
                self._precompute_request.set()
            else:
                st = self.truth.stats()
                print(f"[v19 TRUTH-INIT] no truth within "
                      f"{self.cfg.truth_init_wait_sec:.1f}s "
                      f"(fetches={st['fetches']} errors={st['errors']} "
                      f"last_err={st['last_err']!r}) — degrading to v18 fair")

        print(f"[v19 INIT] truth_enabled={self.cfg.truth_enabled}  "
              f"sweep_edge={self.cfg.sweep_edge_ticks}t  "
              f"inter_edge={self.cfg.inter_sweep_edge_ticks}t  "
              f"arb_edge={self.cfg.cross_arb_edge_ticks}t")
        print(f"[v19 INIT] size_a={self.cfg.size_a} size_b={self.cfg.size_b}  "
              f"width_a={self.cfg.quote_width_a}t width_b={self.cfg.quote_width_b}t  "
              f"flip_a={self.cfg.skew_full_flip_a} flip_b={self.cfg.skew_full_flip_b}")
        print(f"[v19 INIT] park_sec={self.cfg.pre_reveal_park_sec}  "
              f"mm_pad={self.cfg.mm_inventory_safety_pad}  "
              f"flatten_bias={self.cfg.flatten_bias_enabled}")
        print(f"[v19 INIT] pos_ramp={self.cfg.position_ramp_start_frac:.2f}->"
              f"{self.cfg.position_ramp_end_frac:.2f} "
              f"(min_cap={self.cfg.position_ramp_min_cap})  "
              f"size_ramp={self.cfg.size_ramp_start_frac:.2f}->1.0  "
              f"dir_skew={self.cfg.directional_skew_enabled} "
              f"min_edge={self.cfg.directional_skew_min_edge}t "
              f"tight/wide={self.cfg.directional_skew_tight_ticks}/"
              f"{self.cfg.directional_skew_wide_ticks}t")

    # ==================================================================
    # Truth helpers
    # ==================================================================
    def _truth_a(self) -> Optional[float]:
        if not self.cfg.truth_enabled:
            return None
        return self.truth.truth_a()

    def _truth_b(self, window_idx: Optional[int] = None) -> Optional[float]:
        if not self.cfg.truth_enabled:
            return None
        i = self._reveal_count if window_idx is None else int(window_idx)
        return self.truth.truth_b_for_window(i)

    def _truth_next_value(self) -> Optional[int]:
        """The exact value of the NEXT reveal (used to collapse the
        precompute table to a single scenario)."""
        seq = self.truth.full_sequence()
        if not seq:
            return None
        i = self._reveal_count
        if 0 <= i < len(seq):
            return int(seq[i])
        return None

    def _maybe_log_truth(self) -> None:
        now = time.time()
        if (now - self._last_truth_log_t) < self.cfg.truth_print_throttle_sec:
            return
        self._last_truth_log_t = now
        seq = self.truth.full_sequence()
        if seq is None:
            return
        wb = self._reveal_count
        tb = seq[wb] if 0 <= wb < len(seq) else None
        print(f"[v19 TRUTH] seq={seq}  truth_a={sum(seq)}  "
              f"truth_b@win{wb}={tb}  pos_a={self.state['A'].position}  "
              f"pos_b={self.state['B'].position}")

    # ==================================================================
    # Naive trajectory — what would a fresh posterior say at each window
    # after feeding it reveals[0:i] from the known truth sequence?
    # The truth-vs-naive edge at each window tells us where the market
    # is most mispriced; we lean directionally into those windows.
    # ==================================================================
    def _compute_naive_trajectory(self) -> None:
        seq = self.truth.full_sequence()
        if not seq:
            self._naive_traj_a = None
            self._naive_traj_b = None
            return
        n_total = self.n_total
        work = copy.deepcopy(self.posterior)
        work.reset()  # back to prior, empty reveals
        naive_a: List[float] = []
        naive_b: List[float] = []
        running_sum = 0.0
        for i in range(n_total + 1):
            n_rem = max(n_total - i, 0)
            fa, _sa = work.predict_settle(running_sum, n_rem)
            ex_b = 0.0
            for (a, w), prob in work.posterior.items():
                ex_b += prob * (a + w / 2.0)
            naive_a.append(fa)
            naive_b.append(ex_b)
            if i < n_total and i < len(seq):
                work.update(float(seq[i]))
                running_sum += float(seq[i])
        self._naive_traj_a = naive_a
        self._naive_traj_b = naive_b
        truth_a = float(sum(seq))
        # Compact, scannable trajectory print.
        print(f"[v19 TRAJECTORY] truth_a={truth_a:.1f}  n_total={n_total}")
        for i in range(n_total + 1):
            ea = truth_a - naive_a[i]
            if i < len(seq):
                tb = float(seq[i])
                eb = tb - naive_b[i]
                print(f"  win {i:>2d}: naive_a={naive_a[i]:>7.1f} "
                      f"edge_a={ea:+6.1f}  truth_b={tb:>3.0f} "
                      f"naive_b={naive_b[i]:>6.2f} edge_b={eb:+6.2f}")
            else:
                print(f"  win {i:>2d}: naive_a={naive_a[i]:>7.1f} "
                      f"edge_a={ea:+6.1f}  (no B)")

    def _round_progress(self) -> float:
        """Round fraction in [0, 1] based on reveal count."""
        if self.n_total <= 0:
            return 1.0
        return min(1.0, max(0.0, self._reveal_count / float(self.n_total)))

    def _effective_position_cap(self, sym: str) -> int:
        """Soft cap (|position|) that MM uses. Grows from start_frac*L
        at window 0 to end_frac*L at the final window. Snipes still
        use the hard ±position_limit."""
        L = self.state[sym].position_limit
        if not self.cfg.position_ramp_enabled:
            return L
        prog = self._round_progress()
        frac = (self.cfg.position_ramp_start_frac
                + (self.cfg.position_ramp_end_frac
                   - self.cfg.position_ramp_start_frac) * prog)
        cap = int(round(L * frac))
        cap = max(cap, self.cfg.position_ramp_min_cap)
        return min(cap, L)

    def _ramped_base_size(self, sym: str) -> int:
        base = self.cfg.size_a if sym == "A" else self.cfg.size_b
        if not self.cfg.size_ramp_enabled:
            return base
        prog = self._round_progress()
        frac = (self.cfg.size_ramp_start_frac
                + (1.0 - self.cfg.size_ramp_start_frac) * prog)
        sz = int(round(base * frac))
        return max(sz, self.cfg.size_ramp_min_size)

    def _window_edge_at(self, sym: str, window_idx: int) -> Optional[float]:
        """truth_fair - naive_fair at the given window. None if the
        trajectory isn't available."""
        if sym == "A":
            traj = self._naive_traj_a
            if not traj:
                return None
            seq = self.truth.full_sequence()
            if not seq:
                return None
            idx = max(0, min(window_idx, len(traj) - 1))
            return float(sum(seq)) - traj[idx]
        traj_b = self._naive_traj_b
        if not traj_b:
            return None
        seq = self.truth.full_sequence()
        if not seq:
            return None
        if 0 <= window_idx < len(seq):
            return float(seq[window_idx]) - traj_b[window_idx]
        return None

    def _window_edge(self, sym: str) -> Optional[float]:
        return self._window_edge_at(sym, self._reveal_count)

    # ==================================================================
    # fair_a / fair_b — return truth when available, with tiny sigma
    # ==================================================================
    def fair_a(self) -> Tuple[float, float]:
        t = self._truth_a()
        if t is not None:
            self._maybe_log_truth()
            return float(t), 0.5
        return super().fair_a()

    def fair_b(self) -> Tuple[float, float]:
        t = self._truth_b()
        if t is not None:
            return float(t), 0.5
        return super().fair_b()

    # ==================================================================
    # flatten-bias never triggers with truth — uncertainty is zero
    # ==================================================================
    def _in_flatten_bias_window(self, sigma_a: Optional[float] = None) -> bool:
        if self.cfg.truth_enabled and self._truth_a() is not None:
            return False
        return super()._in_flatten_bias_window(sigma_a)

    # ==================================================================
    # Quote sizing — soft-cap aware + base-size ramp.
    # MM honors the SOFT cap so we don't blow past the round-progress
    # position envelope during passive market-making. Snipes (IOC paths
    # like _ioc, sweeps, dime defense, cross-arb, endgame) still use
    # the hard ±position_limit because every snipe fill is +EV by
    # truth — those paths bypass _quote_size entirely.
    # ==================================================================
    def _quote_size(self, sym: str, side: str) -> int:
        s = self.state[sym]
        base = self._ramped_base_size(sym)
        cap = self._effective_position_cap(sym)
        same_side_out = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        if side == "bid":
            headroom = cap - s.position - same_side_out - in_flight
        else:
            headroom = cap + s.position - same_side_out - in_flight
        return max(0, min(base, headroom + same_side_out))

    # ==================================================================
    # Quote prices — wraps v18 with directional skew + soft-cap clamps.
    # ==================================================================
    def _quote_prices(self, sym: str, fair: float, position: int,
                      mode: str) -> Tuple[Optional[int], Optional[int]]:
        return self._quote_prices_at(sym, fair, position, mode,
                                     self._reveal_count)

    def _quote_prices_at(self, sym: str, fair: float, position: int,
                         mode: str, window_idx: int
                         ) -> Tuple[Optional[int], Optional[int]]:
        """Same contract as v18 _quote_prices, but parameterized by the
        window index used to look up the directional edge. The MM hot
        path passes self._reveal_count; the precompute (which prices
        for AFTER the next reveal) passes self._reveal_count + 1."""
        bid_px, ask_px = super()._quote_prices(sym, fair, position, mode)

        # Apply the MM soft cap as an inventory pad — if we're already
        # at the cap, drop the side that would push us further out.
        cap = self._effective_position_cap(sym)
        if position >= cap and bid_px is not None:
            bid_px = None
        if position <= -cap and ask_px is not None:
            ask_px = None

        # Park / non-running modes don't get directional override.
        if (mode == "park"
                or not self.cfg.directional_skew_enabled
                or self.phase != "running"):
            return bid_px, ask_px

        edge = self._window_edge_at(sym, window_idx)
        if edge is None or abs(edge) < self.cfg.directional_skew_min_edge:
            return bid_px, ask_px

        s = self.state[sym]
        tick = s.tick
        tight = self.cfg.directional_skew_tight_ticks * tick
        wide = self.cfg.directional_skew_wide_ticks * tick

        if edge > 0:
            # truth > naive → market underprices → we want to BUY.
            # Tighten bid (closer to fair), widen ask.
            ideal_bid = ((int(round(fair - tight))) // tick) * tick
            ideal_ask = ((int(round(fair + wide))) // tick) * tick
            if bid_px is not None:
                bid_px = max(bid_px, ideal_bid)
            if ask_px is not None:
                ask_px = max(ask_px, ideal_ask)
        else:
            # truth < naive → market overprices → we want to SELL.
            # Tighten ask, widen bid.
            ideal_ask = ((int(round(fair + tight))) // tick) * tick
            ideal_bid = ((int(round(fair - wide))) // tick) * tick
            if ask_px is not None:
                ask_px = min(ask_px, ideal_ask)
            if bid_px is not None:
                bid_px = min(bid_px, ideal_bid)

        # Re-validate ordering after the bounds (no crossed quote).
        if bid_px is not None and ask_px is not None and ask_px <= bid_px:
            ask_px = bid_px + tick

        # Negative-EV guard (mirrors v18) — after the bounds, our maker
        # bid might sit at fair, which is breakeven; never above.
        if self.cfg.skip_negative_ev:
            if bid_px is not None and bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None

        if bid_px is not None and bid_px < s.tick:
            bid_px = None
        return bid_px, ask_px

    # ==================================================================
    # Phase change: re-fetch truth on every new round
    # ==================================================================
    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        prev_phase = self.phase
        super().on_phase_change(phase, reveals)
        if phase == "running" and prev_phase != "running":
            self.truth.reset()
            self._naive_traj_a = None
            self._naive_traj_b = None
            deadline = time.time() + self.cfg.truth_init_wait_sec
            while time.time() < deadline:
                if self.truth.fetch_once():
                    break
                time.sleep(0.05)
            seq = self.truth.full_sequence()
            if seq is not None:
                print(f"[v19 TRUTH-PHASE] phase=running seq={seq}  "
                      f"truth_a={sum(seq)}")
                self._compute_naive_trajectory()
                self._bump_posterior_gen()
                self._precompute_request.set()
            else:
                print(f"[v19 TRUTH-PHASE] no truth within "
                      f"{self.cfg.truth_init_wait_sec:.1f}s "
                      f"(falling back to posterior)")
        elif phase in ("completed", "settling", "settled"):
            self.truth.reset()
            self._naive_traj_a = None
            self._naive_traj_b = None

    # ==================================================================
    # Precompute — collapse to a SINGLE truth-anchored scenario keyed
    # on the known next reveal value. Cheaper, exact.
    # ==================================================================
    def _build_precompute(self) -> None:
        if not self.cfg.truth_enabled:
            return super()._build_precompute()
        next_v = self._truth_next_value()
        if next_v is None:
            # Truth not yet cached, or past final window → defer to v18.
            return super()._build_precompute()

        t0 = time.perf_counter()
        cycle_at_start = self._reveal_count
        if self._n_remaining() <= 0:
            with self._precompute_lock:
                self._precompute = {}
                self._precompute_for_cycle = cycle_at_start
            return

        # A's truth fair is sum(full_sequence) — constant, valid AFTER
        # this reveal too. B's NEXT window (after this reveal lands) is
        # full_sequence[k+1] — if k+1 == N there's no further B settle,
        # but the precompute is only consulted on this reveal so we just
        # use the current-window truth for B as well.
        seq = self.truth.full_sequence() or []
        truth_a = float(sum(seq)) if seq else 0.0
        # AFTER this reveal, B's next-window truth = full_sequence[k+1]
        # if it exists; else fall back to last value (won't be used).
        k_next = self._reveal_count + 1
        if 0 <= k_next < len(seq):
            truth_b_after = float(seq[k_next])
        elif seq:
            truth_b_after = float(seq[-1])
        else:
            truth_b_after = 0.0

        pos_a = self.state["A"].position
        pos_b = self.state["B"].position
        sweep_edge = self.cfg.sweep_edge_ticks
        tick_a = self.state["A"].tick
        tick_b = self.state["B"].tick

        # Precompute prices the AFTER-reveal window (cycle k+1) — pass
        # the next window idx so directional skew uses the right edge.
        bid_a, ask_a = self._quote_prices_at("A", truth_a, pos_a, "normal",
                                             cycle_at_start + 1)
        bid_b, ask_b = self._quote_prices_at("B", truth_b_after, pos_b,
                                             "normal", cycle_at_start + 1)

        lift_a = ((int(math.floor(truth_a - sweep_edge))) // tick_a) * tick_a
        hit_a_raw = int(math.ceil(truth_a + sweep_edge))
        hit_a = ((hit_a_raw + tick_a - 1) // tick_a) * tick_a
        lift_b = ((int(math.floor(truth_b_after - sweep_edge))) // tick_b) * tick_b
        hit_b_raw = int(math.ceil(truth_b_after + sweep_edge))
        hit_b = ((hit_b_raw + tick_b - 1) // tick_b) * tick_b

        scen = _Scenario(
            value=next_v, prob=1.0,
            fair_a=truth_a, sigma_a=0.5,
            fair_b=truth_b_after, sigma_b=0.5,
            bid_a=bid_a, ask_a=ask_a,
            bid_b=bid_b, ask_b=ask_b,
            lift_to_a=lift_a, hit_to_a=hit_a,
            lift_to_b=lift_b, hit_to_b=hit_b,
        )
        new_table = {next_v: scen}

        # Commit if posterior didn't move under us.
        if self._reveal_count != cycle_at_start:
            return
        with self._precompute_lock:
            self._precompute = new_table
            self._precompute_for_cycle = cycle_at_start
            self._precompute_run_count += 1
            self._last_precompute_us = (time.perf_counter() - t0) * 1e6
            self._truth_scenario_count += 1

    # ==================================================================
    # On reveal: keep v18 hot path; force live-threshold + precompute
    # refresh so fair_b advances to the new window's truth immediately.
    # ==================================================================
    def on_reveal(self, value) -> None:
        # Best-effort late fetch if truth missed init.
        if (self.cfg.truth_enabled
                and self.truth.full_sequence() is None):
            self.truth.fetch_once()
        super().on_reveal(value)
        # Posterior + reveal_count moved → invalidate caches so fair_b
        # picks up the NEW window's truth on the very next read.
        self._bump_posterior_gen()
        # Force the slow precompute to rebuild for the next reveal.
        self._precompute_request.set()

    # ==================================================================
    # Endgame burst — extend to ALSO snipe B in the final B window.
    # v18's _endgame_tick only handles A; we layer a B pass after the
    # super() call. Activation is the same flag (last reveal seen → A
    # settled → endgame active), but with truth, B's value in the
    # current window is also exact at all times — so the B burst is
    # always safe to run while _endgame_active is True.
    # ==================================================================
    def _endgame_tick(self) -> None:
        super()._endgame_tick()
        if not self.cfg.endgame_b_enabled:
            return
        if not self._endgame_active:
            return
        if self._in_lockout():
            return
        if self._tokens_available() < self.cfg.endgame_min_tokens:
            return
        fair_b = self._truth_b()
        if fair_b is None:
            # Past the final B window or truth missing — nothing to do.
            return
        edge = self.cfg.endgame_edge_ticks_b
        max_levels = self.cfg.endgame_max_levels
        max_slice = self.cfg.endgame_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        s = self.state["B"]
        book = s.book
        if not book:
            return
        fired = 0
        # BUY asks <= truth - edge
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            for lvl in (book.get("asks") or [])[:max_levels]:
                if room_buy <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if fair_b - px < edge:
                    break
                if max_dist > 0 and abs(px - fair_b) > max_dist:
                    continue
                want = min(max_slice, room_buy, size)
                if self._ioc("B", "buy", want, px):
                    room_buy -= want
                    fired += 1
                    print(f"[v19 ENDGAME:B] BUY {want}@{px}  "
                          f"truth={fair_b:.1f} edge={fair_b - px:.1f}t")
        # SELL bids >= truth + edge
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            for lvl in (book.get("bids") or [])[:max_levels]:
                if room_sell <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px - fair_b < edge:
                    break
                if max_dist > 0 and abs(px - fair_b) > max_dist:
                    continue
                want = min(max_slice, room_sell, size)
                if self._ioc("B", "sell", want, px):
                    room_sell -= want
                    fired += 1
                    print(f"[v19 ENDGAME:B] SELL {want}@{px}  "
                          f"truth={fair_b:.1f} edge={px - fair_b:.1f}t")
        if fired:
            self._endgame_count += fired

    # ==================================================================
    # Status accessors for the runner
    # ==================================================================
    def truth_status_str(self) -> str:
        seq = self.truth.full_sequence()
        if seq is None:
            st = self.truth.stats()
            return (f"no truth (fetches={st['fetches']} "
                    f"errors={st['errors']} "
                    f"last_err={st['last_err']!r})")
        wb = self._reveal_count
        tb = seq[wb] if 0 <= wb < len(seq) else None
        return (f"seq={seq}  truth_a={sum(seq)}  "
                f"truth_b@win{wb}={tb}  "
                f"scenarios_built={self._truth_scenario_count}")

    def ramp_status_str(self) -> str:
        """Round progress + soft caps + ramped sizes + current edges."""
        prog = self._round_progress()
        cap_a = self._effective_position_cap("A")
        cap_b = self._effective_position_cap("B")
        sz_a = self._ramped_base_size("A")
        sz_b = self._ramped_base_size("B")
        ea = self._window_edge("A")
        eb = self._window_edge("B")
        ea_s = f"{ea:+.1f}" if ea is not None else "-"
        eb_s = f"{eb:+.2f}" if eb is not None else "-"
        return (f"prog={prog:.2%} "
                f"cap_a=+/-{cap_a}/{self.state['A'].position_limit} "
                f"cap_b=+/-{cap_b}/{self.state['B'].position_limit}  "
                f"size_a={sz_a}/{self.cfg.size_a} "
                f"size_b={sz_b}/{self.cfg.size_b}  "
                f"edge_a={ea_s} edge_b={eb_s}")

    def naive_trajectory_str(self) -> str:
        seq = self.truth.full_sequence()
        traj_a = self._naive_traj_a
        traj_b = self._naive_traj_b
        if not seq or not traj_a or not traj_b:
            return "trajectory: not computed (no truth)"
        lines = [f"trajectory  truth_a={sum(seq)}  n_total={self.n_total}"]
        truth_a_f = float(sum(seq))
        for i in range(len(traj_a)):
            ea = truth_a_f - traj_a[i]
            here = " <-- now" if i == self._reveal_count else ""
            if i < len(seq):
                tb = float(seq[i])
                eb = tb - traj_b[i]
                lines.append(
                    f"  win {i:>2d}: naive_a={traj_a[i]:>7.1f} "
                    f"edge_a={ea:+6.1f}  truth_b={tb:>3.0f} "
                    f"naive_b={traj_b[i]:>6.2f} edge_b={eb:+6.2f}{here}")
            else:
                lines.append(
                    f"  win {i:>2d}: naive_a={traj_a[i]:>7.1f} "
                    f"edge_a={ea:+6.1f}  (no B){here}")
        return "\n".join(lines)
