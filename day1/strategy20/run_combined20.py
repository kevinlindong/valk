"""Run Strategy v20 + Probe over a single WS connection.

v20 extends v19 (truth-anchored A/B MM) to markets C (binary call) and
D (realized range). Under the truth oracle the fair value for C is
exactly 0 or 100, and the fair value for D is a known constant from
t=0 — every contrary tick is +EV, so v20 loads C/D to position-limit
and IOC-sweeps any mispriced book level.

Run:
    python day1/strategy20/run_combined20.py
    python day1/strategy20/run_combined20.py --log /tmp/v20.jsonl
    python day1/strategy20/run_combined20.py --probe-symbol B

Interactive commands:
    s    status (pos A/B/C/D, fair A/B/C/D, truth seq, ramp, precompute)
    t    truth status (seq, fair_c=0/100, fair_d=range)
    j    naive trajectory (per-window edges + soft cap)
    p    dump the current precompute table (truth-collapsed: 1 row)
    c    per-counterparty fill stats (from probe)
    b    top-of-book snapshot for ALL 4 symbols (A, B, C, D)
    d    detailed C/D status (truth fair, position headroom, last burst)
    r    request a precompute rebuild
    z    trigger a manual cross-arb sweep across all 4 symbols
    f    flatten now (cancel resting + IOC to flat) — all 4 syms
    q    quit (auto-flattens). Ctrl-C also flattens.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from strategy20 import (  # noqa: E402
    GameClient,
    URL, API_KEY, N_PRIOR_SIM, Posterior, Strategy, Config,
)
from probe20 import make_probe20, default_log_path  # noqa: E402


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
        description="Run Strategy v20 + Probe together over one WS connection")
    parser.add_argument("--probe-symbol", default="A",
                        help="Symbol the probe focuses on for its periodic "
                             "book snapshot (strategy trades A/B/C/D "
                             "regardless)")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v20_<YYYYMMDD_HHMMSS>.jsonl")
    args = parser.parse_args()

    if args.log is None:
        args.log = default_log_path()

    c = GameClient(URL, API_KEY)

    try:
        gs = c.game_state()
        instr = gs.get("instruments") or {}
        ia = instr.get("A", {})
        ib = instr.get("B", {})
        ic = instr.get("C", {})
        id_ = instr.get("D", {})
        print(f"[REST] OK  phase={gs.get('phase')}  "
              f"reveals={len(gs.get('reveals') or [])}  "
              f"duration={gs.get('duration')}  "
              f"reveal_interval={gs.get('reveal_interval')}")
        print(f"  A: pl={ia.get('position_limit')}/tick={ia.get('tick_size')}/"
              f"mult={ia.get('multiplier')}")
        print(f"  B: pl={ib.get('position_limit')}/tick={ib.get('tick_size')}/"
              f"mult={ib.get('multiplier')}")
        if ic:
            sp = ic.get("settlement_params") or {}
            print(f"  C: pl={ic.get('position_limit')}/tick={ic.get('tick_size')}/"
                  f"mult={ic.get('multiplier')}  strike_params={sp}")
        else:
            print("  C: <not listed>")
        if id_:
            print(f"  D: pl={id_.get('position_limit')}/tick={id_.get('tick_size')}/"
                  f"mult={id_.get('multiplier')}")
        else:
            print("  D: <not listed>")
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
    probe = make_probe20(c, symbol=args.probe_symbol, log_path=args.log)

    # ---- WS handler fan-out: probe logs, strategy trades ----
    def on_reveal(msg: dict) -> None:
        # Probe first → snapshots book pre-cancel for offline analysis.
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
        elif t == "strikes":
            strat.on_strikes_event(msg)

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

    # Kick a fresh MM cycle if the game is already running so we don't
    # wait one full mm_refresh_sec tick before posting our first quote.
    if c.game_state().get("phase") == "running":
        try:
            strat._refresh_mm_quotes()
        except Exception as e:
            print(f"[v20 INIT-MM] {e!r}")

    print(f"\nCombined v20 runner started.")
    print(f"  Strategy v20: v19 truth-anchored A/B + truth-anchored C/D")
    print(f"  Probe:        JSONL log -> {args.log}  "
          f"(probe_symbol={args.probe_symbol})")
    print("Commands: 's'=status  't'=truth  'j'=trajectory  "
          "'p'=precompute  'c'=probe cp  'b'=books  'd'=CD detail  "
          "'r'=rebuild  'z'=cross-arb  'f'=flatten  'q'=quit.\n")

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
                print(f"  truth: {strat.truth_status_str()}")
                print(f"  ramp:  {strat.ramp_status_str()}")
                print(f"  A: pos={strat.state['A'].position:+d}/"
                      f"{strat.state['A'].position_limit}  "
                      f"fair={fa:.1f}+/-{sa:.1f}")
                print(f"  B: pos={strat.state['B'].position:+d}/"
                      f"{strat.state['B'].position_limit}  "
                      f"fair={fb:.2f}+/-{sb:.2f}")
                # C and D status — strategy20-specific
                if "C" in strat.state:
                    fc, sc = strat.fair_c()
                    print(f"  C: pos={strat.state['C'].position:+d}/"
                          f"{strat.state['C'].position_limit}  "
                          f"fair={fc:.1f}+/-{sc:.1f}  "
                          f"strike_K={getattr(strat, 'c_strike', '?')}")
                if "D" in strat.state:
                    fd, sd = strat.fair_d()
                    print(f"  D: pos={strat.state['D'].position:+d}/"
                          f"{strat.state['D'].position_limit}  "
                          f"fair={fd:.2f}+/-{sd:.2f}")
                print(f"  precompute: {n_scen} scenarios @ cycle "
                      f"{pcycle}  runs={pruns}  last={pus/1000:.1f}ms  "
                      f"truth_scen_built={strat._truth_scenario_count}")
                for sym in strat.SYMS:
                    st = strat.state[sym]
                    print(f"  resting {sym}: bid={len(st.resting['bid'])} "
                          f"ask={len(st.resting['ask'])}"
                          f"  in_flight={st.in_flight['bid']}/"
                          f"{st.in_flight['ask']}")
                print(f"  reqs: modify={strat._modify_count}  "
                      f"post={strat._post_count}  "
                      f"reveal_ioc={strat._reveal_ioc_count}  "
                      f"inter_sweep={strat._inter_sweep_count}  "
                      f"cross_arb={strat._cross_arb_count}  "
                      f"endgame={strat._endgame_count}")
                print(f"  endgame_active={strat._endgame_active}  "
                      f"n_remaining={strat._n_remaining()}")
                print(f"  lockout: cnt={strat._lockout_count}  "
                      f"deferred_sends={strat._sends_deferred}")
            elif cmd == "t":
                print(f"  truth: {strat.truth_status_str()}")
                st = strat.truth.stats()
                print(f"  truth_stats: fetches={st['fetches']} "
                      f"errors={st['errors']} cached={st['cached']} "
                      f"last_err={st['last_err']!r}")
                # C/D truth derivatives, if computed
                if hasattr(strat, "_fair_c_truth"):
                    print(f"  fair_c_truth={strat._fair_c_truth}  "
                          f"fair_d_truth={strat._fair_d_truth}")
            elif cmd == "j":
                print(strat.naive_trajectory_str())
                print(f"  ramp:  {strat.ramp_status_str()}")
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
                for sym in strat.SYMS:
                    if sym in strat.state:
                        print(f"  {sym}: {_fmt_book(strat.state[sym].book)}")
            elif cmd == "d":
                # Detailed C/D status — truth, last burst, headroom
                if hasattr(strat, "cd_status_str"):
                    print(strat.cd_status_str())
                else:
                    print("  (cd_status_str not available)")
            elif cmd == "r":
                strat._precompute_request.set()
                print("  precompute rebuild requested")
            elif cmd == "z":
                # Force a cross-arb sweep across all 4 symbols
                try:
                    n = strat._try_cross_arb("manual")
                    print(f"  cross-arb fired {n} legs")
                except Exception as e:
                    print(f"  cross-arb error: {e!r}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s/t/j/p/c/b/d/r/z/f/q")
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
