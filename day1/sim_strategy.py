"""Offline simulation harness that exercises the LIVE Strategy class.

Builds a deterministic FIFO matching engine + counterparties (NoiseMM,
TightMM, ValueSniper) and runs the actual `day1/strategy.py` Strategy
through a mock GameClient. Events flow through the same WS callbacks the
live bot uses (on_fill, on_message catching quote_add/quote_cancel,
on_reveal, on_game_state), and REST calls hit the same in-memory book.

Use this to backtest knob changes without touching the live exchange.
Writes a JSONL log per run (compatible-ish with day1/analyze.py).

Run:
    python day1/sim_strategy.py                          # 50 rounds, seed 0
    python day1/sim_strategy.py --rounds 200 --seed 7
    python day1/sim_strategy.py --quick                  # 10 rounds smoke test
    python day1/sim_strategy.py --rounds 50 --no-log     # no JSONL output
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import statistics
import sys
import time as _real_time
from dataclasses import dataclass, field
from typing import Callable, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# Import the live strategy module — we will monkey-patch time inside it
# so the deterministic SimClock drives any time-based logic (throttles,
# book-cache TTL, pre-reveal warmup).
import day1.strategy as strategy_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SYMBOL = "A"
TRADER = "KEVD"
TICK = 1
POSITION_LIMIT = 100
MAKER_FEE = 0.5
TAKER_FEE = 0.5
DURATION = 300
REVEAL_INTERVAL = 30

# Prior parameters (match strategy.py)
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8
W_LOGN_MU, W_LOGN_SIGMA = 0.5, 0.7

# Smaller prior than the live bot — 50k samples is enough for sim and
# keeps Strategy startup fast (live uses 1M).
SIM_PRIOR_N = 50_000


# ---------------------------------------------------------------------------
# Sim clock — patched into strategy_mod.time so Strategy sees deterministic time
# ---------------------------------------------------------------------------
class SimClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeTime:
    """Stand-in for the time module inside strategy_mod."""

    def __init__(self, clock: SimClock):
        self._clock = clock

    def time(self) -> float:
        return self._clock.now()

    def sleep(self, _s: float) -> None:  # _sweep_at_settle calls this
        pass


# ---------------------------------------------------------------------------
# Matching engine + event emitter
# ---------------------------------------------------------------------------
@dataclass
class _Order:
    order_id: int
    owner: str
    side: str
    price: int
    qty: int
    remaining: int = field(init=False)

    def __post_init__(self):
        self.remaining = self.qty


class SimMatch:
    """FIFO matcher. Sorted by price, stable-sort preserves insertion order for
    same-price entries (time priority). Emits events for every action via the
    parent SimEnv."""

    def __init__(self, env: "SimEnv"):
        self.env = env
        self.bids: list[_Order] = []
        self.asks: list[_Order] = []
        self._next_order_id = 10_000
        self._next_trade_id = 1

    def _new_order_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def _new_trade_id(self) -> int:
        tid = self._next_trade_id
        self._next_trade_id += 1
        return tid

    def _emit_fill_pair(self, agg: _Order, passive: _Order, price: int, qty: int,
                        passive_remaining: int) -> None:
        trade_id = self._new_trade_id()
        # Public trade tape
        self.env.emit_public({
            "type": "trade",
            "trade_id": trade_id,
            "symbol": SYMBOL,
            "price": price,
            "qty": qty,
            "aggressor": agg.side,
        })
        # Public quote_fill (passive order's fill, broadcast to everyone)
        self.env.emit_public({
            "type": "quote_fill",
            "order_id": passive.order_id,
            "symbol": SYMBOL,
            "side": passive.side,
            "price": price,
            "qty": qty,
            "remaining": passive_remaining,
            "trade_id": trade_id,
        })
        # Private fill to taker
        self.env.emit_private(agg.owner, {
            "type": "fill",
            "order_id": agg.order_id,
            "symbol": SYMBOL,
            "side": agg.side,
            "price": price,
            "qty": qty,
            "liquidity": "taker",
            "counterparty": passive.owner,
            "trade_id": trade_id,
        })
        # Private fill to maker
        self.env.emit_private(passive.owner, {
            "type": "fill",
            "order_id": passive.order_id,
            "symbol": SYMBOL,
            "side": passive.side,
            "price": price,
            "qty": qty,
            "liquidity": "maker",
            "counterparty": agg.owner,
            "trade_id": trade_id,
        })

    def _match(self, agg: _Order) -> list[dict]:
        fills: list[dict] = []
        passive_lst = self.asks if agg.side == "buy" else self.bids
        while agg.remaining > 0 and passive_lst:
            best = passive_lst[0]
            if agg.side == "buy" and agg.price < best.price:
                break
            if agg.side == "sell" and agg.price > best.price:
                break
            # Self-match prevention: real exchange cancels the aggressor's
            # remaining qty against the same-owner passive (probe saw 46-69
            # SMP events per session). Skip this passive level entirely and
            # cancel-aggressor for remaining qty.
            if best.owner == agg.owner:
                self.env.emit_private(agg.owner, {
                    "type": "reject",
                    "order_id": agg.order_id,
                    "reason": "self_match_prevention",
                    "remaining": agg.remaining,
                })
                agg.remaining = 0
                break
            q = min(agg.remaining, best.remaining)
            fills.append({
                "price": best.price,
                "qty": q,
                "buyer": agg.owner if agg.side == "buy" else best.owner,
                "seller": best.owner if agg.side == "buy" else agg.owner,
                "aggressor": agg.side,
            })
            agg.remaining -= q
            best.remaining -= q
            # Pop the fully-filled passive BEFORE emitting -- the emit may
            # reentrantly mutate the book (strategy.on_fill -> step -> modify),
            # and a stale index would corrupt the next iteration.
            if best.remaining == 0 and passive_lst and passive_lst[0] is best:
                passive_lst.pop(0)
            self._emit_fill_pair(agg, best, best.price, q, best.remaining)
        return fills

    def submit_limit(self, owner: str, side: str, price: int, qty: int,
                     tif: str = "gtc") -> tuple[_Order, list[dict]]:
        o = _Order(self._new_order_id(), owner, side, price, qty)
        fills = self._match(o)
        if o.remaining > 0:
            if tif == "gtc":
                lst = self.bids if side == "buy" else self.asks
                lst.append(o)
                if side == "buy":
                    lst.sort(key=lambda x: -x.price)
                else:
                    lst.sort(key=lambda x: x.price)
                self.env.emit_public({
                    "type": "quote_add",
                    "order_id": o.order_id,
                    "symbol": SYMBOL,
                    "side": side,
                    "price": price,
                    "qty": o.remaining,
                })
            # ioc/fok: residual just dies, no rest, no emit
        return o, fills

    def submit_market(self, owner: str, side: str, qty: int) -> tuple[_Order, list[dict]]:
        price = 10**9 if side == "buy" else 0
        o = _Order(self._new_order_id(), owner, side, price, qty)
        fills = self._match(o)
        return o, fills

    def cancel(self, order_id: int, reason: str = "user") -> bool:
        for lst in (self.bids, self.asks):
            for i, o in enumerate(lst):
                if o.order_id == order_id:
                    lst.pop(i)
                    self.env.emit_public({
                        "type": "quote_cancel",
                        "order_id": order_id,
                        "symbol": SYMBOL,
                        "side": o.side,
                        "price": o.price,
                        "qty": o.remaining,
                        "reason": reason,
                    })
                    return True
        return False

    def modify(self, order_id: int, new_price: Optional[int],
               new_qty: Optional[int]) -> tuple[Optional[_Order], list[dict], bool]:
        for lst in (self.bids, self.asks):
            for i, o in enumerate(lst):
                if o.order_id == order_id:
                    target_price = new_price if new_price is not None else o.price
                    target_qty = new_qty if new_qty is not None else o.remaining
                    # qty-only-down at same price keeps priority
                    if target_price == o.price and target_qty <= o.remaining:
                        o.qty = target_qty
                        o.remaining = target_qty
                        return o, [], True
                    # Else cancel + repost (loses priority)
                    lst.pop(i)
                    self.env.emit_public({
                        "type": "quote_cancel",
                        "order_id": order_id,
                        "symbol": SYMBOL,
                        "side": o.side,
                        "price": o.price,
                        "qty": o.remaining,
                        "reason": "modify_replace",
                    })
                    new_o, fills = self.submit_limit(
                        o.owner, o.side, target_price, target_qty)
                    return new_o, fills, False
        return None, [], False

    def cancel_owner(self, owner: str) -> int:
        """Cancel all open orders for an owner. Returns count cancelled."""
        n = 0
        for lst in (self.bids, self.asks):
            ids = [o.order_id for o in lst if o.owner == owner]
            for oid in ids:
                if self.cancel(oid, reason="user"):
                    n += 1
        return n

    def snapshot(self, depth: int = 10) -> dict:
        agg_bids: dict[int, int] = {}
        for o in self.bids:
            agg_bids[o.price] = agg_bids.get(o.price, 0) + o.remaining
        agg_asks: dict[int, int] = {}
        for o in self.asks:
            agg_asks[o.price] = agg_asks.get(o.price, 0) + o.remaining
        bids = [{"price": p, "qty": q}
                for p, q in sorted(agg_bids.items(), reverse=True)[:depth]]
        asks = [{"price": p, "qty": q}
                for p, q in sorted(agg_asks.items())[:depth]]
        return {"symbol": SYMBOL, "bids": bids, "asks": asks}


# ---------------------------------------------------------------------------
# SimEnv — top-level state + pub/sub for events
# ---------------------------------------------------------------------------
class SimEnv:
    def __init__(self, traders: list[str], clock: SimClock,
                 log_path: Optional[str] = None):
        self.clock = clock
        self.match = SimMatch(self)
        self.positions: dict[str, int] = {t: 0 for t in traders}
        self.cash: dict[str, float] = {t: 0.0 for t in traders}

        self.public_subs: list[Callable[[dict], None]] = []
        self.private_subs: dict[str, list[Callable[[dict], None]]] = {
            t: [] for t in traders
        }

        self.log_path = log_path
        self.log_fh: Optional[io.TextIOBase] = (
            open(log_path, "w") if log_path else None)

        # Game state
        self.phase = "waiting"
        self.reveals: list[int] = []
        self.current_a: Optional[int] = None
        self.current_w: Optional[int] = None

    def _stamp(self, ev: dict) -> dict:
        ev["ts_ns"] = int(self.clock.now() * 1e9)
        ev["sent_ns"] = ev["ts_ns"]
        return ev

    def _log(self, kind: str, ev: dict) -> None:
        if self.log_fh is None:
            return
        rec = {"t_ns": ev.get("ts_ns", int(self.clock.now() * 1e9)),
               "kind": kind, **ev}
        self.log_fh.write(json.dumps(rec, default=str) + "\n")

    def emit_public(self, ev: dict) -> None:
        self._stamp(ev)
        # The live combined logger stores all WS events under
        # kind=raw_unknown_msg with the type sub-field.
        self._log("raw_unknown_msg", ev)
        for sub in self.public_subs:
            sub(ev)
        # Apply book/cash side-effects on fills (the matcher emits the
        # fills before any subscriber sees them, so applying here keeps
        # downstream callbacks observing fresh positions).
        if ev.get("type") == "trade":
            self._apply_trade(ev)

    def _apply_trade(self, ev: dict) -> None:
        # Trades are emitted in pairs with the corresponding fill events.
        # The buyer/seller info is on the fills (not the public trade).
        # We adjust positions in emit_private's fill branch instead.
        pass

    def emit_private(self, trader: str, ev: dict) -> None:
        self._stamp(ev)
        if trader == TRADER:
            self._log("fill" if ev.get("type") == "fill" else "private", ev)
        if ev.get("type") == "fill":
            self._apply_private_fill(trader, ev)
        for sub in self.private_subs.get(trader, []):
            sub(ev)

    def _apply_private_fill(self, trader: str, ev: dict) -> None:
        # Adjust position + cash + fees once per fill — we get one private
        # fill per side, so apply only when ev is for `trader` (which is
        # the trader passed in). For the buyer this side is "buy", for the
        # seller "sell".
        qty = ev["qty"]
        price = ev["price"]
        if ev["side"] == "buy":
            self.positions[trader] += qty
            self.cash[trader] -= price * qty
        else:
            self.positions[trader] -= qty
            self.cash[trader] += price * qty
        fee = TAKER_FEE if ev["liquidity"] == "taker" else MAKER_FEE
        self.cash[trader] -= fee * qty

    def emit_game_state(self, phase: str) -> None:
        ev = self._stamp({
            "type": "game_state",
            "phase": phase,
            "paused": False,
            "reveals": list(self.reveals),
            "running_sum": sum(self.reveals),
        })
        self._log("game_state", ev)
        for sub in self.public_subs:
            sub(ev)

    def emit_reveal(self, idx: int, value: int) -> None:
        ev = self._stamp({
            "type": "reveal",
            "index": idx,
            "value": value,
            "running_sum": sum(self.reveals),
        })
        self._log("reveal", ev)
        for sub in self.public_subs:
            sub(ev)

    def emit_settlement(self, settle: int) -> None:
        ev = self._stamp({
            "type": "settlement",
            "prices": {SYMBOL: settle},
        })
        self._log("settlement", ev)
        for sub in self.public_subs:
            sub(ev)

    def close(self) -> None:
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None


# ---------------------------------------------------------------------------
# Mock GameClient — implements the live GameClient interface against SimEnv
# ---------------------------------------------------------------------------
class MockClient:
    def __init__(self, env: SimEnv, trader: str = TRADER):
        self.env = env
        self.trader = trader

        # Callbacks (Strategy overrides these)
        self.on_fill: Callable[[dict], None] = lambda m: None
        self.on_trade: Callable[[dict], None] = lambda m: None
        self.on_book: Callable[[dict], None] = lambda m: None
        self.on_reveal: Callable[[dict], None] = lambda m: None
        self.on_game_state: Callable[[dict], None] = lambda m: None
        self.on_settlement: Callable[[dict], None] = lambda m: None
        self.on_ack: Callable[[dict], None] = lambda m: None
        self.on_cancel_ack: Callable[[dict], None] = lambda m: None
        self.on_modify_ack: Callable[[dict], None] = lambda m: None
        self.on_reject: Callable[[dict], None] = lambda m: None
        self.on_message: Callable[[dict], None] = lambda m: None

        self.env.public_subs.append(self._dispatch)
        self.env.private_subs[trader].append(self._dispatch)

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        self.on_message(msg)
        if t == "fill":
            self.on_fill(msg)
        elif t == "trade":
            self.on_trade(msg)
        elif t == "book":
            self.on_book(msg)
        elif t == "reveal":
            self.on_reveal(msg)
        elif t == "game_state":
            self.on_game_state(msg)
        elif t == "settlement":
            self.on_settlement(msg)
        elif t == "order_ack":
            self.on_ack(msg)
        elif t == "cancel_ack":
            self.on_cancel_ack(msg)
        elif t == "modify_ack":
            self.on_modify_ack(msg)
        elif t == "reject":
            self.on_reject(msg)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wait_forever(self) -> None:
        pass

    # ---- REST ----

    def game_state(self) -> dict:
        return {
            "phase": self.env.phase,
            "paused": False,
            "duration": DURATION,
            "reveal_interval": REVEAL_INTERVAL,
            "reveals": list(self.env.reveals),
            "running_sum": sum(self.env.reveals),
            "instruments": {SYMBOL: {
                "symbol": SYMBOL,
                "tick_size": TICK,
                "position_limit": POSITION_LIMIT,
            }},
            "fees": {"maker_per_lot": MAKER_FEE, "taker_per_lot": TAKER_FEE},
        }

    def positions(self) -> dict:
        return {"positions": {SYMBOL: int(self.env.positions[self.trader])}}

    def book(self, symbol: Optional[str] = None, depth: int = 10) -> dict:
        return self.env.match.snapshot(depth=depth)

    def _resp(self, order: _Order, fills: list[dict]) -> dict:
        my_trades = [{"price": f["price"], "qty": f["qty"]} for f in fills]
        status = ("partial" if 0 < order.remaining < order.qty
                  else "open" if order.remaining > 0
                  else "filled")
        return {
            "order": {
                "order_id": order.order_id,
                "price": order.price,
                "qty": order.qty,
                "remaining": order.remaining,
                "status": status,
            },
            "trades": my_trades,
        }

    def buy(self, symbol: str, price: int, qty: int,
            client_order_id: Optional[str] = None, tif: str = "gtc") -> dict:
        o, fills = self.env.match.submit_limit(self.trader, "buy", price, qty, tif)
        return self._resp(o, fills)

    def sell(self, symbol: str, price: int, qty: int,
             client_order_id: Optional[str] = None, tif: str = "gtc") -> dict:
        o, fills = self.env.match.submit_limit(self.trader, "sell", price, qty, tif)
        return self._resp(o, fills)

    def buy_ioc(self, symbol: str, price: int, qty: int) -> dict:
        o, fills = self.env.match.submit_limit(self.trader, "buy", price, qty, "ioc")
        return self._resp(o, fills)

    def sell_ioc(self, symbol: str, price: int, qty: int) -> dict:
        o, fills = self.env.match.submit_limit(self.trader, "sell", price, qty, "ioc")
        return self._resp(o, fills)

    def buy_market(self, symbol: str, qty: int, tif: str = "ioc") -> dict:
        o, fills = self.env.match.submit_market(self.trader, "buy", qty)
        return self._resp(o, fills)

    def sell_market(self, symbol: str, qty: int, tif: str = "ioc") -> dict:
        o, fills = self.env.match.submit_market(self.trader, "sell", qty)
        return self._resp(o, fills)

    def modify(self, order_id: int, price: Optional[int] = None,
               qty: Optional[int] = None) -> dict:
        new_o, fills, kept = self.env.match.modify(order_id, price, qty)
        if new_o is None:
            raise RuntimeError(f"modify: order {order_id} not found")
        resp = self._resp(new_o, fills)
        resp["kept_priority"] = kept
        return resp

    def cancel(self, order_id: int) -> dict:
        ok = self.env.match.cancel(order_id, reason="user")
        if not ok:
            raise RuntimeError(f"cancel: order {order_id} not found")
        return {"cancelled": True, "order_id": order_id}

    def cancel_all(self, symbol: Optional[str] = None) -> dict:
        n = self.env.match.cancel_owner(self.trader)
        return {"cancelled": n}

    def my_orders(self, symbol: Optional[str] = None) -> list:
        out = []
        for lst in (self.env.match.bids, self.env.match.asks):
            for o in lst:
                if o.owner == self.trader:
                    out.append({
                        "order_id": o.order_id,
                        "side": "buy" if o.side == "buy" else "sell",
                        "price": o.price,
                        "qty": o.remaining,
                    })
        return out


# ---------------------------------------------------------------------------
# Counterparties
# ---------------------------------------------------------------------------
def _true_remaining_stats(env: SimEnv, a: int, w: int) -> tuple[float, float]:
    """Conditional mean/std of total settle given known (a,w) and the reveals
    so far. This is what a counterparty WITH oracle knowledge of (a,w) would
    quote against. Used to give counterparties a non-trivial signal."""
    n_total = DURATION // REVEAL_INTERVAL
    n_remaining = max(n_total - len(env.reveals), 0)
    running = sum(env.reveals)
    if n_remaining == 0:
        return float(running), 0.0
    mean = running + n_remaining * (a + w / 2.0)
    var = n_remaining * (w * (w + 2)) / 12.0
    return mean, math.sqrt(max(var, 0.0))


class NoiseMM:
    """Wide-spread MM. Posts around true fair + Gaussian noise scaled by sigma."""

    def __init__(self, name: str, env: SimEnv, rng: random.Random,
                 noise_mult: float = 0.4, spread_mult: float = 0.30, qty: int = 10,
                 interval_s: float = 1.5):
        self.name = name
        self.env = env
        self.rng = rng
        self.noise_mult = noise_mult
        self.spread_mult = spread_mult
        self.qty = qty
        self.interval_s = interval_s
        self.bid_id: Optional[int] = None
        self.ask_id: Optional[int] = None
        self.next_step_at: float = 0.0

    def maybe_step(self, true_fair: float, true_sigma: float) -> None:
        t = self.env.clock.now()
        if t < self.next_step_at:
            return
        self.next_step_at = t + self.interval_s * (0.7 + 0.6 * self.rng.random())
        # Cancel existing
        if self.bid_id is not None:
            self.env.match.cancel(self.bid_id, reason="user")
            self.bid_id = None
        if self.ask_id is not None:
            self.env.match.cancel(self.ask_id, reason="user")
            self.ask_id = None
        noise = self.rng.gauss(0, max(true_sigma * self.noise_mult, 3.0))
        mm_fair = true_fair + noise
        spread = max(2, int(true_sigma * self.spread_mult))
        bid_px = max(1, int(mm_fair - spread))
        ask_px = max(bid_px + TICK, int(mm_fair + spread))
        pos = self.env.positions[self.name]
        if POSITION_LIMIT - pos >= self.qty:
            o, _ = self.env.match.submit_limit(self.name, "buy", bid_px, self.qty)
            if o.remaining > 0:
                self.bid_id = o.order_id
        if POSITION_LIMIT + pos >= self.qty:
            o, _ = self.env.match.submit_limit(self.name, "sell", ask_px, self.qty)
            if o.remaining > 0:
                self.ask_id = o.order_id


class TightMM(NoiseMM):
    """Tighter spread, less noise."""

    def __init__(self, name: str, env: SimEnv, rng: random.Random):
        super().__init__(name, env, rng, noise_mult=0.2, spread_mult=0.15,
                         qty=5, interval_s=1.0)


class ValueSniper:
    """Taker bot with a noisy view of true fair. Sweeps mispriced levels."""

    def __init__(self, name: str, env: SimEnv, rng: random.Random,
                 noise_mult: float = 0.2, edge_per_sigma: float = 0.30,
                 min_edge: float = 1.0, interval_s: float = 0.4, max_qty: int = 8):
        self.name = name
        self.env = env
        self.rng = rng
        self.noise_mult = noise_mult
        self.edge_per_sigma = edge_per_sigma
        self.min_edge = min_edge
        self.interval_s = interval_s
        self.max_qty = max_qty
        self.next_step_at: float = 0.0

    def maybe_step(self, true_fair: float, true_sigma: float) -> None:
        t = self.env.clock.now()
        if t < self.next_step_at:
            return
        self.next_step_at = t + self.interval_s * (0.7 + 0.6 * self.rng.random())
        my_fair = true_fair + self.rng.gauss(0, max(true_sigma * self.noise_mult, 1.0))
        edge_req = TAKER_FEE + max(self.min_edge, self.edge_per_sigma * true_sigma)
        snap = self.env.match.snapshot(10)
        # Take cheap asks
        for level in snap["asks"]:
            if my_fair - level["price"] <= edge_req:
                break
            pos = self.env.positions[self.name]
            headroom = POSITION_LIMIT - pos
            if headroom <= 0:
                break
            q = min(level["qty"], headroom, self.max_qty)
            if q <= 0:
                break
            self.env.match.submit_limit(self.name, "buy", level["price"], q, "ioc")
        # Take expensive bids
        snap = self.env.match.snapshot(10)
        for level in snap["bids"]:
            if level["price"] - my_fair <= edge_req:
                break
            pos = self.env.positions[self.name]
            headroom = POSITION_LIMIT + pos
            if headroom <= 0:
                break
            q = min(level["qty"], headroom, self.max_qty)
            if q <= 0:
                break
            self.env.match.submit_limit(self.name, "sell", level["price"], q, "ioc")


# ---------------------------------------------------------------------------
# Round driver
# ---------------------------------------------------------------------------
def sample_round_params(rng: random.Random) -> tuple[int, int]:
    a = int(math.floor(rng.lognormvariate(A_LOGN_MU, A_LOGN_SIGMA)))
    w = 1 + int(math.floor(rng.lognormvariate(W_LOGN_MU, W_LOGN_SIGMA)))
    return a, w


@dataclass
class RoundResult:
    round_idx: int
    a: int
    w: int
    settle: int
    pnl: float
    max_abs_pos: int
    fills: int
    snipes: int
    cap_binds: int


def run_round(env: SimEnv, strategy, counterparties: list,
              rng: random.Random, round_idx: int,
              quiet: bool = True) -> RoundResult:
    # Reset state
    env.match = SimMatch(env)  # fresh book between rounds (live exchange does this)
    for k in env.positions:
        env.positions[k] = 0
        env.cash[k] = 0.0
    env.reveals = []
    a, w = sample_round_params(rng)
    env.current_a, env.current_w = a, w
    # Pre-roll all reveals for the round so settle is deterministic from rng
    pre_reveals = [rng.randint(a, a + w)
                   for _ in range(DURATION // REVEAL_INTERVAL)]
    settle = sum(pre_reveals)

    # Counterparty state reset
    for cp in counterparties:
        cp.bid_id = getattr(cp, "bid_id", None)
        cp.ask_id = getattr(cp, "ask_id", None)
        cp.bid_id = None
        cp.ask_id = None
        cp.next_step_at = 0.0

    # Trackers
    fills_count = [0]
    snipes_count = [0]
    max_abs_pos = [0]
    cap_binds = [0]

    # Subscribe a sniffer for our trader to count fills + cap-bind events
    def sniff(msg: dict) -> None:
        if msg.get("type") == "fill":
            fills_count[0] += 1
            if msg.get("liquidity") == "taker":
                snipes_count[0] += 1
            ap = abs(env.positions[TRADER])
            if ap > max_abs_pos[0]:
                max_abs_pos[0] = ap

    env.private_subs[TRADER].append(sniff)

    # Phase to running
    env.phase = "running"
    env.clock.advance(0.01)
    env.emit_game_state("running")

    # Driver: ticks at 50ms granularity; counterparties step on their own
    # cadence; reveals fire at multiples of reveal_interval.
    tick_dt = 0.05
    t_elapsed = 0.0
    n_total = DURATION // REVEAL_INTERVAL
    next_reveal_idx = 0
    settle_emitted = False
    while t_elapsed < DURATION + 5.0:
        # Reveals
        while (next_reveal_idx < n_total and
               t_elapsed >= (next_reveal_idx + 1) * REVEAL_INTERVAL):
            x = pre_reveals[next_reveal_idx]
            env.reveals.append(x)
            env.emit_reveal(next_reveal_idx, x)
            next_reveal_idx += 1

        # Counterparties react to current state
        true_fair, true_sigma = _true_remaining_stats(env, a, w)
        for cp in counterparties:
            cp.maybe_step(true_fair, true_sigma)

        ap = abs(env.positions[TRADER])
        if ap > max_abs_pos[0]:
            max_abs_pos[0] = ap

        env.clock.advance(tick_dt)
        t_elapsed += tick_dt

        if next_reveal_idx >= n_total and not settle_emitted:
            # All reveals in; let strategy run sweep + then settle
            env.emit_settlement(settle)
            settle_emitted = True
            # Give strategy a few more ticks to sweep
            for _ in range(20):
                env.clock.advance(0.05)
            break

    # Final state changes: phase -> settled
    env.phase = "settled"
    env.emit_game_state("settled")

    # PnL: cash + position * settle, minus any flatten happens via Strategy
    # already settled book/cash. (We don't auto-flatten — settlement
    # gives every contract its settle value.)
    pos = env.positions[TRADER]
    pnl = env.cash[TRADER] + pos * settle

    # Clean up our private sniffer for next round
    env.private_subs[TRADER].remove(sniff)

    return RoundResult(
        round_idx=round_idx,
        a=a, w=w, settle=settle, pnl=pnl,
        max_abs_pos=max_abs_pos[0],
        fills=fills_count[0],
        snipes=snipes_count[0],
        cap_binds=cap_binds[0],
    )


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------
def build_strategy(client: MockClient) -> "strategy_mod.Strategy":
    post = strategy_mod.Posterior(n_sim=SIM_PRIOR_N)
    return strategy_mod.Strategy(client, post, symbol=SYMBOL)


def wire_strategy(client: MockClient, strat: "strategy_mod.Strategy") -> None:
    """Mirror the wiring in strategy_mod.main() — without the input loop."""
    def on_reveal(msg):
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        strat.on_fill_event(msg)

    def on_game_state(msg):
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_message(msg):
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    client.on_reveal = on_reveal
    client.on_fill = on_fill
    client.on_game_state = on_game_state
    client.on_message = on_message
    client.on_trade = lambda m: None
    client.on_book = lambda m: None
    client.on_settlement = lambda m: None
    client.on_ack = lambda m: None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--quick", action="store_true",
                        help="10-round smoke test")
    parser.add_argument("--no-log", action="store_true",
                        help="Skip JSONL log output")
    parser.add_argument("--log", default=None,
                        help="JSONL log path (default: day1/logs/sim_log_<ts>.jsonl)")
    parser.add_argument("--verbose", action="store_true",
                        help="Don't suppress Strategy's stdout chatter")
    args = parser.parse_args()

    rounds = 10 if args.quick else args.rounds
    log_path = None
    if not args.no_log:
        ts = _real_time.strftime("%Y%m%d_%H%M%S")
        default_path = os.path.join(_THIS_DIR, "logs", f"sim_log_{ts}.jsonl")
        log_path = args.log or default_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    rng = random.Random(args.seed)

    # Set up clock + monkey-patch time inside strategy module
    clock = SimClock()
    fake_time = _FakeTime(clock)
    real_strategy_time = strategy_mod.time
    strategy_mod.time = fake_time

    traders = [TRADER, "noise_mm", "tight_mm", "value_sniper"]
    env = SimEnv(traders=traders, clock=clock, log_path=log_path)
    client = MockClient(env, trader=TRADER)

    # Build Strategy (this calls game_state(), positions() on construction)
    env.phase = "waiting"
    print(f"Building Strategy + Posterior (n_sim={SIM_PRIOR_N:,})...")
    strat = build_strategy(client)
    wire_strategy(client, strat)
    print(f"Posterior has {len(strat.posterior.prior)} (a,w) cells.")

    counterparties = [
        NoiseMM("noise_mm", env, random.Random(args.seed + 1)),
        TightMM("tight_mm", env, random.Random(args.seed + 2)),
        ValueSniper("value_sniper", env, random.Random(args.seed + 3)),
    ]

    # Suppress Strategy's prints unless asked
    if not args.verbose:
        sys.stdout = open(os.devnull, "w") if False else _DevNullKeepStderr()

    results: list[RoundResult] = []
    try:
        for i in range(rounds):
            r = run_round(env, strat, counterparties, rng, i)
            results.append(r)
            _print_progress(i, rounds, r)
    finally:
        # Restore real time + stdout
        strategy_mod.time = real_strategy_time
        if not args.verbose:
            sys.stdout = sys.__stdout__
        env.close()

    _print_summary(results, log_path)


class _DevNullKeepStderr:
    """Swallow Strategy's print() output but still allow our progress prints
    via the real stdout. Implemented as a callable file-like wrapper."""

    def write(self, _s: str) -> int:
        return 0

    def flush(self) -> None:
        pass


def _print_progress(i: int, n: int, r: RoundResult) -> None:
    # Write to real stdout
    msg = (f"round {i+1:3d}/{n}  a={r.a:2d} w={r.w:2d} settle={r.settle:3d}  "
           f"pnl={r.pnl:+8.1f}  maxpos={r.max_abs_pos:3d}  "
           f"fills={r.fills:3d}  snipes={r.snipes:3d}")
    sys.__stdout__.write(msg + "\n")
    sys.__stdout__.flush()


def _print_summary(results: list[RoundResult], log_path: Optional[str]) -> None:
    out = sys.__stdout__
    pnls = [r.pnl for r in results]
    maxpos = [r.max_abs_pos for r in results]
    fills = [r.fills for r in results]
    snipes = [r.snipes for r in results]
    out.write("\n" + "=" * 64 + "\n")
    out.write(f"SIM SUMMARY  ({len(results)} rounds)\n")
    out.write("=" * 64 + "\n")
    out.write(f"PnL    mean={statistics.mean(pnls):+.1f}  "
              f"median={statistics.median(pnls):+.1f}  "
              f"stdev={statistics.pstdev(pnls):.1f}\n")
    out.write(f"       min={min(pnls):+.1f}  max={max(pnls):+.1f}  "
              f"sum={sum(pnls):+.1f}\n")
    out.write(f"       VaR_5={_percentile(pnls, 0.05):+.1f}  "
              f"VaR_95={_percentile(pnls, 0.95):+.1f}\n")
    out.write(f"max|pos|  mean={statistics.mean(maxpos):.1f}  "
              f"median={statistics.median(maxpos):.1f}  "
              f"max={max(maxpos)}\n")
    out.write(f"fills     mean={statistics.mean(fills):.1f}  "
              f"median={statistics.median(fills):.1f}  "
              f"max={max(fills)}\n")
    out.write(f"snipes    mean={statistics.mean(snipes):.1f}  "
              f"median={statistics.median(snipes):.1f}  "
              f"max={max(snipes)}\n")
    pos_pnls = [r.pnl for r in results if r.pnl > 0]
    neg_pnls = [r.pnl for r in results if r.pnl <= 0]
    out.write(f"win rate {len(pos_pnls)}/{len(results)} = "
              f"{len(pos_pnls)/len(results):.1%}  "
              f"(+mean {statistics.mean(pos_pnls) if pos_pnls else 0:+.1f}, "
              f"-mean {statistics.mean(neg_pnls) if neg_pnls else 0:+.1f})\n")
    if log_path:
        out.write(f"log -> {log_path}\n")


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(q * (len(s) - 1))))
    return s[i]


if __name__ == "__main__":
    main()
