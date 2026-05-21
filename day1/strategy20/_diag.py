"""Diagnostic for v20 logs:
  1. Fill timing per round (bin by t_offset since first event).
  2. BBO presence — for each book snapshot, are we at best bid / best ask?
  3. Reveal-time snipes — what happens in the 5-50ms window after a reveal?
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict, Counter
from typing import Dict, List

PATHS = sys.argv[1:] or [
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_012105.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011924.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_v20_20260521_011401.jsonl",
]


def load_events(path: str) -> List[dict]:
    out = []
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def split_by_round(events):
    rounds = []
    cur = []
    for e in events:
        cur.append(e)
        t = e.get("type") or e.get("event")
        if t == "settlement":
            rounds.append(cur)
            cur = []
    if cur:
        rounds.append(cur)
    return rounds


def analyze_fill_timing(rounds):
    print("=" * 72)
    print("FILL TIMING per round (seconds since first event in round)")
    print("=" * 72)
    for i, r in enumerate(rounds):
        # Find round start
        ts0 = None
        for e in r:
            if e.get("type") == "game_state" and e.get("phase") == "running":
                ts0 = e.get("t_ns")
                break
        if ts0 is None:
            ts0 = r[0].get("t_ns")
        if ts0 is None:
            continue
        # Bin fills by 5-sec windows; track reveals
        bins = Counter()
        fills_by_sym = defaultdict(lambda: defaultdict(int))
        reveals = []
        settled = None
        for e in r:
            t = e.get("type")
            if t == "reveal":
                dt = (e["t_ns"] - ts0) / 1e9
                reveals.append((e.get("index"), dt))
            elif t == "fill":
                dt = (e["t_ns"] - ts0) / 1e9
                b = int(dt // 5) * 5
                bins[b] += e.get("qty", 1)
                fills_by_sym[b][e.get("symbol")] += e.get("qty", 1)
            elif t == "settlement":
                settled = e
        total_dur = (r[-1]["t_ns"] - ts0) / 1e9 if r else 0
        n_fills = sum(bins.values())
        pnl = (settled or {}).get("pnl")
        print(f"\nRound {i+1}: dur={total_dur:.1f}s fills={n_fills} pnl={pnl}")
        print(f"  reveals: {reveals}")
        all_bins = sorted(bins) or [0]
        max_b = max(all_bins)
        for b in range(0, int(max_b) + 5, 5):
            q = bins.get(b, 0)
            bysym = dict(fills_by_sym.get(b, {}))
            bar = "#" * min(40, q // 2)
            print(f"  t={b:>3d}-{b+5}s  qty={q:>3d}  {bysym!r:30s} {bar}")


def reconstruct_bbo_at(snapshot_events):
    """Return (best_bid, best_ask) for the last book event seen."""
    pass


def analyze_bbo_presence(rounds):
    print()
    print("=" * 72)
    print("BBO PRESENCE — for each 'book' event, are our orders at best?")
    print("=" * 72)
    # Track our live order_ids per symbol.
    for i, r in enumerate(rounds):
        # Build set of our order_ids (from order_ack events that are ours).
        our_orders: Dict[int, dict] = {}   # oid -> {symbol, side, price, qty}
        # Walk events in order. For each book event, check.
        bbo_checks = defaultdict(lambda: {"checked": 0, "at_bid": 0, "at_ask": 0})
        for e in r:
            t = e.get("type")
            if t == "order_ack":
                oid = e.get("order_id")
                # Our orders include our COID prefix or appear via quote_add
                # right after we sent. We approximate "ours" = ones that
                # produce a fill where liquidity is set (we always have a
                # liquidity field on our fills).
                if oid is not None:
                    our_orders[oid] = {
                        "symbol": e.get("symbol"),
                        "side": e.get("side"),
                        "price": e.get("price"),
                        "qty": e.get("qty"),
                    }
            elif t == "modify_ack":
                oid = e.get("order_id")
                if oid in our_orders:
                    our_orders[oid]["price"] = e.get("price", our_orders[oid]["price"])
                    our_orders[oid]["qty"] = e.get("qty", our_orders[oid]["qty"])
            elif t == "cancel_ack":
                oid = e.get("order_id")
                our_orders.pop(oid, None)
            elif t == "fill":
                oid = e.get("order_id")
                if e.get("liquidity") == "maker":
                    pass  # don't remove maker fills (could be partial)
                elif e.get("liquidity") == "taker":
                    our_orders.pop(oid, None)
            elif t == "book":
                sym = e.get("symbol")
                bids = e.get("bids") or []
                asks = e.get("asks") or []
                if not bids or not asks:
                    continue
                best_bid = bids[0].get("price") if bids else None
                best_ask = asks[0].get("price") if asks else None
                our_buy_px = [o["price"] for o in our_orders.values()
                              if o.get("symbol") == sym and o.get("side") == "buy"]
                our_sell_px = [o["price"] for o in our_orders.values()
                               if o.get("symbol") == sym and o.get("side") == "sell"]
                bbo_checks[sym]["checked"] += 1
                if best_bid is not None and our_buy_px and max(our_buy_px) >= best_bid:
                    bbo_checks[sym]["at_bid"] += 1
                if best_ask is not None and our_sell_px and min(our_sell_px) <= best_ask:
                    bbo_checks[sym]["at_ask"] += 1
        print(f"\nRound {i+1}:")
        for sym in ("A", "B", "C", "D"):
            x = bbo_checks.get(sym, {"checked": 0, "at_bid": 0, "at_ask": 0})
            if x["checked"] == 0:
                continue
            print(f"  {sym}: book_events={x['checked']:>4d}  "
                  f"at_bid={x['at_bid']:>4d} ({x['at_bid']/x['checked']*100:5.1f}%)  "
                  f"at_ask={x['at_ask']:>4d} ({x['at_ask']/x['checked']*100:5.1f}%)")


def analyze_reveal_snipes(rounds):
    print()
    print("=" * 72)
    print("REVEAL-TIME SNIPES — what happens in 0-100ms after each reveal?")
    print("=" * 72)
    for i, r in enumerate(rounds):
        reveal_idx = -1
        for e in r:
            t = e.get("type")
            if t == "reveal":
                reveal_idx += 1
                rev_t = e["t_ns"]
                # Pull events in next 100ms
                window = [x for x in r
                          if rev_t < x["t_ns"] <= rev_t + 100_000_000]
                window_types = Counter(x.get("type") for x in window)
                fills_in_win = [x for x in window if x.get("type") == "fill"]
                # Group by symbol/side/liquidity
                fill_summary = defaultdict(int)
                for f in fills_in_win:
                    key = (f.get("symbol"), f.get("side"), f.get("liquidity"))
                    fill_summary[key] += f.get("qty", 1)
                # Look for order_ack with IOC (no tif logged, but IOC=auto-canceled fast)
                acks = [x for x in window if x.get("type") == "order_ack"]
                rejects = [x for x in window if x.get("type") == "reject"]
                print(f"\nR{i+1} reveal[{reveal_idx}] (value={e.get('value')}):")
                print(f"  events_in_100ms: {dict(window_types)}")
                print(f"  fills: {dict(fill_summary)}")
                if rejects:
                    for rj in rejects[:3]:
                        print(f"  REJECT: {rj.get('reason')} {rj.get('symbol')} {rj.get('price')}")
                # Latency of first event we sent (any order_ack) after reveal
                first_ack_dt = None
                for a in acks:
                    sent = a.get("sent_ns") or a.get("ts_ns")
                    if sent is None:
                        continue
                    dt = (sent - rev_t) / 1e6
                    if first_ack_dt is None or dt < first_ack_dt:
                        first_ack_dt = dt
                if first_ack_dt is not None:
                    print(f"  first_order_sent: {first_ack_dt:+.1f}ms post-reveal "
                          f"({len(acks)} acks total in window)")
                else:
                    print(f"  NO ORDER SENT in 100ms after reveal!")


def main():
    for p in PATHS:
        print("\n" + "#" * 72)
        print(f"# {p.rsplit('/', 1)[-1]}")
        print("#" * 72)
        events = load_events(p)
        rounds = split_by_round(events)
        analyze_fill_timing(rounds)
        analyze_bbo_presence(rounds)
        analyze_reveal_snipes(rounds)


if __name__ == "__main__":
    main()
