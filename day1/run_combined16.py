"""Run Strategy v16 + Probe together over one WS connection.

v16 vs v15: bot-only informedness (no human hardcodes), adaptive CP
learning from CPProfile adverse_ticks, generic stale-quote attacker
(background thread), and bigger position targets (target=70, step=20,
max_pay=5t) with ±100 limit safety. See strategy16.py for the full
design and the v15-log analysis that motivated each change.

Run:
    python day1/run_combined16.py
    python day1/run_combined16.py --log day1/sess.jsonl
    python day1/run_combined16.py --symbol A

Interactive commands:
    s    status (position, bias, target, fair, B-implied, stale-total)
    p    dump current precompute table
    c    per-counterparty fill stats (from probe)
    cp   per-counterparty profile (from strategy's CPProfile, tagged)
    b    informed-bias state + target position
    m    market-tape state (A side)
    x    B-tape state (flow / VWAP / nudge)
    k    learned CP weights (adaptive)
    z    stale-attack stats
    t    truth oracle snapshot (full_sequence, fair_a/b, polls)
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
from strategy16 import (  # noqa: E402
    URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM, CP_WEIGHT, STALE_CPS)
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v16 + Probe together over one WS connection")
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v16_<YYYYMMDD_HHMMSS>.jsonl")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v16_{ts}.jsonl"

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
    print(f"[v16] reveal_interval={strat.reveal_interval}s  "
          f"position_limit={strat.position_limit}  "
          f"quote_qty={strat.cfg.quote_qty}/{strat.cfg.quote_qty_normal}  "
          f"informed_bias={strat.cfg.informed_bias_max_ticks}t/"
          f"{strat.cfg.informed_bias_decay_sec}s  "
          f"dir_target={strat.cfg.directional_position_target}lots@"
          f"{strat.cfg.directional_threshold_ticks}t  "
          f"step={strat.cfg.directional_step_qty} "
          f"pay<={strat.cfg.directional_max_pay_ticks}t  "
          f"stale_attack={strat.cfg.stale_attack_enabled}")

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

    print(f"\nCombined v16 runner started.")
    print(f"  Strategy v16: live trading (symbol={args.symbol}) "
          f"bot-only weights + adaptive learning + stale-attacker")
    print(f"  Probe:        JSONL log -> {args.log}")
    print("Commands: 's'=status  'p'=precompute  'c'=probe cp  "
          "'cp'=strat cp  'b'=bias+target  'm'=market  "
          "'x'=B-tape  'k'=learned  'z'=stale  't'=truth  "
          "'f'=flatten  'q'=quit.\n")

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
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}  fair_eff={fair_eff:.1f}  "
                      f"bid={bid_px} ask={ask_px}  "
                      f"k={k}/{strat.n_total}\n"
                      f"  v16: precompute={n_scen}@cycle{pcycle} "
                      f"runs={strat._precompute_run_count}  "
                      f"next_reveal_in={nxt_in}  "
                      f"stale_total={strat._stale_attack_total}\n"
                      f"  bias={bias:+.2f}t (cp={strat._informed_bias_cp} "
                      f"w={strat._informed_bias_weight:.2f}) -> "
                      f"target_pos={tgt:+d}  gap={tgt - strat.position:+d}\n"
                      f"  B_implied={b_str}  B_flow buy={buy_q} sell={sell_q} "
                      f"net={buy_q - sell_q:+d}")
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
                        tag = f" [bot w={weight:.2f}]"
                    elif cp_id in STALE_CPS:
                        tag = " [STALE-sweep]"
                    elif cp_id in strat._cp_learned_weight:
                        tag = f" [LEARNED w={strat._cp_learned_weight[cp_id]:.2f}]"
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
                print(f"  CP_WEIGHT (bot-only): "
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
                b_nudge = strat._b_nudge_ticks(0.0)
                b_str = f"{b_imp:.2f}" if b_imp is not None else "-"
                print(f"  B trades in window ({strat.cfg.b_tape_window_sec}s):"
                      f" {len(window)}  buffered={len(strat._b_trades)}")
                print(f"  B implied X_next      : {b_str}")
                print(f"  B aggressor flow      : buy={buy_q} sell={sell_q} "
                      f"net={buy_q - sell_q:+d} "
                      f"(thr={strat.cfg.b_tape_flow_threshold})")
                print(f"  B nudge (current)     : {b_nudge:+.2f}t "
                      f"(cap={strat.cfg.b_nudge_max_ticks}t)")
            elif cmd == "k":
                if not strat._cp_learned_weight:
                    print("  no learned CP weights yet "
                          "(need >= cp_learning_min_fills maker fills)")
                else:
                    rows = sorted(strat._cp_learned_weight.items(),
                                  key=lambda kv: -kv[1])
                    print(f"  learned weights for {len(rows)} unknown CPs:")
                    for cp_id, w in rows:
                        snap = strat.cpp.snapshot().get(cp_id)
                        adv = snap.adverse_ticks if snap else 0.0
                        n = (snap.taker_count + snap.maker_count) if snap else 0
                        print(f"    {cp_id:<14s} learned_w={w:.2f}  "
                              f"adv={adv:+.1f}t  fills={n}")
            elif cmd == "z":
                print(f"  stale-attack total swept: "
                      f"{strat._stale_attack_total} lots  "
                      f"last={strat._last_stale_attack_lot}")
                print(f"  config: min_edge={strat.cfg.stale_attack_min_edge_ticks}t  "
                      f"min_size={strat.cfg.stale_attack_min_size}  "
                      f"max_slice={strat.cfg.stale_attack_max_slice}  "
                      f"pad={strat.cfg.stale_attack_position_pad}")
            elif cmd == "t":
                seq = strat._round_sequence
                if seq is None:
                    print("  truth not fetched yet (waiting for running phase)")
                else:
                    revealed = list(strat.posterior.reveals)
                    print(f"  full_sequence : {seq}  (sum={sum(seq)})")
                    print(f"  revealed      : {revealed}  "
                          f"({len(revealed)}/{len(seq)})")
                    print(f"  truth_a = {strat._round_truth_a}  "
                          f"truth_b = {strat._round_truth_b}")
                    if strat._trajectory:
                        t = strat._trajectory
                        print(f"  trajectory    : targets="
                              f"{t['target_per_window']}  "
                              f"best_win={t['best_window']} "
                              f"max_edge={t['max_edge']:.1f}t")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; "
                      f"try s/p/c/cp/b/m/x/k/z/t/f/q")
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
