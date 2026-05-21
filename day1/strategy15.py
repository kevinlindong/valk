"""Day 1 live trading strategy v15 — leverage B-tape as forward signal for A.

WHY v15: tick_settlement events in v13 log prove B settles at the LAST
reveal value of the same X_i sequence that A sums to. Specifically:

  Game 1 A reveals = 6, 5, 5, 5, 3, 5  -> A settle=29, B settle=5 (=X_6)
  Game 2 A reveals = 25,24,11,20,25,21 -> A settle=126, B settle=21 (=X_6)

So B(t) = market estimate of X_next (the next A reveal value). The
informed_sniper_next bot CVALK on B is configured to trade B knowing
the upcoming X. We can't see CVALK by name on the public tape (quote_add
events strip counterparty), but their info LEAKS into B's price and
aggressor flow. We piggyback on B's market to bias A.

Concrete signal from v13 log B VWAP in 5s pre-reveal vs subsequent A reveal:
  reveal A= 6 : B_vwap= 7.64 buy_q=9  sell_q=5  (off by +1.6)
  reveal A= 5 : B_vwap= 5.40 buy_q=2  sell_q=3  (off by +0.4)
  reveal A= 5 : B_vwap= 7.48 buy_q=27 sell_q=6  (off by +2.5, strong-buy)
  reveal A= 5 : B_vwap= 4.10 buy_q=3  sell_q=7  (off by -0.9, strong-sell)
  reveal A= 3 : B_vwap= 4.92 buy_q=4  sell_q=9  (off by +1.9, sell)
  reveal A= 5 : B_vwap= 5.00 buy_q=6  sell_q=2  (close)
  reveal A=25 : B_vwap= 9.53 buy_q=53 sell_q=2  (off by -15.5, MASSIVE buy)
  reveal A=24 : B_vwap=25.59 buy_q=21 sell_q=11 (close)
  reveal A=11 : B_vwap=21.28 buy_q=2  sell_q=65 (off by +10, MASSIVE sell)
  reveal A=20 : B_vwap=17.20 buy_q=1  sell_q=9  (off by -2.8)
  reveal A=25 : B_vwap=20.92 buy_q=38 sell_q=10 (off by -4)
  reveal A=21 : B_vwap=21.00 buy_q=20 sell_q=10 (exact)

The B aggressor-flow column is even more useful than B vwap: strong-buy
B in game2 reveal#1 predicted the jump to 25; strong-sell B at reveal#3
predicted the drop to 11. Even when B vwap is laggy, the FLOW DIRECTION
on B is a clean leading indicator of the next A reveal direction.

WHAT'S NEW (delta from v14):

  A. B_TAPE SUBSCRIPTION. Strategy now listens to ALL trade events
     (not only symbol=A). B trades feed a separate ring buffer
     `_b_trades` with (ts, price, qty, aggressor).

  B. SYNTHETIC B_TAPE BIAS. Every `b_tape_check_interval_sec` we
     compute B's aggressor-flow imbalance over a short window
     (b_tape_window_sec). If |flow_q| >= b_tape_flow_threshold (lots),
     we set the informed bias as if a CP named "B_TAPE" with weight
     `b_tape_weight` had aggressed in that direction. Re-uses the v14
     bias plumbing -> directional position-targeting fires for free.

  C. B-MID PRIOR FOR X_NEXT. `_b_implied_next_value()` returns recent
     B vwap if recent volume suffices. Used to nudge fair_eff for A
     when remaining reveals exist: shifts fair by
       n_remaining * (b_implied - prior_mean_per_reveal) * b_nudge_factor.
     Conservative: capped at b_nudge_max_ticks. Adds to bias rather
     than replacing it.

  D. CVALK / GVALK / IVALK / FVALK weights kept at 1.0 in CP_WEIGHT —
     if any of them ever fill us on A directly (e.g. cross-market
     hedge), we still treat them as informed.

DESIGN INVARIANTS PRESERVED (from v14):
  * Lock-free hot path, PrecomputedScenario dict lookup, single
    _snipe path, cancel+post only, WS book cache primary.
  * Precompute built from raw posterior — bias does NOT enter
    precomputed scenarios (reveals supersede prior flow signal).

Run:
    python day1/run_combined15.py
    python day1/strategy15.py            # standalone
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

from strategy12 import (  # noqa: F401  (re-export for runners)
    URL, API_KEY, N_PRIOR_SIM, Posterior, PrecomputedScenario,
    CPProfile,
)
from strategy14 import (  # noqa: F401
    CP_WEIGHT, STALE_CPS,
    Config as ConfigV14,
    Strategy as StrategyV14,
)


@dataclass
class Config(ConfigV14):
    """Strict superset of v14 Config with B-tape signal knobs."""
    # ---- B-tape monitoring ----
    b_tape_enabled: bool = True
    b_tape_window_sec: float = 6.0
    b_tape_check_interval_sec: float = 0.5
    # If |buy_q - sell_q| in B over the window >= this, trigger bias.
    b_tape_flow_threshold: int = 8
    # Treat B-tape signal as a synthetic CP at this informedness weight.
    b_tape_weight: float = 0.6
    # Max B-tape ring buffer length.
    b_trade_buf_len: int = 1024

    # ---- B-mid implied X_next prior ----
    b_nudge_factor: float = 0.5    # how much of the (b_implied - prior_mean)
                                    # delta we apply per remaining reveal
    b_nudge_max_ticks: float = 4.0  # cap absolute fair_eff shift contribution
    b_nudge_min_volume: int = 5     # require this much B volume in window
    b_nudge_min_volume_pre_first: int = 10  # higher bar pre-first-reveal


class Strategy(StrategyV14):
    """v15: v14 + B-tape signal feeding informed bias and fair nudge."""

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

        # Init B-tape state BEFORE super (background threads may call our
        # _vwap_clamped_fair override which reads _b_trades).
        self._b_trades: deque[tuple[float, float, int, str]] = deque(
            maxlen=kwargs["config"].b_trade_buf_len)
        self._last_b_tape_check_t: float = 0.0
        self._b_implied_cache: tuple[float, Optional[float]] = (0.0, None)

        super().__init__(*args, **kwargs)

        print(f"[v15 B_TAPE] enabled={self.cfg.b_tape_enabled}  "
              f"window={self.cfg.b_tape_window_sec}s  "
              f"flow_thresh={self.cfg.b_tape_flow_threshold}lots  "
              f"weight={self.cfg.b_tape_weight}")
        print(f"[v15 B_NUDGE] factor={self.cfg.b_nudge_factor}  "
              f"max={self.cfg.b_nudge_max_ticks}t  "
              f"min_vol={self.cfg.b_nudge_min_volume}")

    # ==================================================================
    # B-tape ingestion
    # ==================================================================
    def on_trade(self, msg: dict) -> None:
        # Route A-tape to base; B-tape to our buffer.
        sym = msg.get("symbol")
        if sym == self.symbol:
            super().on_trade(msg)
            return
        if sym != "B":
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
        # No lock needed: deque append is atomic in CPython.
        self._b_trades.append((now, price, qty, aggressor))
        # Throttle bias check.
        if (self.cfg.b_tape_enabled
                and now - self._last_b_tape_check_t
                >= self.cfg.b_tape_check_interval_sec):
            self._last_b_tape_check_t = now
            try:
                self._maybe_apply_b_tape_bias(now)
            except Exception as e:
                print(f"[v15 B_TAPE] check error: {e!r}")

    def _b_window(self, now: float) -> list[tuple[float, float, int, str]]:
        cutoff = now - self.cfg.b_tape_window_sec
        # Snapshot to avoid mutation during iteration.
        snap = list(self._b_trades)
        return [r for r in snap if r[0] >= cutoff]

    def _b_implied_next_value(self, now: Optional[float] = None
                              ) -> Optional[float]:
        """Volume-weighted B price over recent window. Returns None when
        insufficient volume. Cached for 100ms."""
        if now is None:
            now = time.time()
        ct, cv = self._b_implied_cache
        if now - ct < 0.1:
            return cv
        window = self._b_window(now)
        if not window:
            self._b_implied_cache = (now, None)
            return None
        num = 0.0
        den = 0
        for _, p, q, _ in window:
            num += p * q
            den += q
        if (len(self.posterior.reveals) == 0
                and den < self.cfg.b_nudge_min_volume_pre_first):
            self._b_implied_cache = (now, None)
            return None
        if den < self.cfg.b_nudge_min_volume:
            self._b_implied_cache = (now, None)
            return None
        val = num / den
        self._b_implied_cache = (now, val)
        return val

    def _b_aggressor_flow(self, now: float) -> tuple[int, int]:
        """(buy_qty, sell_qty) over the recent B window."""
        window = self._b_window(now)
        buy_q = sum(q for _, _, q, a in window if a == "buy")
        sell_q = sum(q for _, _, q, a in window if a == "sell")
        return buy_q, sell_q

    # ==================================================================
    # B-tape -> synthetic bias
    # ==================================================================
    def _maybe_apply_b_tape_bias(self, now: float) -> None:
        if self.phase != "running":
            return
        buy_q, sell_q = self._b_aggressor_flow(now)
        net = buy_q - sell_q
        if abs(net) < self.cfg.b_tape_flow_threshold:
            return
        # Direction: B flow buy-heavy => X_next likely HIGHER => bias A +.
        # We encode that as "synthetic CP bought from us" (side_we=sell).
        side_we = "sell" if net > 0 else "buy"
        weight = self.cfg.b_tape_weight
        prev = self._informed_bias_now()
        self._update_informed_bias("B_TAPE", side_we, weight)
        new = self._informed_bias_now()
        # Only print on meaningful changes
        if abs(new - prev) >= 0.5:
            print(f"[v15 B_TAPE_BIAS] net={net:+d}lots over "
                  f"{self.cfg.b_tape_window_sec}s -> "
                  f"synthetic {side_we} signal, bias={new:+.2f}t")

    # ==================================================================
    # B-mid prior nudge on fair (additive, capped)
    # ==================================================================
    def _b_nudge_ticks(self, fair: float) -> float:
        if not self.cfg.b_tape_enabled or self.cfg.b_nudge_factor <= 0:
            return 0.0
        if self.cfg.b_nudge_max_ticks <= 0:
            return 0.0
        n_rem = self._n_remaining()
        if n_rem <= 0:
            return 0.0
        now = time.time()
        b_imp = self._b_implied_next_value(now)
        if b_imp is None:
            return 0.0
        # prior_mean_per_reveal: best guess of one X_i from current posterior
        k = len(self.posterior.reveals)
        if k == 0:
            # Use prior expected value per reveal (from initial fair_and_sigma)
            try:
                prior_total, _ = self.posterior.predict_settle(0.0, self.n_total)
                prior_per_reveal = prior_total / max(self.n_total, 1)
            except Exception:
                return 0.0
        else:
            # Per-reveal mean from posterior: (fair_total - running_sum) / n_remaining
            try:
                fair_total, _ = self.posterior.predict_settle(
                    sum(self.posterior.reveals), n_rem)
            except Exception:
                return 0.0
            prior_per_reveal = (fair_total - sum(self.posterior.reveals)) / max(n_rem, 1)
        delta_per_reveal = b_imp - prior_per_reveal
        # Apply to remaining reveals at the configured factor.
        nudge = self.cfg.b_nudge_factor * delta_per_reveal * n_rem
        # Cap.
        cap = self.cfg.b_nudge_max_ticks
        if nudge > cap:
            nudge = cap
        elif nudge < -cap:
            nudge = -cap
        return nudge

    # ==================================================================
    # Override pricing: stack bias + B-nudge on top of v14
    # ==================================================================
    def _vwap_clamped_fair(self, fair: float) -> float:
        # v14 returns vwap_clamped + bias. Add B-nudge on top.
        base = super()._vwap_clamped_fair(fair)
        nudge = self._b_nudge_ticks(fair)
        return base + nudge

    # ==================================================================
    # Reset on phase change
    # ==================================================================
    def on_phase_change(self, phase: Optional[str],
                        reveals: list[float]) -> None:
        super().on_phase_change(phase, reveals)
        if phase == "running":
            self._b_trades.clear()
            self._last_b_tape_check_t = 0.0
            self._b_implied_cache = (0.0, None)


# ---------------------------------------------------------------------------
# Standalone runner. Prefer run_combined15.
# ---------------------------------------------------------------------------
def main() -> None:
    from sdk.client import GameClient
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol="A")
    print(f"v15: reveal_interval={strat.reveal_interval}s  "
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

    print("\nBot started (v15). Commands: s=status, b=bias, x=B-tape, "
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
                fair_eff = strat._vwap_clamped_fair(fair)
                b_imp = strat._b_implied_next_value()
                b_str = f"{b_imp:.2f}" if b_imp is not None else "-"
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"fair_eff={fair_eff:.1f}  bias={bias:+.2f}t  target={tgt:+d}  "
                      f"B_implied={b_str}  "
                      f"k={len(strat.posterior.reveals)}/{strat.n_total}")
            elif cmd == "x":
                now = time.time()
                b_imp = strat._b_implied_next_value(now)
                buy_q, sell_q = strat._b_aggressor_flow(now)
                window = strat._b_window(now)
                print(f"  B_implied={b_imp}  buy_q={buy_q} sell_q={sell_q} "
                      f"net={buy_q - sell_q:+d}  trades={len(window)}")
            elif cmd == "b":
                bias = strat._informed_bias_now()
                tgt = strat._bias_target_position()
                age = time.time() - strat._informed_bias_set_t
                print(f"  bias={bias:+.2f}t age={age:.1f}s  "
                      f"cp={strat._informed_bias_cp!r} w={strat._informed_bias_weight:.2f}  "
                      f"target_pos={tgt:+d}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown {cmd!r}; try s/b/x/f/q")
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
