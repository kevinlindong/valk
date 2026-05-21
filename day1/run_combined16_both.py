"""Run Strategy v16 on BOTH A and B simultaneously over one WS.

Two v16 Strategy instances share one GameClient. Each strategy
independently fetches the truth oracle (admin endpoint, idempotent)
and computes its own trajectory/skewed quotes for its symbol. WS
events are dispatched by symbol — fills/books/trades/quote events
only go to the matching strategy; reveals and phase changes go to
both (they aren't symbol-specific).

Run:
    python day1/run_combined16_both.py
    python day1/run_combined16_both.py --log day1/sess.jsonl

Interactive commands:
    s             status (both A and B side-by-side)
    p             precompute table (both)
    t             truth/trajectory snapshot (both)
    a:<cmd>       run <cmd> against the A strategy only
                  (e.g., `a:s`, `a:p`, `a:f`)
    b:<cmd>       same, against the B strategy only
    f             flatten BOTH
    q             quit (auto-flattens). Ctrl-C also flattens.

Per-strategy sub-commands (forwarded via `a:` or `b:`):
    s p c cp b m x k z t f
    (same as run_combined16.py)
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


def _print_status(strat) -> None:
    fair, sigma = strat.fair_and_sigma()
    bid_px, ask_px, _, _ = strat.desired_quotes()
    k = len(strat.posterior.reveals)
    now = time.time()
    nxt = strat._next_reveal_at()
    nxt_in = f"{nxt - now:.2f}s" if nxt is not None else "-"
    with strat._precompute_lock:
        n_scen = len(strat._precomputed)
        pcycle = strat._precompute_for_cycle
    tgt_win = strat._current_window_target()
    win_idx = strat._current_window_index()
    print(f"  [{strat.symbol}] phase={strat.phase}  pos={strat.position}/"
          f"{strat.position_limit}  fair={fair:.1f}+/-{sigma:.1f}  "
          f"bid={bid_px} ask={ask_px}  k={k}/{strat.n_total}\n"
          f"      precompute={n_scen}@cycle{pcycle}  "
          f"next_reveal_in={nxt_in}  "
          f"window={win_idx}  target={tgt_win}  "
          f"stale_total={strat._stale_attack_total}")


def _print_precompute(strat) -> None:
    with strat._precompute_lock:
        items = sorted(strat._precomputed.items())
        pcycle = strat._precompute_for_cycle
    print(f"  [{strat.symbol}] precompute (cycle={pcycle}, n={len(items)}):")
    for v, sc in items:
        print(f"    v={v:>2d}  P={sc.marginal_prob:.3f}  "
              f"fair={sc.fair:6.1f}+/-{sc.sigma:4.1f}  "
              f"bid={sc.bid_px} ask={sc.ask_px}  "
              f"lift<={sc.lift_to_px} hit>={sc.hit_to_px}")


def _print_truth(strat) -> None:
    seq = strat._round_sequence
    if seq is None:
        print(f"  [{strat.symbol}] truth not fetched yet")
        return
    revealed = list(strat.posterior.reveals)
    print(f"  [{strat.symbol}] full_sequence : {seq}  (sum={sum(seq)})")
    print(f"      revealed      : {revealed}  "
          f"({len(revealed)}/{len(seq)})")
    print(f"      truth_a = {strat._round_truth_a}  "
          f"truth_b = {strat._round_truth_b}")
    if strat._trajectory:
        t = strat._trajectory
        print(f"      trajectory    : targets="
              f"{t['target_per_window']}  "
              f"best_win={t['best_window']} "
              f"max_edge={t['max_edge']:.1f}t")


def _run_sub_command(strat, sub: str) -> None:
    """Forward a single sub-command (s/p/t/f/c/cp/b/m/x/k/z) to one
    strategy. Mirrors the per-strategy commands in run_combined16.py."""
    if sub == "s":
        _print_status(strat)
    elif sub == "p":
        _print_precompute(strat)
    elif sub == "t":
        _print_truth(strat)
    elif sub == "f":
        strat.flatten()
    elif sub == "cp":
        snap = strat.cpp.snapshot()
        rows = sorted(snap.items(),
                      key=lambda kv: -(kv[1].qty_we_bought
                                       + kv[1].qty_we_sold))
        print(f"  [{strat.symbol}] CPProfile ({len(snap)} cps):")
        for cp_id, s in rows[:30]:
            weight = CP_WEIGHT.get(cp_id)
            if weight is not None:
                tag = f" [bot w={weight:.2f}]"
            elif cp_id in STALE_CPS:
                tag = " [STALE-sweep]"
            elif cp_id in strat._cp_learned_weight:
                tag = (f" [LEARNED "
                       f"w={strat._cp_learned_weight[cp_id]:.2f}]")
            else:
                tag = " [unknown]"
            print(f"    {cp_id:<14s} bought={s.qty_we_bought:>4d} "
                  f"sold={s.qty_we_sold:>4d}  "
                  f"taker={s.taker_count} maker={s.maker_count}  "
                  f"adv={s.adverse_ticks:+.1f}t{tag}")
    elif sub == "b":
        now = time.time()
        bias = strat._informed_bias_now(now)
        tgt = strat._bias_target_position()
        age = (now - strat._informed_bias_set_t
               if strat._informed_bias_set_t > 0 else float("inf"))
        print(f"  [{strat.symbol}] bias_now={bias:+.2f}t  "
              f"raw={strat._informed_bias_ticks:+.2f}t  "
              f"set_age={age:.1f}s  cp={strat._informed_bias_cp!r}  "
              f"weight={strat._informed_bias_weight:.2f}\n"
              f"      target_pos={tgt:+d}  current_pos={strat.position}  "
              f"gap={tgt - strat.position:+d}")
    elif sub == "m":
        book_mid = strat._market_mid_from_book()
        book_str = f"{book_mid:.2f}" if book_mid is not None else "-"
        vwap = strat._trade_vwap()
        vwap_str = f"{vwap:.2f}" if vwap is not None else "-"
        signal = strat._market_signal()
        signal_str = f"{signal:.2f}" if signal is not None else "-"
        print(f"  [{strat.symbol}] trades buffered: "
              f"{len(strat._recent_trades)}  "
              f"vwap={vwap_str}  book_mid={book_str}  signal={signal_str}")
    elif sub == "x":
        now = time.time()
        b_imp = strat._b_implied_next_value(now)
        buy_q, sell_q = strat._b_aggressor_flow(now)
        b_str = f"{b_imp:.2f}" if b_imp is not None else "-"
        print(f"  [{strat.symbol}] B implied={b_str}  "
              f"B flow buy={buy_q} sell={sell_q} "
              f"net={buy_q - sell_q:+d}")
    elif sub == "k":
        if not strat._cp_learned_weight:
            print(f"  [{strat.symbol}] no learned CP weights yet")
        else:
            rows = sorted(strat._cp_learned_weight.items(),
                          key=lambda kv: -kv[1])
            print(f"  [{strat.symbol}] learned weights "
                  f"({len(rows)} cps):")
            for cp_id, w in rows:
                snap = strat.cpp.snapshot().get(cp_id)
                adv = snap.adverse_ticks if snap else 0.0
                n = (snap.taker_count + snap.maker_count) if snap else 0
                print(f"    {cp_id:<14s} learned_w={w:.2f}  "
                      f"adv={adv:+.1f}t  fills={n}")
    elif sub == "z":
        print(f"  [{strat.symbol}] stale-attack swept: "
              f"{strat._stale_attack_total} lots  "
              f"last={strat._last_stale_attack_lot}")
    else:
        print(f"  [{strat.symbol}] unknown sub-command {sub!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dual-symbol v16 runner (A and B share one WS)")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v16both_"
                             "<YYYYMMDD_HHMMSS>.jsonl")
    parser.add_argument("--probe-symbol", default="A",
                        help="Symbol the probe focuses on for its log "
                             "(both strategies trade their own symbol)")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v16both_{ts}.jsonl"

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
        print(f"[REST] FAILED: {e}\n  -> URL ({URL}) unreachable.")
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

    # One posterior is fine — each Strategy holds a reference and reads
    # but the posterior is updated by reveals (which both strategies share).
    # However, each Strategy mutates posterior.reveals on on_reveal — so we
    # need TWO separate Posterior instances to avoid double-applying reveals.
    print(f"\nBuilding priors ({N_PRIOR_SIM:,} MC samples each)...")
    postA = Posterior()
    postB = Posterior()
    print(f"Prior support has {len(postA.prior)} distinct (a, w) pairs.")

    stratA = Strategy(c, postA, symbol="A")
    stratB = Strategy(c, postB, symbol="B")

    for s in (stratA, stratB):
        print(f"[v16-{s.symbol}] reveal_interval={s.reveal_interval}s  "
              f"position_limit={s.position_limit}  "
              f"quote_qty={s.cfg.quote_qty}/{s.cfg.quote_qty_normal}  "
              f"dir_target={s.cfg.directional_position_target}@"
              f"{s.cfg.directional_threshold_ticks}t  "
              f"stale_attack={s.cfg.stale_attack_enabled}")

    probe = Probe(c, args.probe_symbol, args.log,
                  probe_mode="passive", probe_interval=999999.0)
    probe._safety_check = lambda: None

    # ---- WS routing ----------------------------------------------------
    # Reveals + phase changes are NOT symbol-specific — both strategies
    # get them. Fills/books/trades/quote events ARE — route by symbol.

    def _dispatch_by_symbol(msg: dict, fn_name: str) -> None:
        sym = msg.get("symbol")
        if sym == "A":
            getattr(stratA, fn_name)(msg)
        elif sym == "B":
            getattr(stratB, fn_name)(msg)
        # If symbol missing/unknown, ignore (rare; per-symbol handlers
        # self-filter and would no-op anyway).

    def on_reveal(msg: dict) -> None:
        stratA.on_reveal(msg["value"])
        stratB.on_reveal(msg["value"])
        probe.on_reveal(msg)

    def on_fill(msg: dict) -> None:
        _dispatch_by_symbol(msg, "on_fill_event")
        probe.on_fill(msg)

    def on_game_state(msg: dict) -> None:
        probe.on_game_state(msg)
        phase = msg.get("phase")
        reveals = msg.get("reveals") or []
        stratA.on_phase_change(phase, reveals)
        stratB.on_phase_change(phase, reveals)

    def on_trade(msg: dict) -> None:
        probe.on_trade(msg)
        _dispatch_by_symbol(msg, "on_trade")

    def on_book(msg: dict) -> None:
        _dispatch_by_symbol(msg, "on_book_event")
        probe.on_book(msg)

    def on_message(msg: dict) -> None:
        probe.on_message(msg)
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            _dispatch_by_symbol(msg, "on_quote_event")
        elif t == "tick_settlement":
            # B-only event: server marks pos at value, resets to 0. We must
            # sync local state and re-quote for the new window.
            _dispatch_by_symbol(msg, "on_tick_settlement")

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
        stratA.step(reconcile=True)
        stratB.step(reconcile=True)

    print(f"\nCombined v16 DUAL-SYMBOL runner started.")
    print(f"  Strategy A: live trading symbol=A (truth + trajectory + MM)")
    print(f"  Strategy B: live trading symbol=B (truth + trajectory + MM)")
    print(f"  Probe:      JSONL log -> {args.log}  "
          f"(probe_symbol={args.probe_symbol})")
    print("Commands: 's'=both status  'p'=both precompute  "
          "'t'=both truth  'f'=flatten BOTH  "
          "'a:<cmd>'/'b:<cmd>' target one side  'q'=quit\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "":
                continue
            # Per-symbol prefix: a:<sub> or b:<sub>
            if cmd.startswith("a:"):
                _run_sub_command(stratA, cmd[2:].strip())
                continue
            if cmd.startswith("b:"):
                _run_sub_command(stratB, cmd[2:].strip())
                continue
            if cmd == "s":
                _print_status(stratA)
                _print_status(stratB)
            elif cmd == "p":
                _print_precompute(stratA)
                _print_precompute(stratB)
            elif cmd == "t":
                _print_truth(stratA)
                _print_truth(stratB)
            elif cmd == "c":
                probe.cp.print_summary()
            elif cmd == "f":
                stratA.flatten()
                stratB.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            else:
                print(f"  unknown command {cmd!r}; "
                      f"try s/p/t/c/f/q  or  a:<cmd> / b:<cmd>")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Stopping probe; flattening both; closing log...")
        probe.stop()
        for s in (stratA, stratB):
            try:
                s.stop()
            except Exception:
                pass
        for s in (stratA, stratB):
            try:
                s.flatten()
            except Exception as e:
                print(f"flatten[{s.symbol}] error: {e}")
        try:
            probe.log_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
