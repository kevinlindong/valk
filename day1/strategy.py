"""
Day 1 live trading strategy.

Game model (handout): settlement = sum of N draws X_i ~ Uniform(a, b),
where (a, w = b-a) are drawn once at the start of each round from two
LogNormal-floored priors and never change for that round. We see one X_i
every reveal_interval seconds.

Strategy:

  1. Posterior over (a, w): discrete grid built from the prior; Bayes update
     on each reveal with uniform likelihood plus the support constraint
     a <= x <= a+w. Exposes predict_settle(running_sum, n_remaining) using
     law-of-total-variance to combine within-round draw variance and
     posterior uncertainty over (a, w).
  2. Fair value F and uncertainty sigma drive both quoting and sniping.
     - Passive quotes: bid at F - edge + skew, ask at F + edge + skew, where
       edge scales with sigma and skew is linear in inventory.
     - Snipes any quote whose mispricing vs F exceeds taker_fee + buffer * sigma.
  3. Safety guards (added to prevent the "instant -100" failure mode):
     - cancel_all on the server at every phase->running transition (stale
       resting orders from the previous round get auto-lifted otherwise).
     - No quoting AND no sniping until we have at least one reveal in this
       round (prior-only fair has wide uncertainty AND is biased relative
       to the realized (a, w); both legs are dangerous before the first reveal).

Run:
    python day1/strategy.py
While running: type 's'+Enter for status, 'f'+Enter to flatten, 'q'+Enter to quit.
Ctrl-C also flattens and exits cleanly.
"""

from __future__ import annotations

import math
import os
import random
import sys
import threading
import time
from collections import defaultdict
from typing import Optional

# Allow `python day1/strategy.py` from project root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from sdk.client import GameClient  # noqa: E402


# ---------------------------------------------------------------------------
# Connection (edit URL / API_KEY here)
# ---------------------------------------------------------------------------
URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"


# ---------------------------------------------------------------------------
# Posterior over (a, w)
# ---------------------------------------------------------------------------
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8   # a = floor(LogN(mu, sigma))
W_LOGN_MU, W_LOGN_SIGMA = 0.5, 0.7   # w = 1 + floor(LogN(mu, sigma))
N_PRIOR_SIM = 1_000_000              # MC samples to build the discrete prior


class Posterior:
    """Discrete joint posterior over (a, w) given reveals X_i ~ Uniform(a, a+w)."""

    def __init__(
        self,
        a_mu: float = A_LOGN_MU,
        a_sigma: float = A_LOGN_SIGMA,
        w_mu: float = W_LOGN_MU,
        w_sigma: float = W_LOGN_SIGMA,
        n_sim: int = N_PRIOR_SIM,
    ):
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for _ in range(n_sim):
            a_v = int(math.floor(random.lognormvariate(a_mu, a_sigma)))
            w_v = 1 + int(math.floor(random.lognormvariate(w_mu, w_sigma)))
            counts[(a_v, w_v)] += 1
        total = sum(counts.values())
        self.prior: dict[tuple[int, int], float] = {k: v / total for k, v in counts.items()}
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
            # Discrete-uniform likelihood: support is {a, a+1, ..., a+w} (w+1
            # integers), so probability mass is 1/(w+1), NOT 1/w. The notebook
            # design notes flagged this as a TODO; simulation.py's ImprovedBot
            # confirmed it; ported here.
            new_p = p / (w + 1)
            new[(a, w)] = new_p
            total += new_p
        if total <= 0:
            print(f"WARN: reveal {x} outside posterior support; ignoring update")
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def mean_x(self) -> float:
        return sum(p * (a + w / 2.0) for (a, w), p in self.posterior.items())

    def predict_settle(self, running_sum: float, n_remaining: int) -> tuple[float, float]:
        """Mean and std of the total final settlement.

        Uses the law of total variance:
            Var[S_rem] = E[Var[S_rem | a,w]] + Var[E[S_rem | a,w]]
        """
        if n_remaining <= 0:
            return float(running_sum), 0.0
        e_inner = 0.0
        e2_inner = 0.0
        e_var = 0.0
        for (a, w), p in self.posterior.items():
            inner_mean = n_remaining * (a + w / 2.0)
            # Discrete-uniform variance over {a, ..., a+w} is w(w+2)/12,
            # not w^2/12. Same notebook-flagged TODO as the likelihood fix above.
            inner_var = n_remaining * (w * (w + 2)) / 12.0
            e_inner += p * inner_mean
            e2_inner += p * inner_mean * inner_mean
            e_var += p * inner_var
        var_total = e_var + (e2_inner - e_inner * e_inner)
        return running_sum + e_inner, math.sqrt(max(var_total, 0.0))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class Strategy:
    def __init__(self, client: GameClient, posterior: Posterior, symbol: str = "A"):
        self.c = client
        self.symbol = symbol
        self.posterior = posterior
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

        # Seed posterior with whatever reveals already happened in the current round.
        self.posterior.reset(gs.get("reveals") or [])

        # Position from server, not a local guess.
        self.position = int(client.positions()["positions"].get(symbol, 0))

        # Locally tracked phase, kept in sync via on_phase_change. Avoids
        # making a REST game_state() call on every WS event.
        self.phase: Optional[str] = gs.get("phase")

        # Throttle for on_quote_event: with ~17 quote_add events/sec in the
        # live market, running maybe_snipe (book REST + maybe IOC) on every
        # one would saturate. Skip if last check was < this many seconds ago.
        self._last_quote_event_t: float = 0.0
        self.quote_event_throttle_sec: float = 0.10

        # resting[side] -> {"order_id", "price", "qty"} or None
        self.resting: dict[str, Optional[dict]] = {"bid": None, "ask": None}

        # ---------- knobs ----------
        # Reverted from over-tuned tight values after a session of -EV trade
        # spam (560 fills, 82% taker, ~740 in fees, losing PnL). Lesson: tuning
        # to one session's median spread (2 ticks) made snipes fire on
        # 1-tick "mispricings" that were really our own fair-value noise.
        #
        # Robust values that work across all observed sessions (spread 2-6 ticks):
        #   - Spread floor 1.5 keeps maker margin > maker_fee (0.5) per fill.
        #   - Snipe edge >= taker_fee + 1.5 = 2.0 ticks, well above our model
        #     noise floor. Combined with sigma_buffer this becomes ~3+ in early
        #     round and ~2 in late round.
        #   - max_snipes_per_round is a HARD cap so even if the threshold is
        #     wrong, fees can't run away.
        self.quote_qty = 5             # match market median (3-7)
        self.min_edge = 1.5            # 3-tick spread; per-fill maker margin ~1.0
        self.edge_per_sigma = 0.25
        self.skew_per_unit = 0.20      # passive flatten preferred over taker flatten
        self.snipe_buffer_sigma = 0.60 # require sigma-scaled buffer; defensive
        self.snipe_min_edge = 1.5      # need >= 2.0 ticks total edge to snipe
        self.snipe_full_size_sigma = 1.5
        self.snipe_book_depth = 10
        # HARD trade-rate ceiling (per round) -- defense against fee runaway
        # even if snipe edge calc is wrong. The +366 session used 4 snipes;
        # the disaster session fired ~100+. 30 is a reasonable middle.
        self.max_snipes_per_round = 30
        self._snipe_count_this_round = 0
        # "Slightly tighter than them" -- step 1 tick inside market best when
        # safe (NEVER below the min_edge floor from fair, so per-fill margin
        # is preserved). Read from a 1s-cached book to avoid REST spam.
        self.tight_quoting_enabled = True
        self._book_cache: Optional[dict] = None
        self._book_cache_t: float = 0.0
        self._book_cache_max_age_sec: float = 1.0
        # Safety: require at least this many reveals before quoting or sniping.
        self.min_reveals_to_quote = 1
        self.min_reveals_to_snipe = 1
        # Post-final-reveal behavior: when n_remaining == 0, fair is no longer
        # theoretical -- it's the exact settlement (sigma == 0). At that point:
        #   - quoting passive spread on a known value gives away free edge to
        #     anyone who hits us at fair-edge (and we have no informational
        #     advantage anymore), so cancel quotes.
        #   - sniping is still pure profit -- any visible mispricing is someone
        #     ELSE making a mistake on a value we both know, so keep snipping.
        self.quote_after_final_reveal = False
        self.snipe_after_final_reveal = True
        # ----------------------------

    # -------- helpers --------

    def _running_sum(self) -> float:
        return sum(self.posterior.reveals)

    def _n_remaining(self) -> int:
        return max(self.n_total - len(self.posterior.reveals), 0)

    def fair_and_sigma(self) -> tuple[float, float]:
        return self.posterior.predict_settle(self._running_sum(), self._n_remaining())

    def reconcile_position(self) -> None:
        try:
            self.position = int(self.c.positions()["positions"].get(self.symbol, 0))
        except Exception as e:
            print(f"reconcile_position failed: {e}")

    def desired_quotes(self) -> tuple[Optional[int], Optional[int], float, float]:
        fair, sigma = self.fair_and_sigma()
        if len(self.posterior.reveals) < self.min_reveals_to_quote:
            return None, None, fair, sigma
        # All reveals in -- fair is the exact settle, not a theoretical.
        # Don't market-make on it (we'd give away edge for no informational gain).
        if self._n_remaining() == 0 and not self.quote_after_final_reveal:
            return None, None, fair, sigma

        edge = max(self.min_edge, self.edge_per_sigma * sigma)
        skew = -self.position * self.skew_per_unit

        bid_px = int(math.floor(fair - edge + skew))
        ask_px = int(math.ceil(fair + edge + skew))
        if ask_px <= bid_px:
            ask_px = bid_px + self.tick

        if self.position + self.quote_qty > self.position_limit:
            bid_px = None
        if self.position - self.quote_qty < -self.position_limit:
            ask_px = None

        return bid_px, ask_px, fair, sigma

    # -------- order plumbing --------

    def _record_fill_from_trades(self, side: str, trades: list[dict]) -> int:
        filled = sum(t["qty"] for t in trades)
        if filled:
            self.position += filled if side == "buy" else -filled
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
        try:
            r = method(self.symbol, price=price, qty=self.quote_qty)
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

    def _reprice(self, side: str, want_px: int) -> None:
        rest = self.resting[side]
        if rest is None:
            self._post(side, want_px)
            return
        if rest["price"] == want_px and rest["qty"] == self.quote_qty:
            return  # leave it alone -- keep queue priority
        sgn_side = "buy" if side == "bid" else "sell"
        try:
            res = self.c.modify(rest["order_id"], price=want_px, qty=self.quote_qty)
        except Exception:
            self._safe_cancel(side)
            self._post(side, want_px)
            return
        o = res["order"]
        self._record_fill_from_trades(sgn_side, res.get("trades", []))
        if o["status"] in ("open", "partial"):
            self.resting[side] = {
                "order_id": o["order_id"],
                "price": o["price"],
                "qty": o["remaining"],
            }
        else:
            self.resting[side] = None

    # -------- sniping --------

    def maybe_snipe(self, fair: float, sigma: float) -> bool:
        """Take any mispriced level. Returns True if we took anything.

        Skips levels matching our own resting prices (the public book doesn't
        expose owner, so we look at self.resting). Avoids self_match_prevention
        round-trips that the probe showed happening 46-69 times per session.

        When sigma is low (snipe_full_size_sigma), uses position headroom
        instead of quote_qty as the per-snipe cap -- fair is near-certain so
        any mispricing is real profit. Walks deeper book (snipe_book_depth).
        """
        if len(self.posterior.reveals) < self.min_reveals_to_snipe:
            return False
        if self._n_remaining() == 0 and not self.snipe_after_final_reveal:
            return False
        # Hard rate limit -- defense against fee runaway if our edge calc is
        # mid-round wrong. Post-final-reveal sweep is exempted (settle is
        # known, every snipe is risk-free profit).
        if (self._n_remaining() > 0 and
                self._snipe_count_this_round >= self.max_snipes_per_round):
            return False
        try:
            book = self.c.book(self.symbol, depth=self.snipe_book_depth)
            self._book_cache = book  # share with desired_quotes
            self._book_cache_t = time.time()
        except Exception:
            return False

        # Our own resting prices -- skip these in the snipe loop.
        our_ask_px = self.resting["ask"]["price"] if self.resting["ask"] else None
        our_bid_px = self.resting["bid"]["price"] if self.resting["bid"] else None

        if self._n_remaining() == 0:
            # All reveals in: settle is known exactly. Any mispricing > taker_fee
            # is a risk-free profitable trade. No min_edge or sigma buffer --
            # take everything visible. This is the "take a position that is
            # profitable immediately" sweep.
            edge_required = self.taker_fee
        else:
            edge_required = self.taker_fee + max(self.snipe_min_edge, self.snipe_buffer_sigma * sigma)
        # Below this sigma we're confident enough to take the whole level.
        snipe_cap = self.position_limit if sigma < self.snipe_full_size_sigma else self.quote_qty
        took_any = False

        for level in book.get("asks") or []:
            if our_ask_px is not None and level["price"] == our_ask_px:
                continue  # don't snipe our own ask
            mispricing = fair - level["price"]
            if mispricing <= edge_required:
                break
            headroom = self.position_limit - self.position
            if headroom <= 0:
                break
            qty = min(level["qty"], headroom, snipe_cap)
            if qty <= 0:
                break
            try:
                res = self.c.buy_ioc(self.symbol, price=level["price"], qty=qty)
            except Exception:
                break
            filled = self._record_fill_from_trades("buy", res.get("trades", []))
            if filled:
                took_any = True
                print(f"  SNIPE buy  {filled} @ {level['price']}  fair={fair:.1f}  edge={mispricing:.1f}")
            else:
                break

        for level in book.get("bids") or []:
            if our_bid_px is not None and level["price"] == our_bid_px:
                continue  # don't snipe our own bid
            mispricing = level["price"] - fair
            if mispricing <= edge_required:
                break
            headroom = self.position_limit + self.position
            if headroom <= 0:
                break
            qty = min(level["qty"], headroom, snipe_cap)
            if qty <= 0:
                break
            try:
                res = self.c.sell_ioc(self.symbol, price=level["price"], qty=qty)
            except Exception:
                break
            filled = self._record_fill_from_trades("sell", res.get("trades", []))
            if filled:
                took_any = True
                print(f"  SNIPE sell {filled} @ {level['price']}  fair={fair:.1f}  edge={mispricing:.1f}")
            else:
                break

        return took_any

    # -------- top-level step --------

    def step(self, *, reconcile: bool = False) -> None:
        with self.lock:
            try:
                phase = self.c.game_state().get("phase")
            except Exception:
                return
            self.phase = phase  # keep local phase in sync (read by on_quote_event)
            if phase != "running":
                if phase == "settled":
                    self.resting = {"bid": None, "ask": None}
                return

            if reconcile:
                self.reconcile_position()

            _, _, fair, sigma = self.desired_quotes()
            self.maybe_snipe(fair, sigma)

            bid_px, ask_px, fair, sigma = self.desired_quotes()

            if bid_px is None:
                self._safe_cancel("bid")
            else:
                self._reprice("bid", bid_px)

            if ask_px is None:
                self._safe_cancel("ask")
            else:
                self._reprice("ask", ask_px)

            print(
                f"QUOTE  fv={fair:6.1f} +/-{sigma:4.1f}  pos={self.position:+4d}  "
                f"bid={bid_px}  ask={ask_px}  k={len(self.posterior.reveals)}/{self.n_total}"
            )

    # -------- event handlers --------

    def on_reveal(self, value: float) -> None:
        with self.lock:
            self.posterior.update(value)
            k = len(self.posterior.reveals)
            running = sum(self.posterior.reveals)
            is_final = (k >= self.n_total)
            if is_final:
                # All info is in: fair == settle exactly. Cancel passive quotes
                # immediately (don't wait for the next step) so they don't
                # interfere with the sweep below.
                print(f"FINAL  all {k}/{self.n_total} reveals in. "
                      f"settle={int(running)}  passive quotes -> cancel; sweeping book.")
                if not self.quote_after_final_reveal:
                    self._safe_cancel("bid")
                    self._safe_cancel("ask")
        self.step(reconcile=True)
        if is_final and self.snipe_after_final_reveal:
            # Sweep the book of any profitable level. Settle is known, so
            # every mispricing > taker_fee is risk-free. Keep calling
            # maybe_snipe until it stops finding things.
            self._sweep_at_settle(running_sum=float(sum(self.posterior.reveals)))

    def _sweep_at_settle(self, running_sum: float) -> None:
        """Take a position that is profitable immediately at the known settle.

        Calls maybe_snipe in a loop until no more profitable levels remain or
        we hit the position limit. Each call walks snipe_book_depth levels;
        looping handles the case where someone reposts a mispriced quote in
        the brief window after we sweep.
        """
        for sweep_pass in range(6):  # ~600ms worth of attempts at 100ms book ttl
            with self.lock:
                took = self.maybe_snipe(running_sum, 0.0)
            if not took:
                return
            # Tiny pause so we don't slam REST. Other bots may repost; we
            # come back around to grab the new mispriced level.
            time.sleep(0.05)

    def on_fill_event(self, msg: dict) -> None:
        with self.lock:
            side = msg["side"]
            qty = msg["qty"]
            order_id = msg["order_id"]
            for key, expected_side in (("bid", "buy"), ("ask", "sell")):
                rest = self.resting[key]
                if rest is not None and rest["order_id"] == order_id and side == expected_side:
                    rest["qty"] -= qty
                    if rest["qty"] <= 0:
                        self.resting[key] = None
                    break
        self.step(reconcile=True)

    def on_quote_event(self, msg: dict) -> None:
        """React to a new (or cancelled) quote on the book.

        Discovered from probe data: the server pushes 'quote_add' / 'quote_cancel'
        on the private WS for EVERY order, not just ours. This lets us snipe
        stale quotes within the ~1s window before the posting bot reprices.

        Cheap-path design (fires ~17x/sec):
          - throttle: skip if last check was within quote_event_throttle_sec
          - phase check via local self.phase (no REST call)
          - reveal-count guard
          - then take the lock and do the snipe scan
        """
        now = time.time()
        if now - self._last_quote_event_t < self.quote_event_throttle_sec:
            return
        if self.phase != "running":
            return
        if len(self.posterior.reveals) < self.min_reveals_to_snipe:
            return
        self._last_quote_event_t = now
        with self.lock:
            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return
            self.maybe_snipe(fair, sigma)

    def on_phase_change(self, phase: Optional[str], reveals: list[float]) -> None:
        with self.lock:
            self.phase = phase
            if phase == "running":
                # CRITICAL: cancel any orders left over from a previous round.
                # The local self.resting dict is only our handle list -- the server
                # may still have stale orders on the book that will be lifted
                # the instant the new round starts (most common cause of
                # "instant +-position_limit at game start").
                try:
                    self.c.cancel_all()
                    print("CANCEL_ALL on phase->running")
                except Exception as e:
                    print(f"cancel_all on phase change failed: {e}")
                self.posterior.reset(reveals)
                self.resting = {"bid": None, "ask": None}
                self.reconcile_position()
        if phase == "running":
            self.step()

    def flatten(self) -> None:
        """Emergency: cancel all and flatten to zero via market orders."""
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


# ---------------------------------------------------------------------------
# Inspect helpers (formerly notebook cells)
# ---------------------------------------------------------------------------
def print_status(strat: Strategy, c: GameClient) -> None:
    fair, sigma = strat.fair_and_sigma()
    bid_px, ask_px, _, _ = strat.desired_quotes()
    print("-" * 60)
    print(f"phase          : {c.game_state().get('phase')}")
    print(f"reveals so far : {strat.posterior.reveals}")
    print(f"fair value     : {fair:.2f}  +/- {sigma:.2f}")
    print(f"local position : {strat.position}")
    try:
        print(f"server position: {c.positions()}")
        print(f"open orders    : {c.my_orders()}")
        print(f"book           : {c.book(strat.symbol)}")
    except Exception as e:
        print(f"(server query failed: {e})")
    print(f"desired quotes : bid={bid_px}  ask={ask_px}")
    print(f"resting        : {strat.resting}")
    print("-" * 60)


# ---------------------------------------------------------------------------
# Main: wire up WS feed and run until interrupted
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

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(
            f"FILL   {msg['side']:>4s} {msg['qty']} @ {msg['price']}  "
            f"liq={msg.get('liquidity')} vs {msg.get('counterparty')}"
        )
        strat.on_fill_event(msg)

    def on_trade(msg):
        pass  # public tape; ignored

    def on_book(msg):
        pass  # too noisy to step on every delta; snipes happen on reveal/fill

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
        # The SDK's catch-all sees raw events INCLUDING quote_add / quote_cancel
        # (which the typed handlers don't dispatch). These are the per-order book
        # delta stream -- when a fresh quote shows up that's mispriced relative
        # to our fair, we want to snipe it BEFORE the posting bot reprices.
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
    print("Commands: 's' = status, 'f' = flatten now, 'q' = quit (auto-flattens), Ctrl-C to quit.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                # Detached stdin (e.g. running under nohup) -- just sleep forever.
                while True:
                    time.sleep(60)
            if cmd == "s":
                print_status(strat, c)
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
