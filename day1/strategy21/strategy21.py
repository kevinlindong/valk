"""Day 1 strategy v21 — v20 with the admin truth oracle removed.

The only intentional behavioural difference vs v20 is the SOURCE of
truth: where v20 calls `/api/admin/truth` with the admin key and
collapses fair_a/c/d to constants the moment that endpoint returns,
v21 *infers* truth from observing fills against the advantaged
("informed") bots configured in `day1/bot_config_dump.json`.

Everything else — width/skew MM, dime defense, hit-and-retreat,
shadow-dime, bounds-arb, identity table, tape-driven dime/widen,
inventory shift, soft-cap ramp, parallel reveal sweep, cross-arb,
scalp-out at cap, lockout/orphan handling — is inherited from
`strategy20.Strategy` unchanged.

Mechanism:

  `BotIntelligence` reads the bot config, classifies each bot name
  by its game-class (oracle_sweeper, oracle_twapper, informed_*,
  bayes_taker, slow_informed, true_mean_taker, etc.), then watches
  OUR fills. When the counterparty on one of our fills is one of
  these informed bots, that fill is a directional bound on the
  symbol's fair value:

      counterparty BUYS from us  → truth_sym > fill_price
                                   (informed thinks it's worth more
                                    than what they paid)
      counterparty SELLS to us   → truth_sym < fill_price

  Oracle bots (VANG=C, GVAN=D) trade at edge=0 and *know* truth,
  so a single fill collapses C to {0, 100} and tightly brackets D.
  Other informed bots have edge_threshold > 0 so we widen the bound
  by that many ticks.

  These per-symbol bounds are then surfaced as `truth_a/_b/_c/_d`
  point estimates (midpoint of bounds, or boundary value if one
  side is unknown) once they pass a tightness check. The v20
  strategy machinery treats the resulting point estimate exactly
  like the admin-truth value used to be.

Run:
    python day1/strategy21/run_combined21.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Path bootstrap: import the v20 strategy + sdk_client + posterior from
# the sibling strategy20/ directory so we don't duplicate that code.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_V20_DIR = os.path.join(os.path.dirname(_THIS_DIR), "strategy20")
for _p in (_THIS_DIR, _V20_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk_client import GameClient  # noqa: E402
from strategy12 import (  # noqa: E402, F401
    URL, API_KEY, N_PRIOR_SIM, Posterior,
)
from strategy20 import (  # noqa: E402
    Config as ConfigV20,
    Strategy as StrategyV20,
)

__all__ = [
    "URL", "API_KEY", "N_PRIOR_SIM",
    "Posterior", "BotIntelligence",
    "Config", "Strategy",
]


# ===========================================================================
# Bot-config-driven truth inferrer
# ===========================================================================

# Strongest signals: pure oracles know truth deterministically (edge=0).
ORACLE_CLASSES = frozenset({"oracle_sweeper", "oracle_twapper"})

# Informed-but-not-pure-oracle bots. They act on the same true reveal
# information but with an edge threshold and/or stochastic firing — so
# every fill is still a directional bound on truth, just looser by the
# bot's configured edge_threshold (we read that off the config below).
INFORMED_CLASSES = frozenset({
    "informed_sniper",          # A: VALKC, lead 1500ms, fires p=0.4
    "informed_sniper_next",     # B: CVALK, fires p=1.0 — strong B signal
    "informed_binary_taker",    # C: VANE, edge=0
    "informed_range_taker",     # D: DVAN, edge=0
    "informed_twapper",         # B: GVALK, edge=0, slices over a window
    "true_mean_taker",          # B: IVALK, edge=0
    "slow_informed",            # A: VALKJ, edge=1
    "bayes_taker",              # A: VALKM, edge=2
    "mixed_sweeper",            # A: VALKE, edge=1, informed_prob=0.7
    "directional_taker",        # A/D: VALKG/EVAN, leans toward truth (weak)
    "directional_taker_next",   # B: FVALK, leans toward next reveal (weak)
})

# Default config path: <repo-root>/day1/bot_config_dump.json
_DEFAULT_BOT_CONFIG = os.path.normpath(os.path.join(
    _THIS_DIR, "..", "bot_config_dump.json"))


@dataclass
class _SymBound:
    """Per-symbol inferred bounds on truth.

    For A: bound on truth_a = sum(reveals over the round).
    For B: bound on truth_b for the CURRENT window. Resets on each
           reveal because B settles per-tick.
    For C: bound on truth_c ∈ {0, 100}. Collapses to a point as soon
           as we have any oracle fill (price > 50 → 100, price < 50 → 0).
    For D: bound on truth_d = max(reveals) - min(reveals).
    """
    lower: Optional[float] = None
    upper: Optional[float] = None
    # Source tags for diagnostics.
    lower_src: str = ""
    upper_src: str = ""
    n_oracle_fills: int = 0
    n_informed_fills: int = 0

    def mid(self) -> Optional[float]:
        if self.lower is not None and self.upper is not None:
            return 0.5 * (self.lower + self.upper)
        return None

    def width(self) -> Optional[float]:
        if self.lower is not None and self.upper is not None:
            return self.upper - self.lower
        return None


class BotIntelligence:
    """Drop-in replacement for `strategy19.TruthOracle`.

    Loads `bot_config_dump.json` at construction and classifies every
    configured bot as oracle / informed / uninformed. Each call to
    `update_from_fill(...)` ingests one of our fills; if the
    counterparty is an informed bot the price moves the per-symbol
    truth bound. Surface methods (`truth_a`, `truth_b_for_window`,
    etc.) return a point estimate (midpoint of the inferred bound)
    once the bound is tight enough to be useful, otherwise None.

    Public API mirrors `TruthOracle` so the rest of the v20 codepath
    (which reads `self.truth.<method>()`) keeps working.
    """

    # Tightness threshold: a symbol's bound is considered "locked"
    # (truth-anchored) once the width is at or below this many ticks.
    # C is binary so we treat it as locked the moment any oracle fill
    # has fixed its boundary (lower==upper). The default 4t aligns
    # roughly with the MM widths v20 uses elsewhere (A=4, B=4, D=4),
    # so a locked bound has the same scale of uncertainty as one MM
    # cycle of skew.
    LOCK_WIDTH_TICKS_A: float = 6.0
    LOCK_WIDTH_TICKS_B: float = 2.0
    LOCK_WIDTH_TICKS_C: float = 1.0   # binary — needs to collapse
    LOCK_WIDTH_TICKS_D: float = 3.0

    def __init__(self, config_path: Optional[str] = None,
                 c_strike: Optional[int] = None):
        self._lock = threading.Lock()
        self._bots_by_name: Dict[str, Dict] = {}
        self._classes_loaded: List[str] = []
        self._config_path = config_path or _DEFAULT_BOT_CONFIG
        self._load_config()

        # Per-symbol bounds.
        self._bounds: Dict[str, _SymBound] = {
            s: _SymBound() for s in ("A", "B", "C", "D")
        }
        # B settles per tick — we track ONLY the current window's B
        # bound; on each reveal it resets. (Use `note_reveal()`.)
        self._b_current_window: int = 0
        # Cumulative running sum + count, used to widen A's lower bound
        # as more reveals come in (truth_a >= max(running_sum, lower)).
        self._running_sum: float = 0.0
        self._reveals_seen: int = 0
        # Min / max of revealed values — gives a deterministic lower
        # bound on D (truth_d >= max-min seen so far).
        self._reveal_min: Optional[int] = None
        self._reveal_max: Optional[int] = None

        # C strike (the binary settles 100 if sum >= K else 0). Read
        # from the strategy on init; without it we can't translate a
        # price > 50 into "C=100" with confidence.
        self.c_strike: Optional[int] = c_strike

        # Diagnostics.
        self._fill_count: int = 0
        self._unknown_cp_count: int = 0
        self._last_update_t: float = 0.0
        self._last_err: str = ""

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        path = self._config_path
        try:
            with open(path, "r") as f:
                cfg = json.load(f)
        except Exception as e:
            self._last_err = f"load: {e!r}"
            print(f"[v21 BOT-INTEL] failed to load {path}: {e!r}")
            return
        self._classes_loaded = list(cfg.get("classes") or [])
        configured = cfg.get("configured") or {}
        running = cfg.get("running") or {}
        # Prefer the "running" map (live tournament) but fall back to
        # "configured" — they're usually identical.
        merged: Dict[str, Dict] = {}
        for name, spec in configured.items():
            merged[name] = dict(spec)
        for name, spec in running.items():
            merged[name] = dict(spec)
        self._bots_by_name = merged
        n_oracle = sum(1 for s in merged.values()
                       if s.get("class") in ORACLE_CLASSES)
        n_informed = sum(1 for s in merged.values()
                         if s.get("class") in INFORMED_CLASSES)
        print(f"[v21 BOT-INTEL] loaded {len(merged)} bots from "
              f"{os.path.basename(path)}  oracle={n_oracle} "
              f"informed={n_informed}")

    # ------------------------------------------------------------------
    # Bot classification helpers
    # ------------------------------------------------------------------
    def is_oracle(self, name: Optional[str]) -> bool:
        if not name:
            return False
        spec = self._bots_by_name.get(name)
        return bool(spec) and spec.get("class") in ORACLE_CLASSES

    def is_informed(self, name: Optional[str]) -> bool:
        if not name:
            return False
        spec = self._bots_by_name.get(name)
        if not spec:
            return False
        cls = spec.get("class")
        return cls in ORACLE_CLASSES or cls in INFORMED_CLASSES

    def bot_class(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        spec = self._bots_by_name.get(name)
        return spec.get("class") if spec else None

    def bot_spec(self, name: Optional[str]) -> Optional[Dict]:
        if not name:
            return None
        return self._bots_by_name.get(name)

    def _edge_threshold_ticks(self, spec: Dict) -> float:
        """Bot-config edge_threshold (ticks). Oracle bots default 0.

        Used to slacken the truth-bound for non-oracle informed bots:
        a bayes_taker with edge_threshold=2 firing at price P means
        |truth - P| > 2, so the truth bound widens by 2t."""
        cls = spec.get("class")
        if cls in ORACLE_CLASSES:
            return 0.0
        return float(spec.get("edge_threshold") or 0.0)

    # ------------------------------------------------------------------
    # Round lifecycle
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Clear all inferred bounds — call on phase → running."""
        with self._lock:
            for s in self._bounds:
                self._bounds[s] = _SymBound()
            self._b_current_window = 0
            self._running_sum = 0.0
            self._reveals_seen = 0
            self._reveal_min = None
            self._reveal_max = None
            self._fill_count = 0
            self._unknown_cp_count = 0

    def set_c_strike(self, k: Optional[int]) -> None:
        with self._lock:
            self.c_strike = int(k) if k is not None else None

    def note_reveal(self, value: float, window_idx_after: int,
                    n_total: Optional[int] = None) -> None:
        """Update internal state for one new reveal.

        * resets the B current-window bound (B settles per tick).
        * widens A's lower bound by the running sum (truth_a is at
          least the sum of revealed values).
        * narrows D's lower bound (truth_d >= max(seen)-min(seen)).
        * if all reveals are in (window_idx_after == n_total) we lock
          A exactly to running_sum, D exactly to max-min, and C to
          {0, 100} based on running_sum vs strike.
        """
        v = float(value)
        with self._lock:
            self._running_sum += v
            self._reveals_seen += 1
            if self._reveal_min is None or v < self._reveal_min:
                self._reveal_min = v
            if self._reveal_max is None or v > self._reveal_max:
                self._reveal_max = v
            self._b_current_window = window_idx_after
            self._bounds["B"] = _SymBound()  # fresh window

            # Lower bound on A: at least running_sum (since X_i >= 0
            # for the day-1 game). Strengthen only if tighter than
            # the existing bound.
            a = self._bounds["A"]
            if a.lower is None or self._running_sum > a.lower:
                a.lower = self._running_sum
                a.lower_src = f"running_sum@{self._reveals_seen}"

            d = self._bounds["D"]
            seen_range = (self._reveal_max - self._reveal_min) \
                if (self._reveal_max is not None
                    and self._reveal_min is not None) else 0.0
            if d.lower is None or seen_range > d.lower:
                d.lower = seen_range
                d.lower_src = f"range@{self._reveals_seen}"

            # End-of-round pin.
            if n_total is not None and window_idx_after >= n_total > 0:
                a.lower = a.upper = self._running_sum
                a.lower_src = a.upper_src = "round_end"
                d.lower = d.upper = seen_range
                d.lower_src = d.upper_src = "round_end"
                c = self._bounds["C"]
                if self.c_strike is not None:
                    pin = 100.0 if self._running_sum >= self.c_strike else 0.0
                    c.lower = c.upper = pin
                    c.lower_src = c.upper_src = "round_end"

    # ------------------------------------------------------------------
    # Fill ingestion — the core learning step.
    # ------------------------------------------------------------------
    def update_from_fill(self, msg: Dict) -> None:
        """Ingest ONE of our fills. Direction conventions:

            msg["side"]    = OUR side ("buy" / "sell")
            msg["price"]   = trade price
            msg["counterparty"] = name of the other bot (or human)

        If WE bought at P from CP (cp sold to us) and CP is informed,
        then CP thinks `truth <= P - edge` → upper bound on truth.

        If WE sold at P to CP (cp bought from us) and CP is informed,
        then CP thinks `truth >= P + edge` → lower bound on truth.
        """
        sym = msg.get("symbol")
        if sym not in ("A", "B", "C", "D"):
            return
        cp = msg.get("counterparty")
        if not cp:
            return
        spec = self._bots_by_name.get(cp)
        if not spec:
            with self._lock:
                self._unknown_cp_count += 1
            return
        if not self.is_informed(cp):
            return
        try:
            price = float(msg.get("price") or 0)
            qty = int(msg.get("qty") or 0)
        except Exception:
            return
        if qty <= 0:
            return
        our_side = msg.get("side")
        if our_side not in ("buy", "sell"):
            return

        edge = self._edge_threshold_ticks(spec)
        cls = spec.get("class")
        is_oracle = cls in ORACLE_CLASSES

        # CP's side is the OPPOSITE of ours. Their inferred bound:
        #   cp_side == buy  → truth >= price + edge   (lower bound)
        #   cp_side == sell → truth <= price - edge   (upper bound)
        if our_side == "sell":
            inferred_lower = price + edge
            inferred_upper = None
        else:
            inferred_lower = None
            inferred_upper = price - edge

        with self._lock:
            b = self._bounds[sym]
            src = f"{cls}:{cp}@{price:.0f}"
            if inferred_lower is not None:
                if b.lower is None or inferred_lower > b.lower:
                    b.lower = inferred_lower
                    b.lower_src = src
            if inferred_upper is not None:
                if b.upper is None or inferred_upper < b.upper:
                    b.upper = inferred_upper
                    b.upper_src = src
            if is_oracle:
                b.n_oracle_fills += 1
            else:
                b.n_informed_fills += 1
            self._fill_count += 1
            self._last_update_t = time.time()

            # Special handling for C (binary {0, 100}): collapse to
            # the boundary as soon as any informed fill discriminates
            # which side of 50 truth lies on (and the C strike is
            # configured). One oracle fill is enough.
            if sym == "C":
                # If CP BUYS at price > strike-implied breakpoint, C=100.
                # If CP SELLS at price < breakpoint, C=0.
                # Approximation: use 50 as the breakpoint if no other
                # info (C settles 0 or 100; mid is 50).
                bp = 50.0
                if our_side == "sell" and price >= bp:
                    b.lower = b.upper = 100.0
                    b.lower_src = b.upper_src = f"C-collapse:{cp}@{price:.0f}"
                elif our_side == "buy" and price <= bp:
                    b.lower = b.upper = 0.0
                    b.lower_src = b.upper_src = f"C-collapse:{cp}@{price:.0f}"

    # ------------------------------------------------------------------
    # Truth-API surface — mirrors TruthOracle
    # ------------------------------------------------------------------
    def fetch_once(self) -> bool:
        """No remote fetch. Always returns False so callers fall
        through to whatever passive logic they have."""
        return False

    def full_sequence(self) -> Optional[List[int]]:
        """We can't reconstruct the per-reveal sequence from fills."""
        return None

    def truth_a(self) -> Optional[float]:
        with self._lock:
            b = self._bounds["A"]
            if b.lower is None or b.upper is None:
                return None
            w = b.upper - b.lower
            if w > self.LOCK_WIDTH_TICKS_A:
                return None
            return 0.5 * (b.lower + b.upper)

    def truth_b_for_window(self, window_idx: int) -> Optional[float]:
        with self._lock:
            if window_idx != self._b_current_window:
                # B bounds only valid for the current open window.
                return None
            b = self._bounds["B"]
            if b.lower is None or b.upper is None:
                return None
            w = b.upper - b.lower
            if w > self.LOCK_WIDTH_TICKS_B:
                return None
            return 0.5 * (b.lower + b.upper)

    def truth_c(self) -> Optional[float]:
        with self._lock:
            b = self._bounds["C"]
            if b.lower is None or b.upper is None:
                return None
            # Binary: only return when bounds have actually collapsed.
            if abs(b.upper - b.lower) > self.LOCK_WIDTH_TICKS_C:
                return None
            mid = 0.5 * (b.lower + b.upper)
            return 100.0 if mid >= 50.0 else 0.0

    def truth_d(self) -> Optional[float]:
        with self._lock:
            b = self._bounds["D"]
            if b.lower is None or b.upper is None:
                return None
            w = b.upper - b.lower
            if w > self.LOCK_WIDTH_TICKS_D:
                return None
            return 0.5 * (b.lower + b.upper)

    def stats(self) -> Dict:
        with self._lock:
            return {
                "cached": False,
                "fetches": 0,
                "errors": 0,
                "last_err": self._last_err,
                "fills_ingested": self._fill_count,
                "unknown_cp_fills": self._unknown_cp_count,
                "bounds": {
                    s: {
                        "lower": self._bounds[s].lower,
                        "upper": self._bounds[s].upper,
                        "lower_src": self._bounds[s].lower_src,
                        "upper_src": self._bounds[s].upper_src,
                        "oracle_fills": self._bounds[s].n_oracle_fills,
                        "informed_fills": self._bounds[s].n_informed_fills,
                    }
                    for s in ("A", "B", "C", "D")
                },
            }


# ===========================================================================
# Config — pure passthrough of v20 Config. v21 only changes the truth
# *source*, not any MM / sweep / dime / bounds-arb knob.
# ===========================================================================
@dataclass
class Config(ConfigV20):
    # ---- coid prefix (so v21 sessions are distinguishable in logs) ----
    client_order_id_prefix: str = "v21"

    # Bot-config path (override if running outside the repo layout).
    bot_config_path: Optional[str] = None


# ===========================================================================
# Strategy v21
# ===========================================================================
class Strategy(StrategyV20):
    """v20 with TruthOracle replaced by BotIntelligence.

    Everything inherited; we only override:

      * `__init__`            — swap `self.truth` for BotIntelligence.
      * `_refresh_cd_truth_cache` — read per-symbol estimates directly
                              from BotIntelligence (no full_sequence).
      * `_compute_naive_trajectory` — no-op (no full sequence to
                              compare a naive posterior against).
      * `on_fill_event`       — feed each of our fills into the
                              inferrer BEFORE super so downstream
                              truth-aware paths (cross-arb, MM, etc.)
                              see the updated bound on the next tick.
      * `on_reveal`           — call `bot_intel.note_reveal(...)` so
                              the inferrer's running_sum / D-range
                              bounds advance with the public reveals.
      * `on_phase_change`     — reset the inferrer at round start.
      * `truth_status_str`    — show the inferrer's bounds rather
                              than the oracle's cached sequence.
    """

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

        # The bot inferrer needs to exist BEFORE super().__init__ so
        # that any background thread or fast-precompute kicked off
        # inside the parent constructor can call `self.truth.*` without
        # NPE. Same lifecycle invariant as v19's TruthOracle.
        bot_cfg_path = getattr(config, "bot_config_path", None)
        self.bot_intel = BotIntelligence(config_path=bot_cfg_path)

        super().__init__(client, posterior, config)

        # `super().__init__` (v19) installed `self.truth = TruthOracle()`.
        # Replace it AFTER super so any v19/v20 init that already cached
        # `self.truth.full_sequence()` (returns None on TruthOracle when
        # the fetch fails — which it will, since we don't carry the
        # admin key here) is harmlessly re-pointed at the inferrer for
        # subsequent calls.
        self.truth = self.bot_intel

        # Hand the C strike to the inferrer so it can pin C={0,100}
        # at end-of-round.
        self.bot_intel.set_c_strike(self.c_strike)

        # Recompute the C/D truth cache + identity table from whatever
        # the inferrer already has (typically empty — bounds populate
        # only after the first informed fill / reveal).
        self._refresh_cd_truth_cache()
        try:
            self._recompute_identity_table()
        except Exception:
            pass

        print(f"[v21 INIT] truth source = BotIntelligence "
              f"(no /api/admin/truth)  bots="
              f"{len(self.bot_intel._bots_by_name)}")

    # ==================================================================
    # Override: rebuild the v20 truth cache directly from the inferrer
    # rather than waiting on `full_sequence()` to return a complete seq.
    # The v20 strategy code consumes these fields:
    #   _fair_a_truth, _fair_c_truth, _fair_d_truth, _truth_b_seq,
    #   _cd_truth_for_seq.
    # We populate them whenever the inferrer has a tight-enough bound
    # on that symbol; otherwise leave them None (the strategy then
    # falls through to its posterior-based fair).
    # ==================================================================
    def _refresh_cd_truth_cache(self) -> None:
        bi = getattr(self, "bot_intel", None)
        if bi is None:
            # super() ran before our __init__ finished — be defensive.
            self._fair_a_truth = None
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._truth_b_seq = None
            self._cd_truth_for_seq = None
            return
        ta = bi.truth_a()
        tc = bi.truth_c()
        td = bi.truth_d()
        tb = bi.truth_b_for_window(self._reveal_count)

        new_a = float(ta) if ta is not None else None
        new_c = float(tc) if tc is not None else None
        new_d = float(td) if td is not None else None

        # `_truth_b_seq` is a tuple indexed by reveal_count in v20's
        # `fair_b`. We don't know the future B's, so build a sparse
        # tuple with None for unseen windows and the current bound
        # at index = _reveal_count. (v20 reads only that index, so a
        # sparse list is safe; we use a list-of-floats with the
        # current entry filled in.)
        b_seq: Optional[Tuple[float, ...]] = None
        if tb is not None and self.n_total > 0:
            arr: List[Optional[float]] = [None] * self.n_total
            if 0 <= self._reveal_count < self.n_total:
                arr[self._reveal_count] = float(tb)
            # v20.fair_b uses `seq[reveal_count]` and treats it as
            # the truth value; only that one slot is read so leaving
            # other slots as None is harmless. But to keep
            # consistency with the v20 tuple type we substitute 0.0
            # for None slots and rely on the reveal-count gate.
            b_seq = tuple(0.0 if x is None else x for x in arr)

        # Cache-key tuple change detection (matches v20 semantics:
        # only invalidate downstream caches when the inferred sequence
        # changes). We synthesise a fingerprint from the current
        # bounds rather than the (unknown) full sequence.
        fp = (new_a, new_c, new_d, tb, self._reveal_count)

        if fp == self._cd_truth_for_seq:
            return
        self._cd_truth_for_seq = fp
        self._fair_a_truth = new_a
        self._fair_c_truth = new_c
        self._fair_d_truth = new_d
        self._truth_b_seq = b_seq

        # Compact log — only when something locks in.
        locked = [s for s, v in (("A", new_a), ("B", tb),
                                 ("C", new_c), ("D", new_d))
                  if v is not None]
        if locked:
            ta_s = f"{new_a:.1f}" if new_a is not None else "-"
            tb_s = f"{tb:.1f}" if tb is not None else "-"
            tc_s = f"{new_c:.0f}" if new_c is not None else "-"
            td_s = f"{new_d:.1f}" if new_d is not None else "-"
            print(f"[v21 INFER] locked={','.join(locked)}  "
                  f"fair_a={ta_s} fair_b@{self._reveal_count}={tb_s} "
                  f"fair_c={tc_s} fair_d={td_s}")

    # ==================================================================
    # Override: v19's `_compute_naive_trajectory` needs the full
    # reveal sequence; we don't have it. Make it a no-op so the v20
    # init path doesn't crash. Downstream callers tolerate `None`
    # trajectories (they just disable directional skew).
    # ==================================================================
    def _compute_naive_trajectory(self) -> None:
        self._naive_traj_a = None
        self._naive_traj_b = None

    # ==================================================================
    # Override: feed every one of our fills into the inferrer BEFORE
    # super so cross-arb / dime / sweep code on the same event sees
    # the updated bound.
    # ==================================================================
    def on_fill_event(self, msg: dict) -> None:
        try:
            self.bot_intel.update_from_fill(msg)
            # If this fill tightened a bound, refresh the v20 truth
            # cache so downstream MM / cross-arb pulls the new fair.
            self._refresh_cd_truth_cache()
        except Exception as e:
            print(f"[v21 BOT-INTEL:ERR] {type(e).__name__}: {e}")
        super().on_fill_event(msg)

    # ==================================================================
    # Override: pass each reveal through to the inferrer (B per-window
    # reset, A running-sum lower bound, D realized-range lower bound,
    # end-of-round pinning). Then refresh the v20 truth cache so the
    # post-reveal sweep / quote refresh sees any newly tight bound.
    # ==================================================================
    def on_reveal(self, value: float) -> None:
        try:
            new_count = self._reveal_count + 1
            self.bot_intel.note_reveal(
                value=value,
                window_idx_after=new_count,
                n_total=self.n_total,
            )
        except Exception as e:
            print(f"[v21 BOT-INTEL:REVEAL-ERR] {type(e).__name__}: {e}")
        super().on_reveal(value)
        try:
            self._refresh_cd_truth_cache()
        except Exception:
            pass

    # ==================================================================
    # Override: reset the inferrer on a fresh round.
    # ==================================================================
    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        prev_phase = self.phase
        # v20 already clears _fair_*_truth on running edge; we also need
        # to clear BotIntelligence so the next round starts from a
        # blank slate (otherwise C={0,100} bound from the prior round
        # would leak through).
        if phase == "running" and prev_phase != "running":
            try:
                self.bot_intel.reset()
                # Re-seed strike (it can change between rounds).
                self.bot_intel.set_c_strike(self.c_strike)
            except Exception as e:
                print(f"[v21 BOT-INTEL:RESET-ERR] {type(e).__name__}: {e}")
        super().on_phase_change(phase, reveals)
        # super().on_phase_change calls _refresh_cd_truth_cache for us
        # on the running edge — but it does so with our overridden
        # version, so the inferrer's (empty) state is what we'll read.

    # ==================================================================
    # Override: status string for the runner's 's' / 't' commands.
    # Replace the oracle's "seq=[...]" with the inferrer's per-symbol
    # bounds.
    # ==================================================================
    def truth_status_str(self) -> str:
        bi = getattr(self, "bot_intel", None)
        if bi is None:
            return "no bot-intel"
        st = bi.stats()
        parts = [
            f"fills_ingested={st['fills_ingested']}",
            f"unknown_cp={st['unknown_cp_fills']}",
        ]
        for sym in ("A", "B", "C", "D"):
            b = st["bounds"][sym]
            lo = f"{b['lower']:.1f}" if b['lower'] is not None else "-"
            hi = f"{b['upper']:.1f}" if b['upper'] is not None else "-"
            nO = b['oracle_fills']
            nI = b['informed_fills']
            parts.append(f"{sym}:[{lo},{hi}]({nO}O/{nI}I)")
        return "  ".join(parts)
