#!/usr/bin/env python3
"""
Analyze reveal-race latency and behaviour for v10 strategy.

Run:
    python3 day1/analyze_reveal_race_v3.py [LOGFILE ...]

If no logs are given the script analyses the two newest v10 combined logs.

Background
----------
On each reveal we (KEVD) want to lift cheap asks / hit rich bids that have
not yet been pulled by their owners ("stragglers"). The public tape (`trade`
events) does NOT name parties; only events about OUR OWN orders contain a
counterparty. Therefore we can measure precisely:
    - the latency from reveal -> first event we participate in
    - whether OUR taker IOC fired before someone else's IOC hit us as maker
    - cancel rates by symbol around a reveal
We cannot derive competitors' reveal->first-trade latency from this tape
because trade events do not carry trader IDs (only aggressor side). What we
CAN do is bucket the public trades by aggressor side and time-bucket, and
identify the named counterparties for every trade that involves us via the
`fill` events (which carry `counterparty`).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict


DEFAULT_LOGS = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_222654.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_222449.jsonl",
]

OUR_TRADER = "KEVD"


def load(path):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def percentile(xs, p):
    if not xs:
        return None
    xs2 = sorted(xs)
    k = (len(xs2) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs2) - 1)
    if f == c:
        return xs2[f]
    return xs2[f] + (xs2[c] - xs2[f]) * (k - f)


def fmt(x, suffix="ms"):
    if x is None:
        return "  n/a "
    return f"{x:7.1f}{suffix}"


def analyse(path):
    print("=" * 88)
    print(f"FILE: {path}")
    print("=" * 88)
    events = load(path)

    # Settlement positions/PnL
    settlements = [e for e in events if e.get("kind") == "settlement"]
    cp_summary = [e for e in events if e.get("kind") == "counterparty_summary"]

    # Reveals (sent_ns is the exchange-time anchor)
    reveals = [e for e in events if e.get("kind") == "reveal"]
    # Game break: when index resets to 1, that's a new game
    games = []
    cur = []
    for r in reveals:
        if cur and r["index"] == 1:
            games.append(cur)
            cur = []
        cur.append(r)
    if cur:
        games.append(cur)
    print(f"Events: {len(events):,}   Reveals: {len(reveals)}   Games: {len(games)}")

    # ----- 1. Reveal-to-first-trade latency -----
    print()
    print("1. REVEAL-TO-FIRST-TRADE LATENCY")
    print("-" * 88)
    print("  We have:")
    print("   * `trade` events (public tape, no trader IDs, only aggressor side)")
    print("   * `fill` events (OUR fills, with `counterparty` + `liquidity`)")
    print("   * `quote_fill` events (OUR maker fills, no counterparty)")
    print("  -> First trade WE participated in = first (fill OR quote_fill) after reveal")
    print("  -> First trade in market = first `trade` event after reveal")
    print("  -> First IOC WE fired (taker) = first `fill` with liquidity='taker' after reveal")
    print()

    rows = []  # (reveal_idx, first_trade_dt, our_first_dt, our_first_kind, taker_dt, who_won)
    our_first_dts = []
    market_first_dts = []
    our_taker_dts = []

    for ri, r in enumerate(reveals):
        rn = r["sent_ns"]
        first_trade_dt = None
        first_trade = None
        our_first_dt = None
        our_first_kind = None
        first_taker_dt = None
        # We treat sent_ns as exchange-stamp. Anything with sent_ns >= rn.
        for e in events:
            sn = e.get("sent_ns")
            if sn is None or sn < rn:
                continue
            dt = (sn - rn) / 1e6  # ms
            k = e.get("kind")
            if k == "trade" and first_trade_dt is None:
                first_trade_dt = dt
                first_trade = e
            if k in ("fill", "quote_fill") and our_first_dt is None:
                our_first_dt = dt
                our_first_kind = k
            if k == "fill" and e.get("liquidity") == "taker" and first_taker_dt is None:
                first_taker_dt = dt
            if first_trade_dt is not None and our_first_dt is not None and first_taker_dt is not None:
                break
        # cap at 5s for sanity (next reveal is 15s away)
        within_window = lambda x: x is not None and x <= 5000
        rows.append({
            "idx": ri + 1,
            "global": f"g?/r{r['index']}",
            "first_trade_dt": first_trade_dt,
            "our_first_dt": our_first_dt,
            "our_first_kind": our_first_kind,
            "first_taker_dt": first_taker_dt,
            "first_trade_agg": first_trade.get("aggressor") if first_trade else None,
        })
        if within_window(our_first_dt):
            our_first_dts.append(our_first_dt)
        if within_window(first_trade_dt):
            market_first_dts.append(first_trade_dt)
        if first_taker_dt is not None and first_taker_dt <= 5000:
            our_taker_dts.append(first_taker_dt)

    print("  Per-reveal table (ms after reveal):")
    print("    rev  market_first  our_first(kind)   our_first_IOC   first_agg")
    for r in rows:
        market = fmt(r["first_trade_dt"])
        ours = fmt(r["our_first_dt"])
        kind = (r["our_first_kind"] or "-")[:10]
        taker = fmt(r["first_taker_dt"])
        agg = r["first_trade_agg"] or "-"
        print(f"    {r['idx']:>3}  {market}   {ours} ({kind:10})  {taker}     {agg}")
    print()
    print("  Aggregate (reveals where event happened within 5s of reveal):")
    print(f"    market first-trade latency  p50={fmt(percentile(market_first_dts,50))}  p90={fmt(percentile(market_first_dts,90))}  n={len(market_first_dts)}")
    print(f"    OUR first-event latency     p50={fmt(percentile(our_first_dts,50))}  p90={fmt(percentile(our_first_dts,90))}  n={len(our_first_dts)}")
    print(f"    OUR first IOC (taker) lat   p50={fmt(percentile(our_taker_dts,50))}  p90={fmt(percentile(our_taker_dts,90))}  n={len(our_taker_dts)}")
    # Note: when first_trade and our_first are equal (same sent_ns), we tied / participated
    same_count = sum(1 for r in rows
                     if r["first_trade_dt"] is not None and r["our_first_dt"] is not None
                     and abs(r["first_trade_dt"] - r["our_first_dt"]) < 0.01)
    print(f"    reveals where market-first-trade == ours (we participated): {same_count}/{len(rows)}")

    # ----- 2. Win rate at reveals -----
    print()
    print("2. WIN RATE AT REVEALS")
    print("-" * 88)
    print("  Definition: who was the AGGRESSOR on the first trade within 1s after reveal?")
    print("  We define 'we won' as: the first trade was a `fill` with liquidity='taker'")
    print("  (i.e. our IOC reached the book first). 'We were hit' means the first trade")
    print("  was a `quote_fill` on us (someone else's IOC was first).")
    print()
    wins_us_taker = 0
    we_were_maker = 0
    not_us = 0
    no_trade = 0
    for r in rows:
        if r["first_trade_dt"] is None or r["first_trade_dt"] > 1000:
            no_trade += 1
            continue
        # Did we participate?
        if r["our_first_dt"] is not None and abs(r["our_first_dt"] - r["first_trade_dt"]) < 1.0:
            # we were in first trade; were we taker (IOC) or maker (got hit)?
            if r["our_first_kind"] == "fill":
                wins_us_taker += 1
            else:
                we_were_maker += 1
        else:
            not_us += 1
    n = len(rows)
    pct = lambda x: 100.0 * x / n if n else 0.0
    print(f"  Reveals total              : {n}")
    print(f"  We fired first IOC (won)   : {wins_us_taker:>3}  ({pct(wins_us_taker):.1f}%)")
    print(f"  We were hit first (lost)   : {we_were_maker:>3}  ({pct(we_were_maker):.1f}%)")
    print(f"  Someone else trades first  : {not_us:>3}  ({pct(not_us):.1f}%)")
    print(f"  No trade within 1s         : {no_trade:>3}  ({pct(no_trade):.1f}%)")

    # ----- 3. Stragglers exploited -----
    print()
    print("3. STRAGGLERS EXPLOITED  (our taker IOCs within 300ms of reveal)")
    print("-" * 88)
    taker_per_reveal = []
    taker_lots_per_reveal = []
    cp_taker_counts = Counter()
    for r in reveals:
        rn = r["sent_ns"]
        n_fills = 0
        lots = 0
        for e in events:
            sn = e.get("sent_ns")
            if sn is None:
                continue
            dt = (sn - rn) / 1e6
            if dt < 0 or dt > 300:
                continue
            if e.get("kind") == "fill" and e.get("liquidity") == "taker":
                n_fills += 1
                lots += e.get("qty", 0)
                cp_taker_counts[e.get("counterparty", "?")] += e.get("qty", 0)
        taker_per_reveal.append(n_fills)
        taker_lots_per_reveal.append(lots)
    print(f"  Reveals with >=1 taker IOC in 300ms: {sum(1 for x in taker_per_reveal if x>0)}/{len(reveals)}")
    print(f"  Taker fills per reveal: mean={statistics.mean(taker_per_reveal):.2f}  max={max(taker_per_reveal)}")
    print(f"  Taker lots per reveal : mean={statistics.mean(taker_lots_per_reveal):.2f}  max={max(taker_lots_per_reveal)}  total={sum(taker_lots_per_reveal)}")
    if cp_taker_counts:
        print("  Stragglers we lifted/hit (counterparty -> lots):")
        for cp, lots in cp_taker_counts.most_common():
            print(f"    {cp:<8} {lots} lots")
    # Were there stragglers inside our lift_to/hit_to bounds? Use book_after_reveal.
    bar = [e for e in events if e.get("kind") == "book_after_reveal"]
    print(f"  book_after_reveal snapshots: {len(bar)}")
    # Show first 3 BARs as evidence
    print("  Top-of-book within 1 tick of expected post-reveal mid (first 3 reveals):")
    for i, snap in enumerate(bar[:3]):
        bids = snap.get("bids") or []
        asks = snap.get("asks") or []
        bid_str = ", ".join(f"{b['price']}x{b['qty']}" for b in bids[:3])
        ask_str = ", ".join(f"{a['price']}x{a['qty']}" for a in asks[:3])
        print(f"    snap{i+1}  bids[{bid_str}]  asks[{ask_str}]")

    # ----- 4. Pre-reveal cancel rush -----
    print()
    print("4. PRE-REVEAL CANCEL RUSH (-600ms to 0 around reveal)")
    print("-" * 88)
    # We can only see OUR cancel acks. Look at quote_cancel events with reason='user'
    # vs reason='tick_settle' (latter is the exchange auto-cancelling).
    our_user_cancels = []
    our_first_cancel_dt = []
    tick_settle_cancels = []
    cancel_reasons_by_window = defaultdict(Counter)
    for r in reveals:
        rn = r["sent_ns"]
        first = None
        n_user = 0
        n_settle = 0
        for e in events:
            sn = e.get("sent_ns")
            if sn is None:
                continue
            dt = (sn - rn) / 1e6
            if e.get("kind") == "quote_cancel":
                reason = e.get("reason", "?")
                if -600 <= dt <= 0:
                    if reason == "user":
                        n_user += 1
                        if first is None:
                            first = dt
                    elif reason == "tick_settle":
                        n_settle += 1
                    cancel_reasons_by_window[reason]["pre"] += 1
                if 0 <= dt <= 600:
                    cancel_reasons_by_window[reason]["post"] += 1
        our_user_cancels.append(n_user)
        if first is not None:
            our_first_cancel_dt.append(first)
        tick_settle_cancels.append(n_settle)

    print("  In the 600ms BEFORE each reveal:")
    print(f"    our `user` cancels per reveal : mean={statistics.mean(our_user_cancels):.1f}  max={max(our_user_cancels)}")
    if our_first_cancel_dt:
        print(f"    our FIRST `user` cancel time  : p50={percentile(our_first_cancel_dt,50):.0f}ms  p90={percentile(our_first_cancel_dt,10):.0f}ms (most-negative = earliest)")
        print(f"    earliest first cancel observed: {min(our_first_cancel_dt):.0f}ms")
        print(f"    latest   first cancel observed: {max(our_first_cancel_dt):.0f}ms")
    print(f"    `tick_settle` cancels per rev : mean={statistics.mean(tick_settle_cancels):.1f}  (these are post-reveal server auto-cancels)")
    print()
    print("  Cancel reason totals (-600..0 'pre' vs 0..+600 'post'):")
    for reason, cnt in cancel_reasons_by_window.items():
        print(f"    reason={reason:<14} pre={cnt['pre']:>4}  post={cnt['post']:>4}")
    print()
    print("  Note: we cannot see COMPETITORS' cancels in this tape (the venue only")
    print("  echoes back our own user cancels). Indirect signal: surviving stragglers")
    print("  at T+0 (see section 3) gives a rough proxy.")

    # ----- 5. PnL summary -----
    print()
    print("5. PNL SUMMARY")
    print("-" * 88)
    for s in settlements:
        print(f"  trader={s.get('trader')} pos_A={s.get('positions',{}).get('A')} "
              f"cash_A={s.get('cash',{}).get('A')} fees={s.get('fees')} "
              f"pnl={s.get('pnl')} pnl_by={s.get('pnl_by_symbol')} trades={s.get('trades')}")
    # counterparty_summary tells us avg_price & qty against named bots
    print()
    if cp_summary:
        for s in cp_summary:
            print("  Per-counterparty stats (from our fills only):")
            for cp, st in s.get("stats", {}).items():
                print(f"    {cp:<8} fills={st['n_fills']:>3}  bought={st['qty_we_bought']:>3}  sold={st['qty_we_sold']:>3}  "
                      f"maker={st['our_role_maker']:>3}  taker={st['our_role_taker']:>3}  avg_px={st['avg_price']:.2f}")

    # ----- 6. Rate-limit / infra signals -----
    print()
    print("6. INFRA / RATE-LIMIT DIAGNOSTICS")
    print("-" * 88)
    # rejects?
    rejs = [e for e in events if e.get("kind") == "reject" or e.get("type") == "reject"]
    print(f"  reject events: {len(rejs)}")
    for rj in rejs[:5]:
        print(f"    {rj}")
    # Rate of outgoing actions per second by us
    # Use ack and quote_cancel events as proxies for outgoing requests we sent
    out_times = []
    for e in events:
        if e.get("kind") in ("ack", "modify_ack", "cancel_ack"):
            out_times.append(e.get("sent_ns"))
    out_times = [t for t in out_times if t is not None]
    out_times.sort()
    # peak 1-second window
    peak = 0
    j = 0
    for i, t in enumerate(out_times):
        while out_times[j] < t - 1_000_000_000:
            j += 1
        peak = max(peak, i - j + 1)
    print(f"  Peak ack rate (acks/sec): {peak}  (rate limit advertised: 20/s)")

    # Look at lag: t_ns (local receive) - sent_ns (exchange stamp) for trades — wire delay
    lags = []
    for e in events:
        if e.get("kind") == "trade":
            t = e.get("t_ns"); s = e.get("sent_ns")
            if t and s:
                lags.append((t - s)/1e6)
    if lags:
        print(f"  Public tape wire lag (t_ns - sent_ns): p50={percentile(lags,50):.1f}ms  p90={percentile(lags,90):.1f}ms  p99={percentile(lags,99):.1f}ms")
    # Same for ack
    acklag = []
    for e in events:
        if e.get("kind") == "ack":
            t = e.get("t_ns"); s = e.get("sent_ns")
            if t and s:
                acklag.append((t - s)/1e6)
    if acklag:
        print(f"  Our ack lag (t_ns - sent_ns):           p50={percentile(acklag,50):.1f}ms  p90={percentile(acklag,90):.1f}ms  p99={percentile(acklag,99):.1f}ms")

    return {
        "n_reveals": len(reveals),
        "our_first_p50": percentile(our_first_dts, 50),
        "our_first_p90": percentile(our_first_dts, 90),
        "our_taker_p50": percentile(our_taker_dts, 50),
        "our_taker_p90": percentile(our_taker_dts, 90),
        "wins_us_taker": wins_us_taker,
        "we_were_maker": we_were_maker,
        "not_us": not_us,
        "no_trade": no_trade,
        "taker_lots_total": sum(taker_lots_per_reveal),
        "rows": rows,
    }


def main(paths):
    results = []
    for p in paths:
        if not os.path.exists(p):
            print(f"!! missing: {p}")
            continue
        results.append((p, analyse(p)))
        print()
    if len(results) > 1:
        print("=" * 88)
        print("CROSS-LOG SUMMARY")
        print("=" * 88)
        for p, r in results:
            print(f"  {os.path.basename(p)}: n_reveals={r['n_reveals']:>2}  "
                  f"our_first p50={fmt(r['our_first_p50'])}/p90={fmt(r['our_first_p90'])}  "
                  f"IOC p50={fmt(r['our_taker_p50'])}/p90={fmt(r['our_taker_p90'])}  "
                  f"wins(taker)={r['wins_us_taker']}  hit-as-maker={r['we_were_maker']}  "
                  f"no-trade={r['no_trade']}  taker_lots_total={r['taker_lots_total']}")


if __name__ == "__main__":
    paths = sys.argv[1:] or DEFAULT_LOGS
    main(paths)
