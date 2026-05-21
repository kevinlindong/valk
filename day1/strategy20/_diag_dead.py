"""For one dead-patch round, examine what's actually in the book vs our quote.

Methodology:
  - quote_add events without a "v20" coid prefix are from other bots.
  - quote_add events with our coid are us. (Need to verify - look at sample.)
  - We can identify our orders by looking at order_ack events (those are
    100% ours -- they're the server's response to our REST sends).
  - For each book event during the dead patch, snapshot:
      * best_bid / best_ask
      * our latest quote prices on each side (tracked from order_ack)
      * delta = our_price - best_bid (negative = we're inside)
"""
from __future__ import annotations
import json, sys
from collections import defaultdict

PATH = sys.argv[1] if len(sys.argv) > 1 else \
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011924.jsonl"

events = []
with open(PATH) as f:
    for line in f:
        try: events.append(json.loads(line))
        except: pass

# Find round 1 (first round)
rounds = []; cur = []
for e in events:
    cur.append(e)
    if e.get("type") == "settlement":
        rounds.append(cur); cur = []
if cur: rounds.append(cur)

print(f"rounds: {len(rounds)}")

# Use round 0 (R1)
r = rounds[0]
ts0 = None
for e in r:
    if e.get("type") == "game_state" and e.get("phase") == "running":
        ts0 = e["t_ns"]; break
if ts0 is None: ts0 = r[0]["t_ns"]

# Build "our orders" tracker.
# Approach: any order_ack tells us an oid is ours; track price/qty.
# modify_ack updates price/qty for an oid (or creates new oid).
# cancel_ack removes an oid.
# fill of liquidity=taker removes oid; liquidity=maker decrements qty.
our_orders = {}  # oid -> {symbol, side, price, qty}

# Per-symbol snapshot history.
snapshots = []  # list of (t, symbol, best_bid, best_ask, our_bid, our_ask)

for e in r:
    t = e.get("type")
    et = (e["t_ns"] - ts0)/1e9 if "t_ns" in e else None
    if t in ("order_ack", "modify_ack"):
        oid = e.get("order_id")
        if oid:
            entry = our_orders.get(oid) or {}
            entry["symbol"] = e.get("symbol") or entry.get("symbol")
            entry["side"] = e.get("side") or entry.get("side")
            entry["price"] = e.get("price", entry.get("price"))
            entry["qty"] = e.get("qty", entry.get("qty"))
            our_orders[oid] = entry
    elif t == "cancel_ack":
        oid = e.get("order_id")
        our_orders.pop(oid, None)
    elif t == "fill":
        oid = e.get("order_id")
        liq = e.get("liquidity")
        if liq == "taker":
            our_orders.pop(oid, None)
        elif liq == "maker":
            o = our_orders.get(oid)
            if o is not None:
                left = (o.get("qty") or 0) - (e.get("qty") or 0)
                if left <= 0:
                    our_orders.pop(oid, None)
                else:
                    o["qty"] = left
    elif t == "quote_cancel":
        oid = e.get("order_id")
        if oid in our_orders:
            our_orders.pop(oid, None)
    elif t == "book":
        sym = e.get("symbol")
        bids = e.get("bids") or []
        asks = e.get("asks") or []
        if not bids and not asks: continue
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        our_bids = sorted([o["price"] for o in our_orders.values()
                           if o.get("symbol") == sym and o.get("side") == "buy"], reverse=True)
        our_asks = sorted([o["price"] for o in our_orders.values()
                           if o.get("symbol") == sym and o.get("side") == "sell"])
        our_bid = our_bids[0] if our_bids else None
        our_ask = our_asks[0] if our_asks else None
        snapshots.append((et, sym, best_bid, best_ask, our_bid, our_ask))

# Sample every ~5s
print(f"\ntotal snapshots: {len(snapshots)}")
print(f"\nDead-patch sample (sampling every 5s, all 4 symbols):")
print(f"  t  | sym | best_bid | best_ask | our_bid | our_ask | edge_bid | edge_ask")
print(f"  ----+-----+----------+----------+---------+---------+----------+---------")
last_t_by_sym = defaultdict(lambda: -100)
for snap in snapshots:
    t, sym, bb, ba, ob, oa = snap
    if t - last_t_by_sym[sym] < 5: continue
    last_t_by_sym[sym] = t
    edge_b = (ob - bb) if (ob is not None and bb is not None) else None
    edge_a = (ba - oa) if (oa is not None and ba is not None) else None
    print(f"  {t:5.1f} | {sym:>3} | {str(bb):>8} | {str(ba):>8} | {str(ob):>7} | {str(oa):>7} | "
          f"{str(edge_b):>8} | {str(edge_a):>8}")
