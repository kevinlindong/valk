"""Run Strategy v9 (live trading) and Probe (passive market-data logger)
together over a SINGLE WebSocket connection.

  - Strategy v9: v8 base + reveal-zone IOC block + K=1 reveal-math guard +
                 stale-fair drift cancel + tighter inventory kill +
                 opening-round quote clamp + scheduler diagnostics.
                 See strategy9.py docstring for the audit-grounded
                 rationale.
  - Probe:       logs every WS event to a per-run JSONL file.

The probe's auto-flatten safety is disabled (strategy owns position).

Run:
    python day1/run_combined9.py
    python day1/run_combined9.py --log day1/sess.jsonl
    python day1/run_combined9.py --symbol A
    python day1/run_combined9.py --informed-trader ORACLE_NAME

The --informed-trader flag is passed through to the strategy (same
semantics as v7/v8).

Interactive:
    s   strategy status (v9: danger-zone + drift anchor surfaced)
    c   per-counterparty fill stats
    i   informed-trader state
    m   market-tape state
    f   flatten now
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
from strategy9 import URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM  # noqa: E402
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v9 + Probe together over one WS connection"
    )
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v9_<YYYYMMDD_HHMMSS>.jsonl. "
                             "Always APPEND mode -- existing data preserved. "
                             "The logs/ directory is created automatically.")
    parser.add_argument("--informed-trader", default=None,
                        dest="informed_trader",
                        help="Counterparty id of the informed trader. "
                             "Pass an empty string to disable.")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v9_{ts}.jsonl"

    c = GameClient(URL, API_KEY)

    # ---- startup self-check ----
    try:
        gs = c.game_state()
        instr_a = (gs.get("instruments") or {}).get("A", {})
        print(f"[REST] OK  phase={gs.get('phase')}  "
              f"reveals={len(gs.get('reveals') or [])}  "
              f"duration={gs.get('duration')}  "
              f"reveal_interval={gs.get('reveal_interval')}  "
              f"A.position_limit={instr_a.get('position_limit')}  "
              f"A.tick={instr_a.get('tick_size')}  "
              f"A.multiplier={instr_a.get('multiplier')}")
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
    print(f"[v9] reveal_interval={strat.reveal_interval}s  "
          f"position_limit={strat.position_limit}  "
          f"danger_zone={strat.reveal_danger_zone_sec}s  "
          f"reveal_math_min_k={strat.reveal_math_min_k} "
          f"(k1_widening={strat.reveal_math_k1_widening})  "
          f"drift_cancel_ticks={strat.drift_cancel_ticks}  "
          f"inv_hard_kill_frac={strat.inv_hard_kill_frac}  "
          f"opening_clamp_ticks={strat.opening_clamp_ticks}  "
          f"prior_E_settle={strat._prior_E_settle:.1f}  "
          f"sean_window={strat.sean_window_sec}s  "
          f"sean_qty={strat.sean_window_qty}  "
          f"snipe_min_edge={strat.snipe_min_edge}  "
          f"snipe_max_disagreement={strat.snipe_max_disagreement}  "
          f"max_snipes_per_round={strat.max_snipes_per_round}")

    probe = Probe(c, args.symbol, args.log,
                  probe_mode="passive", probe_interval=999999.0)
    # Disable probe's safety auto-flatten (designed for MAX_PROBE_POS=5).
    probe._safety_check = lambda: None

    # ---- chained WS handlers ----
    def on_reveal(msg: dict) -> None:
        probe.on_reveal(msg)
        strat.on_reveal(msg["value"])

    def on_fill(msg: dict) -> None:
        probe.on_fill(msg)
        strat.on_fill_event(msg)

    def on_game_state(msg: dict) -> None:
        probe.on_game_state(msg)
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_trade(msg: dict) -> None:
        probe.on_trade(msg)
        strat.on_trade(msg)

    def on_message(msg: dict) -> None:
        probe.on_message(msg)
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

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
    print(f"  Strategy v9: live trading (symbol={args.symbol})")
    print(f"  Probe:       JSONL log -> {args.log}")
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
                fair_eff_informed = strat._effective_fair(fair)
                fair_eff = strat._vwap_clamped_fair(fair_eff_informed)
                rm = strat._reveal_math_fair()
                rm_str = f"{rm:.1f}" if rm is not None else "-"
                bid_px, ask_px, _, _ = strat.desired_quotes()
                k = len(strat.posterior.reveals)
                final = (k >= strat.n_total)
                in_window = strat._in_sean_window()
                in_zone = strat._in_reveal_danger_zone()
                nxt = strat._next_reveal_at()
                nxt_in_str = (f"{nxt - time.time():.2f}s"
                              if nxt is not None else "-")
                market_mid = strat._market_signal()
                vwap = strat._trade_vwap(30.0)
                mid_str = f"{market_mid:.1f}" if market_mid is not None else "-"
                vwap_str = f"{vwap:.1f}" if vwap is not None else "-"
                marker = " [FINAL: sweep mode]" if final else ""
                anchor_str = (f"{strat._last_quote_fair_eff:.1f}"
                              if strat._last_quote_fair_eff is not None else "-")
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}  "
                      f"fair_eff={fair_eff:.1f}  reveal_math={rm_str}  "
                      f"bid={bid_px} ask={ask_px}  k={k}/{strat.n_total}"
                      f"{marker}\n"
                      f"  v9: danger_zone={in_zone}  sean_window={in_window}  "
                      f"next_reveal_in={nxt_in_str}  "
                      f"drift_anchor={anchor_str}  "
                      f"snipes_used={strat._snipe_count_this_round}/"
                      f"{strat.max_snipes_per_round}\n"
                      f"  market: market_signal={mid_str}  "
                      f"vwap30s={vwap_str}  "
                      f"trades_buffered={len(strat._recent_trades)}")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "i":
                tid = strat.informed_trader_id
                if not tid:
                    print("  informed-trader feature DISABLED")
                else:
                    now = time.time()
                    bid_cool = strat._informed_cooloff_until.get("bid", 0.0)
                    ask_cool = strat._informed_cooloff_until.get("ask", 0.0)
                    bid_left = max(0.0, bid_cool - now)
                    ask_left = max(0.0, ask_cool - now)
                    lo = strat._informed_fair_lo
                    hi = strat._informed_fair_hi
                    lo_str = f"{lo:.1f}" if lo is not None else "-"
                    hi_str = f"{hi:.1f}" if hi is not None else "-"
                    print(f"  informed trader id   : {tid!r}")
                    print(f"  fills this round     : "
                          f"{strat._informed_fill_count} "
                          f"({strat._informed_total_qty} lots)")
                    print(f"  truth bound          : [{lo_str}, {hi_str}]")
                    print(f"  cool-off remaining   : "
                          f"bid={bid_left:.1f}s  ask={ask_left:.1f}s")
            elif cmd == "m":
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
