"""Analyze reveal -> first-trade latency and reveal-interval jitter.

Q1: At each reveal, who is first to trade -- us (KEVD) or someone else?
Q2: How precisely timed are reveals -- strict schedule or jitter?

All times in nanoseconds in the logs; we use ts_ns (authoritative exchange ts).
"""

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

LOGS = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_214348.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_212826.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_212723.jsonl",
]

WINDOW_NS = 500_000_000  # 500 ms


def percentile(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def load_events(path):
    """Load events; segment by session_start (each is a separate game)."""
    sessions = []
    current = None
    with open(path) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind") or ev.get("type")
            if kind == "session_start":
                if current is not None:
                    sessions.append(current)
                current = {
                    "path": path,
                    "events": [],
                    "reveal_interval_s": None,
                }
            if current is None:
                # tolerate stray events before any session_start
                current = {"path": path, "events": [], "reveal_interval_s": None}
            current["events"].append(ev)
            if kind == "game_state" and current["reveal_interval_s"] is None:
                ri = ev.get("reveal_interval")
                if ri:
                    current["reveal_interval_s"] = ri
    if current is not None:
        sessions.append(current)
    return sessions


def analyze_q1(sessions):
    """For each reveal, find first trade(s) within 500ms (by ts_ns)."""
    reveals = []  # list of dicts
    first_trade_deltas_ms = []
    first_trade_by_us = 0
    first_trade_by_other = 0
    reveals_with_no_trade = 0
    # break down lost-races
    other_aggressor_counts = Counter()  # by aggressor side
    other_symbol_counts = Counter()
    other_first_counterparty_proxy = Counter()  # via quote_fill order_id != ours
    # how often when WE traded first, we were taker vs maker
    our_first_liquidity_counts = Counter()
    # delta distribution split by who-was-first
    delta_when_we_first = []
    delta_when_other_first = []
    # absolute time-from-reveal of OUR first fill (even if we weren't first)
    our_first_fill_deltas_ms = []
    no_our_fill_in_window = 0

    # additionally: for trades in the 500ms window that match a KEVD fill,
    # count "our trades" vs "other trades"
    our_trade_count = 0
    other_trade_count = 0

    # latency of our first IOC ack after each reveal
    our_first_ioc_ack_deltas_ms = []
    no_ioc_in_window = 0

    for sess in sessions:
        events = sess["events"]
        # build lookups
        trades_by_ts = []  # list of (ts_ns, trade_event)
        fills_by_trade_id = {}
        quote_fills_by_trade_id = defaultdict(list)
        reveal_events = []
        our_ioc_acks_by_ts = []  # ts_ns of our IOC orders

        for ev in events:
            t = ev.get("type") or ev.get("kind")
            if t == "trade":
                ts = ev.get("ts_ns")
                if ts is not None:
                    trades_by_ts.append((ts, ev))
            elif t == "fill":
                tid = ev.get("trade_id")
                if tid is not None:
                    fills_by_trade_id[tid] = ev
            elif t == "quote_fill":
                tid = ev.get("trade_id")
                if tid is not None:
                    quote_fills_by_trade_id[tid].append(ev)
            elif t == "reveal":
                reveal_events.append(ev)
            elif t == "ack" or ev.get("kind") == "ack":
                if ev.get("trader") == "KEVD" and ev.get("tif") == "ioc":
                    ts = ev.get("ts_ns")
                    if ts is not None:
                        our_ioc_acks_by_ts.append(ts)
        our_ioc_acks_by_ts.sort()

        trades_by_ts.sort(key=lambda x: x[0])

        for rev in reveal_events:
            # Reveal ts_ns is None on this exchange (no server-stamped ts on
            # reveal events). Use sent_ns -- that's the exchange-side time
            # when the reveal message was emitted, comparable to trade ts_ns.
            rev_ts = rev.get("sent_ns")
            if rev_ts is None:
                rev_ts = rev.get("ts_ns") or rev.get("t_ns")
            if rev_ts is None:
                continue
            window_end = rev_ts + WINDOW_NS

            # find trades in (rev_ts, window_end]
            window_trades = [
                (ts, tr) for ts, tr in trades_by_ts if rev_ts < ts <= window_end
            ]

            if not window_trades:
                reveals_with_no_trade += 1
                continue

            first_ts, first_tr = window_trades[0]
            delta_ms = (first_ts - rev_ts) / 1e6
            first_trade_deltas_ms.append(delta_ms)

            first_tid = first_tr.get("trade_id")
            our_fill = fills_by_trade_id.get(first_tid)

            if our_fill is not None:
                first_trade_by_us += 1
                our_first_liquidity_counts[our_fill.get("liquidity", "?")] += 1
                delta_when_we_first.append(delta_ms)
            else:
                first_trade_by_other += 1
                other_aggressor_counts[first_tr.get("aggressor", "?")] += 1
                other_symbol_counts[first_tr.get("symbol", "?")] += 1
                delta_when_other_first.append(delta_ms)
                # who made the resting side (proxy)
                qfs = quote_fills_by_trade_id.get(first_tid, [])
                # if any quote_fill exists but no fill on our side, the maker was someone else
                if qfs:
                    other_first_counterparty_proxy["other_maker"] += 1
                else:
                    other_first_counterparty_proxy["unknown"] += 1

            # count all trades in window: ours vs others
            our_first_fill_seen = False
            for ts, tr in window_trades:
                tid = tr.get("trade_id")
                if tid in fills_by_trade_id:
                    our_trade_count += 1
                    if not our_first_fill_seen:
                        our_first_fill_seen = True
                        our_first_fill_deltas_ms.append((ts - rev_ts) / 1e6)
                else:
                    other_trade_count += 1
            if not our_first_fill_seen:
                no_our_fill_in_window += 1

            # find our first IOC ack in the window
            first_ioc_ts = next(
                (ts for ts in our_ioc_acks_by_ts if rev_ts < ts <= window_end),
                None,
            )
            if first_ioc_ts is not None:
                our_first_ioc_ack_deltas_ms.append((first_ioc_ts - rev_ts) / 1e6)
            else:
                no_ioc_in_window += 1

            reveals.append(
                {
                    "rev_ts": rev_ts,
                    "delta_ms": delta_ms,
                    "ours": our_fill is not None,
                    "first_tr": first_tr,
                }
            )

    return {
        "total_reveals": len(first_trade_deltas_ms) + reveals_with_no_trade,
        "reveals_with_trade_in_500ms": len(first_trade_deltas_ms),
        "reveals_no_trade_in_500ms": reveals_with_no_trade,
        "first_trade_deltas_ms": first_trade_deltas_ms,
        "delta_when_we_first": delta_when_we_first,
        "delta_when_other_first": delta_when_other_first,
        "first_trade_by_us": first_trade_by_us,
        "first_trade_by_other": first_trade_by_other,
        "our_first_liquidity_counts": dict(our_first_liquidity_counts),
        "other_aggressor_counts": dict(other_aggressor_counts),
        "other_symbol_counts": dict(other_symbol_counts),
        "other_first_counterparty_proxy": dict(other_first_counterparty_proxy),
        "our_trade_count_in_window": our_trade_count,
        "other_trade_count_in_window": other_trade_count,
        "our_first_fill_deltas_ms": our_first_fill_deltas_ms,
        "reveals_with_no_our_fill_in_500ms": no_our_fill_in_window,
        "our_first_ioc_ack_deltas_ms": our_first_ioc_ack_deltas_ms,
        "reveals_with_no_ioc_ack_in_500ms": no_ioc_in_window,
    }


def analyze_q2(sessions):
    """Compute inter-reveal interval distribution + drift relative to first reveal.

    Reveal events in this log usually have ts_ns=None (server doesn't stamp
    them). We use t_ns (probe receive time) as the per-reveal arrival time --
    that's what the strategy actually sees.

    Games are segmented by reveal index reset (index=1 starts a new game).
    """
    intervals_s_global = []
    drift_residuals_ms = []  # per-session, residual from expected schedule
    drift_per_session = []

    # Build per-game reveal series across all sessions
    games = []  # list of {ri_s, ts_list, t_ns_used}
    for sess in sessions:
        ri_s = sess["reveal_interval_s"] or 15.0
        reveal_evs = [
            ev
            for ev in sess["events"]
            if (ev.get("type") or ev.get("kind")) == "reveal"
        ]
        current_game = None
        for ev in reveal_evs:
            idx = ev.get("index")
            t_use = ev.get("ts_ns")
            if t_use is None:
                t_use = ev.get("t_ns")  # fallback: probe receive time
            if t_use is None:
                continue
            if idx == 1 or current_game is None:
                if current_game is not None and len(current_game["ts_list"]) >= 2:
                    games.append(current_game)
                current_game = {"ri_s": ri_s, "ts_list": [], "from": sess["path"]}
            current_game["ts_list"].append(t_use)
        if current_game is not None and len(current_game["ts_list"]) >= 2:
            games.append(current_game)

    per_session = []
    for si, g in enumerate(games):
        ri_s = g["ri_s"]
        ts_list = sorted(g["ts_list"])
        if len(ts_list) < 2:
            continue
        intervals_s = [
            (ts_list[i + 1] - ts_list[i]) / 1e9 for i in range(len(ts_list) - 1)
        ]
        intervals_s_global.extend(intervals_s)
        # drift: predicted ts_i = ts_0 + i * ri_s
        residuals = []
        t0 = ts_list[0]
        for i, ts in enumerate(ts_list):
            predicted = t0 + i * int(ri_s * 1e9)
            residuals.append((ts - predicted) / 1e6)  # ms
        drift_residuals_ms.extend(residuals)
        drift_per_session.append(
            {
                "n_reveals": len(ts_list),
                "ri_s": ri_s,
                "first_to_last_drift_ms": residuals[-1] if residuals else 0.0,
                "max_abs_residual_ms": max(abs(r) for r in residuals) if residuals else 0.0,
            }
        )
        per_session.append(
            {
                "session_index": si,
                "ri_s": ri_s,
                "n_reveals": len(ts_list),
                "intervals_s": intervals_s,
            }
        )

    summary = {
        "n_intervals": len(intervals_s_global),
        "intervals_s_mean": statistics.mean(intervals_s_global) if intervals_s_global else float("nan"),
        "intervals_s_stdev": statistics.pstdev(intervals_s_global) if len(intervals_s_global) > 1 else 0.0,
        "intervals_s_min": min(intervals_s_global) if intervals_s_global else float("nan"),
        "intervals_s_max": max(intervals_s_global) if intervals_s_global else float("nan"),
        "drift_residuals_ms_p50": percentile(drift_residuals_ms, 50),
        "drift_residuals_ms_p90": percentile(drift_residuals_ms, 90),
        "drift_residuals_ms_p99": percentile(drift_residuals_ms, 99),
        "drift_residuals_ms_max_abs": max(abs(r) for r in drift_residuals_ms) if drift_residuals_ms else 0.0,
        "per_session": per_session,
        "drift_per_session": drift_per_session,
    }
    # signed jitter (vs nominal interval)
    if intervals_s_global:
        # use modal/session ri
        rs = [s["ri_s"] for s in per_session]
        nominal = statistics.median(rs) if rs else 5.0
        signed_jitter_ms = [(i - nominal) * 1000.0 for i in intervals_s_global]
        summary["nominal_interval_s"] = nominal
        summary["signed_jitter_ms_mean"] = statistics.mean(signed_jitter_ms)
        summary["signed_jitter_ms_stdev"] = statistics.pstdev(signed_jitter_ms) if len(signed_jitter_ms) > 1 else 0.0
        summary["signed_jitter_ms_min"] = min(signed_jitter_ms)
        summary["signed_jitter_ms_max"] = max(signed_jitter_ms)
        summary["abs_jitter_ms_p50"] = percentile([abs(j) for j in signed_jitter_ms], 50)
        summary["abs_jitter_ms_p90"] = percentile([abs(j) for j in signed_jitter_ms], 90)
        summary["abs_jitter_ms_p99"] = percentile([abs(j) for j in signed_jitter_ms], 99)
    return summary


def fmt_pct(num, den):
    return f"{num}/{den} ({100.0 * num / den:.1f}%)" if den else "0/0"


def main():
    all_sessions = []
    for p in LOGS:
        if not Path(p).exists():
            print(f"skip missing: {p}")
            continue
        sess = load_events(p)
        print(f"loaded {len(sess)} session(s) from {Path(p).name}")
        all_sessions.extend(sess)

    print(f"\nTOTAL SESSIONS: {len(all_sessions)}")

    # Q1
    q1 = analyze_q1(all_sessions)
    print("\n" + "=" * 70)
    print("Q1 -- WHO IS FIRST TO TRADE AFTER EACH REVEAL (500ms window)")
    print("=" * 70)
    n_total = q1["total_reveals"]
    n_with = q1["reveals_with_trade_in_500ms"]
    n_without = q1["reveals_no_trade_in_500ms"]
    print(f"Total reveals: {n_total}")
    print(f"  Reveals with >=1 trade in 500ms: {n_with}")
    print(f"  Reveals with NO trade in 500ms : {n_without}")

    deltas = q1["first_trade_deltas_ms"]
    if deltas:
        print(
            f"\nFirst-trade latency after reveal (ms): "
            f"mean={statistics.mean(deltas):.2f} stdev={statistics.pstdev(deltas):.2f} "
            f"min={min(deltas):.2f} max={max(deltas):.2f}"
        )
        print(
            f"  p50={percentile(deltas, 50):.2f}  p75={percentile(deltas, 75):.2f}  "
            f"p90={percentile(deltas, 90):.2f}  p99={percentile(deltas, 99):.2f}"
        )

    print(
        f"\nFirst trade was OUR fill (KEVD): "
        f"{fmt_pct(q1['first_trade_by_us'], n_with)}"
    )
    print(
        f"First trade was someone ELSE:    "
        f"{fmt_pct(q1['first_trade_by_other'], n_with)}"
    )
    print(f"  When OURS, liquidity breakdown: {q1['our_first_liquidity_counts']}")
    print(f"  When OTHER, aggressor breakdown: {q1['other_aggressor_counts']}")
    print(f"  When OTHER, symbol breakdown   : {q1['other_symbol_counts']}")

    dw = q1["delta_when_we_first"]
    do = q1["delta_when_other_first"]
    if dw:
        print(
            f"\nDelta when WE were first (ms): "
            f"p50={percentile(dw, 50):.2f}  mean={statistics.mean(dw):.2f}  "
            f"min={min(dw):.2f}  max={max(dw):.2f}"
        )
    if do:
        print(
            f"Delta when OTHER was first(ms): "
            f"p50={percentile(do, 50):.2f}  mean={statistics.mean(do):.2f}  "
            f"min={min(do):.2f}  max={max(do):.2f}"
        )

    print(
        f"\nAll trades in window: ours={q1['our_trade_count_in_window']}, "
        f"others={q1['other_trade_count_in_window']}"
    )
    print(
        f"Reveals where WE got NO fill in 500ms: "
        f"{fmt_pct(q1['reveals_with_no_our_fill_in_500ms'], n_with)}"
    )

    ofd = q1["our_first_fill_deltas_ms"]
    if ofd:
        print(
            f"\nOur first fill latency (when we DID fill within 500ms) ms: "
            f"p50={percentile(ofd, 50):.2f}  p90={percentile(ofd, 90):.2f}  "
            f"max={max(ofd):.2f}"
        )

    ioc = q1["our_first_ioc_ack_deltas_ms"]
    if ioc:
        print(
            f"\nOur first IOC ack after reveal (ms): "
            f"p50={percentile(ioc, 50):.2f}  mean={statistics.mean(ioc):.2f}  "
            f"p90={percentile(ioc, 90):.2f}  min={min(ioc):.2f}  max={max(ioc):.2f}"
        )
    print(
        f"Reveals with NO KEVD IOC ack in 500ms: "
        f"{fmt_pct(q1['reveals_with_no_ioc_ack_in_500ms'], n_with + n_without)}"
    )

    # Q2
    q2 = analyze_q2(all_sessions)
    print("\n" + "=" * 70)
    print("Q2 -- REVEAL INTERVAL JITTER")
    print("=" * 70)
    print(f"Inter-reveal samples: {q2['n_intervals']}")
    print(f"Nominal reveal interval: {q2.get('nominal_interval_s', 'n/a')}s")
    print(
        f"Interval (s): mean={q2['intervals_s_mean']:.6f}  "
        f"stdev={q2['intervals_s_stdev']:.6f}  "
        f"min={q2['intervals_s_min']:.6f}  max={q2['intervals_s_max']:.6f}"
    )
    print(
        f"Signed jitter (ms vs nominal): "
        f"mean={q2.get('signed_jitter_ms_mean', float('nan')):.3f}  "
        f"stdev={q2.get('signed_jitter_ms_stdev', float('nan')):.3f}  "
        f"min={q2.get('signed_jitter_ms_min', float('nan')):.3f}  "
        f"max={q2.get('signed_jitter_ms_max', float('nan')):.3f}"
    )
    print(
        f"|Jitter| ms: p50={q2.get('abs_jitter_ms_p50', float('nan')):.3f}  "
        f"p90={q2.get('abs_jitter_ms_p90', float('nan')):.3f}  "
        f"p99={q2.get('abs_jitter_ms_p99', float('nan')):.3f}"
    )
    print(
        f"\nDrift residual (ts - (ts0 + i*interval)) ms: "
        f"p50={q2['drift_residuals_ms_p50']:.3f}  "
        f"p90={q2['drift_residuals_ms_p90']:.3f}  "
        f"p99={q2['drift_residuals_ms_p99']:.3f}  "
        f"max|.|={q2['drift_residuals_ms_max_abs']:.3f}"
    )
    print("\nPer-session drift summary (first-to-last residual, max |residual|, ms):")
    for ds in q2["drift_per_session"]:
        print(
            f"  n={ds['n_reveals']:>2}  ri={ds['ri_s']}s  "
            f"first_to_last_drift={ds['first_to_last_drift_ms']:.3f}ms  "
            f"max_abs={ds['max_abs_residual_ms']:.3f}ms"
        )

    # filtered view: exclude games whose max |residual| > 1000ms (paused games)
    clean_games = [ds for ds in q2["drift_per_session"] if ds["max_abs_residual_ms"] <= 1000.0]
    if clean_games:
        clean_max_abs = [ds["max_abs_residual_ms"] for ds in clean_games]
        clean_first_to_last = [abs(ds["first_to_last_drift_ms"]) for ds in clean_games]
        print(
            f"\nClean-game view (excluding {len(q2['drift_per_session']) - len(clean_games)} paused-game "
            f"outlier(s) with max|residual|>1s):"
        )
        print(
            f"  n_games={len(clean_games)}  "
            f"max|residual| ms: mean={statistics.mean(clean_max_abs):.3f}  "
            f"max={max(clean_max_abs):.3f}"
        )
        print(
            f"  end-of-game drift ms: mean={statistics.mean(clean_first_to_last):.3f}  "
            f"max={max(clean_first_to_last):.3f}"
        )

    # also: how often was our first fill BEFORE someone else's first trade
    # already implied by first_trade_by_us / first_trade_by_other ratio


if __name__ == "__main__":
    main()
