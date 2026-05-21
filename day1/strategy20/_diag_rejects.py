"""Check rejects, lockouts, our edge vs BBO, and what happens at quiet patches."""
from __future__ import annotations
import json, sys
from collections import defaultdict, Counter

PATHS = sys.argv[1:] or [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_012105.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011924.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011401.jsonl",
]

def main():
    for p in PATHS:
        events = []
        with open(p) as f:
            for line in f:
                try: events.append(json.loads(line))
                except: pass
        rounds = []; cur = []
        for e in events:
            cur.append(e)
            if e.get("type") == "settlement":
                rounds.append(cur); cur = []
        if cur: rounds.append(cur)

        print("\n" + "#"*72)
        print(f"# {p.rsplit('/',1)[-1]}")
        print("#"*72)
        for ri, r in enumerate(rounds):
            settled = next((e for e in r if e.get("type") == "settlement"), None)
            if not settled and ri < len(rounds)-1: continue
            print(f"\n=== R{ri+1} ===")
            # Rejects
            rejects = [e for e in r if e.get("type") == "reject"]
            print(f"  rejects: {len(rejects)}")
            for rj in rejects[:5]:
                print(f"    {rj.get('reason')} sym={rj.get('symbol')} px={rj.get('price')} qty={rj.get('qty')}")

            # BBO + our quotes — at each book event, track edge vs fair
            # Skip — too noisy. Instead: how often does the book change without us responding?

            # Per-symbol bot landscape: aggressor=buy means someone hit ask; aggressor=sell hit bid
            ts0 = None
            for e in r:
                if e.get("type") == "game_state" and e.get("phase") == "running":
                    ts0 = e["t_ns"]; break
            if ts0 is None and r: ts0 = r[0].get("t_ns")

            # Count fills by time window, AND count quote_add/quote_cancel from non-us
            # (we can't easily separate ours, but most are bots since our order_acks << quote_adds)
            bins_fill = defaultdict(int)
            bins_qadd = defaultdict(int)
            bins_qcanc = defaultdict(int)
            bins_book = defaultdict(int)
            bins_ack = defaultdict(int)
            bins_trade = defaultdict(int)
            pos = defaultdict(int)
            pos_at_time = defaultdict(dict)
            for e in r:
                if not ts0: continue
                t = e.get("type")
                if "t_ns" not in e: continue
                dt = (e["t_ns"] - ts0)/1e9
                b = int(dt // 5) * 5
                if t == "fill":
                    bins_fill[b] += e.get("qty", 1)
                    sym = e.get("symbol"); side = e.get("side"); q = e.get("qty",1)
                    pos[sym] += q if side == "buy" else -q
                    pos_at_time[b][sym] = pos[sym]
                elif t == "quote_add": bins_qadd[b] += 1
                elif t == "quote_cancel": bins_qcanc[b] += 1
                elif t == "book": bins_book[b] += 1
                elif t == "order_ack": bins_ack[b] += 1
                elif t == "trade": bins_trade[b] += 1

            # Per-sym fill totals
            sym_fills = defaultdict(int)
            for e in r:
                if e.get("type") == "fill":
                    sym_fills[e.get("symbol")] += e.get("qty",1)

            print(f"  fills_by_sym: {dict(sym_fills)}")

            # Show timeline for "trades dry up" verification
            max_b = max(list(bins_qadd) + list(bins_fill) + list(bins_ack) + [0])
            print(f"  5s_bins (qty totals): fill / our_ack / total_qadd / total_trade / pos_snapshot")
            for b in range(0, int(max_b)+5, 5):
                pos_snap = pos_at_time.get(b, {})
                pos_str = ",".join(f"{s}={pos_snap[s]}" for s in sorted(pos_snap)) if pos_snap else ""
                print(f"    t={b:>3d}: fill={bins_fill.get(b,0):>3d}  "
                      f"ack={bins_ack.get(b,0):>3d}  "
                      f"qadd={bins_qadd.get(b,0):>4d}  "
                      f"trade={bins_trade.get(b,0):>3d}  "
                      f"pos:{pos_str}")

if __name__ == "__main__":
    main()
