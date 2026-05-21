#!/usr/bin/env python3
"""Targeted analysis: did the 0.65s quote-blackout work?

For each log we ask:
  Q1 In window T-0.65s..T+0 (pre-reveal blackout), how many fills did we incur
     where we were the MAKER (quote_fill or fill with liquidity=='maker')?
     Aggregate by counterparty.

  Q1b In window T+0..T+0.3s, how many maker-side fills (hit-bait that survived
      blackout because we did not cancel everything fast enough OR posted in the
      first ms after reveal)?

  Q2 Distribution of `quote_post` / `quote_modify` / `modify_replace` events
     in window T-0.65s..T+0. If the blackout is working, these should be 0.

  Q3 Where do the surviving maker-side fills come from in time? Bucket them in
     50ms bins to see if they cluster at T+0..+50ms (= old quotes that survived
     the pre-cancel because cancels haven't acked yet).

  Q4 Per-log totals: maker-side lots given up vs taker-side lots taken in
     T-0.65..T+0.3 (i.e. net of reveal-race).
"""
from __future__ import annotations
import json, os, sys, statistics
from collections import Counter, defaultdict

OUR = "KEVD"


def load(p):
    out = []
    with open(p) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def percentile(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k); c = min(f+1, len(xs)-1)
    if f == c: return xs[f]
    return xs[f] + (xs[c]-xs[f])*(k-f)


def analyse(path):
    print("="*88)
    print("FILE:", os.path.basename(path))
    print("="*88)
    events = load(path)
    reveals = [e for e in events if e.get("kind")=="reveal"]
    print(f"events={len(events)}  reveals={len(reveals)}")

    # --- 1. Maker-side fills in T-0.65..T+0 (the BLACKOUT WINDOW)
    pre_maker_fills = []   # (rev_idx, dt_ms, qty, counterparty, kind)
    pre_maker_lots = 0
    post0_50_maker_lots = 0
    post50_100_maker_lots = 0
    post100_300_maker_lots = 0
    # bin all maker fills in T-1000..T+1000 by 50ms bins
    bin_lots = defaultdict(int)   # bin_lo_ms -> lots given up as maker
    taker_lots_post = 0
    # quote-posting actions in blackout
    blackout_quote_posts = []   # (rev_idx, dt_ms, side)
    blackout_quote_acks = []    # ack of `place` we sent during blackout
    blackout_modifies = []

    for ri, r in enumerate(reveals):
        rn = r["sent_ns"]
        for e in events:
            sn = e.get("sent_ns")
            if sn is None:
                continue
            dt = (sn - rn)/1e6
            if dt < -700 or dt > 400:
                continue
            k = e.get("kind")
            # Maker-side fills:
            #   - quote_fill (our resting MM quote got eaten)
            #   - fill w/ liquidity=='maker'
            is_maker_fill = False
            qty = e.get("qty", 0) or 0
            cp = e.get("counterparty", "?")
            if k == "quote_fill":
                is_maker_fill = True
            elif k == "fill" and e.get("liquidity")=="maker":
                is_maker_fill = True
            elif k == "fill" and e.get("liquidity")=="taker":
                if 0 <= dt <= 300:
                    taker_lots_post += qty
            if is_maker_fill:
                if -650 <= dt <= 0:
                    pre_maker_fills.append((ri+1, dt, qty, cp, k))
                    pre_maker_lots += qty
                if 0 <= dt < 50:
                    post0_50_maker_lots += qty
                elif 50 <= dt < 100:
                    post50_100_maker_lots += qty
                elif 100 <= dt < 300:
                    post100_300_maker_lots += qty
                # binning
                bin_lo = (int(dt)//50)*50
                bin_lots[bin_lo] += qty
            # Quote post/modify activity during blackout
            if -650 <= dt <= 0:
                if k == "ack" and e.get("type")=="ack":
                    # ack with op possibly 'order'
                    pass
                if k == "ack":
                    op = e.get("op") or e.get("type")
                    # Our codepath logs quote posts as 'ack' with op=='order'
                    # but also includes IOC acks. We attempt to filter by qty/price
                    pass
                if k == "modify_ack":
                    blackout_modifies.append((ri+1, dt))

    print()
    print("Q1  MAKER-SIDE FILLS in BLACKOUT WINDOW (-650..0 ms):")
    if pre_maker_fills:
        print(f"  total fills={len(pre_maker_fills)}  lots={pre_maker_lots}")
        cp_lots = Counter()
        for (ri, dt, q, cp, k) in pre_maker_fills:
            cp_lots[cp] += q
        for cp, q in cp_lots.most_common():
            print(f"    counterparty {cp:<8} lots={q}")
        # show first few
        for (ri, dt, q, cp, k) in pre_maker_fills[:10]:
            print(f"    rev{ri:>2}  dt={dt:>6.1f}ms  qty={q:<3} cp={cp:<6} kind={k}")
    else:
        print("  NONE   <-- blackout fully suppressed in-window maker fills")
    print()
    print("Q1b MAKER-SIDE FILLS AT T+0..T+50ms (immediately post-reveal):")
    print(f"  lots given up 0-50ms   : {post0_50_maker_lots}")
    print(f"  lots given up 50-100ms : {post50_100_maker_lots}")
    print(f"  lots given up 100-300ms: {post100_300_maker_lots}")
    print(f"  taker lots taken 0-300 : {taker_lots_post}")

    print()
    print("Q3  MAKER-FILL TIMING HISTOGRAM (50ms bins, -700..+400ms):")
    for lo in sorted(bin_lots):
        bar = "#"*int(bin_lots[lo])
        print(f"  [{lo:>5}, {lo+50:>5}) lots={bin_lots[lo]:>3}  {bar}")

    # --- 2. Modify/order activity in blackout
    if blackout_modifies:
        print()
        print(f"Q2  modify_ack events in BLACKOUT WINDOW: {len(blackout_modifies)}")
        for ri, dt in blackout_modifies[:8]:
            print(f"    rev{ri:>2}  dt={dt:>6.1f}ms")
    else:
        print()
        print("Q2  modify_ack events in BLACKOUT WINDOW: 0")

    # --- Final PnL
    sets = [e for e in events if e.get("kind")=="settlement" and e.get("trader")==OUR]
    pnl_total = sum(s.get("pnl", 0) for s in sets)
    trades = sum(s.get("trades", 0) for s in sets)
    fees = sum(s.get("fees", 0) for s in sets)
    print()
    print(f"FINAL PNL = {pnl_total}  trades={trades}  fees={fees}  games={len(sets)}")

    # --- IOC fire latency: time from reveal to first taker fill
    ioc_lats = []
    for r in reveals:
        rn = r["sent_ns"]
        first_taker = None
        for e in events:
            sn = e.get("sent_ns")
            if sn is None or sn < rn:
                continue
            dt = (sn - rn)/1e6
            if dt > 1000:
                continue
            if e.get("kind")=="fill" and e.get("liquidity")=="taker":
                if first_taker is None or dt < first_taker:
                    first_taker = dt
        if first_taker is not None:
            ioc_lats.append(first_taker)
    print(f"first-taker latency: p50={percentile(ioc_lats,50):.1f}ms  p90={percentile(ioc_lats,90):.1f}ms  n={len(ioc_lats)}" if ioc_lats else "first-taker latency: n=0 (no taker fills within 1s of any reveal)")

    return {
        "n_reveals": len(reveals),
        "blackout_maker_lots": pre_maker_lots,
        "blackout_maker_fills": len(pre_maker_fills),
        "post0_50_maker_lots": post0_50_maker_lots,
        "post50_100_maker_lots": post50_100_maker_lots,
        "post100_300_maker_lots": post100_300_maker_lots,
        "taker_lots_post": taker_lots_post,
        "pnl": pnl_total,
        "trades": trades,
        "fees": fees,
        "ioc_p50": percentile(ioc_lats, 50) if ioc_lats else None,
        "ioc_p90": percentile(ioc_lats, 90) if ioc_lats else None,
    }


def main():
    paths = sys.argv[1:]
    rs = []
    for p in paths:
        rs.append((p, analyse(p)))
        print()
    print("="*88)
    print("CROSS-LOG SUMMARY  (per-reveal averages where helpful)")
    print("="*88)
    print(f"{'file':<58} {'rev':>3} {'blkt_lots':>9} {'0-50ms':>7} {'50-100':>7} {'100-300':>7} {'tk_lots':>8} {'PnL':>6} {'IOC_p50':>8} {'IOC_p90':>8}")
    for p, r in rs:
        nm = os.path.basename(p)[-20:]
        ioc50 = f"{r['ioc_p50']:.0f}" if r['ioc_p50'] is not None else "-"
        ioc90 = f"{r['ioc_p90']:.0f}" if r['ioc_p90'] is not None else "-"
        print(f"{nm:<58} {r['n_reveals']:>3} {r['blackout_maker_lots']:>9} {r['post0_50_maker_lots']:>7} {r['post50_100_maker_lots']:>7} {r['post100_300_maker_lots']:>7} {r['taker_lots_post']:>8} {r['pnl']:>6} {ioc50:>8} {ioc90:>8}")

if __name__ == "__main__":
    main()
