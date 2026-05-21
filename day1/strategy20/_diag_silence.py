"""Why do we go silent for 30-60s mid-round?

For each round, find the silent window(s) (>10s between consecutive
order_ack/cancel_ack/modify_ack from us). Look at what's happening:
  - rejects
  - position state
  - book activity (other people active or quiet?)
  - quote_cancel from us (own-order cancel) during silence
"""
from __future__ import annotations
import json, sys
from collections import defaultdict

PATHS = sys.argv[1:] or [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_012105.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011924.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011401.jsonl",
]

def load(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except: pass

def main():
    for p in PATHS:
        print("\n" + "#"*72)
        print(f"# {p.rsplit('/',1)[-1]}")
        print("#"*72)
        events = list(load(p))
        # Find round boundaries
        rounds = []
        cur = []
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
            if ts0 is None and r:
                ts0 = r[0].get("t_ns")
            if ts0 is None: continue

            # Our-action events = anything where WE acted (order_ack, modify_ack,
            # cancel_ack). The probe doesn't tag "ours" but these are private
            # responses to OUR sends — so by definition ours.
            our_acts = []
            for e in r:
                t = e.get("type")
                if t in ("order_ack", "modify_ack", "cancel_ack"):
                    our_acts.append(((e["t_ns"] - ts0)/1e9, t, e.get("symbol"), e.get("order_id")))

            # Find gaps > 5s between successive acts
            gaps = []
            for i in range(1, len(our_acts)):
                dt = our_acts[i][0] - our_acts[i-1][0]
                if dt > 3.0:
                    gaps.append((our_acts[i-1][0], our_acts[i][0], dt))

            # Settlement / pnl
            settled = next((e for e in r if e.get("type") == "settlement"), None)
            pnl = (settled or {}).get("pnl")
            n_fills = sum(1 for e in r if e.get("type") == "fill")

            print(f"\nR{ri+1}: dur={(r[-1]['t_ns']-ts0)/1e9:.1f}s  n_fills={n_fills}  pnl={pnl}  "
                  f"our_acts={len(our_acts)}")
            if not gaps:
                print(f"  no silence > 3s")
                continue
            print(f"  silence gaps > 3s:")
            for g in gaps[:8]:
                start_t, end_t, dt = g
                # In the gap, how many trade/book events from others?
                gap_events = defaultdict(int)
                rejects = []
                for e in r:
                    et = (e["t_ns"] - ts0)/1e9
                    if start_t <= et <= end_t:
                        gap_events[e.get("type")] += 1
                        if e.get("type") == "reject":
                            rejects.append(e)
                print(f"    t={start_t:6.1f}s -> t={end_t:6.1f}s  ({dt:.1f}s silent)  "
                      f"events_during_gap={dict(gap_events)}")
                if rejects:
                    for rj in rejects[:3]:
                        print(f"      REJECT: {rj}")

if __name__ == "__main__":
    main()
