"""Run Strategy v10 (precompute + fast-path) and Probe together over one WS.

  - Strategy v10: background _precompute_worker computes "if next reveal
                  = v, do X" plans for every plausible v. on_reveal does a
                  dict lookup, atomic posterior swap, immediate quote post,
                  and walks the top of book against pre-priced IOC bounds
                  -- no Bayesian math in the hot path. Between reveals,
                  no-edge pull + adverse-fill skew. See strategy10.py
                  docstring for full design.
  - Probe:        logs every WS event to a per-run JSONL file.

Run:
    python day1/run_combined10.py
    python day1/run_combined10.py --log day1/sess.jsonl
    python day1/run_combined10.py --symbol A
    python day1/run_combined10.py --informed-trader ORACLE_NAME

Interactive commands:
    s   status (v10: precompute count + age + fast-path stats)
    p   dump the current precompute table (per-value scenarios)
    c   per-counterparty fill stats
    i   informed-trader state
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
from strategy10 import URL, API_KEY, Posterior, Strategy, N_PRIOR_SIM  # noqa: E402
from probe import Probe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Strategy v10 + Probe together over one WS connection"
    )
    parser.add_argument("--symbol", default="A")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: "
                             "day1/logs/combined_log_v10_<YYYYMMDD_HHMMSS>.jsonl")
    parser.add_argument("--informed-trader", default=None,
                        dest="informed_trader",
                        help="Counterparty id of the informed trader.")
    args = parser.parse_args()

    if args.log is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.log = f"day1/logs/combined_log_v10_{ts}.jsonl"

    c = GameClient(URL, API_KEY)

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
        print(f"[AUTH] FAILED: {e}\n  -> API key rejected.")
        sys.exit(1)
    ws_url = URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/private"
    print(f"[WS] will connect to {ws_url}?api_key=<redacted>")

    print(f"\nBuilding prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = Strategy(c, post, symbol=args.symbol,
                     informed_trader_id=args.informed_trader)
    if strat.informed_trader_id:
        print(f"[INFORMED] tracking trader id = {strat.informed_trader_id!r}  "
              f"cool-off = {strat.informed_cooloff_sec:.0f}s")
    else:
        print("[INFORMED] disabled")
    print(f"[v10] reveal_interval={strat.reveal_interval}s  "
          f"position_limit={strat.position_limit}  "
          f"precompute_min_prob={strat.precompute_min_prob}  "
          f"precompute_lead={strat.precompute_lead_sec}s  "
          f"precompute_max_values={strat.precompute_max_values}  "
          f"no_edge_pull_ticks={strat.no_edge_pull_ticks}  "
          f"penny_aggressive={strat.penny_aggressive}  "
          f"adverse_skew={strat.adverse_fill_skew_ticks}ticks/"
          f"{strat.adverse_fill_decay_sec}s  "
          f"pre_reveal_post={strat.pre_reveal_post_sec}s "
          f"pre_reveal_post_qty={strat.pre_reveal_post_qty}")

    probe = Probe(c, args.symbol, args.log,
                  probe_mode="passive", probe_interval=999999.0)
    probe._safety_check = lambda: None

    def on_reveal(msg: dict) -> None:
        # Strategy first: probe.on_reveal writes JSON to disk (~50-100us)
        # and prints to stdout. On the reveal-race path that overhead pushes
        # our IOC submit later. Run the strategy first; probe logs after.
        strat.on_reveal(msg["value"])
        probe.on_reveal(msg)

    def on_fill(msg: dict) -> None:
        # Same reasoning: strategy updates position state before probe logs.
        strat.on_fill_event(msg)
        probe.on_fill(msg)

    def on_game_state(msg: dict) -> None:
        probe.on_game_state(msg)
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_trade(msg: dict) -> None:
        probe.on_trade(msg)
        strat.on_trade(msg)

    def on_book(msg: dict) -> None:
        # Feed the strategy first: it just stores the snapshot for the
        # hot-path _book_cache (cheap, no I/O). Probe logs after.
        # Diagnose runs (2026-05-19) showed REST /book has a ~3% tail
        # of 90-184ms spikes; WS book lands ~1ms after reveal, so this
        # is the right source for the reveal-race book read.
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

    print(f"\nCombined runner started.")
    print(f"  Strategy v10: live trading (symbol={args.symbol})")
    print(f"  Probe:        JSONL log -> {args.log}")
    print("Commands: 's'=status  'p'=precompute  'c'=cp stats  'i'=informed  "
          "'m'=market  'f'=flatten  'q'=quit.\n")

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
                bid_px, ask_px, _, _ = strat.desired_quotes()
                k = len(strat.posterior.reveals)
                in_window = strat._in_pre_reveal_window()
                nxt = strat._next_reveal_at()
                nxt_in_str = (f"{nxt - time.time():.2f}s"
                              if nxt is not None else "-")
                with strat._precompute_lock:
                    n_scen = len(strat._precomputed)
                    pcycle = strat._precompute_for_cycle
                print(f"  phase={strat.phase}  pos={strat.position}  "
                      f"fair={fair:.1f}+/-{sigma:.1f}  "
                      f"fair_eff={fair_eff:.1f}  bid={bid_px} ask={ask_px}  "
                      f"k={k}/{strat.n_total}\n"
                      f"  v10: precompute={n_scen}@cycle{pcycle} "
                      f"last_ms={strat._last_precompute_us/1000:.1f}  "
                      f"runs={strat._precompute_run_count}  "
                      f"pre_reveal_window={in_window}  next_reveal_in={nxt_in_str}\n"
                      f"  recent_adverse: "
                      f"bid={strat._adverse_skew('bid', time.time()):.2f}t  "
                      f"ask={strat._adverse_skew('ask', time.time()):.2f}t")
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
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"  unknown command {cmd!r}; try s/p/c/i/m/f/q")
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
