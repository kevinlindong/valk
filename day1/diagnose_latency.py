"""day1/diagnose_latency.py

Pure-observation diagnostic for exchange network and server timing.

Why: Strategy v10 sits 30-40 ms behind first-IOC competitors at every
reveal race. Closing that gap depends on which part of the stack is
the bottleneck (REST POST RTT, WS frame delivery, our own dispatch).
This script measures all three on a live game window so the next round
of infra tuning starts from numbers, not guesses.

What gets logged (JSONL, line-buffered, one record per event):
  * Pre-flight REST: /api/game, /api/positions, /api/book, /api/instruments
    with rtt_us per call.
  * Every WS frame (typed and untyped) carries:
        t_ns                   epoch ns at receive (local)
        t_perf_ns              monotonic ns (use for inter-event delta)
        server_sent_ns         server send time copied from frame
        skew_us                t_ns - server_sent_ns  (network + clock skew)
        gap_us                 perf delta since previous WS frame
        payload                full WS message
  * REST RTT probe loop (default 1 Hz): rotates book / positions / game
    and logs rtt_us + tokens_left after each call.
  * Optional --probe-orders: every N s (default 5) sends a non-marketable
    buy IOC at price=1, qty=1. Cannot cross (no one sells at 1), IOC
    kills residual -> clean write-path round-trip, zero market impact.
  * Derived per-reveal: reveal -> first-trade and reveal -> first-book
    latency on the public tape.

Single-WS-per-key constraint: the exchange only allows one private WS
per API key, so this MUST run INSTEAD of run_combined10.py for the game
window you want to characterize.

Interactive (when stdin is a tty):
    s    live stats (RTT percentiles, skew, msg counts)
    q    quit (Ctrl-C also flushes + writes session_end record)

Run:
    python day1/diagnose_latency.py
    python day1/diagnose_latency.py --probe-interval 2.0
    python day1/diagnose_latency.py --probe-orders --probe-orders-interval 10
    python day1/diagnose_latency.py --log /tmp/diag.jsonl --duration 300

Analyze afterwards (rough):
    jq -c 'select(.kind=="rest_book")'    day1/logs/diag_*.jsonl | head
    jq -c 'select(.kind=="ws_reveal")'    day1/logs/diag_*.jsonl
    jq -c 'select(.kind=="ws_trade" and .reveal_to_trade_us!=null)' \\
          day1/logs/diag_*.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from typing import Callable, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
for _p in (_PARENT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk.client import GameClient  # noqa: E402
from strategy10 import URL, API_KEY  # noqa: E402


class Diagnostic:
    def __init__(self, client: GameClient, symbol: str, log_path: str,
                 probe_interval: float, probe_orders: bool,
                 probe_orders_interval: float):
        self.c = client
        self.symbol = symbol
        self.probe_interval = probe_interval
        self.probe_orders = probe_orders
        self.probe_orders_interval = probe_orders_interval

        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        self.log_file = open(log_path, "a", buffering=1)
        self.log_path = log_path
        print(f"[LOG] writing JSONL to {log_path}")

        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._t0_perf_ns = time.perf_counter_ns()
        self._t0_wall_ns = time.time_ns()
        self._msg_count = 0
        self._first_msg_perf_ns: Optional[int] = None
        self._last_msg_perf_ns: Optional[int] = None

        self._last_reveal_perf_ns: Optional[int] = None
        self._last_reveal_server_ns: Optional[int] = None
        self._last_reveal_index: Optional[int] = None
        self._first_trade_after_reveal_done = True
        self._first_book_after_reveal_done = True
        self._phase: Optional[str] = None

        # Rolling buffers used only for the 's' live readout. The full
        # log is the source of truth -- these are bounded so 'h'/'s' is cheap.
        self._rest_rtt_us: dict[str, deque] = {
            ep: deque(maxlen=1000) for ep in
            ("preflight", "book", "positions", "game", "order_probe")
        }
        self._ws_skew_us: deque = deque(maxlen=2000)
        self._ws_gap_us: deque = deque(maxlen=2000)
        self._ws_type_count: dict[str, int] = {}
        self._reveal_to_trade_us: deque = deque(maxlen=200)
        self._reveal_to_book_us: deque = deque(maxlen=200)

        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Logging primitive
    # ------------------------------------------------------------------
    def log(self, kind: str, **fields) -> None:
        rec = {
            "t_ns": time.time_ns(),
            "t_perf_ns": time.perf_counter_ns(),
            "kind": kind,
            **fields,
        }
        try:
            self.log_file.write(json.dumps(rec, default=str) + "\n")
        except Exception as e:
            sys.stderr.write(f"log write failed: {e}\n")

    # ------------------------------------------------------------------
    # WS event timing (called exactly once per WS frame)
    # ------------------------------------------------------------------
    def _enrich(self, etype: str, msg: dict) -> dict:
        now_perf = time.perf_counter_ns()
        now_wall = time.time_ns()
        gap_us: Optional[int]
        with self._lock:
            self._msg_count += 1
            if self._first_msg_perf_ns is None:
                self._first_msg_perf_ns = now_perf
                print(f"[WS] first message after "
                      f"{(now_perf - self._t0_perf_ns)/1e6:.1f}ms; "
                      f"type={etype!r}")
            if self._last_msg_perf_ns is not None:
                gap_us = int((now_perf - self._last_msg_perf_ns) / 1000)
                self._ws_gap_us.append(gap_us)
            else:
                gap_us = None
            self._last_msg_perf_ns = now_perf
            self._ws_type_count[etype] = self._ws_type_count.get(etype, 0) + 1

        server_ns = msg.get("sent_ns")
        skew_us: Optional[int] = None
        if isinstance(server_ns, (int, float)):
            skew_us = int((now_wall - int(server_ns)) / 1000)
            with self._lock:
                self._ws_skew_us.append(skew_us)
        return {
            "etype": etype,
            "gap_us": gap_us,
            "skew_us": skew_us,
            "server_sent_ns": server_ns,
        }

    # ------------------------------------------------------------------
    # WS handler factory -- one closure per typed callback
    # ------------------------------------------------------------------
    def make_handler(self, etype: str) -> Callable[[dict], None]:
        def handler(msg: dict) -> None:
            enrich = self._enrich(etype, msg)
            extra: dict = {}

            if etype == "reveal":
                with self._lock:
                    self._last_reveal_perf_ns = time.perf_counter_ns()
                    self._last_reveal_server_ns = msg.get("sent_ns")
                    self._last_reveal_index = msg.get("index")
                    self._first_trade_after_reveal_done = False
                    self._first_book_after_reveal_done = False
                print(f"[REVEAL] #{msg.get('index')} value={msg.get('value')}"
                      f"  skew={enrich['skew_us']}us"
                      f"  ws_gap_prev={enrich['gap_us']}us")
            elif etype == "trade":
                with self._lock:
                    if (not self._first_trade_after_reveal_done
                            and self._last_reveal_perf_ns is not None):
                        delta_us = int(
                            (time.perf_counter_ns() - self._last_reveal_perf_ns)
                            / 1000
                        )
                        self._reveal_to_trade_us.append(delta_us)
                        extra["reveal_to_trade_us"] = delta_us
                        extra["reveal_index"] = self._last_reveal_index
                        self._first_trade_after_reveal_done = True
            elif etype == "book":
                with self._lock:
                    if (not self._first_book_after_reveal_done
                            and self._last_reveal_perf_ns is not None):
                        delta_us = int(
                            (time.perf_counter_ns() - self._last_reveal_perf_ns)
                            / 1000
                        )
                        self._reveal_to_book_us.append(delta_us)
                        extra["reveal_to_book_us"] = delta_us
                        extra["reveal_index"] = self._last_reveal_index
                        self._first_book_after_reveal_done = True
            elif etype == "game_state":
                with self._lock:
                    prev = self._phase
                    self._phase = msg.get("phase")
                if self._phase != prev:
                    print(f"[GAME] phase {prev} -> {self._phase} "
                          f"reveals={len(msg.get('reveals') or [])}")
            elif etype == "settlement":
                print(f"[SETTLE] prices={msg.get('prices')} "
                      f"pnl={msg.get('pnl')}")
            elif etype == "reject":
                print(f"[REJECT] {msg}")
            elif etype == "fill":
                print(f"[FILL] {msg.get('side')} {msg.get('qty')}"
                      f"@{msg.get('price')} liq={msg.get('liquidity')}"
                      f" vs {msg.get('counterparty')}")

            self.log("ws_" + etype, payload=msg, **enrich, **extra)
        return handler

    def on_message(self, msg: dict) -> None:
        # The SDK fires on_message FIRST for every WS frame, then dispatches a
        # typed callback for known types. We must skip the typed types here or
        # we'd double-log AND double-count.
        t = msg.get("type")
        typed = {"fill", "trade", "book", "reveal", "game_state",
                 "settlement", "order_ack", "cancel_ack", "modify_ack",
                 "reject"}
        if t in typed:
            return
        etype = t or "unknown"
        enrich = self._enrich(etype, msg)
        self.log("ws_" + etype, payload=msg, **enrich)
        if t not in {"quote_add", "quote_cancel", "quote_modify",
                     "quote_fill", "hello"}:
            print(f"[RAW] unknown type={t!r} msg={msg}")

    # ------------------------------------------------------------------
    # REST probe loop (rotates endpoints)
    # ------------------------------------------------------------------
    def _rest_call(self, name: str, fn: Callable[[], object]) -> None:
        t0 = time.perf_counter_ns()
        try:
            res = fn()
            rtt_us = int((time.perf_counter_ns() - t0) / 1000)
            self._rest_rtt_us[name].append(rtt_us)
            self.log("rest_" + name, rtt_us=rtt_us,
                     tokens_left=self.c.tokens_available(),
                     ok=True,
                     response_size=len(str(res)))
        except Exception as e:
            rtt_us = int((time.perf_counter_ns() - t0) / 1000)
            self.log("rest_" + name, rtt_us=rtt_us, ok=False, err=str(e))

    def _rest_probe_loop(self) -> None:
        funcs = [
            ("book", lambda: self.c.book(self.symbol, depth=5)),
            ("positions", self.c.positions),
            ("game", self.c.game_state),
        ]
        i = 0
        while not self._stop.is_set():
            name, fn = funcs[i % len(funcs)]
            self._rest_call(name, fn)
            i += 1
            self._stop.wait(self.probe_interval)

    # ------------------------------------------------------------------
    # Optional order-probe loop (non-marketable IOC -- pure RTT measure)
    # ------------------------------------------------------------------
    def _order_probe_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter_ns()
            try:
                res = self.c.buy_ioc(self.symbol, price=1, qty=1)
                rtt_us = int((time.perf_counter_ns() - t0) / 1000)
                self._rest_rtt_us["order_probe"].append(rtt_us)
                self.log("rest_order_probe", rtt_us=rtt_us, ok=True,
                         response=res,
                         tokens_left=self.c.tokens_available())
            except Exception as e:
                rtt_us = int((time.perf_counter_ns() - t0) / 1000)
                self.log("rest_order_probe", rtt_us=rtt_us, ok=False,
                         err=str(e))
            self._stop.wait(self.probe_orders_interval)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        t = threading.Thread(target=self._rest_probe_loop, daemon=True,
                             name="rest_probe")
        t.start()
        self._threads.append(t)
        if self.probe_orders:
            t = threading.Thread(target=self._order_probe_loop, daemon=True,
                                 name="order_probe")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    @staticmethod
    def _pct(xs: list, q: float) -> Optional[int]:
        if not xs:
            return None
        xs2 = sorted(xs)
        k = max(0, min(len(xs2) - 1, int(round(q * (len(xs2) - 1)))))
        return xs2[k]

    def _quantiles(self, buf) -> dict:
        xs = list(buf)
        return {
            "p50": self._pct(xs, 0.5),
            "p90": self._pct(xs, 0.9),
            "p99": self._pct(xs, 0.99),
            "max": max(xs) if xs else None,
            "n": len(xs),
        }

    def stats(self) -> dict:
        with self._lock:
            return {
                "uptime_s": (time.perf_counter_ns() - self._t0_perf_ns) / 1e9,
                "msg_count": self._msg_count,
                "phase": self._phase,
                "type_count": dict(self._ws_type_count),
                "ws_skew_us": self._quantiles(self._ws_skew_us),
                "ws_gap_us": self._quantiles(self._ws_gap_us),
                "rest_rtt_us": {
                    name: self._quantiles(buf)
                    for name, buf in self._rest_rtt_us.items()
                },
                "reveal_to_trade_us": self._quantiles(self._reveal_to_trade_us),
                "reveal_to_book_us": self._quantiles(self._reveal_to_book_us),
            }

    def print_stats(self) -> None:
        s = self.stats()
        print(f"  uptime={s['uptime_s']:.1f}s msgs={s['msg_count']} "
              f"phase={s['phase']}")
        types = ", ".join(f"{k}={v}" for k, v in sorted(s["type_count"].items()))
        print(f"  by type: {types}")
        for name, p in s["rest_rtt_us"].items():
            if p["n"] == 0:
                continue
            print(f"  REST {name:>13s}  p50={p['p50']:>5}us  "
                  f"p90={p['p90']:>6}us  p99={p['p99']:>6}us  "
                  f"max={p['max']:>6}us  n={p['n']}")
        p = s["ws_skew_us"]
        if p["n"]:
            print(f"  WS skew (recv-sent)    p50={p['p50']:>5}us  "
                  f"p90={p['p90']:>6}us  p99={p['p99']:>6}us  n={p['n']}")
        p = s["ws_gap_us"]
        if p["n"]:
            print(f"  WS inter-arrival       p50={p['p50']:>5}us  "
                  f"p90={p['p90']:>6}us  p99={p['p99']:>6}us  n={p['n']}")
        p = s["reveal_to_trade_us"]
        if p["n"]:
            print(f"  reveal->first-trade    p50={p['p50']:>5}us  "
                  f"p90={p['p90']:>6}us  p99={p['p99']:>6}us  n={p['n']}")
        p = s["reveal_to_book_us"]
        if p["n"]:
            print(f"  reveal->first-book     p50={p['p50']:>5}us  "
                  f"p90={p['p90']:>6}us  p99={p['p99']:>6}us  n={p['n']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pure-observation network/timing diagnostic. "
                    "Run INSTEAD of run_combined10.py for one game window "
                    "(single private WS per API key)."
    )
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL output path. Default: "
                             "day1/logs/diag_<YYYYMMDD_HHMMSS>.jsonl")
    parser.add_argument("--probe-interval", type=float, default=1.0,
                        help="Seconds between REST RTT probes (default 1.0)")
    parser.add_argument("--probe-orders", action="store_true",
                        help="Also send buy IOC at price=1 to measure "
                             "write-path RTT. Won't cross, IOC kills residual.")
    parser.add_argument("--probe-orders-interval", type=float, default=5.0,
                        help="Seconds between order probes (default 5.0)")
    parser.add_argument("--duration", type=float, default=0,
                        help="Auto-exit after N seconds (0 = run until 'q')")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/diag_{ts}.jsonl"

    c = GameClient(URL, API_KEY)

    # ---- Pre-flight REST: hit each endpoint once, measure RTT, print --------
    print(f"[CONNECT] URL={URL}")
    preflight: dict[str, dict] = {}
    for name, fn in (
        ("game_state", c.game_state),
        ("positions", c.positions),
        ("book", lambda: c.book(args.symbol, depth=5)),
        ("instruments", c.instruments),
    ):
        t0 = time.perf_counter_ns()
        try:
            r = fn()
            rtt_us = int((time.perf_counter_ns() - t0) / 1000)
            preflight[name] = {"ok": True, "rtt_us": rtt_us}
            preview = ""
            if name == "game_state":
                preview = (f"phase={r.get('phase')} "
                           f"reveals={len(r.get('reveals') or [])} "
                           f"reveal_interval={r.get('reveal_interval')} "
                           f"duration={r.get('duration')}")
            elif name == "book":
                bids = (r.get("bids") or [{}])[:1]
                asks = (r.get("asks") or [{}])[:1]
                preview = f"top_bid={bids} top_ask={asks}"
            elif name == "positions":
                preview = f"{r.get('positions')}"
            elif name == "instruments":
                preview = (f"keys={list(r.keys())}" if isinstance(r, dict)
                           else str(r)[:80])
            print(f"[REST] {name:<12s} OK   rtt={rtt_us:>6d}us   {preview}")
        except Exception as e:
            rtt_us = int((time.perf_counter_ns() - t0) / 1000)
            preflight[name] = {"ok": False, "rtt_us": rtt_us, "err": str(e)}
            print(f"[REST] {name:<12s} FAIL rtt={rtt_us:>6d}us   {e}")

    if not preflight.get("game_state", {}).get("ok"):
        print(f"\n[FATAL] cannot reach exchange at {URL}. exiting.")
        sys.exit(1)
    if not preflight.get("positions", {}).get("ok"):
        print(f"\n[FATAL] API key rejected (positions returned error). exiting.")
        sys.exit(1)

    ws_url = (URL.replace("http://", "ws://").replace("https://", "wss://")
              + "/ws/private")
    print(f"[WS] will connect to {ws_url}?api_key=<redacted>")

    diag = Diagnostic(c, args.symbol, args.log,
                      probe_interval=args.probe_interval,
                      probe_orders=args.probe_orders,
                      probe_orders_interval=args.probe_orders_interval)
    # Record preflight for offline analysis
    for name, p in preflight.items():
        diag._rest_rtt_us["preflight"].append(p["rtt_us"])
    diag.log("session_start",
             url=URL, symbol=args.symbol,
             probe_interval=args.probe_interval,
             probe_orders=args.probe_orders,
             probe_orders_interval=args.probe_orders_interval,
             preflight=preflight)

    # Wire WS handlers (one closure per type)
    c.on_reveal = diag.make_handler("reveal")
    c.on_fill = diag.make_handler("fill")
    c.on_trade = diag.make_handler("trade")
    c.on_book = diag.make_handler("book")
    c.on_game_state = diag.make_handler("game_state")
    c.on_settlement = diag.make_handler("settlement")
    c.on_ack = diag.make_handler("order_ack")
    c.on_cancel_ack = diag.make_handler("cancel_ack")
    c.on_modify_ack = diag.make_handler("modify_ack")
    c.on_reject = diag.make_handler("reject")
    c.on_message = diag.on_message  # catch-all for untyped frames

    c.start()
    diag.start()

    print(f"\n[READY] WS connecting; REST probe @ {args.probe_interval}s; "
          f"order_probe={'ON @ %.1fs' % args.probe_orders_interval if args.probe_orders else 'off'}")
    print(f"        log = {args.log}")
    if args.duration:
        print(f"        will auto-exit after {args.duration:.0f}s")
    print(f"        commands: s=stats  q=quit  (Ctrl-C to flush + exit)\n")

    interactive = sys.stdin.isatty()

    try:
        if not interactive:
            # No tty: just sleep until duration / Ctrl-C / kill.
            print("[NON-INTERACTIVE] no tty on stdin; will not accept commands")
            if args.duration:
                time.sleep(args.duration)
                print(f"\n[TIMEOUT] reached --duration={args.duration:.0f}s")
            else:
                while True:
                    time.sleep(60)
        else:
            t_deadline = (time.monotonic() + args.duration
                          if args.duration > 0 else None)
            while True:
                if t_deadline and time.monotonic() >= t_deadline:
                    print(f"\n[TIMEOUT] reached --duration={args.duration:.0f}s")
                    break
                try:
                    cmd = input().strip().lower()
                except EOFError:
                    while True:
                        if t_deadline and time.monotonic() >= t_deadline:
                            print(f"\n[TIMEOUT] reached "
                                  f"--duration={args.duration:.0f}s")
                            return
                        time.sleep(1)
                if cmd == "s":
                    diag.print_stats()
                elif cmd in ("q", "quit", "exit"):
                    break
                elif cmd == "":
                    continue
                else:
                    print(f"  unknown command {cmd!r}; try s / q")
    except KeyboardInterrupt:
        print("\n[INT] flushing + exiting")
    finally:
        diag.stop()
        diag.log("session_end", stats=diag.stats())
        print("\nFinal stats:")
        diag.print_stats()
        try:
            diag.log_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
