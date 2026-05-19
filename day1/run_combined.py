"""
Run Strategy (live trading) and Probe (passive market-data logger) together
over a SINGLE WebSocket connection.

The exchange allows only one private WS per API key, so you can't just run
strategy.py and probe.py side-by-side -- one would steal the other's slot.
This runner shares one client and dispatches each WS event to both.

  - Strategy: makes trades using the Bayesian posterior. Market-makes
              with quote_qty=5 quotes at min_edge=1.0 (matching the
              current 2-tick median market spread). Snipes mispriced quotes
              via on_quote_event (throttled to 100ms). When the final reveal
              arrives, cancels all passive quotes and sweeps the book of any
              level mispriced by more than taker_fee -- "take a position
              that is profitable immediately" rule.
  - Probe:    logs every WS event (reveals, public trades, quote_add/cancel/
              fill, our fills, ack/reject, undocumented types) to a per-run
              JSONL file for offline analysis.

The probe's auto-flatten safety is disabled in this runner (it was designed
for standalone probing with MAX_PROBE_POS=5 -- the real strategy uses
POSITION_LIMIT=100 and its own flatten logic).

Run:
    python day1/run_combined.py
    python day1/run_combined.py --log day1/sess.jsonl
    python day1/run_combined.py --symbol A

Interactive:
    s   strategy status (phase, position, fair, quotes, reveal count)
    c   per-counterparty fill stats
    f   flatten now (cancel-all + market-flatten)
    q   quit (auto-flattens). Ctrl-C also flattens.

After a session, analyze the log:
    python day1/analyze.py
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
from strategy import URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM  # noqa: E402
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy + Probe together over one WS connection"
    )
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_<YYYYMMDD_HHMMSS>.jsonl. "
                             "Always APPEND mode -- existing data preserved. "
                             "The logs/ directory is created automatically.")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_{ts}.jsonl"

    c = GameClient(URL, API_KEY)

    # ---- startup self-check (so connection issues are obvious) ----
    try:
        gs = c.game_state()
        print(f"[REST] OK  phase={gs.get('phase')}  "
              f"reveals={len(gs.get('reveals') or [])}  "
              f"duration={gs.get('duration')}  "
              f"reveal_interval={gs.get('reveal_interval')}")
    except Exception as e:
        print(f"[REST] FAILED: {e}\n  -> URL ({URL}) or network unreachable.")
        sys.exit(1)
    try:
        p0 = c.positions()
        print(f"[AUTH] OK  positions={p0}")
    except Exception as e:
        print(f"[AUTH] FAILED: {e}\n  -> API key rejected. "
              f"Check strategy.py URL/API_KEY constants.")
        sys.exit(1)
    ws_url = URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/private"
    print(f"[WS] will connect to {ws_url}?api_key=<redacted>")

    # ---- build the two collaborators ----
    print(f"\nBuilding prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol=args.symbol)

    # Probe in passive mode (logger only). probe_interval is irrelevant since
    # probe_mode='passive' skips the probe thread.
    probe = Probe(c, args.symbol, args.log,
                  probe_mode="passive", probe_interval=999999.0)

    # The probe's _safety_check would auto-flatten at |pos|>5. Strategy uses
    # POSITION_LIMIT=100 and its own flatten path -- disable probe's safety.
    probe._safety_check = lambda: None

    # ---- chained WS handlers: each event reaches BOTH probe and strategy ----
    def on_reveal(msg: dict) -> None:
        probe.on_reveal(msg)            # log + print REVEAL + snapshot book
        strat.on_reveal(msg["value"])   # posterior update + strategy step

    def on_fill(msg: dict) -> None:
        probe.on_fill(msg)              # log + counterparty + print FILL
        strat.on_fill_event(msg)        # adjust resting + step (reconcile)

    def on_game_state(msg: dict) -> None:
        probe.on_game_state(msg)        # log + print PHASE transitions
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_message(msg: dict) -> None:
        probe.on_message(msg)           # log raw unknown types
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)   # throttled snipe on book deltas

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = probe.on_trade
    c.on_book = probe.on_book
    c.on_game_state = on_game_state
    c.on_settlement = probe.on_settlement
    c.on_ack = probe.on_ack
    c.on_cancel_ack = probe.on_cancel_ack
    c.on_modify_ack = probe.on_modify_ack
    c.on_reject = probe.on_reject
    c.on_message = on_message

    c.start()
    probe.start_heartbeat()

    if c.game_state().get("phase") == "running":
        strat.step(reconcile=True)

    print(f"\nCombined runner started.")
    print(f"  Strategy: live trading (symbol={args.symbol})")
    print(f"  Probe:    JSONL log -> {args.log}")
    print("Commands: 's'=status  'c'=counterparty stats  'f'=flatten  "
          "'q'=quit. Ctrl-C also flattens.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                bid_px, ask_px, _, _ = strat.desired_quotes()
                k = len(strat.posterior.reveals)
                final = (k >= strat.n_total)
                # Show the edge_required maybe_snipe is currently using.
                if final:
                    edge_req = strat.taker_fee
                else:
                    edge_req = strat.taker_fee + max(strat.snipe_min_edge,
                                                     strat.snipe_buffer_sigma * sigma)
                marker = " [FINAL: sweep mode]" if final else ""
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}  "
                      f"bid={bid_px} ask={ask_px}  "
                      f"k={k}/{strat.n_total}  snipe_edge_req={edge_req:.2f}"
                      f"{marker}")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s / c / f / q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Stopping probe heartbeat; flattening; closing log...")
        probe.stop()
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
