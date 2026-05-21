"""Run Strategy v12 + Probe together over one WS connection.

v12 vs v11: TCP_NODELAY adapter on REST session, runtime CPProfile,
bigger default sizes (4/6 lot vs 2/5), aggressive pennying allowed even
when biased, flow-asymmetric quoting on |flow_score| >= 0.5. See
strategy12.py docstring for full design.

Run:
    python day1/run_combined12.py
    python day1/run_combined12.py --log day1/sess.jsonl
    python day1/run_combined12.py --symbol A

Interactive commands:
    s   status (precompute + flow score + position)
    p   dump current precompute table
    c   per-counterparty fill stats (from probe)
    cp  per-counterparty profile (from strategy's CPProfile)
    m   market-tape state
    f   flatten now
    q   quit (auto-flattens). Ctrl-C also flattens.
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
from strategy12 import URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM  # noqa: E402
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v12 + Probe together over one WS connection")
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v12_<YYYYMMDD_HHMMSS>.jsonl")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v12_{ts}.jsonl"

    c = GameClient(URL, API_KEY)

    try:
        gs = c.game_state()
        instr_a = (gs.get("instruments") or {}).get("A", {})
        print(f"[REST] OK  phase={gs.get('phase')}  "
              f"reveals={len(gs.get('reveals') or [])}  "
              f"duration={gs.get('duration')}  "
              f"reveal_interval={gs.get('reveal_interval')}  "
              f"A.position_limit={instr_a.get('position_limit')}  "
              f"A.tick={instr_a.get('tick_size')}")
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

    strat = Strategy(c, post, symbol=args.symbol)
    print(f"[v12] reveal_interval={strat.reveal_interval}s  "
          f"position_limit={strat.position_limit}  "
          f"quote_qty={strat.cfg.quote_qty}/{strat.cfg.quote_qty_normal}  "
          f"penny_min_edge={strat.cfg.penny_min_edge} "
          f"penny_when_biased={strat.cfg.penny_when_biased}  "
          f"flow_thresh={strat.cfg.flow_score_threshold} "
          f"flow_bonus={strat.cfg.flow_size_bonus}lots/"
          f"{strat.cfg.flow_widen_ticks}t  "
          f"adverse_skew={strat.cfg.adverse_skew_ticks}t/"
          f"{strat.cfg.adverse_decay_sec}s")

    probe = Probe(c, args.symbol, args.log,
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
    c.on_reject = probe.on_reject
    c.on_message = on_message

    c.start()
    probe.start_heartbeat()

    if c.game_state().get("phase") == "running":
        strat.step(reconcile=True)

    print(f"\nCombined v12 runner started.")
    print(f"  Strategy v12: live trading (symbol={args.symbol})")
    print(f"  Probe:        JSONL log -> {args.log}")
    print("Commands: 's'=status  'p'=precompute  'c'=probe cp  "
          "'cp'=strat cp  'm'=market  'f'=flatten  'q'=quit.\n")

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
                in_window = strat._in_pre_reveal_window()
                nxt = strat._next_reveal_at()
                nxt_in = f"{nxt - time.time():.2f}s" if nxt is not None else "-"
                with strat._precompute_lock:
                    n_scen = len(strat._precomputed)
                    pcycle = strat._precompute_for_cycle
                now = time.time()
                bp, sp = strat.cpp.directional_pressure()
                bq = strat._current_quote_qty("bid")
                aq = strat._current_quote_qty("ask")
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}  "
                      f"bid={bid_px}x{bq} ask={ask_px}x{aq}  "
                      f"k={k}/{strat.n_total}\n"
                      f"  v12: precompute={n_scen}@cycle{pcycle} "
                      f"last_ms={strat._last_precompute_us/1000:.1f}  "
                      f"runs={strat._precompute_run_count}  "
                      f"pre_reveal_window={in_window}  next_reveal_in={nxt_in}\n"
                      f"  recent_adverse: "
                      f"bid={strat._adverse_skew('bid', now):.2f}t  "
                      f"ask={strat._adverse_skew('ask', now):.2f}t  "
                      f"flow buy={bp:.2f} sell={sp:.2f}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precomputed.items())
                    pcycle = strat._precompute_for_cycle
                print(f"  precompute table (cycle={pcycle}, n={len(items)}):")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.marginal_prob:.3f}  "
                          f"fair={sc.fair:6.1f}+/-{sc.sigma:4.1f}  "
                          f"bid={sc.bid_px} ask={sc.ask_px}  "
                          f"lift<={sc.lift_to_px} hit>={sc.hit_to_px}")
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "cp":
                snap = strat.cpp.snapshot()
                rows = sorted(snap.items(),
                              key=lambda kv: -(kv[1].qty_we_bought
                                               + kv[1].qty_we_sold))
                print(f"  strat.CPProfile ({len(snap)} counterparties):")
                for cp_id, s in rows[:30]:
                    print(f"    {cp_id:<14s} bought={s.qty_we_bought:>4d} "
                          f"sold={s.qty_we_sold:>4d}  "
                          f"taker={s.taker_count} maker={s.maker_count}  "
                          f"adv={s.adverse_ticks:+.1f}t")
            elif cmd == "m":
                book_mid = strat._market_mid_from_book()
                book_str = f"{book_mid:.2f}" if book_mid is not None else "-"
                vwap = strat._trade_vwap()
                vwap_str = f"{vwap:.2f}" if vwap is not None else "-"
                signal = strat._market_signal()
                signal_str = f"{signal:.2f}" if signal is not None else "-"
                print(f"  recent trades buffered: "
                      f"{len(strat._recent_trades)}")
                print(f"  vwap (30s window)    : {vwap_str}")
                print(f"  book mid             : {book_str}")
                print(f"  blended market signal: {signal_str}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s/p/c/cp/m/f/q")
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
