#!/usr/bin/env python3
"""
Compare v10 trading logs from before/after infrastructure change.

Pre-change (4 logs):  WS book pushes were NOT fed to strategy cache;
                      no warm-up of urllib3 conn pool.
Post-change (3 logs): on_book_event feeds _book_cache; 6 parallel HTTP
                      conns pre-warmed at __init__.

Records used:
  reveal           — when prediction landed
  fill             — our fills (has liquidity=maker|taker + counterparty)
  quote_fill       — our resting-quote fills (always maker, no
                     counterparty/liquidity fields)
  trade            — public tape (winner's IOC etc.)
  ack              — our order acks
  settlement       — end-of-game pnl
"""
import json
import os
import statistics as stat
from collections import defaultdict, Counter

POST = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_232722.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_232900.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_233037.jsonl",
]
PRE = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_224419.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_224557.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_225715.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v10_20260519_225906.jsonl",
]

MS = 1_000_000  # ns per ms
WINDOW_NS = 200 * MS


def load(path):
    out = []
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def pct(arr, p):
    if not arr:
        return float("nan")
    arr = sorted(arr)
    k = max(0, min(len(arr) - 1, int(round((p / 100.0) * (len(arr) - 1)))))
    return arr[k]


def analyze_one(path):
    recs = load(path)
    reveals = [r for r in recs if r.get("kind") == "reveal"]
    fills = [r for r in recs if r.get("kind") == "fill"]
    qfills = [r for r in recs if r.get("kind") == "quote_fill"]
    trades = [r for r in recs if r.get("kind") == "trade"]
    acks = [r for r in recs if r.get("kind") == "ack"]
    settl = [r for r in recs if r.get("kind") == "settlement"]
    cps = [r for r in recs if r.get("kind") == "counterparty_summary"]

    # PnL
    pnl = settl[0]["pnl"] if settl else None
    fees = settl[0].get("fees") if settl else None

    # Reveal-windowed maker fills (within +/- 200ms)
    reveal_t = sorted(r["t_ns"] for r in reveals)

    def within_window(t):
        for rt in reveal_t:
            if abs(t - rt) <= WINDOW_NS:
                return rt
        return None

    reveals_with_maker_hit = set()
    reveals_with_taker_hit = set()

    # all "our" fills = fill + quote_fill (treat quote_fill as maker)
    our_fills = []
    for r in fills:
        liq = r.get("liquidity", "?")
        our_fills.append((r["t_ns"], liq, r.get("side"), r.get("price"),
                          r.get("qty"), r.get("counterparty", "?"),
                          r.get("order_id"), r.get("fee", 0)))
    for r in qfills:
        # quote_fill: we were the resting quote -> maker, no counterparty
        our_fills.append((r["t_ns"], "maker", r.get("side"), r.get("price"),
                          r.get("qty"), "?", r.get("order_id"), 0))
    our_fills.sort()

    for tns, liq, side, px, qty, cp, oid, fee in our_fills:
        rt = within_window(tns)
        if rt is None:
            continue
        if liq == "maker":
            reveals_with_maker_hit.add(rt)
        elif liq == "taker":
            reveals_with_taker_hit.add(rt)

    # First fill (any) latency post-reveal (next fill within e.g. 2s)
    first_fill_lat_ms = []
    first_taker_lat_ms = []
    for rt in reveal_t:
        ours_after = [f for f in our_fills if f[0] > rt and f[0] - rt < 2_000 * MS]
        if ours_after:
            first_fill_lat_ms.append((ours_after[0][0] - rt) / MS)
        ours_taker_after = [f for f in our_fills if f[0] > rt and f[1] == "taker" and f[0] - rt < 2_000 * MS]
        if ours_taker_after:
            first_taker_lat_ms.append((ours_taker_after[0][0] - rt) / MS)

    # Reveal -> first public trade latency
    trade_t = sorted(t["t_ns"] for t in trades)
    first_trade_lat_ms = []
    for rt in reveal_t:
        nxt = next((t for t in trade_t if t > rt and t - rt < 2_000 * MS), None)
        if nxt is not None:
            first_trade_lat_ms.append((nxt - rt) / MS)

    # Counterparty top list (maker = they hit us; taker = we hit them)
    cp_hits_us = Counter()
    we_snipe = Counter()
    for r in fills:
        cp = r.get("counterparty", "?")
        qty = r.get("qty", 0)
        if r.get("liquidity") == "maker":
            cp_hits_us[cp] += qty
        elif r.get("liquidity") == "taker":
            we_snipe[cp] += qty

    # Volume by role
    maker_qty = sum(r.get("qty", 0) for r in fills if r.get("liquidity") == "maker")
    maker_qty += sum(r.get("qty", 0) for r in qfills)
    taker_qty = sum(r.get("qty", 0) for r in fills if r.get("liquidity") == "taker")

    # Wire RTT: for each filled order_id, find the latest ack t_ns BEFORE its first fill t_ns
    fill_by_oid = defaultdict(list)
    for r in fills:
        fill_by_oid[r["order_id"]].append(r["t_ns"])
    for r in qfills:
        fill_by_oid[r["order_id"]].append(r["t_ns"])

    ack_by_oid = defaultdict(list)
    for r in acks:
        ack_by_oid[r["order_id"]].append(r["t_ns"])

    rtt_ms = []
    rtt_taker_ms = []
    taker_oids = {r["order_id"] for r in fills if r.get("liquidity") == "taker"}
    for oid, ftns_list in fill_by_oid.items():
        first_fill = min(ftns_list)
        relevant_acks = [a for a in ack_by_oid.get(oid, []) if a <= first_fill]
        if relevant_acks:
            gap = (first_fill - max(relevant_acks)) / MS
            rtt_ms.append(gap)
            if oid in taker_oids:
                rtt_taker_ms.append(gap)

    # taker-snipe details
    taker_details = []
    for r in fills:
        if r.get("liquidity") == "taker":
            taker_details.append((r["t_ns"], r.get("side"), r.get("price"),
                                  r.get("qty"), r.get("counterparty", "?")))

    return {
        "path": path,
        "name": os.path.basename(path),
        "pnl": pnl,
        "fees": fees,
        "n_reveals": len(reveals),
        "n_fills": len(fills),
        "n_quote_fills": len(qfills),
        "n_acks": len(acks),
        "n_trades": len(trades),
        "reveals_with_maker_hit": len(reveals_with_maker_hit),
        "reveals_with_taker_hit": len(reveals_with_taker_hit),
        "first_fill_lat_ms": first_fill_lat_ms,
        "first_taker_lat_ms": first_taker_lat_ms,
        "first_trade_lat_ms": first_trade_lat_ms,
        "cp_hits_us": cp_hits_us,
        "we_snipe": we_snipe,
        "maker_qty": maker_qty,
        "taker_qty": taker_qty,
        "rtt_ms": rtt_ms,
        "rtt_taker_ms": rtt_taker_ms,
        "taker_details": taker_details,
        "cp_summary": cps[0]["stats"] if cps else None,
        "sample_taker_fills": [r for r in fills if r.get("liquidity") == "taker"][:3],
        "sample_maker_fills": [r for r in fills if r.get("liquidity") == "maker"][:3],
    }


def summarise(label, results):
    print(f"\n{'='*72}")
    print(f"{label}  ({len(results)} games)")
    print("=" * 72)
    print("Per-game:")
    print(f"  {'file':50s} {'pnl':>6s} {'fees':>6s} {'maker_q':>8s} {'taker_q':>8s}")
    tot_pnl = 0
    tot_fees = 0
    tot_maker = 0
    tot_taker = 0
    tot_reveals = 0
    tot_rev_maker_hit = 0
    tot_rev_taker_hit = 0
    all_first_fill = []
    all_first_taker = []
    all_first_trade = []
    all_rtt = []
    all_rtt_taker = []
    cp_hits_total = Counter()
    we_snipe_total = Counter()
    for r in results:
        pnl = r["pnl"]
        tot_pnl += pnl if pnl is not None else 0
        tot_fees += r["fees"] if r["fees"] else 0
        tot_maker += r["maker_qty"]
        tot_taker += r["taker_qty"]
        tot_reveals += r["n_reveals"]
        tot_rev_maker_hit += r["reveals_with_maker_hit"]
        tot_rev_taker_hit += r["reveals_with_taker_hit"]
        all_first_fill += r["first_fill_lat_ms"]
        all_first_taker += r["first_taker_lat_ms"]
        all_first_trade += r["first_trade_lat_ms"]
        all_rtt += r["rtt_ms"]
        all_rtt_taker += r["rtt_taker_ms"]
        cp_hits_total += r["cp_hits_us"]
        we_snipe_total += r["we_snipe"]
        print(f"  {r['name']:50s} {pnl:>6} {r['fees']:>6} {r['maker_qty']:>8} {r['taker_qty']:>8}")

    n = len(results)
    print()
    print(f"  TOTAL pnl:           {tot_pnl}   avg/game: {tot_pnl/n:.2f}")
    print(f"  TOTAL fees:          {tot_fees:.1f}")
    print(f"  TOTAL maker qty:     {tot_maker}    TOTAL taker qty: {tot_taker}")
    print(f"  taker share of vol:  {tot_taker/max(1,tot_taker+tot_maker)*100:.1f}%")
    print()
    print(f"  reveals total:                          {tot_reveals}")
    print(f"  reveals w/ maker hit within +/-200ms:   {tot_rev_maker_hit}  ({tot_rev_maker_hit/max(1,tot_reveals)*100:.1f}%)")
    print(f"  reveals w/ taker hit within +/-200ms:   {tot_rev_taker_hit}  ({tot_rev_taker_hit/max(1,tot_reveals)*100:.1f}%)")
    print()

    def fmt(arr, units="ms"):
        if not arr:
            return f"n=0"
        return (f"n={len(arr):>3d}  p50={pct(arr,50):>7.2f}{units}  "
                f"p90={pct(arr,90):>7.2f}{units}  p99={pct(arr,99):>7.2f}{units}  "
                f"min={min(arr):>6.2f}  max={max(arr):>7.2f}  mean={stat.mean(arr):>7.2f}")

    print(f"  reveal -> our first fill (any liq):  {fmt(all_first_fill)}")
    print(f"  reveal -> our first TAKER fill:      {fmt(all_first_taker)}")
    print(f"  reveal -> first public TRADE:        {fmt(all_first_trade)}")
    print()
    print(f"  wire RTT (ack -> fill, all):         {fmt(all_rtt)}")
    print(f"  wire RTT (ack -> fill, takers only): {fmt(all_rtt_taker)}")
    print()
    print(f"  Counterparties hitting us (maker fills, qty):")
    for cp, q in cp_hits_total.most_common(10):
        print(f"      {cp:8s} {q}")
    print(f"  Counterparties we snipe (taker fills, qty):")
    for cp, q in we_snipe_total.most_common(10):
        print(f"      {cp:8s} {q}")

    return {
        "tot_pnl": tot_pnl,
        "tot_fees": tot_fees,
        "tot_maker": tot_maker,
        "tot_taker": tot_taker,
        "tot_reveals": tot_reveals,
        "rev_maker_hit": tot_rev_maker_hit,
        "rev_taker_hit": tot_rev_taker_hit,
        "first_fill": all_first_fill,
        "first_taker": all_first_taker,
        "first_trade": all_first_trade,
        "rtt": all_rtt,
        "rtt_taker": all_rtt_taker,
    }


def main():
    pre_results = [analyze_one(p) for p in PRE]
    post_results = [analyze_one(p) for p in POST]

    pre = summarise("PRE-CHANGE", pre_results)
    post = summarise("POST-CHANGE", post_results)

    print()
    print("=" * 72)
    print("DELTAS  (post - pre)")
    print("=" * 72)

    pre_n = len(pre_results)
    post_n = len(post_results)
    print(f"  avg pnl/game:      pre={pre['tot_pnl']/pre_n:>7.2f}  post={post['tot_pnl']/post_n:>7.2f}  "
          f"delta={post['tot_pnl']/post_n - pre['tot_pnl']/pre_n:+.2f}")
    print(f"  taker vol share:   pre={pre['tot_taker']/max(1,pre['tot_taker']+pre['tot_maker'])*100:>6.2f}%  "
          f"post={post['tot_taker']/max(1,post['tot_taker']+post['tot_maker'])*100:>6.2f}%")
    print(f"  maker-hit-rate:    pre={pre['rev_maker_hit']/max(1,pre['tot_reveals'])*100:>6.2f}%  "
          f"post={post['rev_maker_hit']/max(1,post['tot_reveals'])*100:>6.2f}%")
    print(f"  taker-hit-rate:    pre={pre['rev_taker_hit']/max(1,pre['tot_reveals'])*100:>6.2f}%  "
          f"post={post['rev_taker_hit']/max(1,post['tot_reveals'])*100:>6.2f}%")

    def med(arr):
        return pct(arr, 50) if arr else float("nan")
    print(f"  reveal->our first fill p50:   pre={med(pre['first_fill']):>7.2f}ms  "
          f"post={med(post['first_fill']):>7.2f}ms  delta={med(post['first_fill'])-med(pre['first_fill']):+.2f}ms")
    print(f"  reveal->our first taker p50:  pre={med(pre['first_taker']):>7.2f}ms  "
          f"post={med(post['first_taker']):>7.2f}ms  delta={med(post['first_taker'])-med(pre['first_taker']):+.2f}ms")
    print(f"  reveal->first public trade p50: pre={med(pre['first_trade']):>7.2f}ms  "
          f"post={med(post['first_trade']):>7.2f}ms  delta={med(post['first_trade'])-med(pre['first_trade']):+.2f}ms")
    print(f"  wire RTT p50:                 pre={med(pre['rtt']):>7.2f}ms  post={med(post['rtt']):>7.2f}ms  "
          f"delta={med(post['rtt'])-med(pre['rtt']):+.2f}ms")
    print(f"  wire RTT taker p50:           pre={med(pre['rtt_taker']):>7.2f}ms  "
          f"post={med(post['rtt_taker']):>7.2f}ms  delta={med(post['rtt_taker'])-med(pre['rtt_taker']):+.2f}ms")

    # Sample fills for verification
    print()
    print("=" * 72)
    print("SAMPLE RECORDS (for jq verification)")
    print("=" * 72)
    for label, results in [("PRE", pre_results), ("POST", post_results)]:
        if not results:
            continue
        r = results[0]
        print(f"  [{label}] first log: {r['name']}")
        for kind, samples in [("taker fills", r["sample_taker_fills"]),
                              ("maker fills", r["sample_maker_fills"])]:
            print(f"    sample {kind}:")
            for s in samples:
                print(f"      t_ns={s['t_ns']} side={s['side']} px={s['price']} "
                      f"qty={s['qty']} cp={s.get('counterparty')} liq={s['liquidity']} "
                      f"oid={s['order_id']}")

    # Per-game taker snipe details for post-change (to show snipe quality)
    print()
    print("  POST-change taker snipes per game (proxy for 'snipes won'):")
    for r in post_results:
        n = len(r["taker_details"])
        print(f"    {r['name']}: {n} taker fills")
        for tns, side, px, qty, cp in r["taker_details"][:6]:
            print(f"        t_ns={tns} {side} {qty}@{px} cp={cp}")
    print("  PRE-change taker snipes per game:")
    for r in pre_results:
        n = len(r["taker_details"])
        print(f"    {r['name']}: {n} taker fills")


if __name__ == "__main__":
    main()
