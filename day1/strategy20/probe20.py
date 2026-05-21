"""v20 log collector — thin wrapper around `probe.Probe`.

Mirrors `strategy19/probe19.py` but emits the log under a v20-specific
filename so v20 sessions don't get tangled with v19 logs during
analysis. v20 trades A/B/C/D; the probe is symbol-agnostic for logging
purposes (every WS event is captured regardless of `symbol`).

Usage from `run_combined20.py`:

    from strategy20.probe20 import make_probe20
    probe = make_probe20(client, symbol="A",
                         log_path="day1/logs/combined_log_v20_...jsonl")
"""

# This module was assembled by inlining `probe_base.py` (the original
# market-microstructure probe + counterparty tracker). probe_base.py
# was deleted after the merge — the runtime behaviour is preserved
# in `_probe_base_main` below, but no longer exposed as a CLI entry
# point. The v20 wrapper `make_probe20(...)` (further down) is what
# `run_combined20.py` uses.

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict
from typing import Dict, Optional



# ===========================================================================
# Inlined from probe_base.py — CounterpartyTracker + Probe + runner
# ===========================================================================

# Keep sibling sdk_client.py importable when run as a script.



# ---------------------------------------------------------------------------
# Connection (edit if your URL / API key differ)
# ---------------------------------------------------------------------------
URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
PROBE_SIZE = 1          # qty per probe order
MAX_PROBE_POS = 5       # auto-flatten if |position| exceeds this
BOOK_THROTTLE_SEC = 0.0 # 0 = log every book update; raise to throttle
HEARTBEAT_SEC = 15.0    # print live status every N seconds
NO_EVENT_WARN_SEC = 20.0  # warn if no WS message received in this long


# ---------------------------------------------------------------------------
# Counterparty tracker
# ---------------------------------------------------------------------------
class CounterpartyTracker:
    """Per-counterparty stats built from our fills and the public tape.

    The market has both bots and humans. Each counterparty has a different
    fingerprint: bots are fast and consistent, humans are slow and bursty.
    This tracker is what lets you tell them apart from a few rounds of data.
    """

    def __init__(self):
        self.lock = threading.RLock()
        # Our fills, keyed by counterparty name
        self.our_fills: dict[str, list[dict]] = defaultdict(list)
        # Public-tape trades (buyer / seller names if exposed)
        self.tape_count: dict[str, int] = defaultdict(int)

    def record_our_fill(self, msg: dict) -> None:
        cp = msg.get("counterparty") or "unknown"
        with self.lock:
            self.our_fills[cp].append({
                "t_ns": time.time_ns(),
                "our_side": msg.get("side"),
                "qty": int(msg.get("qty") or 0),
                "price": float(msg.get("price") or 0),
                "liquidity": msg.get("liquidity"),  # 'maker' or 'taker' (our role)
            })

    def record_tape(self, msg: dict) -> None:
        # The public-tape 'trade' message in this game exposes only aggressor
        # SIDE (buy/sell) and trade_id -- no trader names. We only learn
        # counterparty names from our own fills (via on_fill / counterparty).
        # Names that would be recordable if the server did expose them:
        with self.lock:
            for key in ("buyer", "seller", "maker_trader", "taker_trader"):
                name = msg.get(key)
                if name:
                    self.tape_count[name] += 1

    def stats(self) -> dict:
        with self.lock:
            out: dict[str, dict] = {}
            for cp, fills in self.our_fills.items():
                if not fills:
                    continue
                qty_we_bought = sum(f["qty"] for f in fills if f["our_side"] == "buy")
                qty_we_sold = sum(f["qty"] for f in fills if f["our_side"] == "sell")
                qty_total = qty_we_bought + qty_we_sold
                maker = sum(1 for f in fills if f["liquidity"] == "maker")
                taker = sum(1 for f in fills if f["liquidity"] == "taker")
                vwap = (sum(f["price"] * f["qty"] for f in fills) /
                        max(qty_total, 1))
                out[cp] = {
                    "n_fills": len(fills),
                    "qty_we_bought": qty_we_bought,
                    "qty_we_sold": qty_we_sold,
                    "our_role_maker": maker,
                    "our_role_taker": taker,
                    "avg_price": vwap,
                    "tape_seen": self.tape_count.get(cp, 0),
                }
            # Counterparties seen only on the public tape, not in our fills.
            for cp, n in self.tape_count.items():
                if cp not in out:
                    out[cp] = {
                        "n_fills": 0,
                        "qty_we_bought": 0,
                        "qty_we_sold": 0,
                        "our_role_maker": 0,
                        "our_role_taker": 0,
                        "avg_price": 0.0,
                        "tape_seen": n,
                    }
            return out

    def last_counterparty(self) -> Optional[str]:
        """Counterparty of our most recent fill (or None)."""
        latest_t = -1
        latest_cp: Optional[str] = None
        with self.lock:
            for cp, fills in self.our_fills.items():
                if fills and fills[-1]["t_ns"] > latest_t:
                    latest_t = fills[-1]["t_ns"]
                    latest_cp = cp
        return latest_cp

    def print_summary(self) -> None:
        s = self.stats()
        if not s:
            print("  (no fills or tape data yet)")
            return
        print(f"  {'counterparty':<20s} {'fills':>6s} {'wbought':>8s} "
              f"{'wsold':>7s} {'wmaker':>7s} {'wtaker':>7s} {'vwap':>8s} "
              f"{'tape':>6s}")
        for cp, st in sorted(s.items(), key=lambda kv: -kv[1]["n_fills"]):
            print(f"  {cp[:20]:<20s} {st['n_fills']:>6d} "
                  f"{st['qty_we_bought']:>8d} {st['qty_we_sold']:>7d} "
                  f"{st['our_role_maker']:>7d} {st['our_role_taker']:>7d} "
                  f"{st['avg_price']:>8.2f} {st['tape_seen']:>6d}")


class Probe:
    def __init__(
        self,
        client: GameClient,
        symbol: str,
        log_path: str,
        probe_mode: str,
        probe_interval: float,
        exclude_counterparties: Optional[set[str]] = None,
    ):
        self.c = client
        self.symbol = symbol
        self.probe_mode = probe_mode
        self.probe_interval = probe_interval
        self.exclude_counterparties: set[str] = set(exclude_counterparties or ())

        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        # APPEND mode only. Never use "w" / "wb" here -- that would truncate
        # an existing log on every restart and silently lose prior data.
        self.log_file = open(log_path, "a", buffering=1)  # line-buffered
        self.log_path = log_path
        was_existing = os.path.getsize(log_path) > 0 if os.path.exists(log_path) else False
        print(f"[LOG] writing JSONL to {log_path} "
              f"({'APPENDING to existing file' if was_existing else 'new file'})")

        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._probe_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        # Per-symbol throttle for book snapshots (key=symbol → last log t).
        # Global throttle was dropping cross-symbol books; per-symbol means
        # each instrument's stream is independent.
        self._last_book_t_by_sym: Dict[str, float] = {}
        self.cp = CounterpartyTracker()

        # Liveness tracking -- if no WS messages arrive, the watchdog yells.
        self._msg_count = 0
        self._first_msg_t: Optional[float] = None
        self._last_msg_t: Optional[float] = None
        self._start_t = time.time()

        gs = client.game_state()
        instr = gs["instruments"][symbol]
        self.tick = instr["tick_size"]
        self.position_limit = instr["position_limit"]
        self.phase = gs.get("phase")

        try:
            self.position = int(client.positions()["positions"].get(symbol, 0))
        except Exception:
            self.position = 0

        self.log("session_start", symbol=symbol, mode=probe_mode,
                 probe_interval=probe_interval, tick=self.tick,
                 position_limit=self.position_limit, initial_position=self.position)

    # ---------- logging ----------
    def log(self, kind: str, **fields) -> None:
        rec = {"t_ns": time.time_ns(), "kind": kind, **fields}
        try:
            self.log_file.write(json.dumps(rec, default=str) + "\n")
        except Exception as e:
            # Never let logging crash the probe
            sys.stderr.write(f"log write failed: {e}\n")

    # ---------- WS handlers (also log everything) ----------
    def _touch(self) -> None:
        now = time.time()
        if self._first_msg_t is None:
            self._first_msg_t = now
            print(f"[WS] first message received after {now - self._start_t:.1f}s")
        self._last_msg_t = now
        self._msg_count += 1

    def on_reveal(self, msg: dict) -> None:
        self._touch()
        self.log("reveal", **msg)
        print(f"REVEAL #{msg.get('index')} = {msg.get('value')}  "
              f"running_sum={msg.get('running_sum')}")
        # Snapshot the book just after the reveal for offline analysis. Fire
        # in a daemon thread: in run_combined the strategy's on_reveal runs
        # immediately after this and calls cancel_all -- any synchronous REST
        # here would widen the adverse-pickoff window where a faster taker
        # can lift our stale-priced orders at the OLD fair.
        threading.Thread(
            target=self._snapshot_book_after_reveal,
            daemon=True,
        ).start()

    def _snapshot_book_after_reveal(self) -> None:
        try:
            book = self.c.book(self.symbol)
            self.log("book_after_reveal", **book)
        except Exception as e:
            self.log("book_fetch_error", where="after_reveal", err=str(e))

    def on_fill(self, msg: dict) -> None:
        self._touch()
        self.log("fill", **msg)
        self.cp.record_our_fill(msg)
        with self.lock:
            if msg.get("side") == "buy":
                self.position += msg.get("qty", 0)
            elif msg.get("side") == "sell":
                self.position -= msg.get("qty", 0)
        print(f"FILL  {msg.get('side', '?'):>4s} {msg.get('qty')} @ "
              f"{msg.get('price')}  liq={msg.get('liquidity')} vs "
              f"{msg.get('counterparty')}  pos={self.position}")
        self._safety_check()

    def on_trade(self, msg: dict) -> None:
        self._touch()
        self.log("trade", **msg)
        self.cp.record_tape(msg)

    def on_book(self, msg: dict) -> None:
        self._touch()
        if BOOK_THROTTLE_SEC <= 0.0:
            self.log("book", **msg)
            return
        sym = msg.get("symbol") or ""
        now = time.time()
        last = self._last_book_t_by_sym.get(sym, 0.0)
        if now - last >= BOOK_THROTTLE_SEC:
            self._last_book_t_by_sym[sym] = now
            self.log("book", **msg)

    def on_game_state(self, msg: dict) -> None:
        self._touch()
        prev = self.phase
        self.phase = msg.get("phase")
        self.log("game_state", **msg)
        if prev != self.phase:
            print(f"PHASE  {prev} -> {self.phase}  "
                  f"reveals={len(msg.get('reveals') or [])}")

    def on_settlement(self, msg: dict) -> None:
        self._touch()
        self.log("settlement", **msg)
        self.log("counterparty_summary", stats=self.cp.stats())
        print(f"SETTLE  prices={msg.get('prices')}  pnl={msg.get('pnl')}")

    def on_ack(self, msg: dict) -> None:
        self._touch()
        self.log("ack", **msg)

    def on_cancel_ack(self, msg: dict) -> None:
        self._touch()
        self.log("cancel_ack", **msg)

    def on_modify_ack(self, msg: dict) -> None:
        self._touch()
        self.log("modify_ack", **msg)

    def on_reject(self, msg: dict) -> None:
        self._touch()
        self.log("reject", **msg)
        print(f"REJECT  {msg}")

    def on_message(self, msg: dict) -> None:
        # SDK calls on_message FIRST for every WS frame, then dispatches a
        # typed handler (on_fill, on_trade, ...) for known types. We must
        # skip those here or we'd double-log every fill/trade. Don't _touch
        # either -- the typed handler will.
        t = msg.get("type")
        typed = {"fill", "trade", "book", "reveal", "game_state", "settlement",
                 "order_ack", "cancel_ack", "modify_ack", "reject"}
        if t in typed:
            return
        # Everything else: log under its actual type as the kind so jq filters
        # like `select(.kind=="quote_add")` work cleanly. Empirically the
        # exchange streams quote_add / quote_cancel / quote_fill / quote_modify
        # (per-order book deltas) and a one-shot 'hello' greeting -- none of
        # which appear in the handout (instructions hint: "not all exchange
        # messages provided by the exchange are given in the notebook").
        # Anything we haven't seen before is logged as raw_unknown_msg.
        documented_untyped = {"quote_add", "quote_cancel", "quote_fill",
                              "quote_modify", "hello",
                              # tick_settlement: per-reveal credit on B
                              # (PnL = trade_cash + tick_cash * mult).
                              # strikes: instrument metadata (strike list).
                              # Both stream regularly — logged but not
                              # printed so they don't flood the terminal.
                              "tick_settlement", "strikes"}
        kind = t if t in documented_untyped else "raw_unknown_msg"
        self.log(kind, **msg)
        # Quote-* events fire many times per second and would flood the terminal.
        # Only print truly novel types -- those are the ones worth investigating.
        if t not in documented_untyped:
            print(f"RAW unknown type={t!r}  msg={msg}")

    # ---------- safety ----------
    def _safety_check(self) -> None:
        with self.lock:
            if abs(self.position) > MAX_PROBE_POS:
                self.log("safety_flatten_trigger", position=self.position)
                self._flatten_locked()

    def flatten(self) -> None:
        with self.lock:
            self._flatten_locked()

    def _flatten_locked(self) -> None:
        try:
            self.c.cancel_all()
        except Exception as e:
            self.log("flatten_cancel_all_error", err=str(e))
        try:
            pos = int(self.c.positions()["positions"].get(self.symbol, 0))
        except Exception:
            pos = self.position
        if pos > 0:
            try:
                res = self.c.sell_market(self.symbol, qty=pos)
                self.log("flatten_sell_market", qty=pos, result=res)
                for t in res.get("trades", []):
                    self.position -= t.get("qty", 0)
            except Exception as e:
                self.log("flatten_sell_error", err=str(e))
        elif pos < 0:
            try:
                res = self.c.buy_market(self.symbol, qty=-pos)
                self.log("flatten_buy_market", qty=-pos, result=res)
                for t in res.get("trades", []):
                    self.position += t.get("qty", 0)
            except Exception as e:
                self.log("flatten_buy_error", err=str(e))
        print(f"FLAT  pos was {pos} -> now {self.position}")

    # ---------- probe loop ----------
    def start_probes(self) -> None:
        if self.probe_mode == "passive":
            return
        self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
        self._probe_thread.start()

    def start_heartbeat(self) -> None:
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(HEARTBEAT_SEC)
            if self._stop.is_set():
                return
            now = time.time()
            if self._first_msg_t is None:
                age = now - self._start_t
                if age >= NO_EVENT_WARN_SEC:
                    print(
                        f"[WARN] no WS messages received in {age:.0f}s. "
                        f"Possible causes:\n"
                        f"  - WS not connecting (check URL/firewall): "
                        f"ws://{URL.split('//', 1)[1]}/ws/private\n"
                        f"  - Another client (e.g. strategy.py) is holding "
                        f"this API key's WS slot -- kill it first\n"
                        f"  - Game phase is 'setup' and there's no public "
                        f"book/tape activity yet"
                    )
            else:
                quiet = now - (self._last_msg_t or self._start_t)
                print(f"[HB] phase={self.phase} pos={self.position} "
                      f"msgs={self._msg_count} last={quiet:.1f}s ago")

    def stop(self) -> None:
        self._stop.set()

    def _probe_loop(self) -> None:
        sequence = {
            "repost": [self._probe_repost],
            "fronting": [self._probe_fronting],
            "snipe_latency": [self._probe_snipe_latency],
            "all": [self._probe_repost, self._probe_fronting, self._probe_snipe_latency],
        }[self.probe_mode]

        while not self._stop.is_set():
            self._stop.wait(self.probe_interval)
            if self._stop.is_set():
                return
            if self.phase != "running":
                self.log("probe_skipped", reason="phase_not_running", phase=self.phase)
                continue
            for fn in sequence:
                if self._stop.is_set():
                    return
                try:
                    fn()
                except Exception as e:
                    self.log("probe_error", probe=fn.__name__, err=str(e))
                # small gap between probes within "all" mode
                self._stop.wait(2.0)

    def _can_probe(self, side: str) -> bool:
        """Skip probe if it would push us past the safety position threshold,
        or if our most recent counterparty is on the exclude list (politeness:
        don't repeatedly target the same human player)."""
        with self.lock:
            if side == "buy" and self.position + PROBE_SIZE > MAX_PROBE_POS:
                return False
            if side == "sell" and self.position - PROBE_SIZE < -MAX_PROBE_POS:
                return False
        last_cp = self.cp.last_counterparty()
        if last_cp and last_cp in self.exclude_counterparties:
            self.log("probe_skipped_exclude", last_counterparty=last_cp)
            return False
        return True

    def _sample_book_after(self, probe_name: str, t0_ns: int,
                            sample_ms: tuple[int, ...]) -> None:
        for dt_ms in sample_ms:
            target_ns = t0_ns + dt_ms * 1_000_000
            wait_ns = target_ns - time.time_ns()
            if wait_ns > 0:
                time.sleep(wait_ns / 1e9)
            try:
                snap = self.c.book(self.symbol)
                self.log("probe_book_after", probe=probe_name, dt_ms=dt_ms,
                         bids=snap.get("bids"), asks=snap.get("asks"))
            except Exception as e:
                self.log("probe_book_error", probe=probe_name, dt_ms=dt_ms, err=str(e))

    # ---------- probes ----------
    def _probe_repost(self) -> None:
        """Lift 1 lot at best ask; sample book reactions over the next 5 seconds.

        What to look for in the log:
          - How fast does someone repost an ask? (latency)
          - Is the new ask at a higher price than before? (inventory aversion)
          - Does the SAME counterparty repost (look at owner field if exposed),
            or did a different bot fill the gap?
        """
        if not self._can_probe("buy"):
            self.log("probe_skipped", probe="repost", reason="position_guard")
            return
        try:
            book_before = self.c.book(self.symbol)
        except Exception as e:
            self.log("probe_error", probe="repost", err=f"book before: {e}")
            return
        asks = book_before.get("asks") or []
        if not asks:
            self.log("probe_skipped", probe="repost", reason="no_asks")
            return
        best_ask = asks[0]
        self.log("probe_start", probe="repost", best_ask=best_ask, book=book_before)
        t0 = time.time_ns()
        try:
            res = self.c.buy_ioc(self.symbol, price=best_ask["price"], qty=PROBE_SIZE)
        except Exception as e:
            self.log("probe_error", probe="repost", err=f"buy_ioc: {e}")
            return
        self.log("probe_action", probe="repost", price=best_ask["price"],
                 qty=PROBE_SIZE, result=res, t_send_ns=t0,
                 t_recv_ns=time.time_ns())
        self._sample_book_after("repost", t0, (50, 100, 200, 500, 1000, 2000, 5000))

    def _probe_fronting(self) -> None:
        """Post a bid 1 tick INSIDE best bid (becomes new top-of-book). See if
        anyone fronts it (posts at probe_px + tick) within 3 seconds.

        What to look for:
          - Does another bid appear at probe_px + tick? (a fronting bot)
          - Does an ask get cheaper (bot infers higher fair from our quote)?
          - Does our bid get LIFTED by someone selling? That means someone
            has fair <= probe_px -- they're a useful seller to identify.
        """
        if not self._can_probe("buy"):
            self.log("probe_skipped", probe="fronting", reason="position_guard")
            return
        try:
            book_before = self.c.book(self.symbol)
        except Exception as e:
            self.log("probe_error", probe="fronting", err=f"book before: {e}")
            return
        bids = book_before.get("bids") or []
        asks = book_before.get("asks") or []
        if not bids or not asks:
            self.log("probe_skipped", probe="fronting", reason="empty_book")
            return
        best_bid = bids[0]["price"]
        best_ask = asks[0]["price"]
        probe_px = best_bid + self.tick
        if probe_px >= best_ask:
            self.log("probe_skipped", probe="fronting", reason="spread_too_tight",
                     best_bid=best_bid, best_ask=best_ask)
            return
        self.log("probe_start", probe="fronting", best_bid=best_bid,
                 best_ask=best_ask, probe_px=probe_px, book=book_before)
        t0 = time.time_ns()
        try:
            res = self.c.buy(self.symbol, price=probe_px, qty=PROBE_SIZE, tif="gtc")
        except Exception as e:
            self.log("probe_error", probe="fronting", err=f"buy gtc: {e}")
            return
        order_id = res.get("order", {}).get("order_id")
        self.log("probe_action", probe="fronting", price=probe_px,
                 qty=PROBE_SIZE, result=res, t_send_ns=t0, t_recv_ns=time.time_ns())
        self._sample_book_after("fronting", t0, (50, 100, 200, 500, 1000, 2000, 3000))
        # Always cancel at end
        if order_id is not None:
            try:
                self.c.cancel(order_id)
                self.log("probe_cancel", probe="fronting", order_id=order_id)
            except Exception as e:
                self.log("probe_error", probe="fronting", err=f"cancel: {e}")

    def _probe_snipe_latency(self) -> None:
        """Post an ask 1 tick INSIDE best ask. Time until someone lifts it.

        What to look for:
          - Time-to-fill = lower bound on any sniper's response latency.
          - If filled, counterparty in the fill message tells you who's
            aggressively buying inside-spread prints (likely a snipe-style bot).
          - If NOT filled in 5s, the existing book spread is the binding
            constraint and there's no aggressive taker present.
        """
        if not self._can_probe("sell"):
            self.log("probe_skipped", probe="snipe_lat", reason="position_guard")
            return
        try:
            book_before = self.c.book(self.symbol)
        except Exception as e:
            self.log("probe_error", probe="snipe_lat", err=f"book before: {e}")
            return
        bids = book_before.get("bids") or []
        asks = book_before.get("asks") or []
        if not bids or not asks:
            self.log("probe_skipped", probe="snipe_lat", reason="empty_book")
            return
        best_bid = bids[0]["price"]
        best_ask = asks[0]["price"]
        probe_px = best_ask - self.tick
        if probe_px <= best_bid:
            self.log("probe_skipped", probe="snipe_lat", reason="spread_too_tight",
                     best_bid=best_bid, best_ask=best_ask)
            return
        self.log("probe_start", probe="snipe_lat", best_bid=best_bid,
                 best_ask=best_ask, probe_px=probe_px, book=book_before)
        t0 = time.time_ns()
        try:
            res = self.c.sell(self.symbol, price=probe_px, qty=PROBE_SIZE, tif="gtc")
        except Exception as e:
            self.log("probe_error", probe="snipe_lat", err=f"sell gtc: {e}")
            return
        order_id = res.get("order", {}).get("order_id")
        immediate_remaining = res.get("order", {}).get("remaining", PROBE_SIZE)
        self.log("probe_action", probe="snipe_lat", price=probe_px,
                 qty=PROBE_SIZE, result=res, t_send_ns=t0,
                 t_recv_ns=time.time_ns(),
                 filled_immediately=(immediate_remaining == 0))
        if immediate_remaining == 0:
            return
        # Poll until filled or 5s timeout
        for dt_ms in (50, 100, 200, 500, 1000, 2000, 5000):
            target_ns = t0 + dt_ms * 1_000_000
            wait_ns = target_ns - time.time_ns()
            if wait_ns > 0:
                time.sleep(wait_ns / 1e9)
            try:
                orders = self.c.my_orders(self.symbol)
                still_open = any(o.get("order_id") == order_id for o in orders)
                self.log("probe_check", probe="snipe_lat", dt_ms=dt_ms,
                         still_open=still_open)
                if not still_open:
                    return
            except Exception as e:
                self.log("probe_check_error", probe="snipe_lat", dt_ms=dt_ms,
                         err=str(e))
        # Timed out -- cancel
        if order_id is not None:
            try:
                self.c.cancel(order_id)
                self.log("probe_cancel", probe="snipe_lat", order_id=order_id,
                         reason="timeout_5s")
            except Exception as e:
                self.log("probe_error", probe="snipe_lat", err=f"cancel: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _probe_base_main() -> None:
    parser = argparse.ArgumentParser(description="Market microstructure probe")
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--mode", default="passive",
                        choices=["passive", "repost", "fronting", "snipe_latency", "all"])
    parser.add_argument("--probe-interval", type=float, default=30.0,
                        help="Seconds between probes (default 30 -- intentionally polite)")
    parser.add_argument("--exclude-counterparty", action="append", default=[],
                        metavar="NAME",
                        help="Skip a probe if our most recent fill was against NAME. "
                             "Use repeatedly to exclude multiple counterparties.")
    parser.add_argument("--log", default=None,
                        help="JSONL output path. Default: day1/logs/probe_log_<timestamp>.jsonl "
                             "(per-run file in the centralized logs dir so sessions "
                             "are not interleaved). Whatever path you pass is opened "
                             "in APPEND mode -- existing data is never overwritten.")
    args = parser.parse_args()
    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/probe_log_{ts}.jsonl"

    c = GameClient(URL, API_KEY)

    # Startup self-check: verify REST + auth work before bothering with WS.
    try:
        gs = c.game_state()
        print(f"[REST] OK  phase={gs.get('phase')}  "
              f"reveals={len(gs.get('reveals') or [])}  "
              f"duration={gs.get('duration')}  "
              f"reveal_interval={gs.get('reveal_interval')}")
    except Exception as e:
        print(f"[REST] FAILED: {e}\n  -> URL ({URL}) or network unreachable. "
              f"Fix this before going further.")
        sys.exit(1)
    try:
        pos = c.positions()
        print(f"[AUTH] OK  positions={pos}")
    except Exception as e:
        print(f"[AUTH] FAILED: {e}\n  -> API_KEY ({API_KEY}) is rejected. "
              f"Edit URL/API_KEY at top of probe.py.")
        sys.exit(1)
    ws_url = URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/private"
    print(f"[WS] will connect to {ws_url}?api_key=<redacted>")

    probe = Probe(
        c, args.symbol, args.log, args.mode, args.probe_interval,
        exclude_counterparties=set(args.exclude_counterparty),
    )

    c.on_reveal = probe.on_reveal
    c.on_fill = probe.on_fill
    c.on_trade = probe.on_trade
    c.on_book = probe.on_book
    c.on_game_state = probe.on_game_state
    c.on_settlement = probe.on_settlement
    c.on_ack = probe.on_ack
    c.on_cancel_ack = probe.on_cancel_ack
    c.on_modify_ack = probe.on_modify_ack
    c.on_reject = probe.on_reject
    c.on_message = probe.on_message  # catch-all for undocumented types

    c.start()
    probe.start_probes()
    probe.start_heartbeat()

    print(f"\nProbe running. mode={args.mode}  symbol={args.symbol}  "
          f"log={args.log}")
    if args.exclude_counterparty:
        print(f"Excluding counterparties: {args.exclude_counterparty}")
    print("Commands: 's'=status  'c'=counterparty stats  'f'=flatten  "
          "'q'=quit (auto-flattens). Ctrl-C also flattens.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                # Detached stdin: just sleep forever, keep handlers running.
                while True:
                    time.sleep(60)
            if cmd == "s":
                try:
                    book = c.book(args.symbol)
                    bb = (book.get("bids") or [{}])[0]
                    ba = (book.get("asks") or [{}])[0]
                    print(f"  phase={probe.phase}  pos={probe.position}  "
                          f"best_bid={bb}  best_ask={ba}")
                except Exception as e:
                    print(f"  status error: {e}")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "f":
                probe.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s / c / f / q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Stopping probes; flattening; closing log...")
        probe.stop()
        try:
            probe.flatten()
        except Exception as e:
            print(f"flatten on exit failed: {e}")
        try:
            probe.log_file.close()
        except Exception:
            pass


# ===========================================================================
# v20-specific wrapper (originally probe20.py body)
# ===========================================================================

from strategy20 import GameClient  # noqa: E402

def default_log_path() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"day1/logs/combined_log_v20_{ts}.jsonl"


def make_probe20(client: GameClient, symbol: str = "A",
                 log_path: Optional[str] = None) -> Probe:
    """Build a v20 probe wired for passive logging only.

    The strategy owns all order placement / cancellation; the probe is
    a pure WS-event recorder. We disable its built-in probe scheduler
    (probe_mode='passive', probe_interval huge) and stub _safety_check
    so it never tries to flatten on its own.
    """
    if log_path is None:
        log_path = default_log_path()
    p = Probe(client, symbol, log_path,
              probe_mode="passive", probe_interval=999999.0)
    p._safety_check = lambda: None  # type: ignore[assignment]
    p.log("v20_session_marker", note="strategy20 runner (A/B/C/D)")
    return p


__all__ = [
    "Probe",
    "CounterpartyTracker",
    "GameClient",
    "make_probe20",
    "default_log_path",
]
