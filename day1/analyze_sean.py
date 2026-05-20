#!/usr/bin/env python3
"""Reverse-engineer SEAN's strategy from KEVD's local logs.

Key insight on schema:
- `quote_add`/`quote_fill`/`quote_cancel`/`trade` events in this file are
  PUBLIC stream events. quote_add/quote_cancel/quote_fill in the KEVD log
  ARE NOT just KEVD's own quotes — they are the PUBLIC tape (every actor's
  quote, since we receive the public book).
- `fill` events (kind="fill") are KEVD's *private* fills, with `counterparty`
  field naming the other side. side= is KEVD's side, so SEAN's side is the flip.
- `ack` events are KEVD's own order acknowledgments — these tell us which
  order_ids belong to KEVD. Everything else in quote_add is some OTHER trader.
- To find SEAN's order_ids: from each fill with counterparty=SEAN, look at
  the matching `trade` event (same trade_id). The trade's matching
  quote_add (same price, side opposite to KEVD's, qty consistent) is SEAN.
- Easier: from the `fill` event, the KEVD `order_id` is given; the OTHER side
  of the same trade_id can be found by looking at the `trade` event (which has
  trade_id but not order_id directly). However, the public stream emits a
  `quote_fill` for the resting quote that was hit. By matching trade_id between
  the private `fill` and the public `quote_fill`, we get SEAN's order_id.
"""

import json
import statistics as st
from collections import defaultdict, Counter
from pathlib import Path

FILES = [
    "/Users/kevin/projects/valk/day1/logs/strat7.5_sean_bot.jsonl",
    "/Users/kevin/projects/valk/day1/logs/strat7.5_sean_bot2.jsonl",
    "/Users/kevin/projects/valk/day1/logs/strat7.5_sean_bot3.jsonl",
]


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def analyze_one(path):
    rows = load(path)
    print(f"\n=== {Path(path).name}  ({len(rows)} events) ===")

    # 1. KEVD's own order_ids (from ack events)
    kevd_order_ids = set()
    for r in rows:
        if r.get("kind") == "ack" and "order_id" in r:
            kevd_order_ids.add(r["order_id"])
        if r.get("kind") == "modify_ack" and "order_id" in r:
            kevd_order_ids.add(r["order_id"])

    # 2. Map trade_id -> quote_fill (the resting quote that was hit)
    qf_by_trade = {}
    for r in rows:
        if r.get("kind") == "quote_fill":
            qf_by_trade[r["trade_id"]] = r

    # 3. Find SEAN fills (from KEVD's perspective)
    sean_fills = []  # each: dict with kevd_side, price, qty, ts, liquidity, sean_order_id
    sean_order_ids = set()
    for r in rows:
        if r.get("kind") == "fill" and r.get("counterparty") == "SEAN":
            tid = r["trade_id"]
            qf = qf_by_trade.get(tid)
            sean_oid = None
            if qf is not None and qf.get("order_id") != r.get("order_id"):
                sean_oid = qf["order_id"]
            elif qf is not None:
                # The qf was for KEVD's own quote being hit by SEAN.
                # Then KEVD was the resting maker — but KEVD's `fill` says
                # liquidity=taker would be wrong then; check.
                sean_oid = None
            sean_fills.append({
                "trade_id": tid,
                "kevd_side": r["side"],
                "sean_side": "buy" if r["side"] == "sell" else "sell",
                "price": r["price"],
                "qty": r["qty"],
                "ts_ns": r["ts_ns"],
                "liquidity": r["liquidity"],  # KEVD's role
                "sean_order_id": sean_oid,
            })
            if sean_oid is not None:
                sean_order_ids.add(sean_oid)

    print(f"  KEVD order_ids: {len(kevd_order_ids)}  SEAN fills: {len(sean_fills)}  SEAN order_ids identified: {len(sean_order_ids)}")
    if not sean_fills:
        print("  -> No SEAN trades in this file. Skipping detail.")
        return None

    # 4. For each SEAN order_id, find the original quote_add and any quote_cancel
    qadd_by_oid = {}
    qcancel_by_oid = {}
    qfills_by_oid = defaultdict(list)
    for r in rows:
        k = r.get("kind")
        oid = r.get("order_id")
        if oid is None:
            continue
        if k == "quote_add":
            qadd_by_oid[oid] = r
        elif k == "quote_cancel":
            qcancel_by_oid[oid] = r
        elif k == "quote_fill":
            qfills_by_oid[oid].append(r)

    # 5. SEAN role / side / size distribution
    sean_buys = [f for f in sean_fills if f["sean_side"] == "buy"]
    sean_sells = [f for f in sean_fills if f["sean_side"] == "sell"]
    kevd_taker = [f for f in sean_fills if f["liquidity"] == "taker"]  # SEAN was MAKER
    kevd_maker = [f for f in sean_fills if f["liquidity"] == "maker"]  # SEAN was TAKER

    print(f"  SEAN side: bought {sum(f['qty'] for f in sean_buys)}, sold {sum(f['qty'] for f in sean_sells)}")
    print(f"  SEAN role: maker {sum(f['qty'] for f in kevd_taker)} lots ({len(kevd_taker)} fills), taker {sum(f['qty'] for f in kevd_maker)} lots ({len(kevd_maker)} fills)")
    print(f"  SEAN aggressor rate: {len(kevd_maker)}/{len(sean_fills)} fills SEAN took = {100*len(kevd_maker)/len(sean_fills):.1f}%")

    # 6. SEAN fill prices vs current best bid/ask at fill time
    # Reconstruct the live book by replaying public events.
    # We index a sequence of (t_ns, best_bid, best_ask) snapshots for symbol A.
    # Replay quote_add (add liquidity), quote_fill (decrement), quote_cancel (remove),
    # plus book_periodic as a reset.
    from collections import defaultdict as _dd
    levels_bid = _dd(int)  # price -> qty
    levels_ask = _dd(int)
    orders_open = {}  # oid -> (side, price, qty)
    live_snapshots = []  # list of (t_ns, best_bid, best_ask)

    def _best_bid():
        return max((p for p, q in levels_bid.items() if q > 0), default=None)

    def _best_ask():
        return min((p for p, q in levels_ask.items() if q > 0), default=None)

    def _snap(t):
        live_snapshots.append((t, _best_bid(), _best_ask()))

    events_sorted = sorted(rows, key=lambda r: r.get("t_ns", 0))
    for r in events_sorted:
        if r.get("symbol") not in (None, "A"):
            continue
        k = r.get("kind")
        if k == "quote_add" and r.get("symbol") == "A":
            oid = r["order_id"]; side = r["side"]; px = r["price"]; q = r["qty"]
            orders_open[oid] = (side, px, q)
            if side == "buy":
                levels_bid[px] += q
            else:
                levels_ask[px] += q
            _snap(r["t_ns"])
        elif k == "quote_fill" and r.get("symbol") == "A":
            oid = r["order_id"]; q = r["qty"]; px = r["price"]; side = r["side"]
            if side == "buy":
                levels_bid[px] -= q
                if levels_bid[px] <= 0:
                    levels_bid.pop(px, None)
            else:
                levels_ask[px] -= q
                if levels_ask[px] <= 0:
                    levels_ask.pop(px, None)
            if oid in orders_open:
                s, p, qq = orders_open[oid]
                rem = r.get("remaining", qq - q)
                if rem <= 0:
                    orders_open.pop(oid, None)
                else:
                    orders_open[oid] = (s, p, rem)
            _snap(r["t_ns"])
        elif k == "quote_cancel" and r.get("symbol") == "A":
            oid = r.get("order_id")
            if oid in orders_open:
                s, p, q = orders_open.pop(oid)
                if s == "buy":
                    levels_bid[p] -= q
                    if levels_bid[p] <= 0:
                        levels_bid.pop(p, None)
                else:
                    levels_ask[p] -= q
                    if levels_ask[p] <= 0:
                        levels_ask.pop(p, None)
                _snap(r["t_ns"])
        elif k == "book_periodic" and r.get("symbol") == "A":
            levels_bid.clear(); levels_ask.clear()
            for lvl in r["bids"]:
                levels_bid[lvl["price"]] += lvl["qty"]
            for lvl in r["asks"]:
                levels_ask[lvl["price"]] += lvl["qty"]
            _snap(r["t_ns"])

    books = sorted([r for r in rows if r.get("kind") == "book_periodic" and r.get("symbol") == "A"], key=lambda r: r["t_ns"])

    def live_bbo_at(t_ns):
        # binary search the snapshots for the last one with t<=t_ns
        lo, hi = 0, len(live_snapshots) - 1
        ans = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if live_snapshots[mid][0] <= t_ns:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if ans < 0:
            return None, None
        return live_snapshots[ans][1], live_snapshots[ans][2]

    def book_at(t_ns):
        prev = None
        for b in books:
            if b.get("symbol") != "A":
                continue
            if b["t_ns"] <= t_ns:
                prev = b
            else:
                break
        return prev

    def _bb(b):
        return b["bids"][0]["price"] if (b and b["bids"]) else None

    def _ba(b):
        return b["asks"][0]["price"] if (b and b["asks"]) else None

    # Also build a tick-by-tick fair from the reveal stream:
    # game: 6 reveals of 15s each over 90s. settlement = sum(reveals).
    # a,w from NOTES? from the early book: SEAN posted at 104, market 102-104.
    # We can estimate fair as last_running_sum + (remaining_reveals * mid_per_reveal_estimate).
    # Easier: use observed `running_sum` from reveals + last_settle.
    # Or get a,w from data: reveal values seen.
    reveals = [r for r in rows if r.get("kind") == "reveal"]
    rvalues = [r["value"] for r in reveals]
    print(f"  Reveals: {rvalues}  sum={sum(rvalues)}")
    if rvalues:
        rmin, rmax = min(rvalues), max(rvalues)
        print(f"  Reveal range: [{rmin},{rmax}] -> assumed Uniform[{rmin},{rmax}]  E[X]={(rmin+rmax)/2}")

    # Determine number of reveals per round (from game_state)
    gs = [r for r in rows if r.get("kind") == "game_state"]
    # the first non-empty reveals list gives n_reveals seen pre-settled
    # use duration/reveal_interval
    n_reveals = None
    for g in gs:
        if g.get("duration") and g.get("reveal_interval"):
            n_reveals = int(g["duration"] / g["reveal_interval"])
            break
    print(f"  Round has {n_reveals} reveals total")

    # Compute fair at each SEAN fill time
    def fair_at(t_ns):
        # sum of reveals seen so far + (remaining reveals * E[X])
        if not rvalues:
            return None
        seen = [r for r in reveals if r["t_ns"] <= t_ns]
        seen_sum = sum(r["value"] for r in seen)
        n_seen = len(seen)
        remain = (n_reveals or 6) - n_seen
        e_x = (rmin + rmax) / 2
        return seen_sum + remain * e_x

    # 7. SEAN's quote pricing analysis
    print("\n  --- SEAN fills with book/fair context ---")
    print(f"  {'tid':<5} {'role':<8} {'side':<5} {'px':<5} {'qty':<4} {'bbo':<12} {'mid':<6} {'fair':<7} {'edge(px-mid)':<13} {'oid':<7}")
    edges = []
    edges_from_fair = []
    for f in sean_fills:
        # use both live and periodic
        bid_live, ask_live = live_bbo_at(f["ts_ns"])
        bid = bid_live
        ask = ask_live
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
        fr = fair_at(f["ts_ns"])
        # SEAN's edge from market mid: if SEAN sold at price P, edge = P - mid (positive = above mid = good for SEAN)
        # if SEAN bought at price P, edge = mid - P (positive = below mid = good for SEAN)
        edge_mid = None
        if mid is not None:
            if f["sean_side"] == "sell":
                edge_mid = f["price"] - mid
            else:
                edge_mid = mid - f["price"]
        edge_fair = None
        if fr is not None:
            if f["sean_side"] == "sell":
                edge_fair = f["price"] - fr
            else:
                edge_fair = fr - f["price"]
        if edge_mid is not None:
            edges.append(edge_mid)
        if edge_fair is not None:
            edges_from_fair.append(edge_fair)
        sean_role = "maker" if f["liquidity"] == "taker" else "TAKER"
        bbo = f"{bid}/{ask}"
        print(f"  {f['trade_id']:<5} {sean_role:<8} {f['sean_side']:<5} {f['price']:<5} {f['qty']:<4} {bbo:<12} {str(mid):<6} {str(fr):<7} {str(round(edge_mid,1)) if edge_mid is not None else '-':<13} {str(f['sean_order_id'] or '-'):<7}")

    if edges:
        print(f"\n  SEAN edge from mid (positive = favorable): median={st.median(edges):.1f}  mean={st.mean(edges):.1f}  min={min(edges):.1f}  max={max(edges):.1f}")
    if edges_from_fair:
        print(f"  SEAN edge from FAIR estimate: median={st.median(edges_from_fair):.1f}  mean={st.mean(edges_from_fair):.1f}  min={min(edges_from_fair):.1f}  max={max(edges_from_fair):.1f}")

    # 8. SEAN's quote sizing
    sean_qtys_at_fill = [f["qty"] for f in sean_fills]
    # SEAN's ORIGINAL quote size (from quote_add of the SEAN order_id)
    sean_orig_qtys = []
    for oid in sean_order_ids:
        qa = qadd_by_oid.get(oid)
        if qa:
            sean_orig_qtys.append(qa["qty"])
    if sean_orig_qtys:
        print(f"  SEAN original quote qty: median={st.median(sean_orig_qtys)} min={min(sean_orig_qtys)} max={max(sean_orig_qtys)} mean={st.mean(sean_orig_qtys):.1f}  n={len(sean_orig_qtys)}")
    print(f"  SEAN fill qtys: {Counter(sean_qtys_at_fill).most_common()}")

    # 9. SEAN's quote rest time (for those where SEAN was maker)
    rest_times_ms = []
    for f in sean_fills:
        if f["liquidity"] != "taker":  # KEVD was maker -> SEAN was aggressor -> no rest time
            continue
        oid = f["sean_order_id"]
        if oid is None:
            continue
        qa = qadd_by_oid.get(oid)
        if not qa:
            continue
        dt_ns = f["ts_ns"] - qa["ts_ns"]
        rest_times_ms.append(dt_ns / 1e6)
    if rest_times_ms:
        print(f"  SEAN quote rest-time-until-fill (ms): median={st.median(rest_times_ms):.0f} min={min(rest_times_ms):.0f} max={max(rest_times_ms):.0f}  n={len(rest_times_ms)}")

    # 9b. Timing relative to nearest reveal (seconds since last / until next)
    reveal_times = sorted([r["t_ns"] for r in reveals])
    def reveal_dt(t_ns):
        last_dt = None; next_dt = None
        for rt in reveal_times:
            if rt <= t_ns:
                last_dt = (t_ns - rt) / 1e9
            else:
                next_dt = (rt - t_ns) / 1e9
                break
        return last_dt, next_dt
    print("  --- SEAN quote_add timing relative to reveals (sec since last / until next) ---")
    for oid in sean_order_ids:
        qa = qadd_by_oid.get(oid)
        if not qa:
            continue
        last_dt, next_dt = reveal_dt(qa["ts_ns"])
        print(f"    oid {oid} side={qa['side']} px={qa['price']} qty={qa['qty']} since_last_reveal={last_dt} until_next_reveal={next_dt}")

    # 10. Pre-reveal vs post-reveal SEAN quoting
    # Look at all SEAN order_ids: time of quote_add vs nearest reveal
    pre_reveal_count = 0
    post_reveal_count = 0
    reveal_times = sorted([r["t_ns"] for r in reveals])
    for oid in sean_order_ids:
        qa = qadd_by_oid.get(oid)
        if not qa:
            continue
        t = qa["ts_ns"]
        # find time since last reveal
        last_rev_t = None
        next_rev_t = None
        for rt in reveal_times:
            if rt <= t:
                last_rev_t = rt
            else:
                next_rev_t = rt
                break
        if last_rev_t is None:
            pre_reveal_count += 1
        elif next_rev_t is not None and (next_rev_t - t) < (t - last_rev_t):
            pre_reveal_count += 1  # closer to next reveal
        else:
            post_reveal_count += 1
    print(f"  SEAN quote_add timing: closer to PRE-next-reveal: {pre_reveal_count}, POST-last-reveal: {post_reveal_count}")

    # 11. Does SEAN's quote get cancelled (after partial fill or before fill)?
    sean_cancelled = 0
    sean_filled = 0
    for oid in sean_order_ids:
        if oid in qcancel_by_oid:
            sean_cancelled += 1
        if oid in qfills_by_oid:
            sean_filled += 1
    print(f"  SEAN orders we know about: filled={sean_filled}, cancelled={sean_cancelled} (a single order can be both)")

    # 12. What was the quote_add price diff from mid for SEAN's quotes (at quote_add time)?
    sean_quote_edges_at_post = []
    for oid in sean_order_ids:
        qa = qadd_by_oid.get(oid)
        if not qa:
            continue
        # use live BBO immediately BEFORE this quote was added
        bid, ask = live_bbo_at(qa["ts_ns"] - 1)
        if bid is None or ask is None:
            continue
        mid = (bid + ask) / 2
        side = qa["side"]
        px = qa["price"]
        # edge = how far inside/outside mid
        if side == "sell":
            edge = px - mid  # positive = above mid (passive)
        else:
            edge = mid - px  # positive = below mid (passive)
        sean_quote_edges_at_post.append((side, px, bid, ask, mid, edge, qa["qty"]))
    if sean_quote_edges_at_post:
        edges_only = [e[5] for e in sean_quote_edges_at_post]
        print(f"  SEAN quote edge-from-mid at post time: median={st.median(edges_only):.1f} min={min(edges_only):.1f} max={max(edges_only):.1f}")
        print(f"  Sample SEAN quote_adds (side, px, bid, ask, mid, edge, qty):")
        for s in sean_quote_edges_at_post[:10]:
            print(f"    {s}")

    # 13. Counterparty summary at end
    cs = [r for r in rows if r.get("kind") == "counterparty_summary"]
    if cs:
        stats = cs[-1].get("stats", {})
        for cp, v in stats.items():
            print(f"  cps[{cp}]: n_fills={v['n_fills']} bought={v['qty_we_bought']} sold={v['qty_we_sold']} maker={v['our_role_maker']} taker={v['our_role_taker']} avg_px={v['avg_price']:.2f}")

    # 14. Settlement
    s = [r for r in rows if r.get("kind") == "settlement"]
    if s:
        print(f"  KEVD settlement: pnl={s[-1]['pnl']} pos={s[-1]['positions']} cash={s[-1]['cash']} fees={s[-1]['fees']}")

    return {
        "n_sean_fills": len(sean_fills),
        "sean_taker_pct": 100*len(kevd_maker)/len(sean_fills) if sean_fills else 0,
        "edges_mid": edges,
        "edges_fair": edges_from_fair,
        "qtys_orig": sean_orig_qtys,
        "rest_times_ms": rest_times_ms,
        "n_sean_orders": len(sean_order_ids),
    }


def main():
    aggregated = {"edges_mid": [], "edges_fair": [], "qtys_orig": [], "rest_times_ms": []}
    n_taker_total = 0
    n_fills_total = 0
    for p in FILES:
        r = analyze_one(p)
        if r:
            aggregated["edges_mid"] += r["edges_mid"]
            aggregated["edges_fair"] += r["edges_fair"]
            aggregated["qtys_orig"] += r["qtys_orig"]
            aggregated["rest_times_ms"] += r["rest_times_ms"]
            n_fills_total += r["n_sean_fills"]
            n_taker_total += int(r["sean_taker_pct"] * r["n_sean_fills"] / 100)

    print("\n=== AGGREGATED across all logs ===")
    if aggregated["edges_mid"]:
        e = aggregated["edges_mid"]
        print(f"SEAN edge-from-mid (n={len(e)}): median={st.median(e):.1f} mean={st.mean(e):.1f} stdev={st.pstdev(e):.1f} min={min(e):.1f} max={max(e):.1f}")
        # histogram
        from collections import Counter as C
        bins = C()
        for v in e:
            bins[int(v)] += 1
        print("  edge histogram (px):", sorted(bins.items()))
    if aggregated["edges_fair"]:
        e = aggregated["edges_fair"]
        print(f"SEAN edge-from-FAIR (n={len(e)}): median={st.median(e):.1f} mean={st.mean(e):.1f}")
    if aggregated["qtys_orig"]:
        q = aggregated["qtys_orig"]
        print(f"SEAN original qty (n={len(q)}): median={st.median(q)} mean={st.mean(q):.2f} min={min(q)} max={max(q)}  distribution: {sorted(Counter(q).items())}")
    if aggregated["rest_times_ms"]:
        r = aggregated["rest_times_ms"]
        print(f"SEAN rest-time-till-fill ms (n={len(r)}): median={st.median(r):.0f} mean={st.mean(r):.0f} min={min(r):.0f} max={max(r):.0f}")
    if n_fills_total:
        print(f"SEAN was taker in {n_taker_total}/{n_fills_total} = {100*n_taker_total/n_fills_total:.1f}% of trades with KEVD")


if __name__ == "__main__":
    main()
