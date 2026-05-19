#!/usr/bin/env python3
"""Forensic trace of modify_ack events to understand why strategy.py issues
modify requests that destroy queue priority but appear redundant.

Walks two specific log files, builds per-order_id state from order_ack ->
modify_ack chains, and classifies each modify_ack into buckets:
  - kept_priority true vs false
  - same-price+same-qty vs different
  - whether a fill preceded it on same order_id (post-fill refill pattern)
"""
import json
from collections import defaultdict, Counter

LOGS = [
    "/Users/kevin/projects/valk/day1/logs/combined_log_20260519_125314.jsonl",
    "/Users/kevin/projects/valk/day1/logs/combined_log_20260519_124658.jsonl",
]


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
                continue
    return events


def analyze(path):
    events = load(path)
    n_total = len(events)

    # Track current known (price, remaining qty) per order_id from our own
    # order_ack / modify_ack stream. Fills update remaining as well.
    state = {}  # order_id -> {"price": int, "qty": int}

    # Index modify_acks by file position so we can look back.
    modifies = []  # list of (idx, evt)
    for idx, evt in enumerate(events):
        if evt.get("kind") == "modify_ack":
            modifies.append((idx, evt))

    n_modify = len(modifies)
    n_kept_true = 0
    n_kept_false = 0
    n_kept_missing = 0
    n_same_pxqty = 0          # px AND qty both equal prior state
    n_same_px_diff_qty = 0    # px equal but qty differs (post-fill refill?)
    n_diff_px = 0             # price actually moved
    n_no_prior = 0            # no prior state on record (shouldn't happen)

    # For redundant (same-px+qty) modifies: classify preceding event for same
    # order_id within previous 5 events overall.
    preceding_for_same_pxqty = Counter()
    # For same-px-different-qty modifies: did a fill on the same order_id
    # happen between the previous modify_ack/order_ack and this one?
    n_same_px_diff_qty_after_fill = 0
    n_same_px_diff_qty_no_fill = 0

    # Track fills per order_id between modifies so we can attribute post-fill
    # refills.
    last_seen_idx_for_order = {}  # order_id -> idx of last order/modify_ack

    # Modify counts per order_id for sampling.
    modifies_by_oid = defaultdict(list)  # order_id -> list of idx

    # Walk full event stream sequentially to maintain order-id state.
    fills_since_last_state_update = defaultdict(int)  # order_id -> fill qty since last ack

    for idx, evt in enumerate(events):
        kind = evt.get("kind")
        oid = evt.get("order_id")
        if kind == "order_ack":
            if oid is not None:
                state[oid] = {
                    "price": evt.get("price"),
                    "qty": evt.get("remaining", evt.get("qty")),
                }
                last_seen_idx_for_order[oid] = idx
                fills_since_last_state_update[oid] = 0
        elif kind == "fill":
            # Only count fills on orders we know about (our own resting
            # orders). Taker fills on our snipes also show up here but we
            # don't track them as state we'll modify; they show up in state
            # only if we previously order_ack'd them as gtc -- ignore for
            # this analysis since modifies happen on our maker resting orders.
            if oid in state:
                fills_since_last_state_update[oid] += evt.get("qty", 0)
                # Decrement remaining qty optimistically.
                state[oid]["qty"] = max(
                    0, state[oid]["qty"] - evt.get("qty", 0)
                )
        elif kind == "modify_ack":
            if oid is None:
                continue
            modifies_by_oid[oid].append(idx)
            prior = state.get(oid)
            new_px = evt.get("price")
            new_qty = evt.get("remaining", evt.get("qty"))
            kept = evt.get("kept_priority", None)
            if kept is True:
                n_kept_true += 1
            elif kept is False:
                n_kept_false += 1
            else:
                n_kept_missing += 1

            if prior is None:
                n_no_prior += 1
            elif prior["price"] == new_px and prior["qty"] == new_qty:
                n_same_pxqty += 1
                # Look back at preceding 1-3 events
                back_kinds = []
                for j in range(max(0, idx - 3), idx):
                    back_kinds.append(events[j].get("kind"))
                preceding_for_same_pxqty[tuple(back_kinds)] += 1
            elif prior["price"] == new_px and prior["qty"] != new_qty:
                n_same_px_diff_qty += 1
                if fills_since_last_state_update[oid] > 0:
                    n_same_px_diff_qty_after_fill += 1
                else:
                    n_same_px_diff_qty_no_fill += 1
            else:
                n_diff_px += 1

            # Update state to new modify_ack result. modify creates new
            # order_id sometimes when kept_priority=False; here we follow
            # the order_id in the event itself (server returned). Reset fill
            # counter.
            state[oid] = {"price": new_px, "qty": new_qty}
            last_seen_idx_for_order[oid] = idx
            fills_since_last_state_update[oid] = 0

    # Sample 5 order_ids with >=3 modifies for timeline printout.
    sampled = [
        oid for oid, idxs in modifies_by_oid.items() if len(idxs) >= 3
    ]
    sampled.sort(key=lambda o: -len(modifies_by_oid[o]))
    sampled = sampled[:5]

    timelines = {}
    for oid in sampled:
        rows = []
        for idx, evt in enumerate(events):
            if evt.get("order_id") != oid:
                continue
            kind = evt.get("kind")
            if kind not in ("order_ack", "modify_ack", "fill"):
                continue
            rows.append(
                {
                    "ts": evt.get("ts_ns"),
                    "kind": kind,
                    "price": evt.get("price"),
                    "qty": evt.get("qty"),
                    "remaining": evt.get("remaining"),
                    "kept_priority": evt.get("kept_priority"),
                }
            )
        timelines[oid] = rows

    return {
        "path": path,
        "n_total": n_total,
        "n_modify": n_modify,
        "n_kept_true": n_kept_true,
        "n_kept_false": n_kept_false,
        "n_kept_missing": n_kept_missing,
        "n_same_pxqty": n_same_pxqty,
        "n_same_px_diff_qty": n_same_px_diff_qty,
        "n_same_px_diff_qty_after_fill": n_same_px_diff_qty_after_fill,
        "n_same_px_diff_qty_no_fill": n_same_px_diff_qty_no_fill,
        "n_diff_px": n_diff_px,
        "n_no_prior": n_no_prior,
        "preceding_for_same_pxqty": preceding_for_same_pxqty.most_common(10),
        "timelines": timelines,
    }


def main():
    for path in LOGS:
        r = analyze(path)
        print("=" * 78)
        print("FILE:", path.split("/")[-1])
        print(
            f"  events={r['n_total']}  modify_acks={r['n_modify']}"
        )
        m = r["n_modify"] or 1
        print(
            f"  kept_priority: true={r['n_kept_true']} "
            f"({100 * r['n_kept_true'] / m:.1f}%)  "
            f"false={r['n_kept_false']} "
            f"({100 * r['n_kept_false'] / m:.1f}%)  "
            f"missing={r['n_kept_missing']}"
        )
        print(
            f"  same px+qty vs prior state: {r['n_same_pxqty']} "
            f"({100 * r['n_same_pxqty'] / m:.1f}%)"
        )
        print(
            f"  same px, DIFF qty:          {r['n_same_px_diff_qty']} "
            f"({100 * r['n_same_px_diff_qty'] / m:.1f}%)"
        )
        print(
            f"    of which AFTER a fill on same oid: "
            f"{r['n_same_px_diff_qty_after_fill']} "
            f"({100 * r['n_same_px_diff_qty_after_fill'] / m:.1f}%)"
        )
        print(
            f"    of which with NO fill since last ack: "
            f"{r['n_same_px_diff_qty_no_fill']}"
        )
        print(
            f"  diff px: {r['n_diff_px']} "
            f"({100 * r['n_diff_px'] / m:.1f}%)"
        )
        print(f"  no prior state on file: {r['n_no_prior']}")
        print(
            "  preceding-event tuples for same-px+qty modifies (top):"
        )
        for tup, c in r["preceding_for_same_pxqty"]:
            print(f"    {tup}: {c}")
        print("  --- sampled timelines (top 5 oids by modify count) ---")
        for oid, rows in r["timelines"].items():
            print(f"  order_id={oid}  ({len(rows)} relevant events)")
            for row in rows[:14]:
                print(
                    f"    ts={row['ts']} kind={row['kind']:11s} "
                    f"px={row['price']} qty={row['qty']} "
                    f"rem={row['remaining']} kept={row['kept_priority']}"
                )
            if len(rows) > 14:
                print(f"    ... ({len(rows) - 14} more)")
        print()


if __name__ == "__main__":
    main()
