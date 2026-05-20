"""
Run Strategy v5 (live trading) and Probe (passive market-data logger) together
over a SINGLE WebSocket connection.

The exchange allows only one private WS per API key, so you can't just run
strategy5.py and probe.py side-by-side -- one would steal the other's slot.
This runner shares one client and dispatches each WS event to both.

  - Strategy v5: layers PUBLIC MARKET DATA onto v4. The book midpoint
                 lagged real price discovery in the v4 trial loss round
                 (R2 of combined_log_v4_20260519_150816.jsonl: trade
                 VWAP went 18.5 -> 8.3 while book mid stayed near 18).
                 v5 routes the public `trade` stream through a 60s ring
                 buffer and blends VWAP into the disagreement gate.
                 Also tracks per-counterparty signed net flow vs us and
                 decays the informed-strike weight when a CP is near
                 the +/-100 cap (their next fill is mechanical, not
                 informed). Carries v4 informed-trader override.
                 See strategy5.py docstring for the full rationale.
  - Probe:       logs every WS event (reveals, public trades, quote_add/
                 cancel/fill, our fills, ack/reject, undocumented types) to
                 a per-run JSONL file for offline analysis.

The probe's auto-flatten safety is disabled in this runner (it was designed
for standalone probing with MAX_PROBE_POS=5 -- the real strategy uses
POSITION_LIMIT=100 and its own flatten logic).

Run:
    python day1/run_combined5.py
    python day1/run_combined5.py --log day1/sess.jsonl
    python day1/run_combined5.py --symbol A
    python day1/run_combined5.py --informed-trader ORACLE_NAME

The --informed-trader flag overrides strategy5.INFORMED_TRADER_ID. Pass
the exact counterparty id the exchange uses in fill.counterparty. Set
to an empty string ("") or omit to disable the override.

Interactive:
    s   strategy status (phase, position, fair, quotes, reveal count, strikes)
    c   per-counterparty fill stats
    i   informed-trader state (id, bounds, last fill, cool-offs)
    m   market-tape state (trade VWAP, aggressor imbalance, top CP flow)
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
from strategy5 import URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM  # noqa: E402
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v5 + Probe together over one WS connection"
    )
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v5_<YYYYMMDD_HHMMSS>.jsonl. "
                             "Always APPEND mode -- existing data preserved. "
                             "The logs/ directory is created automatically.")
    parser.add_argument("--informed-trader", default=None,
                        dest="informed_trader",
                        help="Counterparty id of the informed trader who "
                             "knows the underlying (a, w). Overrides "
                             "strategy5.INFORMED_TRADER_ID. Pass an empty "
                             "string to disable.")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v5_{ts}.jsonl"

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

    strat = Strategy(c, post, symbol=args.symbol,
                     informed_trader_id=args.informed_trader)
    if strat.informed_trader_id:
        print(f"[INFORMED] tracking trader id = {strat.informed_trader_id!r}  "
              f"cool-off = {strat.informed_cooloff_sec:.0f}s")
    else:
        print("[INFORMED] disabled (no --informed-trader and "
              "INFORMED_TRADER_ID empty)")

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

    def on_trade(msg: dict) -> None:
        probe.on_trade(msg)             # log public trade
        strat.on_trade(msg)              # v5: feed the trade tape

    def on_message(msg: dict) -> None:
        probe.on_message(msg)           # log raw unknown types
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)   # throttled snipe on book deltas

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = on_trade
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
    print("Commands: 's'=status  'c'=counterparty stats  'i'=informed-trader  "
          "'m'=market-tape  'f'=flatten  'q'=quit. Ctrl-C also flattens.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                fair_eff = strat._effective_fair(fair)
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
                # v4: also surface strike counters, cadence, and informed
                # truth bounds if any.
                extra_bid, extra_ask = strat._informed_extra_edge()
                interval = strat._maker_reprice_interval()
                interval_str = f"{interval:.1f}s" if interval != float("inf") else "OFF"
                clamp_note = ""
                if fair_eff != fair:
                    clamp_note = f"  [clamped {fair:.1f}->{fair_eff:.1f}]"
                # v5: surface the blended market signal + tape stats.
                market_mid = strat._market_signal()
                book_mid = strat._market_mid_from_book()
                vwap = strat._trade_vwap(30.0)
                imb = strat._aggressor_imbalance(30.0)
                mid_str = f"{market_mid:.1f}" if market_mid is not None else "-"
                book_str = f"{book_mid:.1f}" if book_mid is not None else "-"
                vwap_str = f"{vwap:.1f}" if vwap is not None else "-"
                imb_str = f"{imb:+.2f}" if imb is not None else "-"
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}{clamp_note}  "
                      f"bid={bid_px} ask={ask_px}  "
                      f"k={k}/{strat.n_total}  snipe_edge_req={edge_req:.2f}"
                      f"{marker}\n"
                      f"  v4: maker_reprice_every={interval_str}  "
                      f"strikes(bid/ask)={strat._informed_strikes_bid:.2f}/"
                      f"{strat._informed_strikes_ask:.2f}  "
                      f"extra_edge(bid/ask)={extra_bid:.2f}/{extra_ask:.2f}\n"
                      f"  v5: market_signal={mid_str}  "
                      f"book_mid={book_str}  vwap30s={vwap_str}  "
                      f"imb30s={imb_str}  trades={len(strat._recent_trades)}")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "i":
                tid = strat.informed_trader_id
                if not tid:
                    print("  informed-trader feature DISABLED "
                          "(no --informed-trader; INFORMED_TRADER_ID empty)")
                else:
                    now = time.time()
                    bid_cool = strat._informed_cooloff_until.get("bid", 0.0)
                    ask_cool = strat._informed_cooloff_until.get("ask", 0.0)
                    bid_left = max(0.0, bid_cool - now)
                    ask_left = max(0.0, ask_cool - now)
                    lo = strat._informed_fair_lo
                    hi = strat._informed_fair_hi
                    last_t = strat._informed_last_t
                    age = (now - last_t) if last_t > 0 else None
                    age_str = f"{age:.1f}s ago" if age is not None else "never"
                    lo_str = f"{lo:.1f}" if lo is not None else "-"
                    hi_str = f"{hi:.1f}" if hi is not None else "-"
                    print(f"  informed trader id   : {tid!r}")
                    print(f"  fills this round     : "
                          f"{strat._informed_fill_count} "
                          f"({strat._informed_total_qty} lots)")
                    print(f"  last fill            : "
                          f"{age_str}  their_side="
                          f"{strat._informed_last_their_side}  "
                          f"px={strat._informed_last_px}")
                    print(f"  truth bound          : [{lo_str}, {hi_str}]  "
                          f"(>=lo when they bought, <=hi when they sold)")
                    print(f"  cool-off remaining   : "
                          f"bid={bid_left:.1f}s  ask={ask_left:.1f}s")
            elif cmd == "m":
                # v5: market-tape state. Show VWAP/imbalance across the
                # 10s/30s/60s windows + per-CP net flow leaders (close
                # to the +/-100 cap = forced-flatten regime).
                book_mid = strat._market_mid_from_book()
                book_str = f"{book_mid:.2f}" if book_mid is not None else "-"
                print(f"  recent trades buffered: "
                      f"{len(strat._recent_trades)}")
                for window in (10.0, 30.0, 60.0):
                    vwap = strat._trade_vwap(window)
                    imb = strat._aggressor_imbalance(window)
                    vwap_str = f"{vwap:.2f}" if vwap is not None else "-"
                    imb_str = f"{imb:+.2f}" if imb is not None else "-"
                    print(f"    {window:>4.0f}s window  "
                          f"vwap={vwap_str:>6s}  imb={imb_str:>6s}")
                print(f"  book mid             : {book_str}")
                signal = strat._market_signal()
                signal_str = f"{signal:.2f}" if signal is not None else "-"
                print(f"  blended market signal: {signal_str}")
                # Per-CP net flow (top 8 by |flow|).
                flows = sorted(strat._cp_net_flow.items(),
                               key=lambda kv: -abs(kv[1]))[:8]
                if not flows:
                    print("  per-CP net flow      : (none yet)")
                else:
                    print(f"  per-CP net flow (cap_threshold="
                          f"{strat.cp_cap_threshold})")
                    for cp, flow in flows:
                        marker = " <- near cap" if (
                            abs(flow) >= strat.cp_cap_threshold) else ""
                        print(f"    {cp:<12s} {flow:+5d}{marker}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s / c / i / m / f / q")
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
