"""Run Strategy v15 + Probe together over one WS connection.

v15 vs v14: leverages B's market as a forward signal for A. Subscribes
to B trade events (in addition to A), maintains a rolling window of B
aggressor flow + VWAP, and feeds both:
  * synthetic "B_TAPE" CP into v14's informed-bias plumbing
    (so directional position-targeting fires for free), and
  * an additive cap-limited fair_eff nudge based on B-implied X_next
    vs the per-reveal posterior mean.

Rationale: B settles at the LAST reveal of the same X_i sequence
A sums to. Even though the public tape strips counterparty (we can't
see CVALK/GVALK/IVALK by name there), their info LEAKS into B's
price and aggressor flow. See strategy15.py for the full design.

Run:
    python day1/run_combined15.py
    python day1/run_combined15.py --log day1/sess.jsonl
    python day1/run_combined15.py --symbol A

Interactive commands:
    s    status (position, bias, target, fair, B-implied)
    p    dump current precompute table
    c    per-counterparty fill stats (from probe)
    cp   per-counterparty profile (from strategy's CPProfile, tagged)
    b    informed-bias state + target position
    m    market-tape state (A side)
    x    B-tape state (flow / VWAP / nudge)
    f    flatten now
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
from strategy15 import (  # noqa: E402
    URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM, CP_WEIGHT, STALE_CPS)
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v15 + Probe together over one WS connection")
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v15_<YYYYMMDD_HHMMSS>.jsonl")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v15_{ts}.jsonl"

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
    print(f"[v15] reveal_interval={strat.reveal_interval}s  "
          f"position_limit={strat.position_limit}  "
          f"quote_qty={strat.cfg.quote_qty}/{strat.cfg.quote_qty_normal}  "
          f"informed_bias={strat.cfg.informed_bias_max_ticks}t/"
          f"{strat.cfg.informed_bias_decay_sec}s  "
          f"dir_target={strat.cfg.directional_position_target}lots@"
          f"{strat.cfg.directional_threshold_ticks}t  "
          f"step={strat.cfg.directional_step_qty} "
          f"pay<={strat.cfg.directional_max_pay_ticks}t")
    print(f"[v15] b_tape={strat.cfg.b_tape_enabled} "
          f"win={strat.cfg.b_tape_window_sec}s "
          f"flow_thr={strat.cfg.b_tape_flow_threshold}lots "
          f"w={strat.cfg.b_tape_weight}  "
          f"nudge: factor={strat.cfg.b_nudge_factor} "
          f"max={strat.cfg.b_nudge_max_ticks}t "
          f"min_vol={strat.cfg.b_nudge_min_volume}")

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

    print(f"\nCombined v15 runner started.")
    print(f"  Strategy v15: live trading (symbol={args.symbol}) + B-tape signal")
    print(f"  Probe:        JSONL log -> {args.log}")
    print("Commands: 's'=status  'p'=precompute  'c'=probe cp  "
          "'cp'=strat cp  'b'=bias+target  'm'=market  "
          "'x'=B-tape  'f'=flatten  'q'=quit.\n")

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
                now = time.time()
                nxt = strat._next_reveal_at()
                nxt_in = f"{nxt - now:.2f}s" if nxt is not None else "-"
                with strat._precompute_lock:
                    n_scen = len(strat._precomputed)
                    pcycle = strat._precompute_for_cycle
                bias = strat._informed_bias_now(now)
                tgt = strat._bias_target_position()
                fair_eff = strat._vwap_clamped_fair(fair)
                b_imp = strat._b_implied_next_value(now)
                b_str = f"{b_imp:.2f}" if b_imp is not None else "-"
                buy_q, sell_q = strat._b_aggressor_flow(now)
                b_nudge = strat._b_nudge_ticks(fair)
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}  fair_eff={fair_eff:.1f}  "
                      f"bid={bid_px} ask={ask_px}  "
                      f"k={k}/{strat.n_total}\n"
                      f"  v15: precompute={n_scen}@cycle{pcycle} "
                      f"runs={strat._precompute_run_count}  "
                      f"next_reveal_in={nxt_in}\n"
                      f"  bias={bias:+.2f}t (cp={strat._informed_bias_cp} "
                      f"w={strat._informed_bias_weight:.2f}) -> "
                      f"target_pos={tgt:+d}  gap={tgt - strat.position:+d}\n"
                      f"  B_implied={b_str}  B_flow buy={buy_q} sell={sell_q} "
                      f"net={buy_q - sell_q:+d}  B_nudge={b_nudge:+.2f}t")
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
                    weight = CP_WEIGHT.get(cp_id)
                    if weight is not None:
                        tag = f" [w={weight:.2f}]"
                    elif cp_id in STALE_CPS:
                        tag = " [STALE-sweep]"
                    else:
                        tag = " [unknown]"
                    print(f"    {cp_id:<14s} bought={s.qty_we_bought:>4d} "
                          f"sold={s.qty_we_sold:>4d}  "
                          f"taker={s.taker_count} maker={s.maker_count}  "
                          f"adv={s.adverse_ticks:+.1f}t{tag}")
            elif cmd == "b":
                now = time.time()
                bias = strat._informed_bias_now(now)
                tgt = strat._bias_target_position()
                age = (now - strat._informed_bias_set_t
                       if strat._informed_bias_set_t > 0 else float("inf"))
                print(f"  bias_now={bias:+.2f}t  raw={strat._informed_bias_ticks:+.2f}t  "
                      f"set_age={age:.1f}s  "
                      f"cp={strat._informed_bias_cp!r}  "
                      f"weight={strat._informed_bias_weight:.2f}\n"
                      f"  target_pos={tgt:+d}  current_pos={strat.position}  "
                      f"gap={tgt - strat.position:+d}")
                print(f"  CP_WEIGHT registry: "
                      f"{sorted(CP_WEIGHT.items(), key=lambda kv: -kv[1])}")
            elif cmd == "m":
                book_mid = strat._market_mid_from_book()
                book_str = f"{book_mid:.2f}" if book_mid is not None else "-"
                vwap = strat._trade_vwap()
                vwap_str = f"{vwap:.2f}" if vwap is not None else "-"
                signal = strat._market_signal()
                signal_str = f"{signal:.2f}" if signal is not None else "-"
                print(f"  recent A trades buffered: "
                      f"{len(strat._recent_trades)}")
                print(f"  A vwap (30s window)   : {vwap_str}")
                print(f"  A book mid            : {book_str}")
                print(f"  A blended signal      : {signal_str}")
            elif cmd == "x":
                now = time.time()
                b_imp = strat._b_implied_next_value(now)
                buy_q, sell_q = strat._b_aggressor_flow(now)
                window = strat._b_window(now)
                b_nudge = strat._b_nudge_ticks(0.0)  # fair doesn't matter here
                b_str = f"{b_imp:.2f}" if b_imp is not None else "-"
                print(f"  B trades in window ({strat.cfg.b_tape_window_sec}s):"
                      f" {len(window)}  buffered={len(strat._b_trades)}")
                print(f"  B implied X_next      : {b_str}")
                print(f"  B aggressor flow      : buy={buy_q} sell={sell_q} "
                      f"net={buy_q - sell_q:+d} "
                      f"(thr={strat.cfg.b_tape_flow_threshold})")
                print(f"  B nudge (current)     : {b_nudge:+.2f}t "
                      f"(cap={strat.cfg.b_nudge_max_ticks}t)")
                print(f"  bias from B_TAPE?      : "
                      f"cp={strat._informed_bias_cp!r}  "
                      f"bias_now={strat._informed_bias_now(now):+.2f}t")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; "
                      f"try s/p/c/cp/b/m/x/f/q")
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
