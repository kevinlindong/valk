"""Compare v10 AFTER on_reveal restructure + cancel-sec 0.6 vs BEFORE baseline.

Answers four questions:
  Q1  reveal-race win rate on symbol A (BEFORE vs AFTER)
  Q2  our PnL on A, fill mix maker/taker, counterparty signal
  Q3  reveal-interval jitter / mistiming
  Q4  proposed further optimizations (printed conclusions only)

Builds on analyze_reveal_latency.py -- same load/percentile helpers, but
operates on two log groups and prints side-by-side numbers.

All times in nanoseconds; trade matching uses ts_ns (exchange-stamped).
Reveal time has ts_ns=None on this exchange, so we use `sent_ns`.
"""

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

NEW_LOGS = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_220921.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_220835.jsonl",
]
BASELINE_LOGS = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_214348.jsonl",
]

SYMBOL = "A"
WINDOW_NS = 500_000_000  # 500 ms


# ---------- helpers ----------

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
    """Load events as a flat list; one big session per game (small files)."""
    events = []
    with open(path) as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def reveal_ts(ev):
    """Reveal's effective ts (server doesn't stamp ts_ns; use sent_ns)."""
    return ev.get("sent_ns") or ev.get("ts_ns") or ev.get("t_ns")


def index_events(all_events):
    """Group events by type, build trade_id lookups."""
    by_type = defaultdict(list)
    for ev in all_events:
        t = ev.get("type") or ev.get("kind")
        by_type[t].append(ev)
    fills_by_tid = {ev["trade_id"]: ev for ev in by_type["fill"] if ev.get("trade_id") is not None}
    qf_by_tid = defaultdict(list)
    for ev in by_type["quote_fill"]:
        if ev.get("trade_id") is not None:
            qf_by_tid[ev["trade_id"]].append(ev)
    trades_sorted = sorted(
        [ev for ev in by_type["trade"] if ev.get("ts_ns") is not None],
        key=lambda e: e["ts_ns"],
    )
    return by_type, fills_by_tid, qf_by_tid, trades_sorted


# ---------- Q1: reveal-race ----------

def q1_reveal_race(all_events, label):
    by_type, fills_by_tid, qf_by_tid, trades_sorted = index_events(all_events)

    # restrict to symbol A trades for the race
    trades_A = [t for t in trades_sorted if t.get("symbol") == SYMBOL]

    reveals = by_type["reveal"]
    first_ours_count = 0
    first_other_count = 0
    no_trade_count = 0
    delta_first_any_ms = []
    delta_first_ours_ms = []
    delta_first_other_ms = []  # first market trade (any) when other was first
    # also: for each reveal, delta to first NON-ours trade (market baseline)
    delta_first_market_ms = []  # first trade in window, regardless of who
    delta_first_market_other_ms = []  # first trade by SOMEONE ELSE
    aggressor_when_other = Counter()
    our_liquidity_when_first = Counter()

    for rev in reveals:
        rts = reveal_ts(rev)
        if rts is None:
            continue
        win_end = rts + WINDOW_NS
        win = [t for t in trades_A if rts < t["ts_ns"] <= win_end]
        if not win:
            no_trade_count += 1
            continue
        first = win[0]
        d = (first["ts_ns"] - rts) / 1e6
        delta_first_any_ms.append(d)
        delta_first_market_ms.append(d)

        first_tid = first.get("trade_id")
        if first_tid in fills_by_tid:
            first_ours_count += 1
            delta_first_ours_ms.append(d)
            our_liquidity_when_first[fills_by_tid[first_tid].get("liquidity", "?")] += 1
        else:
            first_other_count += 1
            delta_first_other_ms.append(d)
            aggressor_when_other[first.get("aggressor", "?")] += 1

        # first OTHER (non-ours) trade in window
        first_other = next((t for t in win if t.get("trade_id") not in fills_by_tid), None)
        if first_other is not None:
            delta_first_market_other_ms.append((first_other["ts_ns"] - rts) / 1e6)

        # first OUR trade in window
        first_our = next((t for t in win if t.get("trade_id") in fills_by_tid), None)
        if first_our is not None and first_tid not in fills_by_tid:
            # we did trade in window, just not first
            pass

    n_with = first_ours_count + first_other_count
    return {
        "label": label,
        "n_reveals": len(reveals),
        "n_reveals_with_trade_A": n_with,
        "n_reveals_no_trade_A": no_trade_count,
        "first_ours": first_ours_count,
        "first_other": first_other_count,
        "ours_pct": (100.0 * first_ours_count / n_with) if n_with else float("nan"),
        "delta_first_any_ms_p50": percentile(delta_first_any_ms, 50),
        "delta_first_any_ms_p90": percentile(delta_first_any_ms, 90),
        "delta_first_ours_ms_p50": percentile(delta_first_ours_ms, 50),
        "delta_first_other_ms_p50": percentile(delta_first_other_ms, 50),
        "delta_first_market_other_p50": percentile(delta_first_market_other_ms, 50),
        "delta_first_market_other_p90": percentile(delta_first_market_other_ms, 90),
        "our_liquidity_when_first": dict(our_liquidity_when_first),
        "aggressor_when_other": dict(aggressor_when_other),
    }


# ---------- Q2: PnL & fill mix ----------

def q2_pnl(all_events, label):
    by_type, fills_by_tid, _, _ = index_events(all_events)

    # Realized PnL on A from our fills:
    # sell -> +price*qty - fee ; buy -> -price*qty - fee  (residual position
    # marked to settlement price reported in 'settlement' events)
    fills_A = [ev for ev in by_type["fill"] if ev.get("symbol") == SYMBOL]
    cash = 0.0
    fees = 0.0
    pos = 0
    qty_bought = 0
    qty_sold = 0
    maker_qty = 0
    taker_qty = 0
    cp_counter = Counter()  # qty by counterparty
    cp_role = defaultdict(lambda: {"maker_qty": 0, "taker_qty": 0})

    for f in fills_A:
        side = f.get("side")
        px = float(f.get("price", 0))
        qty = int(f.get("qty", 0))
        fee = float(f.get("fee", 0))
        liq = f.get("liquidity", "?")
        cp = f.get("counterparty", "?")
        if side == "buy":
            cash -= px * qty
            pos += qty
            qty_bought += qty
        elif side == "sell":
            cash += px * qty
            pos -= qty
            qty_sold += qty
        fees += fee
        if liq == "maker":
            maker_qty += qty
            cp_role[cp]["maker_qty"] += qty
        elif liq == "taker":
            taker_qty += qty
            cp_role[cp]["taker_qty"] += qty
        cp_counter[cp] += qty

    # Settlement-reported PnL (KEVD only -- competitor PnL not available in log)
    settle_pnls_A = [
        s.get("pnl_by_symbol", {}).get(SYMBOL)
        for s in by_type["settlement"]
        if s.get("trader") == "KEVD"
    ]
    settle_pnls_A = [x for x in settle_pnls_A if x is not None]

    total_qty = maker_qty + taker_qty
    return {
        "label": label,
        "n_fills_A": len(fills_A),
        "qty_bought": qty_bought,
        "qty_sold": qty_sold,
        "net_position_residual": pos,
        "cash_from_fills": cash,
        "fees": fees,
        "raw_pnl_pre_settle_mark": cash - fees,
        "settlement_pnls_A": settle_pnls_A,
        "settlement_pnl_A_total": sum(settle_pnls_A),
        "settlement_pnl_A_mean": statistics.mean(settle_pnls_A) if settle_pnls_A else float("nan"),
        "settlement_pnl_A_median": statistics.median(settle_pnls_A) if settle_pnls_A else float("nan"),
        "maker_qty": maker_qty,
        "taker_qty": taker_qty,
        "maker_pct": (100.0 * maker_qty / total_qty) if total_qty else float("nan"),
        "taker_pct": (100.0 * taker_qty / total_qty) if total_qty else float("nan"),
        "cp_qty_top": cp_counter.most_common(8),
        "cp_role_top": [(cp, dict(d)) for cp, d in
                         sorted(cp_role.items(), key=lambda kv: -(kv[1]["maker_qty"] + kv[1]["taker_qty"]))[:8]],
    }


# ---------- Q3: reveal jitter & timing ----------

def q3_timing(all_events, label):
    by_type, fills_by_tid, _, trades_sorted = index_events(all_events)
    reveals = by_type["reveal"]

    # use t_ns (probe receive) for interval -- that's what strategy sees
    ts_list = []
    for rev in reveals:
        t = rev.get("t_ns")
        if t is None:
            t = reveal_ts(rev)
        if t is not None:
            ts_list.append(t)
    ts_list.sort()

    intervals_s = [(ts_list[i+1] - ts_list[i]) / 1e9 for i in range(len(ts_list) - 1)]

    # nominal interval from game_state
    nominal = None
    for ev in by_type["game_state"]:
        ri = ev.get("reveal_interval")
        if ri:
            nominal = float(ri)
            break
    if nominal is None and intervals_s:
        nominal = statistics.median(intervals_s)

    abs_jit_ms = [abs((i - nominal) * 1000.0) for i in intervals_s] if nominal else []
    signed_jit_ms = [((i - nominal) * 1000.0) for i in intervals_s] if nominal else []

    # latency: gap between reveal ts (sent_ns) and our first IOC/order ack after reveal
    # (use order_ack with trader=KEVD as proxy for IOC send confirmation)
    our_ack_ts = sorted(
        ev.get("ts_ns")
        for ev in by_type["order_ack"]
        if ev.get("trader") == "KEVD" and ev.get("ts_ns") is not None
    )
    first_ack_after_reveal_ms = []
    no_ack = 0
    for rev in reveals:
        rts = reveal_ts(rev)
        if rts is None:
            continue
        first = next((t for t in our_ack_ts if rts < t <= rts + WINDOW_NS), None)
        if first is None:
            no_ack += 1
        else:
            first_ack_after_reveal_ms.append((first - rts) / 1e6)

    # gap between reveal (sent_ns) and first trade on A in window (market)
    trades_A = [t for t in trades_sorted if t.get("symbol") == SYMBOL]
    first_trade_after_reveal_ms = []
    for rev in reveals:
        rts = reveal_ts(rev)
        if rts is None:
            continue
        first = next((t["ts_ns"] for t in trades_A if rts < t["ts_ns"] <= rts + WINDOW_NS), None)
        if first is not None:
            first_trade_after_reveal_ms.append((first - rts) / 1e6)

    return {
        "label": label,
        "n_reveals": len(reveals),
        "n_intervals": len(intervals_s),
        "nominal_s": nominal,
        "intervals_mean_s": statistics.mean(intervals_s) if intervals_s else float("nan"),
        "intervals_stdev_s": statistics.pstdev(intervals_s) if len(intervals_s) > 1 else 0.0,
        "intervals_min_s": min(intervals_s) if intervals_s else float("nan"),
        "intervals_max_s": max(intervals_s) if intervals_s else float("nan"),
        "abs_jitter_ms_p50": percentile(abs_jit_ms, 50),
        "abs_jitter_ms_p90": percentile(abs_jit_ms, 90),
        "abs_jitter_ms_p99": percentile(abs_jit_ms, 99),
        "signed_jit_mean_ms": statistics.mean(signed_jit_ms) if signed_jit_ms else float("nan"),
        "first_ack_after_reveal_ms_p50": percentile(first_ack_after_reveal_ms, 50),
        "first_ack_after_reveal_ms_p90": percentile(first_ack_after_reveal_ms, 90),
        "n_reveals_no_ack": no_ack,
        "first_trade_after_reveal_ms_p50": percentile(first_trade_after_reveal_ms, 50),
        "first_trade_after_reveal_ms_p90": percentile(first_trade_after_reveal_ms, 90),
    }


# ---------- main ----------

def load_group(paths, label):
    evs = []
    for p in paths:
        if not Path(p).exists():
            print(f"skip missing {p}")
            continue
        evs.extend(load_events(p))
        print(f"loaded {p.split('/')[-1]}")
    return evs


def print_q1_pair(a, b):
    print("\n" + "=" * 78)
    print(f"Q1 -- REVEAL-RACE WIN RATE ON SYMBOL {SYMBOL} (500ms window after reveal)")
    print("=" * 78)
    fmt = "{:<40}  {:>14}  {:>14}"
    print(fmt.format("metric", "BEFORE", "AFTER"))
    print(fmt.format("-" * 38, "-" * 12, "-" * 12))
    print(fmt.format("reveals total", a["n_reveals"], b["n_reveals"]))
    print(fmt.format("reveals with trade on A", a["n_reveals_with_trade_A"], b["n_reveals_with_trade_A"]))
    print(fmt.format("reveals with NO trade on A", a["n_reveals_no_trade_A"], b["n_reveals_no_trade_A"]))
    print(fmt.format("first trade WAS OURS (count)", a["first_ours"], b["first_ours"]))
    print(fmt.format("first trade was someone else", a["first_other"], b["first_other"]))
    print(fmt.format("OUR win rate %", f"{a['ours_pct']:.1f}", f"{b['ours_pct']:.1f}"))
    print(fmt.format("median delta any-first ms", f"{a['delta_first_any_ms_p50']:.2f}", f"{b['delta_first_any_ms_p50']:.2f}"))
    print(fmt.format("median delta when WE were first ms", f"{a['delta_first_ours_ms_p50']:.2f}", f"{b['delta_first_ours_ms_p50']:.2f}"))
    print(fmt.format("median delta when OTHER first ms", f"{a['delta_first_other_ms_p50']:.2f}", f"{b['delta_first_other_ms_p50']:.2f}"))
    print(fmt.format("median delta first MARKET (other) ms", f"{a['delta_first_market_other_p50']:.2f}", f"{b['delta_first_market_other_p50']:.2f}"))
    print(fmt.format("p90 delta first MARKET (other) ms", f"{a['delta_first_market_other_p90']:.2f}", f"{b['delta_first_market_other_p90']:.2f}"))
    print(f"\n  our liquidity when we were first: BEFORE={a['our_liquidity_when_first']}  AFTER={b['our_liquidity_when_first']}")
    print(f"  aggressor side when OTHER was first: BEFORE={a['aggressor_when_other']}  AFTER={b['aggressor_when_other']}")


def print_q2_pair(a, b):
    print("\n" + "=" * 78)
    print(f"Q2 -- OUR PNL ON SYMBOL {SYMBOL} + FILL MIX")
    print("=" * 78)
    fmt = "{:<42}  {:>14}  {:>14}"
    print(fmt.format("metric", "BEFORE", "AFTER"))
    print(fmt.format("-" * 40, "-" * 12, "-" * 12))
    print(fmt.format("n fills on A", a["n_fills_A"], b["n_fills_A"]))
    print(fmt.format("qty bought / qty sold", f"{a['qty_bought']}/{a['qty_sold']}", f"{b['qty_bought']}/{b['qty_sold']}"))
    print(fmt.format("net residual position", a["net_position_residual"], b["net_position_residual"]))
    print(fmt.format("cash from fills (raw)", f"{a['cash_from_fills']:.1f}", f"{b['cash_from_fills']:.1f}"))
    print(fmt.format("fees paid", f"{a['fees']:.1f}", f"{b['fees']:.1f}"))
    print(fmt.format("settlement n games", len(a["settlement_pnls_A"]), len(b["settlement_pnls_A"])))
    print(fmt.format("settlement total PnL on A", f"{a['settlement_pnl_A_total']:.1f}", f"{b['settlement_pnl_A_total']:.1f}"))
    print(fmt.format("settlement mean PnL/game on A", f"{a['settlement_pnl_A_mean']:.1f}", f"{b['settlement_pnl_A_mean']:.1f}"))
    print(fmt.format("settlement median PnL/game on A", f"{a['settlement_pnl_A_median']:.1f}", f"{b['settlement_pnl_A_median']:.1f}"))
    print(fmt.format("maker qty / taker qty", f"{a['maker_qty']}/{a['taker_qty']}", f"{b['maker_qty']}/{b['taker_qty']}"))
    print(fmt.format("% maker / % taker", f"{a['maker_pct']:.0f}%/{a['taker_pct']:.0f}%", f"{b['maker_pct']:.0f}%/{b['taker_pct']:.0f}%"))
    print(f"\n  per-game PnL list on A: BEFORE={a['settlement_pnls_A']}  AFTER={b['settlement_pnls_A']}")
    print(f"\n  Top counterparties by qty traded (BEFORE):")
    for cp, q in a["cp_qty_top"]:
        print(f"     {cp:8s}  qty={q}")
    print(f"  Top counterparties by qty traded (AFTER):")
    for cp, q in b["cp_qty_top"]:
        print(f"     {cp:8s}  qty={q}")


def print_q3_pair(a, b):
    print("\n" + "=" * 78)
    print("Q3 -- REVEAL INTERVAL JITTER & TIMING")
    print("=" * 78)
    fmt = "{:<44}  {:>14}  {:>14}"
    print(fmt.format("metric", "BEFORE", "AFTER"))
    print(fmt.format("-" * 42, "-" * 12, "-" * 12))
    print(fmt.format("reveals total", a["n_reveals"], b["n_reveals"]))
    print(fmt.format("inter-reveal interval samples", a["n_intervals"], b["n_intervals"]))
    print(fmt.format("nominal reveal interval (s)", f"{a['nominal_s']}", f"{b['nominal_s']}"))
    print(fmt.format("interval mean (s)", f"{a['intervals_mean_s']:.4f}", f"{b['intervals_mean_s']:.4f}"))
    print(fmt.format("interval stdev (s)", f"{a['intervals_stdev_s']:.4f}", f"{b['intervals_stdev_s']:.4f}"))
    print(fmt.format("interval min/max (s)", f"{a['intervals_min_s']:.3f}/{a['intervals_max_s']:.3f}",
                     f"{b['intervals_min_s']:.3f}/{b['intervals_max_s']:.3f}"))
    print(fmt.format("|jitter| p50/p90/p99 (ms)",
                     f"{a['abs_jitter_ms_p50']:.1f}/{a['abs_jitter_ms_p90']:.1f}/{a['abs_jitter_ms_p99']:.1f}",
                     f"{b['abs_jitter_ms_p50']:.1f}/{b['abs_jitter_ms_p90']:.1f}/{b['abs_jitter_ms_p99']:.1f}"))
    print(fmt.format("signed jitter mean (ms)", f"{a['signed_jit_mean_ms']:.1f}", f"{b['signed_jit_mean_ms']:.1f}"))
    print(fmt.format("our first ack after reveal p50 ms", f"{a['first_ack_after_reveal_ms_p50']:.2f}",
                     f"{b['first_ack_after_reveal_ms_p50']:.2f}"))
    print(fmt.format("our first ack after reveal p90 ms", f"{a['first_ack_after_reveal_ms_p90']:.2f}",
                     f"{b['first_ack_after_reveal_ms_p90']:.2f}"))
    print(fmt.format("reveals with no ack in 500ms", a["n_reveals_no_ack"], b["n_reveals_no_ack"]))
    print(fmt.format("first market trade on A after reveal p50 ms",
                     f"{a['first_trade_after_reveal_ms_p50']:.2f}",
                     f"{b['first_trade_after_reveal_ms_p50']:.2f}"))
    print(fmt.format("first market trade on A after reveal p90 ms",
                     f"{a['first_trade_after_reveal_ms_p90']:.2f}",
                     f"{b['first_trade_after_reveal_ms_p90']:.2f}"))
    print("\n  Note: '[FAST #N]', '[FAST-SWEEP]', '[SCHED]', and 'actual_vs_predicted'")
    print("  print telemetry is NOT captured in these JSONL logs (stdout only).")
    print("  Recommendation: pipe stdout through a tee/JSON-wrap so future runs capture it.")


def main():
    print("Loading BEFORE (baseline) logs...")
    before_evs = load_group(BASELINE_LOGS, "BEFORE")
    print("\nLoading AFTER (post-optimization) logs...")
    after_evs = load_group(NEW_LOGS, "AFTER")
    print(f"\nBEFORE events: {len(before_evs)}   AFTER events: {len(after_evs)}\n")

    q1b = q1_reveal_race(before_evs, "BEFORE")
    q1a = q1_reveal_race(after_evs, "AFTER")
    print_q1_pair(q1b, q1a)

    q2b = q2_pnl(before_evs, "BEFORE")
    q2a = q2_pnl(after_evs, "AFTER")
    print_q2_pair(q2b, q2a)

    q3b = q3_timing(before_evs, "BEFORE")
    q3a = q3_timing(after_evs, "AFTER")
    print_q3_pair(q3b, q3a)

    print("\n" + "=" * 78)
    print("Q4 -- PROPOSED OPTIMIZATIONS (see report text -- data-driven discussion)")
    print("=" * 78)


if __name__ == "__main__":
    main()
