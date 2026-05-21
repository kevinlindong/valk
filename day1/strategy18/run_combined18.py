"""Run Strategy v18 + Probe over a single WS connection.

v18 = SEAN-style dual-symbol MM with full precompute hot path,
0.8s pre-reveal park, post-reveal IOC sweep, penny/dime BBO, and
inter-reveal stale-quote sweep. See `strategy18.py` for the design.

Run:
    python day1/strategy18/run_combined18.py
    python day1/strategy18/run_combined18.py --log /tmp/v18.jsonl
    python day1/strategy18/run_combined18.py --probe-symbol B

Interactive commands:
    s    status (pos A/B, fair A/B, precompute size, next-reveal ETA)
    p    dump the current precompute table
    c    per-counterparty fill stats (from probe)
    b    top-of-book snapshot for A and B
    r    request a precompute rebuild
    f    flatten now (cancel resting + IOC to flat)
    q    quit (auto-flattens). Ctrl-C also flattens.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DAY1_DIR = os.path.dirname(_THIS_DIR)
_REPO = os.path.dirname(_DAY1_DIR)
for _p in (_REPO, _DAY1_DIR, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk.client import GameClient  # noqa: E402
from strategy18.strategy18 import (  # noqa: E402
    URL, API_KEY, N_PRIOR_SIM, Posterior, Strategy, Config,
)
from strategy18.probe18 import make_probe18, default_log_path  # noqa: E402


def _fmt_book(book):
    if not book:
        return "[]"
    bb = (book.get("bids") or [])[:1]
    ba = (book.get("asks") or [])[:1]
    bb_s = f"{bb[0]['price']}x{bb[0]['qty']}" if bb else "-"
    ba_s = f"{ba[0]['price']}x{ba[0]['qty']}" if ba else "-"
    return f"bid={bb_s} ask={ba_s}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v18 + Probe together over one WS connection")
    parser.add_argument("--probe-symbol", default="A",
                        help="Symbol the probe focuses on for its periodic "
                             "book snapshot (strategy trades A and B "
                             "regardless)")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v18_<YYYYMMDD_HHMMSS>.jsonl")
    args = parser.parse_args()

    if args.log is None:
        args.log = default_log_path()

    c = GameClient(URL, API_KEY)

    try:
        gs = c.game_state()
        instr = gs.get("instruments") or {}
        ia = instr.get("A", {})
        ib = instr.get("B", {})
        print(f"[REST] OK  phase={gs.get('phase')}  "
              f"reveals={len(gs.get('reveals') or [])}  "
              f"duration={gs.get('duration')}  "
              f"reveal_interval={gs.get('reveal_interval')}  "
              f"A.pl={ia.get('position_limit')}/tick={ia.get('tick_size')}  "
              f"B.pl={ib.get('position_limit')}/tick={ib.get('tick_size')}")
    except Exception as e:
        print(f"[REST] FAILED: {e}\n  -> URL ({URL}) or network unreachable.")
        sys.exit(1)
    try:
        p0 = c.positions()
        print(f"[AUTH] OK  positions={p0}")
    except Exception as e:
        print(f"[AUTH] FAILED: {e}\n  -> API key rejected.")
        sys.exit(1)
    ws_url = URL.replace("http://", "ws://").replace(
        "https://", "wss://") + "/ws/private"
    print(f"[WS] will connect to {ws_url}?api_key=<redacted>")

    print(f"\nBuilding prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, Config())
    probe = make_probe18(c, symbol=args.probe_symbol, log_path=args.log)

    # ---- WS handler fan-out: probe logs, strategy trades ----
    def on_reveal(msg: dict) -> None:
        # Probe first → snapshots the book pre-cancel for offline analysis.
        # Strategy second → fires IOC sweep + applies precomputed quotes.
        probe.on_reveal(msg)
        strat.on_reveal(msg["value"])

    def on_fill(msg: dict) -> None:
        strat.on_fill_event(msg)
        probe.on_fill(msg)

    def on_game_state(msg: dict) -> None:
        probe.on_game_state(msg)
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_trade(msg: dict) -> None:
        probe.on_trade(msg)
        strat.on_trade(msg)

    def on_book(msg: dict) -> None:
        # Strategy first: book event drives inter-reveal stale sweep,
        # which must fire before any probe-side I/O latency.
        strat.on_book_event(msg)
        probe.on_book(msg)

    def on_message(msg: dict) -> None:
        probe.on_message(msg)
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    def on_reject(msg: dict) -> None:
        # Strategy first: records lockout cutoff that gates subsequent sends.
        strat.on_reject_event(msg)
        probe.on_reject(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = on_trade
    c.on_book = on_book
    c.on_game_state = on_game_state
    c.on_settlement = probe.on_settlement
    c.on_ack = probe.on_ack
    c.on_cancel_ack = probe.on_cancel_ack
    c.on_modify_ack = probe.on_modify_ack
    c.on_reject = on_reject
    c.on_message = on_message

    c.start()
    probe.start_heartbeat()

    # Kick a fresh MM cycle if the game is already running so we don't wait
    # one full mm_refresh_sec tick before posting our first quote.
    if c.game_state().get("phase") == "running":
        try:
            strat._refresh_mm_quotes()
        except Exception as e:
            print(f"[v18 INIT-MM] {e!r}")

    print(f"\nCombined v18 runner started.")
    print(f"  Strategy v18: dual-symbol MM + precompute hot path + "
          f"0.8s park + post-reveal sweep + penny/dime + inter-sweep")
    print(f"  Probe:        JSONL log -> {args.log}  "
          f"(probe_symbol={args.probe_symbol})")
    print("Commands: 's'=status  'p'=precompute  'c'=probe cp  "
          "'b'=books  'r'=rebuild  'f'=flatten  'q'=quit.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fa, sa = strat.fair_a()
                fb, sb = strat.fair_b()
                now = time.time()
                nxt = strat._next_reveal_at()
                nxt_in = f"{nxt - now:.2f}s" if nxt is not None else "-"
                with strat._precompute_lock:
                    n_scen = len(strat._precompute)
                    pcycle = strat._precompute_for_cycle
                    pruns = strat._precompute_run_count
                    pus = strat._last_precompute_us
                print(f"  phase={strat.phase}  k={strat._reveal_count}/"
                      f"{strat.n_total}  rs={strat._running_sum:.0f}  "
                      f"next_reveal_in={nxt_in}")
                print(f"  A: pos={strat.state['A'].position:+d}/"
                      f"{strat.state['A'].position_limit}  "
                      f"fair={fa:.1f}+/-{sa:.1f}")
                print(f"  B: pos={strat.state['B'].position:+d}/"
                      f"{strat.state['B'].position_limit}  "
                      f"fair={fb:.2f}+/-{sb:.2f}")
                print(f"  precompute: {n_scen} scenarios @ cycle "
                      f"{pcycle}  runs={pruns}  last={pus/1000:.1f}ms")
                ra = strat.state["A"].resting
                rb = strat.state["B"].resting
                ifa = strat.state["A"].in_flight
                ifb = strat.state["B"].in_flight
                print(f"  reqs: modify={strat._modify_count}  "
                      f"post={strat._post_count}  "
                      f"reveal_ioc={strat._reveal_ioc_count}  "
                      f"inter_sweep={strat._inter_sweep_count}  "
                      f"cross_arb={strat._cross_arb_count}  "
                      f"endgame={strat._endgame_count}")
                print(f"  endgame_active={strat._endgame_active}  "
                      f"n_remaining={strat._n_remaining()}")
                with strat._live_lock:
                    la = strat._live_thresholds.get("A")
                    lb = strat._live_thresholds.get("B")
                    flive_runs = strat._fast_precompute_run_count
                    flive_us = strat._last_fast_precompute_us
                if la is not None and lb is not None:
                    flat = la.in_flatten_window
                    print(f"  live: A lift/hit=[{la.lift_to},{la.hit_to}]"
                          f"  flat-relax=[{la.lift_to_flat},{la.hit_to_flat}]"
                          f"  fair={la.fair:.1f}")
                    print(f"  live: B lift/hit=[{lb.lift_to},{lb.hit_to}]"
                          f"  flat-relax=[{lb.lift_to_flat},{lb.hit_to_flat}]"
                          f"  fair={lb.fair:.2f}")
                    print(f"  fast_precomp: runs={flive_runs} "
                          f"last={flive_us:.0f}us flatten_window={flat}")
                print(f"  resting A: bid={len(ra['bid'])} ask={len(ra['ask'])}"
                      f"  in_flight={ifa['bid']}/{ifa['ask']}")
                print(f"  resting B: bid={len(rb['bid'])} ask={len(rb['ask'])}"
                      f"  in_flight={ifb['bid']}/{ifb['ask']}")
                print(f"  lockout: cnt={strat._lockout_count}  "
                      f"deferred_sends={strat._sends_deferred}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precompute.items())
                    pcycle = strat._precompute_for_cycle
                print(f"  precompute table (cycle={pcycle}, n={len(items)}):")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.prob:.3f}  "
                          f"A=[{sc.bid_a},{sc.ask_a}]@{sc.fair_a:.1f}+/-"
                          f"{sc.sigma_a:.1f}  "
                          f"sweep=[{sc.lift_to_a},{sc.hit_to_a}]  "
                          f"B=[{sc.bid_b},{sc.ask_b}]@{sc.fair_b:.2f}+/-"
                          f"{sc.sigma_b:.2f}  "
                          f"sweep=[{sc.lift_to_b},{sc.hit_to_b}]")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "b":
                print(f"  A: {_fmt_book(strat.state['A'].book)}")
                print(f"  B: {_fmt_book(strat.state['B'].book)}")
            elif cmd == "r":
                strat._precompute_request.set()
                print("  precompute rebuild requested")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s/p/c/b/r/f/q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Stopping probe heartbeat; flattening; closing log...")
        probe.stop()
        try:
            strat.stop()
        except Exception:
            pass
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten error: {e}")
        try:
            probe.log_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
