"""Run Strategy v17 + Probe together over one WS connection.

v17 vs v16: dual-symbol passive market making (A AND B) with
reveal-time precompute hot path and cross-market arbitrage.
See strategy17.py for the full design.

Run:
    python day1/run_combined17.py
    python day1/run_combined17.py --log day1/sess.jsonl
    python day1/run_combined17.py --probe-symbol A

Interactive commands:
    s    status (pos A/B, fair A/B, precompute size, next-reveal ETA)
    p    dump current precompute table (per-X scenarios for both symbols)
    c    per-counterparty fill stats (from probe)
    a    arb state: book mids, implied X from each market, edge
    b    book snapshot for A and B (top of book)
    f    flatten now (cancel resting + IOC both positions flat)
    q    quit (auto-flattens). Ctrl-C also flattens.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
for _p in (_PARENT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk.client import GameClient  # noqa: E402
from strategy17 import (  # noqa: E402
    URL, API_KEY, N_PRIOR_SIM, Posterior, Strategy, Config)
from probe import Probe  # noqa: E402


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
        description="Run Strategy v17 + Probe together over one WS connection")
    parser.add_argument("--probe-symbol", default="A",
                        help="Symbol the probe focuses on for its log "
                             "(strategy trades A and B regardless)")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v17_<YYYYMMDD_HHMMSS>.jsonl")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v17_{ts}.jsonl"

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

    probe = Probe(c, args.probe_symbol, args.log,
                  probe_mode="passive", probe_interval=999999.0)
    probe._safety_check = lambda: None

    def on_reveal(msg: dict) -> None:
        strat.on_reveal(msg["value"])
        probe.on_reveal(msg)

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
        strat.on_book_event(msg)
        probe.on_book(msg)

    def on_message(msg: dict) -> None:
        probe.on_message(msg)
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = on_trade
    c.on_book = on_book
    c.on_game_state = on_game_state
    c.on_settlement = probe.on_settlement
    c.on_ack = probe.on_ack
    c.on_cancel_ack = probe.on_cancel_ack
    c.on_modify_ack = probe.on_modify_ack
    def on_reject(msg: dict) -> None:
        # Strategy first: it records the server lockout cutoff and gates
        # subsequent sends. Probe logging is fire-and-forget after.
        strat.on_reject_event(msg)
        probe.on_reject(msg)
    c.on_reject = on_reject
    c.on_message = on_message

    c.start()
    probe.start_heartbeat()

    # Kick a fresh MM cycle if the game is already running
    if c.game_state().get("phase") == "running":
        try:
            strat._refresh_mm_quotes()
        except Exception as e:
            print(f"[v17 INIT-MM] {e!r}")

    print(f"\nCombined v17 runner started.")
    print(f"  Strategy v17: dual-symbol passive MM + precompute hot path "
          f"+ cross-arb + stale sweeper + reconcile")
    print(f"  Probe:        JSONL log -> {args.log}  "
          f"(probe_symbol={args.probe_symbol})")
    print("Commands: 's'=status  'p'=precompute  'c'=probe cp  "
          "'a'=arb  'b'=books  'f'=flatten  'q'=quit.\n")

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
                # Latency-exploit + safety visibility.
                ifa = strat.state["A"].in_flight
                ifb = strat.state["B"].in_flight
                ra = strat.state["A"].resting
                rb = strat.state["B"].resting
                print(f"  reqs: modify={strat._modify_count}  "
                      f"post={strat._post_count}  "
                      f"in_flight A={ifa['bid']}/{ifa['ask']} "
                      f"B={ifb['bid']}/{ifb['ask']}")
                print(f"  resting A: bid={len(ra['bid'])} ask={len(ra['ask'])}  "
                      f"B: bid={len(rb['bid'])} ask={len(rb['ask'])}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precompute.items())
                    pcycle = strat._precompute_for_cycle
                print(f"  precompute table (cycle={pcycle}, n={len(items)}):")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.prob:.3f}  "
                          f"A=[{sc.bid_a},{sc.ask_a}]@{sc.fair_a:.1f}+/-"
                          f"{sc.sigma_a:.1f}  "
                          f"B=[{sc.bid_b},{sc.ask_b}]@{sc.fair_b:.2f}+/-"
                          f"{sc.sigma_b:.2f}")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "a":
                mid_a = strat._book_mid("A")
                mid_b = strat._book_mid("B")
                n_rem = strat._n_remaining()
                if mid_a is None or mid_b is None or n_rem < 1:
                    print(f"  arb unavailable: mid_A={mid_a} mid_B={mid_b} "
                          f"n_rem={n_rem}")
                else:
                    impl_a = (mid_a - strat._running_sum) / n_rem
                    impl_b = mid_b
                    diff = impl_a - impl_b
                    trig = ("YES" if abs(diff) >= strat.cfg.arb_min_edge_ticks
                            else "no")
                    print(f"  mid_A={mid_a:.2f}  mid_B={mid_b:.2f}  "
                          f"n_rem={n_rem}  rs={strat._running_sum:.0f}")
                    print(f"  impl_X_A={impl_a:.2f}  impl_X_B={impl_b:.2f}  "
                          f"diff={diff:+.2f}t  (edge>="
                          f"{strat.cfg.arb_min_edge_ticks}t  trigger={trig})")
            elif cmd == "b":
                print(f"  A: {_fmt_book(strat.state['A'].book)}")
                print(f"  B: {_fmt_book(strat.state['B'].book)}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s/p/c/a/b/f/q")
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
