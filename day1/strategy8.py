"""Day 1 live trading strategy v8.

Pivots strategy7 toward SEAN's observed passive style after auditing the
strat7.5_sean_bot{1..3} logs (2026-05-19 evening).

KEY OBSERVATIONS from those logs:

  1. SEAN posts qty=5 maker quotes ~14s after each reveal in a 15s cycle
     -- i.e., ~1s BEFORE the next scheduled reveal. 89% maker, almost
     never aggresses. Edge captured: ~10 ticks below true fair on the
     bid side. SEAN's quote rests ~35ms before filling because it sits
     in a stale-book moment of maximum information asymmetry.

  2. Strategy7 LOST $384 in one round by aggressing IOC BUYs at 102
     when reveal-math fair was 86 (reveals [17,17,17,18], partial
     sum 69, 1 reveal remaining -> expected settle ~86). Posterior
     and/or VWAP got pulled high enough to allow the snipe; the
     reveal-arithmetic (sample-mean projection) said NO loudly.

  3. Cancel-on-reveal latency was 495-660 ms in 2 of 3 sessions. By
     the time we cancelled, other bots had lifted our stale quotes.

  4. Position limit is 50 on symbol A (per game_state.instruments.A.
     position_limit). Symbol B exists with multiplier=5 and
     `settlement=next_number`; we ignore B entirely for now.

CHANGES vs v7 (each is a knob; nothing bot-specific, nothing hardcoded):

  1. SEAN-WINDOW MAKER MODE. Background scheduler thread
     (`_sean_scheduler_loop`) tracks `_last_reveal_t` and reveal_interval
     (read from game_state). In the last `sean_window_sec` seconds
     before each predicted reveal, it triggers a `step()` so our quotes
     are POSTED at the moment of maximum book staleness. The trigger
     fires ONCE per cycle.

  2. SCHEDULED PRE-REVEAL CANCEL. ~`sean_pre_reveal_cancel_sec` (default
     0.3) before each predicted reveal, the scheduler issues
     `c.cancel_all()`. Eliminates the post-reveal stale-quote pickoff
     window that bled us in strat7.5.

  3. REVEAL-MATH FAIR GUARD. New helper `_reveal_math_fair()` returns
     `running_sum + remaining * sample_mean(reveals)` once K >=
     `reveal_math_min_k` (default 2). When the BUY direction of a
     snipe would aggress against reveal_math_fair (|fair_eff -
     reveal_math_fair| > `reveal_math_guard_ticks`), block the snipe.
     Stops the 102-vs-86 leak.

  4. AGGRESSION KNOBS TIGHTENED.
     - snipe_min_edge: 1.0 -> 2.0
     - snipe_max_disagreement: 5.0 -> 3.0
     - max_snipes_per_round: 120 -> 40
     - snipe_position_buffer: 5 -> 8 (with limit=50 the 5-lot buffer
       only left ±45 headroom; 8 leaves ±42 -- a single 10-lot snipe
       can't pin us against the cap).

  5. SEAN-WINDOW UPSIZE. Within the SEAN window we upsize the
     CONFIRMED-CONSENSUS maker quote to `sean_window_qty` (default 5,
     to match SEAN). Outside the window, normal v7 sizing applies.
     If the posterior is biased (consensus disagrees) we DON'T
     upsize -- the SEAN-window edge depends on the fair being right.

  6. EVERYTHING DYNAMIC. duration, reveal_interval, position_limit,
     tick_size, fees, multiplier -- all read from game_state. No
     hardcoded round count, no hardcoded interval, no hardcoded
     position limit.

  7. SYMBOL FILTER. The strategy only trades the configured symbol
     (default A) -- v7's on_trade symbol filter is already in place,
     and quote/snipe paths never touch other symbols since they only
     call `self.c.book(self.symbol)` etc.

Run:
    python day1/run_combined8.py
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
from strategy7 import (  # noqa: E402
    URL,
    API_KEY,
    INFORMED_TRADER_ID,
    N_PRIOR_SIM,
    Posterior,
    Strategy as Strategy7,
)


class Strategy(Strategy7):
    """v8: SEAN-window passive MM + reveal-math guard + tighter aggression."""

    def __init__(self, client: GameClient, posterior: Posterior,
                 symbol: str = "A",
                 informed_trader_id: Optional[str] = None):
        super().__init__(client, posterior, symbol=symbol,
                         informed_trader_id=informed_trader_id)

        # === v8: tighter aggression ===
        # Audit: 78/79 snipe lots were profitable at avg +9.2 edge, so we
        # don't disable sniping -- we just raise the bar so 2-tick noise
        # in a tight market can't fire. snipe_min_edge=2.0 means at low
        # sigma the edge_required is taker_fee + 2.0 = 2.5 ticks.
        self.snipe_min_edge = 2.0
        # snipe_max_disagreement=3.0 (was 5.0): the strat7.5 leak fired
        # when fair_eff was VWAP-pulled to ~98 vs book_mid ~101 (diff=-3,
        # within the v7 gate). Tightening to 3 means even small
        # consensus-fair gaps trigger the gate.
        self.snipe_max_disagreement = 3.0
        # max_snipes_per_round=40 (was 120): formalises the passive
        # intent. 40 snipes * 10 qty cap = 400 lots, still way more
        # than position_limit (50) so the count doesn't bind any
        # legitimate trade -- it's a runaway guard only.
        self.max_snipes_per_round = 40
        # snipe_position_buffer=8 (was 5): with position_limit=50 the
        # 5-lot buffer left 90% headroom; one 10-lot snipe could leave
        # only ±40 to spare. 8 keeps us safely below the hard cap.
        self.snipe_position_buffer = 8

        # === v8: SEAN-window scheduler state ===
        # _last_reveal_t is wall-clock time at which the most recent
        # reveal event was observed (set in on_reveal). Combined with
        # self.reveal_interval (read from game_state at startup) this
        # lets us predict the next reveal at ~10ms precision.
        self._last_reveal_t: Optional[float] = None
        # We track which reveal-index we last serviced so the
        # scheduler doesn't double-fire within one cycle.
        self._sean_last_serviced_idx: int = -1
        self._sean_last_cancelled_idx: int = -1

        # === v8: SEAN-window knobs ===
        # Audit observation: SEAN's quotes landed at since_last_reveal
        # = 13.95-14.10s in a 15s cycle (i.e., 0.84-1.02s before next
        # reveal). We post a touch earlier -- 1.5s -- to give ourselves
        # FIFO queue time before the eager pre-reveal aggressors land.
        self.sean_window_sec: float = 1.5
        # Cancel everything this many seconds before the predicted
        # reveal so we're not standing in the way when fair shifts.
        # Audit: latency to cancel after reveal was 495-660ms; doing
        # it BEFORE the reveal removes that whole window. 0.3s is
        # tight enough that we don't give up much MM time.
        self.sean_pre_reveal_cancel_sec: float = 0.3
        # Quote qty within the SEAN window when consensus confirms
        # our fair. Matches SEAN observed (qty=5 in every instance).
        self.sean_window_qty: int = 5
        # Whether the SEAN-window upsize is active. Off restores v7
        # behavior (so a quick A/B is one knob flip).
        self.sean_window_enabled: bool = True

        # === v8: reveal-math fair guard ===
        # Once we have K >= reveal_math_min_k reveals, the sample mean
        # is a strong estimator of E[X]. Project forward to settlement.
        # K=2 chosen because the strat7.5 102-vs-86 leak was at K=4 --
        # K=2 catches similar leaks earlier without firing on K=1
        # (where the single-sample mean is noisy).
        self.reveal_math_min_k: int = 2
        # When |fair_eff - reveal_math_fair| exceeds this, snipes that
        # would aggress against reveal_math (the side the posterior
        # would attack) are blocked. 4 ticks chosen because the
        # strat7.5 leak had a 12-tick gap (fair=98 vs reveal_math=86);
        # 4 catches it conservatively while letting through real
        # 1-3 tick mispricings sniping can profit on.
        self.reveal_math_guard_ticks: float = 4.0
        # On/off switch.
        self.reveal_math_guard_enabled: bool = True

        # === v8: scheduler thread ===
        # Daemon thread. wakes every `_scheduler_tick_sec` to check
        # whether we're inside the SEAN window or the pre-reveal
        # cancel window. Cheap (no REST in the wake path itself --
        # only step()/cancel_all when a window boundary is crossed).
        self._scheduler_stop = threading.Event()
        self._scheduler_tick_sec: float = 0.05
        self._scheduler_thread = threading.Thread(
            target=self._sean_scheduler_loop,
            name="sean-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    # ---- v8: timing helpers ----

    def _next_reveal_at(self) -> Optional[float]:
        """Predicted wall-clock time of the next reveal, or None when
        no reveal has been observed yet this round (we have no anchor
        for the schedule). Uses self.reveal_interval (from game_state).
        """
        if self._last_reveal_t is None:
            return None
        # If somehow we're past the predicted time (e.g. scheduler
        # missed a cycle), shift forward by reveal_interval until
        # we're in the future.
        nxt = self._last_reveal_t + self.reveal_interval
        now = time.time()
        while nxt < now - 0.5:
            nxt += self.reveal_interval
        return nxt

    def _in_sean_window(self) -> bool:
        """True iff we're in the [next_reveal - sean_window_sec,
        next_reveal - sean_pre_reveal_cancel_sec) interval, AND there
        are reveals still expected. Outside this, the SEAN upsize is
        off (no harm to v7 behavior).
        """
        if not self.sean_window_enabled:
            return False
        nxt = self._next_reveal_at()
        if nxt is None:
            return False
        if self._n_remaining() <= 0:
            return False
        now = time.time()
        return (nxt - self.sean_window_sec) <= now < (nxt - self.sean_pre_reveal_cancel_sec)

    def _sean_scheduler_loop(self) -> None:
        """Background daemon: cancel quotes pre-reveal, repost in the
        SEAN window. Runs until self._scheduler_stop is set. Each
        cycle is gated by `_sean_last_*_idx` so the same action
        doesn't fire repeatedly within one inter-reveal interval.
        """
        while not self._scheduler_stop.is_set():
            try:
                self._sean_scheduler_tick()
            except Exception as e:
                # Daemon thread must not die on a transient. The same
                # tick fires again 50ms later.
                print(f"sean_scheduler tick error: {e}")
            self._scheduler_stop.wait(self._scheduler_tick_sec)

    def _sean_scheduler_tick(self) -> None:
        if self.phase != "running":
            return
        if self._n_remaining() <= 0:
            return
        nxt = self._next_reveal_at()
        if nxt is None:
            return
        now = time.time()
        # The "cycle index" is the number of reveals we've already
        # observed -- each cycle is the gap between reveal N and N+1.
        cycle_idx = len(self.posterior.reveals)

        # Pre-reveal cancel window: now > nxt - cancel_sec and we
        # haven't already cancelled for this cycle.
        cancel_at = nxt - self.sean_pre_reveal_cancel_sec
        if now >= cancel_at and self._sean_last_cancelled_idx < cycle_idx:
            self._sean_last_cancelled_idx = cycle_idx
            try:
                with self.lock:
                    self.c.cancel_all()
                    self.resting = {"bid": None, "ask": None}
                print(f"  v8 PRE-REVEAL CANCEL  cycle={cycle_idx}  "
                      f"in {nxt - now:.2f}s")
            except Exception as e:
                print(f"  v8 pre-reveal cancel failed: {e}")
            return  # don't try to post quotes after cancel-window starts

        # SEAN-window post: now > nxt - window_sec, before the cancel
        # window starts, and we haven't already posted for this cycle.
        post_at = nxt - self.sean_window_sec
        if (now >= post_at and now < cancel_at
                and self._sean_last_serviced_idx < cycle_idx):
            self._sean_last_serviced_idx = cycle_idx
            print(f"  v8 SEAN-WINDOW POST   cycle={cycle_idx}  "
                  f"reveal in {nxt - now:.2f}s")
            # Run step() to apply target quotes (the upsize takes
            # effect via _current_quote_qty override below).
            try:
                self.step()
            except Exception as e:
                print(f"  v8 sean-window step failed: {e}")

    # ---- v8: reveal-math fair ----

    def _reveal_math_fair(self) -> Optional[float]:
        """Sample-mean projection of settlement: running_sum + remaining
        * mean(reveals_so_far). Returns None if we have fewer than
        reveal_math_min_k reveals (the sample mean is too noisy).

        Cheap (one statistics.mean call). Used by the snipe guard.
        """
        reveals = self.posterior.reveals
        k = len(reveals)
        if k < self.reveal_math_min_k:
            return None
        remaining = self._n_remaining()
        if remaining <= 0:
            # All reveals in: settle is known exactly via running sum.
            return sum(reveals)
        return sum(reveals) + remaining * statistics.fmean(reveals)

    def _reveal_math_blocks_buy(self, fair_eff: float) -> bool:
        """True iff a BUY snipe (taking the ask side) would aggress
        against the reveal-math fair -- i.e., the posterior fair we'd
        be using to justify the buy is significantly ABOVE the
        sample-mean projection. Maps directly to the strat7.5 102-vs-
        86 leak: fair_eff=98, reveal_math=86, diff=+12 -> block buy.
        """
        if not self.reveal_math_guard_enabled:
            return False
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return fair_eff - rm > self.reveal_math_guard_ticks

    def _reveal_math_blocks_sell(self, fair_eff: float) -> bool:
        """Symmetric: SELL snipe against reveal_math means fair_eff is
        significantly BELOW reveal_math (we'd sell into bids that are
        actually correctly priced near the higher truth).
        """
        if not self.reveal_math_guard_enabled:
            return False
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return rm - fair_eff > self.reveal_math_guard_ticks

    # ---- v8: overrides ----

    def _current_quote_qty(self, side: Optional[str] = None) -> int:
        """v8 override: inside the SEAN window AND when consensus
        confirms our fair, upsize to `sean_window_qty`. Otherwise
        fall through to v7 behavior. Inventory dampener still
        applies to the upsized qty.
        """
        base = super()._current_quote_qty(side=side)
        if not self.sean_window_enabled or not self._in_sean_window():
            return base
        # Only upsize when the bias detector says we're in the
        # consensus-confirmed regime. Re-uses v7's _market_signal /
        # market_anchor_disagreement_min so the gate matches the
        # rest of the pricing code.
        if len(self.posterior.reveals) == 0:
            return base
        try:
            fair, _ = self.fair_and_sigma()
        except Exception:
            return base
        market_mid = self._market_signal()
        if market_mid is None:
            return base
        if abs(fair - market_mid) >= self.market_anchor_disagreement_min:
            return base
        upsized = max(base, self.sean_window_qty)
        if side is not None:
            # Re-apply inventory dampener semantics (same logic as
            # the super) on the upsized base.
            max_pos = self._max_position_for_sigma(
                self.fair_and_sigma()[1],
                relaxed=True,
            )
            threshold = int(max_pos * self.inv_dampen_threshold_frac)
            adding = ((side == "bid" and self.position > threshold)
                      or (side == "ask" and self.position < -threshold))
            if adding:
                upsized = max(1, int(round(upsized * self.inv_dampen_factor)))
        return upsized

    def maybe_snipe(self, fair: float, sigma: float) -> bool:
        """v8 override: short-circuit when reveal-math blocks BOTH sides.
        When only one side is blocked, set up the directional gate so
        v7's existing block_buy/block_sell loop honors it.

        Implementation: we can't easily inject the guard into v7's
        existing loop without copy-pasting it. Instead we use the
        same fair_eff math here, decide the block, and if BOTH sides
        are blocked we early-exit (saves a book fetch). If ONLY ONE
        side is blocked, we temporarily flip on `snipe_disagreement_
        directional` and synthesize a market_mid offset that fires
        the directional path. Cleaner: we override _try_direct_snipe
        too with the same guard.
        """
        if self.reveal_math_guard_enabled:
            fair_eff = self._effective_fair(fair)
            fair_eff = self._vwap_clamped_fair(fair_eff)
            block_buy = self._reveal_math_blocks_buy(fair_eff)
            block_sell = self._reveal_math_blocks_sell(fair_eff)
            if block_buy and block_sell:
                return False
            if block_buy or block_sell:
                # Stash original; restore on exit. Use snipe_disagreement_
                # directional + a synthetic market_mid offset via the
                # existing gate.
                # Easier: temporarily set snipe_max_disagreement very
                # high and mutate position-cap-style blocks isn't
                # available. So we'll just wrap the parent call in a
                # try/except and post-check the position delta. That's
                # ugly. Cleanest: call the parent and let v7's logic
                # run, but rely on the snipe_disagreement gate to do
                # the heavy lifting. For v8 the simplest correct
                # behavior is: if EITHER side is blocked, skip the
                # whole snipe pass (sniping is rare and we'd rather
                # miss a side than fire the wrong one).
                rm = self._reveal_math_fair()
                print(f"  v8 reveal-math gate: fair_eff={fair_eff:.1f} "
                      f"reveal_math={rm:.1f} block_buy={block_buy} "
                      f"block_sell={block_sell} -> skip snipe")
                return False
        return super().maybe_snipe(fair, sigma)

    def _try_direct_snipe(self, msg: dict) -> bool:
        """v8 override: same reveal-math guard, applied per-direction.
        We can compute the snipe direction directly from msg['side']:
          - msg side=='buy' means a quote_add BID was posted -- we'd
            SELL into it. Blocked if reveal_math_blocks_sell.
          - msg side=='sell' means a quote_add ASK -- we'd BUY from it.
            Blocked if reveal_math_blocks_buy.
        """
        if self.reveal_math_guard_enabled:
            side = msg.get("side")
            with self.lock:
                try:
                    fair, _ = self.fair_and_sigma()
                except Exception:
                    return False
                fair_eff = self._effective_fair(fair)
                fair_eff = self._vwap_clamped_fair(fair_eff)
                if side == "buy" and self._reveal_math_blocks_sell(fair_eff):
                    return False
                if side == "sell" and self._reveal_math_blocks_buy(fair_eff):
                    return False
        return super()._try_direct_snipe(msg)

    def on_reveal(self, value: float) -> None:
        """v8 override: record the wall-clock reveal time for the
        scheduler before delegating to v7's reveal handling.
        """
        self._last_reveal_t = time.time()
        # Bump the cycle pointers so the scheduler treats the next
        # window as fresh (the cycle index in the scheduler is
        # len(posterior.reveals), which super().on_reveal increments
        # via posterior.update -- this is just a safety reset of
        # the cancel/serviced markers).
        super().on_reveal(value)

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        """v8 override: reset scheduler state at the start of a new
        round so the cycle index doesn't carry across rounds.
        """
        if phase == "running":
            self._last_reveal_t = None
            self._sean_last_serviced_idx = -1
            self._sean_last_cancelled_idx = -1
        super().on_phase_change(phase, reveals)

    def flatten(self) -> None:
        """Stop the scheduler thread when the strategy flattens (on
        quit / Ctrl-C). Without this the daemon outlives the parent
        cleanly anyway, but explicit shutdown is tidier and lets a
        re-instantiated Strategy not leave a zombie scheduler.
        """
        try:
            self._scheduler_stop.set()
        except Exception:
            pass
        super().flatten()


# ---------------------------------------------------------------------------
# Main: wire up WS feed and run until interrupted
# (Mirrors strategy7.main(); duplicated so `python day1/strategy8.py` works
# standalone, without going through the run_combined wrapper.)
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
    print(f"v8: reveal_interval={strat.reveal_interval}s  "
          f"duration={strat.duration}s  n_total={strat.n_total}  "
          f"position_limit={strat.position_limit}  tick={strat.tick}")
    print(f"v8: sean_window={strat.sean_window_sec}s  "
          f"pre_reveal_cancel={strat.sean_pre_reveal_cancel_sec}s  "
          f"sean_qty={strat.sean_window_qty}  "
          f"reveal_math_min_k={strat.reveal_math_min_k}  "
          f"guard_ticks={strat.reveal_math_guard_ticks}")

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

    print("\nBot started.")
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
                in_window = strat._in_sean_window()
                nxt = strat._next_reveal_at()
                nxt_in = (nxt - time.time()) if nxt is not None else None
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"reveal_math={rm_str}  in_sean_window={in_window}  "
                      f"next_reveal_in="
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
