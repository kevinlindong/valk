"""Day 1 strategy v21 — v20 with the admin truth oracle removed.

The only intentional behavioural difference vs v20 is the SOURCE of
truth: where v20 calls `/api/admin/truth` with the admin key and
collapses fair_a/c/d to constants the moment that endpoint returns,
v21 *infers* truth from observing fills against the advantaged
("informed") bots configured in `day1/bot_config_dump.json`.

Everything else — width/skew MM, dime defense, hit-and-retreat,
bounds-arb, identity table, tape-driven dime/widen, inventory
shift, soft-cap ramp, parallel reveal sweep, cross-arb, scalp-out
at cap, lockout/orphan handling — is inherited from
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

from strategy20 import (  # noqa: E402, F401
    GameClient,
    URL, API_KEY, N_PRIOR_SIM, Posterior,
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


@dataclass
class _BotInteraction:
    """One of our fills, snapshotted for per-counterparty bookkeeping."""
    t: float
    sym: str
    our_side: str            # "buy" or "sell"
    price: float
    qty: int
    cp: str
    cp_class: str
    is_oracle: bool
    is_informed: bool


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
        # Total reveals this round (used by `_fair_c_prior` to compute
        # the Normal-approx fair_c around the strike for both the
        # informed-fill C-collapse breakpoint and the soft prior on A).
        self.n_total: Optional[int] = None
        # Empirical prior on E[X_i]: the day-1 design uses
        # X_i ~ Uniform{a, ..., a+w} with the lognormal hyperprior in
        # `strategy12.Posterior` giving E[X_i] ≈ 7. Matches v20's
        # `fair_c` Normal-approx so the breakpoint stays consistent.
        self._x_prior_mean: float = 7.0
        self._x_prior_var: float = 16.0

        # Per-counterparty interaction log (every one of our fills).
        # Bounded so a chatty round can't grow it unboundedly.
        self._interactions: List[_BotInteraction] = []
        self._interactions_by_cp: Dict[str, List[_BotInteraction]] = {}
        self._interactions_cap: int = 2048
        # (sym, our_side) → wall-clock until which we treat that side
        # as "just got picked off by an informed bot". Set by
        # `update_from_fill` when counterparty is informed; read by
        # `is_adverse(...)` and the strategy's `_width_for` override.
        self._adverse_until: Dict[Tuple[str, str], float] = {}
        # How long an adverse mark lasts. Strategy injects its config
        # value via `set_widen_duration`.
        self._widen_duration_sec: float = 4.0

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
            self._interactions.clear()
            self._interactions_by_cp.clear()
            self._adverse_until.clear()

    def set_widen_duration(self, seconds: float) -> None:
        """Strategy hook: how long (s) an adverse mark stays active.
        Re-applies to subsequent fills only; existing marks keep their
        original expiry."""
        with self._lock:
            self._widen_duration_sec = max(0.0, float(seconds))

    def set_round_params(self, n_total: Optional[int],
                         x_prior_mean: float = 7.0,
                         x_prior_var: float = 16.0) -> None:
        """Strategy hook: total reveals this round + prior on E[X_i],
        used by the strike-aware C-collapse breakpoint and the soft
        prior on A. Call from `Strategy.__init__` and `on_phase_change`."""
        with self._lock:
            self.n_total = int(n_total) if n_total is not None else None
            self._x_prior_mean = float(x_prior_mean)
            self._x_prior_var = float(x_prior_var)

    def _n_remaining(self) -> int:
        if self.n_total is None:
            return 0
        return max(0, int(self.n_total) - int(self._reveals_seen))

    def _fair_c_prior(self) -> Optional[float]:
        """Strike-aware prior fair value of C ∈ [0, 100] from the
        Normal approximation P(sum >= K | running_sum, n_remaining).

        Mirrors v20's `fair_c` Normal-approx so the BotIntelligence
        C-collapse breakpoint sits at the same fair the rest of the
        strategy is pricing C against. Returns None if c_strike or
        n_total is unset (caller should fall back to 50.0)."""
        K = self.c_strike
        if K is None:
            return None
        n_rem = self._n_remaining()
        if n_rem <= 0:
            # Deterministic: C already settled in principle.
            return 100.0 if self._running_sum >= K else 0.0
        mean_sum = self._running_sum + n_rem * self._x_prior_mean
        sig_sum = math.sqrt(max(n_rem * self._x_prior_var, 1e-9))
        if sig_sum < 1e-6:
            return 100.0 if mean_sum >= K else 0.0
        z = (mean_sum - float(K)) / sig_sum
        p_ge = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return 100.0 * p_ge

    def fair_c_prior(self) -> Optional[float]:
        """Public accessor for `_fair_c_prior` (thread-safe)."""
        with self._lock:
            return self._fair_c_prior()

    def prior_a_from_strike(self) -> Optional[float]:
        """Soft point estimate of truth_a derived from the strike: K
        is "reasonable in comparison to the true value" (game-design
        choice keeps P(sum>=K) in the interesting range), so K is a
        weak prior on E[sum_total]. Returns K, or None if unset.

        Caller should treat this as a HINT (uncertainty ~ x_prior_var
        × n_total), not a locked truth. Used by `truth_status_str`
        and is available to MM for sanity checks; we do NOT pin
        `_bounds["A"]` to K because the user's caveat ("not always
        the case") forbids treating it as ground truth."""
        with self._lock:
            return float(self.c_strike) if self.c_strike is not None else None

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

            # Mid-round C pin from strike: once running_sum >= K, C
            # is GUARANTEED 100 (regardless of remaining reveals). This
            # mirrors v20's identity table but lives in the inferrer
            # so the truth-cache refresh on the next tick picks it up
            # without an oracle fill.
            if self.c_strike is not None:
                c = self._bounds["C"]
                if self._running_sum >= self.c_strike:
                    c.lower = c.upper = 100.0
                    c.lower_src = c.upper_src = (
                        f"running_sum({self._running_sum:.0f})"
                        f">=K({self.c_strike})")

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

        Side effects beyond the truth bound:
          * append a `_BotInteraction` to the interaction log so the
            't' command and adverse-detection queries can scan recent
            fills by counterparty.
          * if CP is informed (oracle or informed_*), mark
            `_adverse_until[(sym, our_side)]` so the MM widens that
            side for `_widen_duration_sec`.
        """
        sym = msg.get("symbol")
        if sym not in ("A", "B", "C", "D"):
            return
        cp = msg.get("counterparty")
        if not cp:
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

        spec = self._bots_by_name.get(cp)
        cls = spec.get("class") if spec else ""
        is_oracle = cls in ORACLE_CLASSES if cls else False
        is_informed = (cls in ORACLE_CLASSES) or (cls in INFORMED_CLASSES) \
            if cls else False
        now = time.time()
        inter = _BotInteraction(
            t=now,
            sym=sym,
            our_side=our_side,
            price=price,
            qty=qty,
            cp=cp,
            cp_class=cls or "",
            is_oracle=is_oracle,
            is_informed=is_informed,
        )

        with self._lock:
            # Always log the interaction so the 't' command surfaces
            # who's trading with us, even for noise/dimer/naive bots.
            self._interactions.append(inter)
            if len(self._interactions) > self._interactions_cap:
                # Drop oldest in chunks to amortise the cost.
                drop = len(self._interactions) - self._interactions_cap
                self._interactions = self._interactions[drop:]
            self._interactions_by_cp.setdefault(cp, []).append(inter)
            if not spec:
                self._unknown_cp_count += 1

            # Adverse mark — informed CP just picked off our `our_side`
            # quote. Widen that side for a few seconds.
            if is_informed and self._widen_duration_sec > 0.0:
                self._adverse_until[(sym, our_side)] = \
                    now + self._widen_duration_sec

        # Non-informed fills carry no truth signal — but we still
        # logged the interaction above. Bail before touching bounds.
        if not is_informed:
            return

        edge = self._edge_threshold_ticks(spec)

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
            self._last_update_t = now

            # Special handling for C (binary {0, 100}): collapse to
            # the boundary as soon as a fill discriminates which side
            # of the strike-aware fair the truth lies on.
            #
            # Breakpoint is the Normal-approx fair_c given the strike
            # K and the reveals seen so far — NOT a hardcoded 50.
            # Example: K=120, running_sum=80, n_rem=8, x_prior_mean=7
            # → mean_sum=136, fair_c_prior ≈ 75. An informed sell at
            # price 30 (which is below 50 but FAR below 75) tells us
            # C=0 even though price >= 0 by definition.
            #
            # Oracle bots know truth exactly and trade with edge=0, so
            # ANY direction trumps the breakpoint (oracle BUY → C=100,
            # oracle SELL → C=0, regardless of price).
            if sym == "C":
                fp = self._fair_c_prior()
                bp = fp if fp is not None else 50.0
                # Asymmetric band so we don't collapse on ambiguous
                # mid-fair fills. Oracle skips the band entirely.
                band = 0.0 if is_oracle else max(2.0, edge + 1.0)
                if our_side == "sell" and price >= bp + band:
                    b.lower = b.upper = 100.0
                    b.lower_src = b.upper_src = (
                        f"C-collapse:{cls}:{cp}@{price:.0f}"
                        f"(bp={bp:.1f},band={band:.1f})")
                elif our_side == "buy" and price <= bp - band:
                    b.lower = b.upper = 0.0
                    b.lower_src = b.upper_src = (
                        f"C-collapse:{cls}:{cp}@{price:.0f}"
                        f"(bp={bp:.1f},band={band:.1f})")

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

    # ------------------------------------------------------------------
    # Adverse-pressure queries — used by the MM `_width_for` override.
    # ------------------------------------------------------------------
    def is_adverse(self, sym: str,
                   our_side: Optional[str] = None,
                   now: Optional[float] = None) -> bool:
        """Did an informed CP pick off our `our_side` quote in `sym`
        within the last `_widen_duration_sec`?

        If `our_side` is None, returns True if EITHER side is adverse
        (caller wants to widen the whole symbol)."""
        t = now if now is not None else time.time()
        with self._lock:
            if our_side in ("buy", "sell"):
                exp = self._adverse_until.get((sym, our_side), 0.0)
                return exp > t
            for side in ("buy", "sell"):
                if self._adverse_until.get((sym, side), 0.0) > t:
                    return True
            return False

    def adverse_remaining(self, sym: str,
                          our_side: Optional[str] = None,
                          now: Optional[float] = None) -> float:
        """Seconds remaining on the strongest active adverse mark for
        `sym` (0.0 if not adverse). Diagnostic only."""
        t = now if now is not None else time.time()
        with self._lock:
            sides = ("buy", "sell") if our_side is None else (our_side,)
            remaining = 0.0
            for s in sides:
                exp = self._adverse_until.get((sym, s), 0.0)
                r = exp - t
                if r > remaining:
                    remaining = r
            return remaining

    def informed_pressure_for(self, sym: str, side: str,
                              window_sec: float,
                              now: Optional[float] = None) -> int:
        """Count of informed fills against `sym`/`side` within the
        last `window_sec`. `side` is OUR side that got hit. Higher
        count = stronger directional signal that the informed flow
        believes truth is on the opposite side of our quote."""
        if side not in ("buy", "sell"):
            return 0
        t = now if now is not None else time.time()
        cutoff = t - max(0.0, float(window_sec))
        with self._lock:
            return sum(
                1 for it in self._interactions
                if it.is_informed and it.sym == sym
                and it.our_side == side and it.t >= cutoff
            )

    def cp_summary(self, window_sec: Optional[float] = None,
                   limit: int = 12,
                   now: Optional[float] = None) -> List[Dict]:
        """Aggregate per-counterparty fill summary, most-recent first.

        Returns up to `limit` entries; each row has cp, class,
        is_informed, n, last_t, syms (set), and signed_qty (sells - buys
        from our side, so + means we bought net from them)."""
        t = now if now is not None else time.time()
        cutoff = (t - float(window_sec)) if window_sec is not None else 0.0
        with self._lock:
            rows: List[Dict] = []
            for cp, lst in self._interactions_by_cp.items():
                recent = [it for it in lst if it.t >= cutoff]
                if not recent:
                    continue
                signed = 0
                syms = set()
                for it in recent:
                    syms.add(it.sym)
                    signed += it.qty if it.our_side == "buy" else -it.qty
                last = recent[-1]
                rows.append({
                    "cp": cp,
                    "cls": last.cp_class,
                    "is_informed": last.is_informed,
                    "is_oracle": last.is_oracle,
                    "n": len(recent),
                    "last_t": last.t,
                    "syms": syms,
                    "signed_qty": signed,
                })
            rows.sort(key=lambda r: (-int(r["is_informed"]),
                                     -r["n"], -r["last_t"]))
            return rows[:max(0, int(limit))]

    def stats(self) -> Dict:
        with self._lock:
            now = time.time()
            adverse = {
                f"{sym}:{side}": max(0.0, exp - now)
                for (sym, side), exp in self._adverse_until.items()
                if exp > now
            }
            return {
                "cached": False,
                "fetches": 0,
                "errors": 0,
                "last_err": self._last_err,
                "fills_ingested": self._fill_count,
                "unknown_cp_fills": self._unknown_cp_count,
                "interactions": len(self._interactions),
                "counterparties": len(self._interactions_by_cp),
                "adverse": adverse,
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

    # ---- v21 adverse-fill widening ----
    # When an informed CP picks off one of our quotes, widen that
    # symbol's MM for a few seconds. Keeps us out of repeat fills
    # against a bot that has a directional view we haven't priced yet.
    informed_widen_enabled: bool = True
    informed_widen_duration_sec: float = 4.0
    informed_widen_extra_ticks: int = 3
    # Window for `informed_pressure_for` (diagnostics + future use).
    informed_pressure_window_sec: float = 8.0


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
        # at end-of-round AND compute the strike-aware breakpoint for
        # C collapse on informed fills.
        self.bot_intel.set_c_strike(self.c_strike)
        # Wire the adverse-widen duration so update_from_fill knows
        # how long to keep the (sym, side) flagged.
        self.bot_intel.set_widen_duration(
            float(getattr(self.cfg, "informed_widen_duration_sec", 4.0))
        )
        # Seed round geometry so `_fair_c_prior()` works from t=0
        # (without it, fair_c_prior falls through to 50.0 and we lose
        # the K-aware C-collapse precision).
        self.bot_intel.set_round_params(n_total=self.n_total)

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
    # Override: widen the MM quote when an informed bot just picked us
    # off. Adds `informed_widen_extra_ticks` on top of v20's `_width_for`
    # for `informed_widen_duration_sec` after every informed fill.
    # ==================================================================
    def _width_for(self, sym: str) -> int:
        base = super()._width_for(sym)
        if not getattr(self.cfg, "informed_widen_enabled", True):
            return base
        bi = getattr(self, "bot_intel", None)
        if bi is None:
            return base
        try:
            if bi.is_adverse(sym):
                extra = int(getattr(self.cfg,
                                    "informed_widen_extra_ticks", 0))
                if extra > 0:
                    return base + extra
        except Exception:
            pass
        return base

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
        going_running = (phase == "running" and prev_phase != "running")
        # v20 already clears _fair_*_truth on running edge; we also need
        # to clear BotIntelligence so the next round starts from a
        # blank slate (otherwise C={0,100} bound from the prior round
        # would leak through).
        if going_running:
            try:
                self.bot_intel.reset()
            except Exception as e:
                print(f"[v21 BOT-INTEL:RESET-ERR] {type(e).__name__}: {e}")
        super().on_phase_change(phase, reveals)
        # super() runs _refresh_c_strike() before _refresh_cd_truth_cache,
        # so self.c_strike is now the NEW round's K. Re-seed bot_intel
        # AFTER super() so it picks up the refreshed K, not the stale
        # one from the prior round.
        if going_running:
            try:
                self.bot_intel.set_c_strike(self.c_strike)
                self.bot_intel.set_round_params(n_total=self.n_total)
            except Exception as e:
                print(f"[v21 BOT-INTEL:RESEED-ERR] {type(e).__name__}: {e}")

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
            f"fills={st['fills_ingested']}/{st['interactions']}",
            f"unk_cp={st['unknown_cp_fills']}",
            f"cps={st['counterparties']}",
        ]
        if bi.c_strike is not None:
            fp = bi.fair_c_prior()
            fp_s = f"{fp:.1f}" if fp is not None else "-"
            parts.append(f"K={bi.c_strike} fairC*={fp_s}")
        for sym in ("A", "B", "C", "D"):
            b = st["bounds"][sym]
            lo = f"{b['lower']:.1f}" if b['lower'] is not None else "-"
            hi = f"{b['upper']:.1f}" if b['upper'] is not None else "-"
            nO = b['oracle_fills']
            nI = b['informed_fills']
            parts.append(f"{sym}:[{lo},{hi}]({nO}O/{nI}I)")
        if st["adverse"]:
            adv = ",".join(f"{k}@{v:.1f}s"
                           for k, v in sorted(st["adverse"].items()))
            parts.append(f"adv={adv}")
        return "  ".join(parts)

    def cp_status_str(self, window_sec: float = 60.0,
                      limit: int = 8) -> str:
        """One-line top-counterparties summary for the 't' / 'b' command."""
        bi = getattr(self, "bot_intel", None)
        if bi is None:
            return "no bot-intel"
        rows = bi.cp_summary(window_sec=window_sec, limit=limit)
        if not rows:
            return f"(no fills in last {window_sec:.0f}s)"
        out = []
        now = time.time()
        for r in rows:
            tag = "O" if r["is_oracle"] else ("I" if r["is_informed"]
                                              else "n")
            syms = "".join(sorted(r["syms"]))
            age = now - r["last_t"]
            cls = r["cls"] or "?"
            out.append(f"{r['cp']}({tag},{cls},{syms},n={r['n']},"
                       f"q={r['signed_qty']:+d},Δ{age:.1f}s)")
        return " | ".join(out)
