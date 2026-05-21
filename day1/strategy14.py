"""Day 1 live trading strategy v14 — directional position-targeting on
informed flow (class-aware) + same-side aggression.

WHY v14: v13 logs (combined_log_v13_20260520_004418.jsonl) show the bias
mechanism FIRED at the right moments but didn't capitalize on the move.

  GAME 2 t=1036.56: VALKJ (slow_informed) BOUGHT 1 lot from us @ 124.
  Their action means "fair > 124". Truth = 126. In the next 5 seconds the
  tape printed 30+ TAKER-BUY trades at 123-127, then settle at 126.
  v13 with bias_max=4 only shifted fair_eff by ~4 ticks and continued
  symmetric MM. Net new long captured in that window: +2 lots (BRYA).
  We had the signal and missed the entire rally.

  GAME 1 t=905.67: VALKJ SOLD us @ 30. Truth = 29. Same story, smaller
  magnitude: we should have flipped to net short but kept buying VALKO's
  stale 26-28 quotes (which happened to be OK because truth=29 anyway).

v13 weights were also wrong: I had `directional_taker` (VALKG) at 1.0
when its lean_prob is only 0.7 (and its "lean" isn't tied to truth, it's
just biased). `informed_sniper_next` (CVALK, B), `informed_twapper`
(GVALK, B), and `true_mean_taker` (IVALK, B) are the strongly informed
B-bots — those weights matter only if we trade B but are included for
generality. Other INTERN bots (BAZC, CHAR, BRYA, ERIK, SEAN) also act
informed when they aggress against us — BAZC took 13 lots @126 exactly
at settle = 126, indicating perfect info. So treat all intern maker-
aggression as bias signal at moderate weight.

WHAT'S NEW (delta from v12; v13 superseded):

  A. CP_WEIGHT reclassified from /api/admin/bots `class` field. Each
     informed bot's weight reflects its actual class:
        informed_sniper        -> 1.0    (knows truth)
        slow_informed          -> 1.0    (knows truth, slow)
        true_mean_taker        -> 1.0    (knows true mean)
        informed_twapper       -> 1.0    (knows truth, TWAPs)
        informed_sniper_next   -> 1.0    (knows next reveal)
        bayes_taker            -> 0.7    (bayesian inference)
        directional_taker_next -> 0.7    (knows next direction)
        mixed_sweeper(p=0.3)   -> 0.3    (30% informed)
        directional_taker(p=.7)-> 0.3    (biased, not informed)
        predictive_mm(acc=.55) -> 0.10   (barely informed)
        predictive_avg_mm      -> 0.10
     Plus other interns at 0.5-0.7 as proxy "informed" labels.

  B. DIRECTIONAL MODE. When |bias| >= directional_threshold_ticks:
       * Suspend OPPOSITE-side maker (don't fight informed flow).
       * Same-side maker stays/tightens (still grab passive flow).
       * Active IOC sweep in bias direction every step + on each
         quote_event (not just at reveal).
       * Position target = sign(bias) * directional_position_target
         (e.g. ±30 lots when bias is +/- 4t at weight 1.0).
       * `_drive_to_target` IOCs through the book until target hit
         or no more affordable liquidity.

  C. BIGGER BIAS RANGE. max_ticks 4 -> 6, decay 10s -> 15s. Gives more
     headroom for strong-signal scenarios.

  D. SNIPE-WITH-BIAS lower bar. _snipe edge_required gets a discount
     when bias points the same way as the snipe — i.e. informed says
     "fair higher" AND we're considering a buy snipe -> require less
     traditional edge because informed is paying for the conviction.

DESIGN INVARIANTS PRESERVED:
  * Lock-free hot path (IOC futures wait outside main lock).
  * O(1) PrecomputedScenario dict lookup at on_reveal.
  * Single _snipe unified path.
  * Cancel+post only (no modify).
  * WS-driven book cache as primary; REST pre-warm fallback.
  * PrecomputedScenario built from RAW posterior (bias intentionally
    does NOT enter precompute — reveals supersede prior flow signal).

Run:
    python day1/run_combined14.py
    python day1/strategy14.py            # standalone
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from strategy12 import (  # noqa: F401  (re-export for runners)
    URL, API_KEY, N_PRIOR_SIM, Posterior,
    Config as ConfigV12,
    Strategy as StrategyV12,
    PrecomputedScenario,
    CPProfile,
)


# ---------------------------------------------------------------------------
# Counterparty informedness registry
# ---------------------------------------------------------------------------
# Derived from /api/admin/bots leak (day1/bot_config_dump.json) — each
# entry maps a CP id to its "informedness weight" in [0, 1]: the prior
# probability that this CP's aggression carries information about truth.
CP_WEIGHT: dict[str, float] = {
    # -------- Symbol A informed bots --------
    "VALKC": 1.00,   # informed_sniper           — knows truth
    "VALKJ": 1.00,   # slow_informed             — knows truth (slow cadence)
    "VALKM": 0.70,   # bayes_taker               — bayesian, edge_threshold=2
    "VALKE": 0.30,   # mixed_sweeper             — informed_prob=0.3
    "VALKG": 0.30,   # directional_taker         — lean_prob=0.7 (biased only)
    "VALKF": 0.10,   # predictive_mm             — accuracy=0.55

    # -------- Symbol B informed bots (kept for B trading) --------
    "CVALK": 1.00,   # informed_sniper_next      — knows next reveal
    "GVALK": 1.00,   # informed_twapper          — knows truth, TWAPs
    "IVALK": 1.00,   # true_mean_taker           — knows true mean
    "FVALK": 0.70,   # directional_taker_next    — knows next direction
    "DVALK": 0.10,   # predictive_avg_mm         — accuracy=0.55

    # -------- Other interns (treated as moderately informed) --------
    # Rationale: BAZC took 13@126 exactly at settle=126 in our v13 log,
    # which only makes sense if they have signal. Treat all intern
    # maker-aggression as 0.5-0.7 weight.
    "SEAN": 0.70,    # sean bot — bayesian with 100k particles
    "BAZC": 0.60,    # intern (observed perfect-info aggression in v13 log)
    "CHAR": 0.50,    # intern
    "BRYA": 0.50,    # intern
    "ERIK": 0.50,    # intern
}

# CPs whose quotes are by design STALE — sweep these even without bias.
STALE_CPS: set[str] = {"VALKO"}  # stale_quoter, refresh 5s


@dataclass
class Config(ConfigV12):
    """Strict superset of v12 Config with v14 directional-mode knobs."""
    # ---- informed-flow bias (carried over from v13 idea) ----
    informed_bias_max_ticks: float = 6.0   # v13 was 4.0
    informed_bias_decay_sec: float = 15.0  # v13 was 10.0
    informed_min_weight: float = 0.2

    # ---- v14: directional position-targeting ----
    # When |bias_now| in ticks >= this, enter DIRECTIONAL mode:
    # suspend opposite-side quote and drive position toward target.
    directional_threshold_ticks: float = 1.5
    # Max position target at full-weight bias (=informed_bias_max_ticks).
    # Scales linearly with |bias_now| / max_ticks.
    directional_position_target: int = 30
    # Per-step IOC slice cap when driving to target (avoid lifting whole book).
    directional_step_qty: int = 8
    # Max ticks "above fair" we'll pay to drive to target (we sweep through
    # levels priced within fair_eff +/- this).
    directional_max_pay_ticks: float = 3.0
    # Suspend opposite-side maker quote while directional?
    directional_suspend_opposite: bool = True

    # ---- bias-aware snipe discount ----
    # When snipe direction matches bias direction, reduce edge required by
    # (bias_now / max_ticks) * this many ticks.
    snipe_bias_aligned_discount: float = 1.5

    # ---- general "any aggressor is information" (lite) ----
    # When an UNNAMED CP (not in CP_WEIGHT) aggresses maker against us
    # we treat them at this weight. 0 = ignore unknown CPs.
    unknown_cp_default_weight: float = 0.2


class Strategy(StrategyV12):
    """v14: class-aware informed flow + directional position-targeting."""

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

        # ---- v14 informed-bias state (set BEFORE super so background
        # threads in v12 init don't AttributeError) ----
        self._informed_bias_ticks: float = 0.0
        self._informed_bias_set_t: float = 0.0
        self._informed_bias_cp: str = ""
        self._informed_bias_weight: float = 0.0
        # Per-CP last-aggression direction history (used for future scoring).
        self._last_directional_drive_t: float = 0.0

        super().__init__(*args, **kwargs)

        print(f"[v14 INFORMED] max_ticks={self.cfg.informed_bias_max_ticks} "
              f"decay_sec={self.cfg.informed_bias_decay_sec}  "
              f"dir_thresh={self.cfg.directional_threshold_ticks}t  "
              f"dir_target={self.cfg.directional_position_target}lots")
        print(f"[v14 INFORMED] tracked CPs:")
        for cp, w in sorted(CP_WEIGHT.items(), key=lambda kv: -kv[1]):
            print(f"               {cp:<8s} weight={w:.2f}")
        print(f"[v14 INFORMED] stale-sweep CPs: {sorted(STALE_CPS)}")

    # ==================================================================
    # Bias
    # ==================================================================
    def _informed_bias_now(self, now: Optional[float] = None) -> float:
        """Current bias in ticks (decayed). Positive => fair higher."""
        if self._informed_bias_ticks == 0.0:
            return 0.0
        if now is None:
            now = time.time()
        age = now - self._informed_bias_set_t
        if age <= 0:
            return self._informed_bias_ticks
        if age >= self.cfg.informed_bias_decay_sec:
            self._informed_bias_ticks = 0.0
            return 0.0
        decay = 1.0 - age / self.cfg.informed_bias_decay_sec
        return self._informed_bias_ticks * decay

    def _bias_target_position(self) -> int:
        """Signed position target driven by current bias."""
        b = self._informed_bias_now()
        if abs(b) < self.cfg.directional_threshold_ticks:
            return 0
        frac = min(1.0, abs(b) / max(self.cfg.informed_bias_max_ticks, 1e-9))
        magnitude = int(round(self.cfg.directional_position_target * frac))
        return magnitude if b > 0 else -magnitude

    def _update_informed_bias(self, cp: str, side_we: str,
                              weight: float) -> None:
        """Set or reinforce bias from one CP's maker-aggression event."""
        if weight < self.cfg.informed_min_weight:
            return
        magnitude = self.cfg.informed_bias_max_ticks * weight
        # CP bought from us (we sold) => CP thinks fair HIGHER => positive.
        # CP sold to us   (we bought) => CP thinks fair LOWER  => negative.
        if side_we == "sell":
            target = +magnitude
        elif side_we == "buy":
            target = -magnitude
        else:
            return

        existing = self._informed_bias_now()
        if existing == 0.0 or (existing > 0) != (target > 0):
            new_bias = target
        else:
            new_bias = target if abs(target) > abs(existing) else existing
        self._informed_bias_ticks = new_bias
        self._informed_bias_set_t = time.time()
        self._informed_bias_cp = cp
        self._informed_bias_weight = weight
        print(f"[v14 INFORMED-BIAS] cp={cp} we_did={side_we} w={weight:.2f} "
              f"-> bias={new_bias:+.2f}t  target_pos={self._bias_target_position():+d}")

    # ==================================================================
    # Pricing overrides — bias propagates here
    # ==================================================================
    def _vwap_clamped_fair(self, fair: float) -> float:
        base = super()._vwap_clamped_fair(fair)
        bias = self._informed_bias_now()
        return base + bias if bias != 0.0 else base

    # ==================================================================
    # Snipe edge discount when bias is aligned
    # ==================================================================
    def _snipe_edge_required(self, sigma: float, side: str) -> float:
        base = super()._snipe_edge_required(sigma, side)
        b = self._informed_bias_now()
        if b == 0.0 or self.cfg.snipe_bias_aligned_discount <= 0:
            return base
        # Aligned: buy with +bias or sell with -bias
        aligned = (side == "buy" and b > 0) or (side == "sell" and b < 0)
        if not aligned:
            return base
        frac = min(1.0, abs(b) / max(self.cfg.informed_bias_max_ticks, 1e-9))
        discount = self.cfg.snipe_bias_aligned_discount * frac
        return max(self.taker_fee, base - discount)

    # ==================================================================
    # Desired quotes override — suspend opposite side in directional mode
    # ==================================================================
    def desired_quotes(self) -> Tuple[Optional[int], Optional[int],
                                      float, float]:
        bid_px, ask_px, fair, sigma = super().desired_quotes()
        if not self.cfg.directional_suspend_opposite:
            return bid_px, ask_px, fair, sigma
        b = self._informed_bias_now()
        if abs(b) < self.cfg.directional_threshold_ticks:
            return bid_px, ask_px, fair, sigma
        # Directional mode: kill the side that would fight informed flow.
        # +bias (informed says fair higher) => don't OFFER (don't sell).
        # -bias (informed says fair lower) => don't BID  (don't buy).
        if b > 0:
            ask_px = None
        else:
            bid_px = None
        return bid_px, ask_px, fair, sigma

    # ==================================================================
    # Directional driver: IOC toward target position
    # ==================================================================
    def _drive_to_directional_target(self) -> int:
        """If |bias| >= threshold, IOC through book toward target position.

        Returns lots traded (signed positive). Throttled by
        directional_step_qty per call so it amortizes across ticks.
        """
        if self.phase != "running":
            return 0
        if len(self.posterior.reveals) < 1:
            return 0
        target = self._bias_target_position()
        if target == 0:
            return 0
        gap = target - self.position
        if gap == 0:
            return 0
        # Throttle: at most one drive per ~50ms to avoid spamming.
        now = time.time()
        if now - self._last_directional_drive_t < 0.05:
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
        fair_eff = self._vwap_clamped_fair(fair)  # includes bias
        max_pay_ticks = self.cfg.directional_max_pay_ticks
        step_qty_cap = max(1, self.cfg.directional_step_qty)

        # Reveal-math guard still applies — we don't drive into walls.
        if gap > 0 and self._reveal_math_blocks_buy(fair_eff):
            return 0
        if gap < 0 and self._reveal_math_blocks_sell(fair_eff):
            return 0

        # Hard inventory floor — never breach position_limit
        hard_max = self.position_limit
        if gap > 0:
            gap = min(gap, hard_max - self.position)
        else:
            gap = max(gap, -hard_max - self.position)
        if gap == 0:
            return 0

        self._last_directional_drive_t = now

        slice_qty = min(step_qty_cap, abs(gap))
        if gap > 0:
            # BUY through asks within fair_eff + max_pay_ticks
            taken = 0
            for lvl in book.get("asks") or []:
                if taken >= slice_qty:
                    break
                if lvl["price"] > fair_eff + max_pay_ticks:
                    break
                want = min(slice_qty - taken, int(lvl.get("qty") or 0))
                if want <= 0:
                    continue
                if self._snipe("buy", lvl["price"], want):
                    taken += want
            if taken:
                print(f"[v14 DRIVE] +{taken} toward target {target} "
                      f"(pos {self.position}, bias {self._informed_bias_now():+.2f}t)")
            return taken
        else:
            taken = 0
            for lvl in book.get("bids") or []:
                if taken >= slice_qty:
                    break
                if lvl["price"] < fair_eff - max_pay_ticks:
                    break
                want = min(slice_qty - taken, int(lvl.get("qty") or 0))
                if want <= 0:
                    continue
                if self._snipe("sell", lvl["price"], want):
                    taken += want
            if taken:
                print(f"[v14 DRIVE] -{taken} toward target {target} "
                      f"(pos {self.position}, bias {self._informed_bias_now():+.2f}t)")
            return -taken

    # ==================================================================
    # Fill handler: feed bias + drive
    # ==================================================================
    def on_fill_event(self, msg: dict) -> None:
        super().on_fill_event(msg)

        cp = msg.get("counterparty") or ""
        liq = msg.get("liquidity")
        side = msg.get("side")  # OUR side
        qty = int(msg.get("qty") or 0)
        if qty <= 0 or side not in ("buy", "sell"):
            return
        # Only MAKER fills are signal — CP chose to trade against our quote.
        if liq != "maker":
            return

        # Resolve weight: known CP > unknown CP fallback
        if cp in CP_WEIGHT:
            weight = CP_WEIGHT[cp]
        elif cp:
            weight = self.cfg.unknown_cp_default_weight
        else:
            return

        prev_bias = self._informed_bias_now()
        self._update_informed_bias(cp, side, weight)
        new_bias = self._informed_bias_now()

        # If bias is now meaningful, drive toward target.
        if abs(new_bias) >= self.cfg.directional_threshold_ticks:
            try:
                taken = self._drive_to_directional_target()
            except Exception as e:
                print(f"[v14 DRIVE-ERR] {e!r}")
                taken = 0
            # Also reprice quotes immediately so opposite side is killed.
            try:
                with self.lock:
                    bid_px, ask_px, _, _ = self.desired_quotes()
                    self._apply_target_quotes(bid_px, ask_px)
                    self._last_maker_apply_t = time.time()
            except Exception as e:
                print(f"[v14 REPRICE-ERR] {e!r}")

    # ==================================================================
    # Quote event override: drive if bias is active
    # ==================================================================
    def on_quote_event(self, msg: dict) -> None:
        super().on_quote_event(msg)
        # If bias is active, also drive on quote_add events (book changed).
        if abs(self._informed_bias_now()) < self.cfg.directional_threshold_ticks:
            return
        try:
            self._drive_to_directional_target()
        except Exception as e:
            print(f"[v14 DRIVE-ERR] {e!r}")


# ---------------------------------------------------------------------------
# Standalone runner. Prefer run_combined14.
# ---------------------------------------------------------------------------
def main() -> None:
    from sdk.client import GameClient
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol="A")
    print(f"v14: reveal_interval={strat.reveal_interval}s  "
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

    print("\nBot started (v14). Commands: s=status, b=bias+target, "
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
                bias = strat._informed_bias_now()
                tgt = strat._bias_target_position()
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"bias={bias:+.2f}t  target={tgt:+d}  "
                      f"k={len(strat.posterior.reveals)}/{strat.n_total}")
            elif cmd == "b":
                bias = strat._informed_bias_now()
                tgt = strat._bias_target_position()
                age = time.time() - strat._informed_bias_set_t
                print(f"  bias={bias:+.2f}t  raw={strat._informed_bias_ticks:+.2f}t "
                      f"age={age:.1f}s  cp={strat._informed_bias_cp!r}  "
                      f"w={strat._informed_bias_weight:.2f}  target_pos={tgt:+d}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown {cmd!r}; try s/b/f/q")
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
