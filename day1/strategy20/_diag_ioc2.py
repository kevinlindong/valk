"""Wide-window diag: 5s pre and 5s post each reveal — what was happening?"""
from __future__ import annotations
import json, sys
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else \
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_012439.jsonl"

events = []
with open(PATH) as f:
    for line in f:
        try: events.append(json.loads(line))
        except: pass

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
    if not ts0 and r: ts0 = r[0]["t_ns"]

    reveals = [e for e in r if e.get("type") == "reveal"]
    print(f"\nR{ri+1}")
    for ri2, rev in enumerate(reveals):
        rt = rev["t_ns"]
        off = (rt - ts0) / 1e9
        win_pre = [e for e in r if (rt - 5e9) <= e.get("t_ns", 0) < rt]
        win_post = [e for e in r if rt < e.get("t_ns", 0) <= (rt + 5e9)]
        pre_types = Counter(e.get("type") for e in win_pre)
        post_types = Counter(e.get("type") for e in win_post)
        print(f"  reveal[{ri2}] v={rev.get('value')} t={off:.1f}s")
        print(f"    pre  5s: {dict(pre_types)}")
        print(f"    post 5s: {dict(post_types)}")
