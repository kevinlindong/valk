"""Diag: did reveal-time IOC snipes actually fire?

For each reveal, check:
  - order_ack count in 0-50ms post-reveal (fresh oids = IOC sends from sweep_post_reveal)
  - fill count in 0-50ms post-reveal
  - BBO at reveal moment + what we precomputed lift_to/hit_to should be
  - rejects in window (lockout? rate-limit?)
"""
from __future__ import annotations
import json, sys
from collections import defaultdict, Counter

PATHS = sys.argv[1:] or sorted([
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_012105.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011924.jsonl",
])

def load(p):
    out = []
    with open(p) as f:
        for line in f:
            try: out.append(json.loads(line))
            except: pass
    return out

for p in PATHS:
    print("\n" + "#"*72)
    print(f"# {p.rsplit('/',1)[-1]}")
    print("#"*72)
    events = load(p)
    rounds = []; cur = []
    for e in events:
        cur.append(e)
        if e.get("type") == "settlement":
            rounds.append(cur); cur = []
    if cur: rounds.append(cur)

    for ri, r in enumerate(rounds):
        ts0 = None
        for e in r:
            if e.get("type") == "game_state" and e.get("phase") == "running":
                ts0 = e["t_ns"]; break
        if not ts0 and r: ts0 = r[0].get("t_ns")
        if not ts0: continue

        # Get all reveals
        reveals = [(i, e) for i, e in enumerate(r) if e.get("type") == "reveal"]
        if not reveals:
            continue
        print(f"\nR{ri+1}: {len(reveals)} reveals")
        for ri2, (idx, rev) in enumerate(reveals):
            rev_t = rev["t_ns"]
            offset = (rev_t - ts0) / 1e9
            v = rev.get("value")

            # Window 0..50ms after reveal
            win_lo = rev_t
            win_hi = rev_t + 50_000_000
            in_win = [e for e in r
                      if win_lo < e.get("t_ns", 0) <= win_hi]

            # Pre-reveal window -50..0ms (to see what we did in anticipation)
            pre_win = [e for e in r
                       if (rev_t - 50_000_000) <= e.get("t_ns", 0) < rev_t]

            # Categorize
            post_acks = [e for e in in_win if e.get("type") == "order_ack"]
            post_fills = [e for e in in_win if e.get("type") == "fill"]
            post_rej = [e for e in in_win if e.get("type") == "reject"]
            post_mod = [e for e in in_win if e.get("type") == "modify_ack"]

            # Book at reveal moment per sym
            bb_at_rev = {}
            for e in r:
                if e.get("t_ns", 0) > rev_t:
                    break
                if e.get("type") == "book":
                    sym = e.get("symbol")
                    bids = e.get("bids") or []
                    asks = e.get("asks") or []
                    bb_at_rev[sym] = (
                        bids[0]["price"] if bids else None,
                        asks[0]["price"] if asks else None,
                    )

            # Fills before reveal but in pre-window (could be MM fills from anticipation)
            pre_fills = [e for e in pre_win if e.get("type") == "fill"]

            print(f"\n  reveal[{ri2}] v={v} t={offset:.2f}s")
            print(f"    bbo: {bb_at_rev}")
            print(f"    pre_-50ms: fills={len(pre_fills)} acks={sum(1 for e in pre_win if e.get('type')=='order_ack')}")
            print(f"    post_+50ms: acks={len(post_acks)} mods={len(post_mod)} fills={len(post_fills)} rej={len(post_rej)}")
            # Detail: show each post-reveal ack
            for a in post_acks[:6]:
                dt = (a["t_ns"] - rev_t) / 1e6
                print(f"      ack +{dt:5.1f}ms  {a.get('symbol')}/{a.get('side')} {a.get('qty')}@{a.get('price')}  oid={a.get('order_id')}")
            for f in post_fills[:6]:
                dt = (f["t_ns"] - rev_t) / 1e6
                print(f"      FILL +{dt:5.1f}ms  {f.get('symbol')}/{f.get('side')} {f.get('qty')}@{f.get('price')} liq={f.get('liquidity')}")
            for rj in post_rej[:3]:
                dt = (rj["t_ns"] - rev_t) / 1e6
                print(f"      REJ +{dt:5.1f}ms  {rj.get('reason')} {rj.get('symbol')} {rj.get('side')} {rj.get('price')}")
