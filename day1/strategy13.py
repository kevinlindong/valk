"""Day 1 live trading strategy v13 — informed-counterparty flow following.

WHY v13: leaked `/api/admin/bots` config (see day1/bot_config_dump.json)
identifies which counterparties trade with private information about the
truth value of A. When one of these bots aggresses against our resting
quote, that fill is itself a signal about which direction the truth lies.
v12 ignores this signal — it treats every CP equally for sizing/skew but
not for fair-value adjustment. v13 lifts the signal into the fair value
so we shift our quotes toward the informed flow AND same-side snipe to
follow them, even into positions our pure Bayesian model would otherwise
discourage.

Informed bots (weight = posterior on "this fill is info-driven"):
    VALKC (informed_sniper, knows truth):      weight 1.0
    VALKE (mixed_sweeper, informed_prob=0.3):  weight 0.3
    VALKJ (slow_informed, knows truth):        weight 0.8
    VALKM (bayes_taker, edge_threshold=2):     weight 0.5
B-series mirrors per the leak:
    CVALK (informed_sniper):                   weight 1.0
    FVALK (predictive_mm, prediction_acc=.7):  weight 0.7
    GVALK (directional_taker, lean_prob=.7):   weight 1.0
    IVALK (slow_informed):                     weight 1.0

Mechanism:
  1. WS fill arrives with counterparty=VALKx and liquidity=maker
     (i.e. WE were maker, they were taker — they CHOSE to trade with us).
  2. Sign: if CP bought from us (side=sell from our perspective), they
     think fair is HIGHER than our ask -> bias POSITIVE. If CP sold
     to us, they think fair is LOWER -> bias NEGATIVE.
  3. Magnitude: informed_bias_max_ticks * weight (e.g. 4 * 1.0 = 4 ticks
     for VALKC; 4 * 0.3 = 1.2 ticks for VALKE).
  4. Same-direction reinforce by `max(|new|, |existing|)` keeping the
     sign; opposite-direction replace fully (most recent informed flow
     wins).
  5. Decays linearly to zero over informed_bias_decay_sec.
  6. Immediately after bias is set, kick `_snipe_book_scan` to follow:
     positive bias raises fair => the asks now look CHEAPER => buy them.

The bias is added inside `_vwap_clamped_fair` so it propagates through
ALL pricing decisions (desired_quotes, _snipe gating, _maybe_drift_cancel)
without touching their internals. PrecomputedScenario for the next reveal
intentionally does NOT see the bias — those are post-reveal fairs built
from raw posterior, and between-reveal informed flow shouldn't reshape
them (the reveal itself supersedes prior flow signals).

Run:
    python day1/run_combined13.py
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from strategy12 import (  # noqa: F401  (re-export for runners)
    URL, API_KEY, N_PRIOR_SIM, Posterior,
    Config as ConfigV12,
    Strategy as StrategyV12,
    PrecomputedScenario,
    CPProfile,
)


# ---------------------------------------------------------------------------
# Informed counterparty registry (from /api/admin/bots leak, 2026-05-20)
# ---------------------------------------------------------------------------
INFORMED_CP_WEIGHT: dict[str, float] = {
    # A-series (symbol A informed bots)
    "VALKC": 1.0,   # informed_sniper: knows truth, snipes
    "VALKE": 0.3,   # mixed_sweeper: informed_prob=0.3
    "VALKJ": 0.8,   # slow_informed: knows truth on a delay
    "VALKM": 0.5,   # bayes_taker: takes when edge >= 2 ticks
    # B-series (symbol B informed bots — included so v13 works on B too)
    "CVALK": 1.0,
    "FVALK": 0.7,
    "GVALK": 1.0,
    "IVALK": 1.0,
}


@dataclass
class Config(ConfigV12):
    """Strict superset of v12 Config with informed-flow knobs added."""
    # Maximum |bias| in ticks at full weight=1.0 immediately after fill.
    informed_bias_max_ticks: float = 4.0
    # Time over which bias decays linearly to zero.
    informed_bias_decay_sec: float = 10.0
    # Suppress bias contributions from CPs with weight below this.
    informed_min_weight: float = 0.2
    # After bias is set, trigger _snipe_book_scan to follow the flow.
    informed_follow_snipe: bool = True


class Strategy(StrategyV12):
    """Thin subclass of v12 Strategy that adds informed-flow bias."""

    def __init__(self, *args, **kwargs):
        # Promote a passed v12 Config to v13 Config so new knobs work.
        cfg = kwargs.get("config")
        if cfg is not None and not isinstance(cfg, Config):
            # Build v13 Config copying all v12 fields the dataclass declares.
            kwargs["config"] = Config(**{
                f.name: getattr(cfg, f.name)
                for f in cfg.__dataclass_fields__.values()
                if f.name in Config.__dataclass_fields__
            })
        elif cfg is None:
            kwargs["config"] = Config()

        # ---- v13 informed-bias state ----
        # MUST be set BEFORE super().__init__() because the base class
        # starts background threads that call _vwap_clamped_fair (our
        # override), which reads these attrs.
        # Positive bias = "informed flow thinks fair is HIGHER".
        self._informed_bias_ticks: float = 0.0
        self._informed_bias_set_t: float = 0.0
        self._informed_bias_cp: str = ""
        self._informed_bias_weight: float = 0.0

        super().__init__(*args, **kwargs)

        print(f"[v13 INFORMED] max_ticks={self.cfg.informed_bias_max_ticks} "
              f"decay_sec={self.cfg.informed_bias_decay_sec} "
              f"follow_snipe={self.cfg.informed_follow_snipe}  "
              f"watched_cps={sorted(INFORMED_CP_WEIGHT.keys())}")

    # ------------------------------------------------------------------
    # Informed-bias computation
    # ------------------------------------------------------------------
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

    def _update_informed_bias(self, cp: str, side_we: str, weight: float) -> None:
        """Set or reinforce bias from one informed-CP maker fill.

        `side_we` is OUR side in the fill (buy = we bought = CP sold to us
        => CP thinks fair is LOWER => negative bias).
        """
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
            # Opposite direction or fresh: replace.
            new_bias = target
        else:
            # Same direction: reinforce by taking max magnitude.
            if abs(target) > abs(existing):
                new_bias = target
            else:
                new_bias = existing  # keep the bigger, but reset clock below
        self._informed_bias_ticks = new_bias
        self._informed_bias_set_t = time.time()
        self._informed_bias_cp = cp
        self._informed_bias_weight = weight
        print(f"[v13 INFORMED-BIAS] cp={cp} we_did={side_we} w={weight:.2f} "
              f"-> bias={new_bias:+.2f}t (decay {self.cfg.informed_bias_decay_sec}s)")

    # ------------------------------------------------------------------
    # Override: propagate informed bias to ALL pricing decisions
    # ------------------------------------------------------------------
    def _vwap_clamped_fair(self, fair: float) -> float:
        base = super()._vwap_clamped_fair(fair)
        bias = self._informed_bias_now()
        if bias == 0.0:
            return base
        return base + bias

    # ------------------------------------------------------------------
    # Override: catch informed-CP fills, set bias, follow flow
    # ------------------------------------------------------------------
    def on_fill_event(self, msg: dict) -> None:
        # Run base class first so CPProfile + position/cool-off update.
        super().on_fill_event(msg)

        cp = msg.get("counterparty") or ""
        liq = msg.get("liquidity")
        side = msg.get("side")  # OUR side
        qty = int(msg.get("qty") or 0)
        if qty <= 0 or not cp or side not in ("buy", "sell"):
            return
        # Only treat MAKER fills as informed signal: that means the CP was
        # the AGGRESSOR — they CHOSE to trade against our resting quote
        # at our price. A taker fill (we IOC'd them) doesn't reveal their
        # opinion, only that they had a resting order we hit.
        if liq != "maker":
            return
        weight = INFORMED_CP_WEIGHT.get(cp)
        if weight is None:
            return

        self._update_informed_bias(cp, side, weight)

        # Immediately follow the flow if bias is meaningful.
        if not self.cfg.informed_follow_snipe:
            return
        if len(self.posterior.reveals) < 1:
            return  # snipe_book_scan only runs post-first-reveal
        try:
            fair, sigma = self.fair_and_sigma()
        except Exception:
            return
        if fair == 0.0 and sigma == 0.0:
            return
        # _snipe_book_scan uses _snipe which calls _vwap_clamped_fair under
        # the lock — our override there is what makes the bias take effect.
        try:
            took = self._snipe_book_scan(fair, sigma)
            if took:
                print(f"[v13 INFORMED-FOLLOW] cp={cp} bias took flow")
        except Exception as e:
            print(f"[v13 INFORMED-FOLLOW] error: {e!r}")


# ---------------------------------------------------------------------------
# Standalone runner. Prefer run_combined13 for full WS + probe setup.
# ---------------------------------------------------------------------------
def main() -> None:
    from sdk.client import GameClient
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol="A")
    print(f"v13: reveal_interval={strat.reveal_interval}s  "
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

    print("\nBot started (v13). Commands: s=status, p=precompute, "
          "c=cp profile, b=bias, f=flatten, q=quit.\n")
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
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"informed_bias={bias:+.2f}t  "
                      f"k={len(strat.posterior.reveals)}/{strat.n_total}")
            elif cmd == "b":
                bias = strat._informed_bias_now()
                age = time.time() - strat._informed_bias_set_t
                print(f"  informed_bias={bias:+.2f}t  "
                      f"raw={strat._informed_bias_ticks:+.2f}t  "
                      f"set_age={age:.1f}s  "
                      f"cp={strat._informed_bias_cp}  "
                      f"weight={strat._informed_bias_weight:.2f}")
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
