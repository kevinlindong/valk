"""day1/analyze_diag.py

Offline analyzer for diagnose_latency.py JSONL logs.

Run:
    python3 day1/analyze_diag.py day1/logs/diag_YYYYMMDD_HHMMSS.jsonl
    python3 day1/analyze_diag.py day1/logs/diag_a.jsonl day1/logs/diag_b.jsonl

Produces sections (A)-(I) as described in the diag spec:
  A. REST RTT per endpoint (p50/p90/p99/max + >100ms outliers w/ context)
  B. WS skew_us per type (median, tails, stability + min => clock offset)
  C. WS inter-arrival gap distribution + >50ms silences
  D. reveal -> first-trade and reveal -> first-book latency per reveal
  E. WS events in +/- 1s around each reveal, by type
  F. REST RTT samples within +/- 1s of any reveal vs steady state
  G. order_probe RTT (or note that it wasn't on)
  H. Keep-alive efficiency: first REST call vs steady state per endpoint
  I. Token bucket: any moment tokens_left < 2

Everything operates on a single JSONL file at a time.
"""
from __future__ import annotations

import json
import sys
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


def quantiles(xs):
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs),
        "p50": pct(xs, 0.5),
        "p90": pct(xs, 0.9),
        "p99": pct(xs, 0.99),
        "max": max(xs),
        "mean": int(statistics.mean(xs)),
    }


def fmt_q(q):
    if not q or q.get("n", 0) == 0:
        return "n=0"
    return (f"n={q['n']:>4d} min={q['min']:>6d} p50={q['p50']:>6d} "
            f"p90={q['p90']:>7d} p99={q['p99']:>7d} max={q['max']:>7d} "
            f"mean={q['mean']:>6d}")


def wall(ns):
    return datetime.fromtimestamp(ns / 1e9).strftime("%H:%M:%S.%f")[:-3]


def load(path):
    recs = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return recs


# ---------------------------------------------------------------------------
# Section runners
# ---------------------------------------------------------------------------
def section_a_rest(recs):
    """REST RTT per endpoint + outliers."""
    print("=" * 72)
    print("(A) REST RTT distribution per endpoint")
    print("=" * 72)
    by_ep = defaultdict(list)
    raw_by_ep = defaultdict(list)  # keep (rtt, rec) for outlier hunt
    for r in recs:
        k = r.get("kind", "")
        if not k.startswith("rest_"):
            continue
        ep = k[len("rest_"):]
        rtt = r.get("rtt_us")
        if rtt is None:
            continue
        by_ep[ep].append(rtt)
        raw_by_ep[ep].append((rtt, r))

    for ep in sorted(by_ep):
        q = quantiles(by_ep[ep])
        print(f"  {ep:>13s}  {fmt_q(q)}")

    # outliers > 100 ms
    print()
    print("  Outliers (rtt > 100 ms):")
    reveals = [r for r in recs if r.get("kind") == "ws_reveal"]
    reveal_ts = [r["t_ns"] for r in reveals]

    def quote_traffic_around(t_ns, win_ns=200_000_000):
        c = 0
        for r in recs:
            if r.get("kind") not in ("ws_quote_add", "ws_quote_cancel",
                                     "ws_quote_modify"):
                continue
            if abs(r["t_ns"] - t_ns) <= win_ns:
                c += 1
        return c

    found = False
    for ep, lst in raw_by_ep.items():
        for rtt, r in lst:
            if rtt <= 100_000:
                continue
            found = True
            t = r["t_ns"]
            near_reveal_ms = None
            if reveal_ts:
                near = min(reveal_ts, key=lambda x: abs(x - t))
                near_reveal_ms = (t - near) / 1e6
            qtraf = quote_traffic_around(t)
            print(f"    {ep:>13s}  rtt={rtt:>7d}us  wall={wall(t)}  "
                  f"tokens={r.get('tokens_left')}  "
                  f"near_reveal={near_reveal_ms:+.1f}ms  "
                  f"quote_msgs_in_+-200ms={qtraf}")
    if not found:
        print("    (none)")
    print()


def section_b_skew(recs):
    """WS skew_us per message type."""
    print("=" * 72)
    print("(B) WS clock skew (skew_us = t_recv - server_sent), per type")
    print("=" * 72)
    by_type = defaultdict(list)
    all_skew = []
    for r in recs:
        k = r.get("kind", "")
        if not k.startswith("ws_"):
            continue
        sk = r.get("skew_us")
        if sk is None:
            continue
        etype = r.get("etype") or k[len("ws_"):]
        by_type[etype].append(sk)
        all_skew.append((sk, r))

    for et in sorted(by_type):
        q = quantiles(by_type[et])
        print(f"  {et:>15s}  {fmt_q(q)}")

    skew_vals = [s for s, _ in all_skew]
    overall = quantiles(skew_vals)
    print(f"\n  OVERALL          {fmt_q(overall)}")
    if skew_vals:
        # min approximates clock-offset; subtract to estimate one-way net latency
        clk = overall["min"]
        net_p50 = overall["p50"] - clk
        net_p90 = overall["p90"] - clk
        net_p99 = overall["p99"] - clk
        print(f"  est clock_offset ~= min(skew_us) = {clk} us")
        print(f"  est one-way net latency  p50~{net_p50}us  p90~{net_p90}us  "
              f"p99~{net_p99}us  (skew - clock_offset)")

        # drift check: is skew monotonic over time?
        # split into thirds, compare p50
        n = len(all_skew)
        s = sorted(all_skew, key=lambda x: x[1]["t_perf_ns"])
        thirds = [s[: n // 3], s[n // 3: 2 * n // 3], s[2 * n // 3:]]
        med = [pct([x[0] for x in t], 0.5) for t in thirds]
        print(f"  skew p50 by time-third: {med[0]}us -> {med[1]}us -> "
              f"{med[2]}us  "
              f"(drift over session: {med[2] - med[0]:+d}us)")
    print()


def section_c_gap(recs):
    """WS inter-arrival gap + >50ms silences."""
    print("=" * 72)
    print("(C) WS inter-arrival gap distribution")
    print("=" * 72)
    ws = [r for r in recs if r.get("kind", "").startswith("ws_")
          and r.get("gap_us") is not None]
    gaps = [r["gap_us"] for r in ws]
    print(f"  ALL WS frames   {fmt_q(quantiles(gaps))}")

    silences = [(g, r) for g, r in
                ((r["gap_us"], r) for r in ws) if g > 50_000]
    silences.sort(key=lambda x: -x[0])
    print(f"\n  Gaps > 50 ms ({len(silences)} total)"
          f"{'  -- top 10 by size:' if silences else ':'}")
    for g, r in silences[:10]:
        print(f"    gap={g:>7d}us={g/1000:>6.1f}ms  "
              f"wall={wall(r['t_ns'])}  etype={r.get('etype')}  "
              f"kind={r['kind']}")
    print()


def section_d_reveal_to_tape(recs):
    """reveal -> first-trade and reveal -> first-book latency per reveal."""
    print("=" * 72)
    print("(D) Reveal -> first-trade / first-book latency")
    print("=" * 72)
    reveals = [r for r in recs if r.get("kind") == "ws_reveal"]
    reveals.sort(key=lambda r: r["t_perf_ns"])

    # build maps from reveal_index -> latency on the trade/book records
    first_trade = {}
    first_book = {}
    for r in recs:
        if r.get("kind") == "ws_trade" and "reveal_to_trade_us" in r:
            idx = r.get("reveal_index")
            if idx not in first_trade:
                first_trade[idx] = (r["reveal_to_trade_us"], r)
        if r.get("kind") == "ws_book" and "reveal_to_book_us" in r:
            idx = r.get("reveal_index")
            if idx not in first_book:
                first_book[idx] = (r["reveal_to_book_us"], r)

    print(f"  {'idx':>4s}  {'value':>6s}  {'r2trade_us':>11s}  "
          f"{'r2book_us':>11s}  reveal_wall")
    rt_us = []
    rb_us = []
    for r in reveals:
        idx = (r.get("payload") or {}).get("index")
        value = (r.get("payload") or {}).get("value")
        rt = first_trade.get(idx, (None, None))[0]
        rb = first_book.get(idx, (None, None))[0]
        if rt is not None:
            rt_us.append(rt)
        if rb is not None:
            rb_us.append(rb)
        print(f"  {str(idx):>4s}  {str(value):>6s}  "
              f"{str(rt):>11s}  {str(rb):>11s}  {wall(r['t_ns'])}")
    print(f"\n  reveal->first-trade  {fmt_q(quantiles(rt_us))}")
    print(f"  reveal->first-book   {fmt_q(quantiles(rb_us))}")
    print()


def section_e_around_reveal(recs):
    """+/- 1s WS event counts around each reveal, by type."""
    print("=" * 72)
    print("(E) WS event counts in +/- 1s around each reveal")
    print("=" * 72)
    reveals = [r for r in recs if r.get("kind") == "ws_reveal"]
    reveals.sort(key=lambda r: r["t_perf_ns"])
    ws_recs = [r for r in recs if r.get("kind", "").startswith("ws_")]

    WIN = 1_000_000_000  # 1 s in ns (perf monotonic)
    for r in reveals:
        idx = (r.get("payload") or {}).get("index")
        t0 = r["t_perf_ns"]
        before = Counter()
        after = Counter()
        for w in ws_recs:
            dt = w["t_perf_ns"] - t0
            if -WIN <= dt < 0:
                before[w.get("etype") or w.get("kind")] += 1
            elif 0 < dt <= WIN:
                after[w.get("etype") or w.get("kind")] += 1
        keys = sorted(set(before) | set(after))
        print(f"  reveal idx={idx}  wall={wall(r['t_ns'])}")
        for k in keys:
            print(f"      {k:>15s}  before={before[k]:>4d}  "
                  f"after={after[k]:>4d}")
    print()


def section_f_rest_during_reveal(recs):
    """REST RTT in +/- 1s of reveals vs steady state."""
    print("=" * 72)
    print("(F) REST RTT during reveal storms (+/-1s) vs steady state")
    print("=" * 72)
    reveals = [r for r in recs if r.get("kind") == "ws_reveal"]
    rev_perf = [r["t_perf_ns"] for r in reveals]

    WIN = 1_000_000_000  # 1 s

    def near_reveal(t_perf):
        return any(abs(t_perf - r) <= WIN for r in rev_perf)

    rest = [r for r in recs if r.get("kind", "").startswith("rest_")
            and r.get("rtt_us") is not None]
    by_ep = defaultdict(lambda: {"near": [], "far": []})
    for r in rest:
        ep = r["kind"][len("rest_"):]
        bucket = "near" if near_reveal(r["t_perf_ns"]) else "far"
        by_ep[ep][bucket].append(r["rtt_us"])
    for ep in sorted(by_ep):
        n = quantiles(by_ep[ep]["near"])
        f = quantiles(by_ep[ep]["far"])
        print(f"  {ep:>13s}  near-reveal:  {fmt_q(n)}")
        print(f"  {'':>13s}  steady-state: {fmt_q(f)}")
    print()


def section_g_order_probe(recs):
    """Order-probe write-path RTT, and compare to /api/book."""
    print("=" * 72)
    print("(G) Order-probe RTT (write path)")
    print("=" * 72)
    op = [r["rtt_us"] for r in recs if r.get("kind") == "rest_order_probe"
          and r.get("rtt_us") is not None]
    if not op:
        print("  (no rest_order_probe records -- --probe-orders was off)")
        print()
        return
    bk = [r["rtt_us"] for r in recs if r.get("kind") == "rest_book"
          and r.get("rtt_us") is not None]
    print(f"  order_probe   {fmt_q(quantiles(op))}")
    print(f"  rest_book     {fmt_q(quantiles(bk))}")
    if op and bk:
        p50op = pct(op, 0.5)
        p50bk = pct(bk, 0.5)
        print(f"  delta (probe - book) at p50 = {p50op - p50bk} us")
    print()


def section_h_keepalive(recs):
    """First REST call per endpoint vs subsequent calls."""
    print("=" * 72)
    print("(H) Keep-alive efficiency: first call vs steady state")
    print("=" * 72)
    by_ep = defaultdict(list)
    for r in recs:
        k = r.get("kind", "")
        if not k.startswith("rest_"):
            continue
        ep = k[len("rest_"):]
        rtt = r.get("rtt_us")
        if rtt is None:
            continue
        by_ep[ep].append((r["t_perf_ns"], rtt))

    print(f"  {'endpoint':>13s}  {'first_rtt_us':>14s}  {'p50_rest':>10s}  "
          f"{'ratio':>6s}")
    for ep in sorted(by_ep):
        seq = sorted(by_ep[ep])
        if not seq:
            continue
        first = seq[0][1]
        rest_xs = [x[1] for x in seq[1:]]
        p50 = pct(rest_xs, 0.5) if rest_xs else None
        ratio = (first / p50) if p50 else None
        ratio_str = f"{ratio:.2f}x" if ratio is not None else "n/a"
        print(f"  {ep:>13s}  {first:>14d}  "
              f"{(str(p50) if p50 is not None else '-'):>10s}  "
              f"{ratio_str:>6s}")
    # also examine preflight: it precedes the probe loop, may have cold conns
    ss = [r for r in recs if r.get("kind") == "session_start"]
    if ss:
        pf = ss[0].get("preflight") or {}
        if pf:
            print()
            print("  Preflight (cold) RTT vs steady-state probe p50:")
            steady = {ep: pct([x[1] for x in by_ep[ep][1:]], 0.5)
                      for ep in by_ep}
            for name, info in pf.items():
                cold = info.get("rtt_us")
                # map preflight name to probe endpoint key
                key = {"book": "book", "positions": "positions",
                       "game_state": "game"}.get(name)
                s = steady.get(key) if key else None
                ratio = (cold / s) if (s and cold) else None
                ratio_str = f"{ratio:.2f}x" if ratio else "n/a"
                print(f"      {name:>13s}  cold={cold:>7d}us  "
                      f"probe_p50={s}  ratio={ratio_str}")
    print()


def section_i_tokens(recs):
    """Token bucket: any moment tokens_left < 2."""
    print("=" * 72)
    print("(I) Token bucket health (tokens_left < 2)")
    print("=" * 72)
    tokens = [(r["t_ns"], r.get("tokens_left"), r["kind"], r.get("rtt_us"))
              for r in recs
              if r.get("kind", "").startswith("rest_")
              and r.get("tokens_left") is not None]
    if not tokens:
        print("  (no token snapshots logged)")
        print()
        return
    low = [t for t in tokens if t[1] is not None and t[1] < 2]
    minv = min(t[1] for t in tokens if t[1] is not None)
    print(f"  total token snapshots: {len(tokens)}")
    print(f"  min tokens_left observed: {minv:.3f}")
    print(f"  snapshots with tokens_left < 2: {len(low)}")
    for t_ns, tk, kind, rtt in low[:10]:
        print(f"    wall={wall(t_ns)}  tokens={tk:.3f}  kind={kind}  "
              f"rtt={rtt}us")
    print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def analyze(path):
    print()
    print("#" * 72)
    print(f"# Analyzing {path}")
    print(f"#  size={os.path.getsize(path):,} bytes")
    recs = load(path)
    print(f"#  records={len(recs):,}")
    # session window
    if recs:
        t0 = recs[0]["t_ns"]
        t1 = recs[-1]["t_ns"]
        print(f"#  window={wall(t0)} -> {wall(t1)}  "
              f"({(t1 - t0) / 1e9:.1f}s)")
    counts = Counter(r.get("kind", "?") for r in recs)
    print(f"#  kinds={dict(counts)}")
    print("#" * 72)
    section_a_rest(recs)
    section_b_skew(recs)
    section_c_gap(recs)
    section_d_reveal_to_tape(recs)
    section_e_around_reveal(recs)
    section_f_rest_during_reveal(recs)
    section_g_order_probe(recs)
    section_h_keepalive(recs)
    section_i_tokens(recs)


def main():
    if len(sys.argv) < 2:
        print("usage: analyze_diag.py LOG1.jsonl [LOG2.jsonl ...]")
        sys.exit(1)
    for path in sys.argv[1:]:
        analyze(path)


if __name__ == "__main__":
    main()
