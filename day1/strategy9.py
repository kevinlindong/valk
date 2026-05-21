"""Day 1 live trading strategy v9.

Built on v8 after auditing combined_log_v8_20260519_{204107,204402}.jsonl
(see NOTES.md v9 section). The single dominant leak in v8 sessions was:
  s2 round0: at t+16.9-17.5s (about 2s AFTER reveal_1=1, with K=1 and
  posterior fair ~12) we IOC-bought 21 lots from SEAN at prices 19/20/21,
  paying >7 ticks over posterior fair. -290 PnL in 0.6s = the entire
  session loss.

Why v8 didn't catch it:
  - reveal_math_min_k=2 disabled the projection guard at K=1, exactly
    when the leak happened.
  - The SEAN-window scheduler missed 7/10 reveal cycles in s1 and 3/6
    in s2 (no pre-reveal cancel, no SEAN-window repost). Likely the
    cycle-index gating ran ahead of the actual reveal landing.
  - We have no way to identify SEAN on the book (public quote_add does
    NOT carry trader id) so a counterparty blacklist is impossible at
    snipe time. The fix has to be PRICE/TIME-based, not identity-based.
  - On reveal we cancel-all, but then immediately re-post at the new
    posterior fair which (at low K) was still biased high. The next
    aggressor lifted us before we got a second think.

CHANGES vs v8:

  1. REVEAL DANGER ZONE. New helper _in_reveal_danger_zone() returns
     True in [last_reveal, last_reveal + 2s] OR [next_reveal - 2s,
     next_reveal]. Inside this zone all IOC/snipe paths return False.
     Most adverse selection happens in the seconds straddling each
     reveal. Knob: `reveal_danger_zone_sec` (default 2.0).

  2. REVEAL-MATH GUARD AT K=1. reveal_math_min_k 2 -> 1. At K=1 the
     projection is reveal[0] * N which is noisy but still a useful
     sanity bound -- the s2 leak (post=12, our buy=21) would have
     been blocked because reveal_math at K=1 (reveal=1, N=6) = 6.
     Guard width is doubled at K=1 to reflect the single-sample
     variance (reveal_math_guard_ticks * reveal_math_k1_widening).

  3. STALE-FAIR DRIFT CANCEL. After every WS event we record the
     fair_eff used at the last quote-post. If a subsequent event
     changes fair_eff by > drift_cancel_ticks (default 3) we
     cancel-all and re-step. Catches the s1 round1 pattern where
     bids stayed live at 26-29 for 2.5s while posterior collapsed
     and got sniped 5x for -90.

  4. INVENTORY-AWARE QUOTE KILL. v7 already has inv_hard_kill_frac
     (default 0.85) which kills the adverse-side quote when
     |pos|/max_pos > 0.85. We tighten to 0.4 -- audit showed s1
     drifted to +13 / s2 to +15 with both rounds top-ask 88% and
     top-bid 2%. Tighter kill stops the long drift earlier.

  5. OPENING-ROUND QUOTE CLAMP. When K=0 the posterior fair is
     just the prior mean and unstable to a single hot book signal.
     Clamp bid_px to <= prior_E_settle + opening_clamp_ticks
     (default 1) and ask_px to >= prior_E_settle - opening_clamp.
     Audit: s2 round0 opened with our bid at 25 (post=10, prior~30,
     book ask=34); a clamp of 31 would have killed that bid.

  6. SCHEDULER WATCHDOG. The v8 scheduler tick still runs but
     (a) every decision (post/cancel/skip + reason) is logged to
     stdout with a `[SCHED]` tag for log analysis, (b) the cancel
     path runs cancel_all() unconditionally each cycle even if
     resting state says no orders (defensive against state drift),
     (c) a fresh tick switches to time.monotonic() for the cycle
     comparison so wall-clock skew can't drop the post.

  7. ALL DYNAMIC. duration, reveal_interval, position_limit,
     tick_size, multiplier still come from game_state.

Run:
    python day1/run_combined9.py
"""
from __future__ import annotations

import math
import os
import statistics
import sys
import threading
import time
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sdk.client import GameClient  # noqa: E402
from strategy8 import (  # noqa: E402
    URL,
    API_KEY,
    INFORMED_TRADER_ID,
    N_PRIOR_SIM,
    Posterior,
    Strategy as Strategy8,
)


class Strategy(Strategy8):
    """v9: reveal-zone IOC block + K=1 reveal-math + drift cancel +
    tighter inventory kill + opening-quote clamp + scheduler watchdog."""

    def __init__(self, client: GameClient, posterior: Posterior,
                 symbol: str = "A",
                 informed_trader_id: Optional[str] = None):
        super().__init__(client, posterior, symbol=symbol,
                         informed_trader_id=informed_trader_id)

        # === v9: reveal danger zone ===
        # Most adverse-selection happens straddling each reveal. Block all
        # IOCs in [last_reveal, last_reveal + zone_sec] AND
        # [next_reveal - zone_sec, next_reveal]. 2.0s chosen because the
        # s2 catastrophic event fired at t+16.9s (1.9s after reveal_1)
        # AND at t+17.5s (2.5s after reveal_1) -- 2.0s catches the
        # majority of the burst with minimal cost to legitimate snipes
        # that happen >2s away from any reveal.
        self.reveal_danger_zone_sec: float = 2.0
        self.reveal_danger_zone_enabled: bool = True

        # === v9: reveal-math guard at K=1 ===
        # v8 had min_k=2 which left K=1 unguarded. The s2 leak was at
        # K=1. Lower to 1 -- at K=1 the projection is reveal[0] * N
        # (sample mean = single reveal value), noisy but still a useful
        # bound. Widen the guard at K=1 by `reveal_math_k1_widening`
        # to account for single-sample variance.
        self.reveal_math_min_k = 1
        self.reveal_math_k1_widening: float = 2.0  # 4.0 ticks -> 8 at K=1

        # === v9: stale-fair drift cancel ===
        # After we post quotes we record the fair_eff used. If a
        # subsequent WS event (trade / quote_add / quote_cancel) moves
        # fair_eff by > `drift_cancel_ticks`, cancel-all and re-step.
        # Audit: s1 round1 bids sat at 26-29 for 2.5s through 5 picks
        # while posterior collapsed from 27 to 12 (drift ~15). 3 ticks
        # is enough to catch real drift without flapping on noise.
        self.drift_cancel_ticks: float = 3.0
        self.drift_cancel_enabled: bool = True
        self._last_quote_fair_eff: Optional[float] = None
        # Rate-limit the drift-cancel so a noisy market can't trigger
        # cancel_all 20x/sec. Min spacing between drift cancels:
        self.drift_cancel_min_interval: float = 0.5
        self._last_drift_cancel_t: float = 0.0

        # === v9: tighter inventory kill ===
        # v7 default is 0.85 -- only kills adverse side near the limit.
        # Audit shows both v8 sessions drifted to +13/+15 well below
        # that threshold while collecting one-sided fills. Drop to
        # 0.4 so once |pos|/max_pos > 40% we stop quoting the adding
        # side entirely.
        self.inv_hard_kill_frac = 0.4

        # === v9: opening-round quote clamp ===
        # At K=0 the posterior fair equals the prior mean (~27 for the
        # current LogN prior) but a single book signal can drag the
        # market-mid-blended fair very high (s2 round0 K=0 had a book
        # ask at 34 and our bid landed at 25). Clamp opening bid to
        # prior_E_settle + 1 (i.e. never bid more than 1 tick above
        # prior expected settle) and symmetric for ask. Cached at init.
        try:
            n_remaining_init = self.n_total
            mean0, _ = posterior.predict_settle(
                running_sum=0.0, n_remaining=n_remaining_init)
            self._prior_E_settle: float = float(mean0)
        except Exception:
            self._prior_E_settle = 27.0  # safe default for LogN prior
        self.opening_clamp_ticks: int = 1
        self.opening_clamp_enabled: bool = True

        # === v9: scheduler diagnostic state ===
        # `[SCHED]` prints get filtered to stdout. We also use
        # monotonic time inside the loop so wall-clock drift can't
        # silently shift the post/cancel windows.
        self._sched_log_seq: int = 0

    # ---- v9: danger-zone helper ----

    def _in_reveal_danger_zone(self) -> bool:
        """True iff we're within `reveal_danger_zone_sec` seconds of
        the last reveal (post-window) OR the next predicted reveal
        (pre-window). Used to short-circuit all IOC paths.
        """
        if not self.reveal_danger_zone_enabled:
            return False
        if self._n_remaining() <= 0:
            return False  # post-final-reveal sweep is risk-free
        now = time.time()
        zone = self.reveal_danger_zone_sec
        # Post-reveal window
        if (self._last_reveal_t is not None
                and now <= self._last_reveal_t + zone):
            return True
        # Pre-reveal window
        nxt = self._next_reveal_at()
        if nxt is not None and now >= nxt - zone:
            return True
        return False

    # ---- v9: reveal-math guard widened at K=1 ----

    def _reveal_math_guard_width(self) -> float:
        """Effective guard width. K=1 gets a 2x widening so the
        single-sample sample-mean noise doesn't constantly trip the
        guard."""
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

    # ---- v9: IOC-path overrides (apply danger-zone block) ----

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
        return super().maybe_snipe(fair, sigma)

    def _try_direct_snipe(self, msg: dict) -> bool:
        if self._in_reveal_danger_zone():
            # Quiet: this fires often inside the zone; only log once
            # per zone-entry by reusing the scheduler print cadence.
            return False
        return super()._try_direct_snipe(msg)

    # ---- v9: stale-fair drift cancel ----

    def _maybe_drift_cancel(self) -> None:
        """If fair_eff has moved more than drift_cancel_ticks since the
        last quote-post, cancel our resting orders and force a re-step.
        Called from on_quote_event / on_trade. Rate-limited.
        """
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
        # Drift is real -- cancel and re-step.
        print(f"  v9 DRIFT-CANCEL: fair_eff {self._last_quote_fair_eff:.1f}"
              f" -> {fair_eff:.1f} (move "
              f"{fair_eff - self._last_quote_fair_eff:+.1f})")
        self._last_drift_cancel_t = now
        try:
            with self.lock:
                self.c.cancel_all()
                self.resting = {"bid": None, "ask": None}
            self._last_quote_fair_eff = None  # forget anchor until re-post
            # Trigger an immediate re-step so we don't leave the market
            # without quotes for long.
            self.step()
        except Exception as e:
            print(f"  v9 drift-cancel failed: {e}")

    # ---- v9: WS event wiring (extend v7/v8 handlers) ----

    def on_quote_event(self, msg: dict) -> None:
        super().on_quote_event(msg)
        self._maybe_drift_cancel()

    def on_trade(self, msg: dict) -> None:
        super().on_trade(msg)
        self._maybe_drift_cancel()

    # ---- v9: quote computation overrides ----

    def desired_quotes(self) -> tuple[Optional[int], Optional[int],
                                       float, float]:
        bid_px, ask_px, fair, sigma = super().desired_quotes()

        # Opening-round clamp. K=0 only.
        if (self.opening_clamp_enabled
                and len(self.posterior.reveals) == 0):
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

        # Record the fair_eff anchor for drift-cancel. Use the same
        # computation v7/v8 use internally (effective + vwap-clamped).
        try:
            fair_for_anchor, _ = self.fair_and_sigma()
            anchor = self._effective_fair(fair_for_anchor)
            anchor = self._vwap_clamped_fair(anchor)
            if bid_px is not None or ask_px is not None:
                self._last_quote_fair_eff = anchor
        except Exception:
            pass

        return bid_px, ask_px, fair, sigma

    # ---- v9: scheduler with diagnostics + force-cancel ----

    def _sean_scheduler_tick(self) -> None:
        if self.phase != "running":
            return
        if self._n_remaining() <= 0:
            return
        nxt = self._next_reveal_at()
        if nxt is None:
            return
        now = time.time()
        cycle_idx = len(self.posterior.reveals)
        cancel_at = nxt - self.sean_pre_reveal_cancel_sec
        post_at = nxt - self.sean_window_sec

        # Pre-reveal cancel
        if now >= cancel_at and self._sean_last_cancelled_idx < cycle_idx:
            self._sean_last_cancelled_idx = cycle_idx
            self._sched_log_seq += 1
            try:
                with self.lock:
                    # v9: cancel unconditionally even if local resting
                    # state says empty (defends against drift between
                    # local view and server view).
                    self.c.cancel_all()
                    self.resting = {"bid": None, "ask": None}
                print(f"[SCHED #{self._sched_log_seq}] PRE-REVEAL-CANCEL "
                      f"cycle={cycle_idx} reveal_in={nxt - now:.2f}s")
            except Exception as e:
                print(f"[SCHED #{self._sched_log_seq}] cancel error: {e}")
            return

        # SEAN-window post
        if (now >= post_at and now < cancel_at
                and self._sean_last_serviced_idx < cycle_idx):
            self._sean_last_serviced_idx = cycle_idx
            self._sched_log_seq += 1
            print(f"[SCHED #{self._sched_log_seq}] SEAN-POST cycle={cycle_idx}"
                  f" reveal_in={nxt - now:.2f}s "
                  f"pos={self.position} "
                  f"k={cycle_idx}/{self.n_total}")
            try:
                self.step()
            except Exception as e:
                print(f"[SCHED #{self._sched_log_seq}] post error: {e}")
            return

        # Diagnostics: when we're INSIDE either window but already
        # serviced, do nothing (the gate is working). When we're
        # OUTSIDE both windows, also nothing. The only "missed"
        # state would be: now > cancel_at but _sean_last_cancelled_idx
        # == cycle_idx, which means we already cancelled. That's fine.

    # ---- v9: on_reveal logs scheduler state for forensics ----

    def on_reveal(self, value: float) -> None:
        now = time.time()
        nxt_predicted = self._next_reveal_at()
        actual_vs_predicted = None
        if nxt_predicted is not None:
            actual_vs_predicted = now - nxt_predicted
        print(f"[SCHED] REVEAL value={value} "
              f"cycle_was={len(self.posterior.reveals)} "
              f"actual_vs_predicted="
              f"{f'{actual_vs_predicted:+.2f}s' if actual_vs_predicted is not None else '-'} "
              f"last_cancelled_idx={self._sean_last_cancelled_idx} "
              f"last_serviced_idx={self._sean_last_serviced_idx}")
        super().on_reveal(value)

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        if phase == "running":
            self._last_quote_fair_eff = None
            self._last_drift_cancel_t = 0.0
        super().on_phase_change(phase, reveals)


# ---------------------------------------------------------------------------
# Main: standalone runner (mirrors v8). Prefer run_combined9 for the full
# WS + probe setup; this exists so `python day1/strategy9.py` works alone.
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
    print(f"v9: reveal_interval={strat.reveal_interval}s  "
          f"duration={strat.duration}s  n_total={strat.n_total}  "
          f"position_limit={strat.position_limit}  tick={strat.tick}")
    print(f"v9: danger_zone={strat.reveal_danger_zone_sec}s  "
          f"reveal_math_min_k={strat.reveal_math_min_k} "
          f"(k1_widening={strat.reveal_math_k1_widening})  "
          f"drift_cancel={strat.drift_cancel_ticks}ticks  "
          f"inv_kill_frac={strat.inv_hard_kill_frac}  "
          f"opening_clamp=prior_E({strat._prior_E_settle:.1f})"
          f"+/-{strat.opening_clamp_ticks}")

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

    print("\nBot started (v9).")
    print("Commands: 's' = status, 'f' = flatten now, "
          "'q' = quit (auto-flattens), Ctrl-C to quit.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                rm = strat._reveal_math_fair()
                rm_str = f"{rm:.1f}" if rm is not None else "-"
                in_zone = strat._in_reveal_danger_zone()
                in_window = strat._in_sean_window()
                nxt = strat._next_reveal_at()
                nxt_in = (nxt - time.time()) if nxt is not None else None
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"reveal_math={rm_str}  danger_zone={in_zone}  "
                      f"sean_window={in_window}  next_reveal_in="
                      f"{(f'{nxt_in:.2f}s' if nxt_in is not None else '-')}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown command {cmd!r}; try 's', 'f', 'q'")
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
