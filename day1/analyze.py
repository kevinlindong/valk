"""
Analyze a JSONL log from probe.py or run_combined.py and surface findings.

Run:
    python day1/analyze.py                                          # newest in day1/logs/
    python day1/analyze.py --log day1/logs/combined_log_20260518_220803.jsonl
    python day1/analyze.py --log day1/probe_log.jsonl               # legacy single-file logs

Sections printed:
  1. Event-type summary
  2. Discovered (undocumented) WS message types
  3. Phase / round history with settlements
  4. Market structure (quote sizes, lifetimes, churn, spreads)
  5. Public-tape aggressor balance
  6. OUR activity (fills, fee estimate, churn)
  7. Per-counterparty fingerprint
  8. Actionable findings (parameter recommendations)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any


def _newest_default_log() -> str:
    """Return the newest JSONL log under day1/logs/, falling back to legacy
    locations (day1/*.jsonl) so old logs are still discoverable."""
    patterns = (
        "day1/logs/combined_log_*.jsonl",
        "day1/logs/probe_log_*.jsonl",
        "day1/logs/*.jsonl",
        "day1/combined_log_*.jsonl",   # legacy
        "day1/probe_log_*.jsonl",      # legacy
        "day1/probe_log.jsonl",        # legacy
    )
    candidates: list[str] = []
    for p in patterns:
        candidates.extend(glob.glob(p))
    candidates = sorted(set(candidates), key=os.path.getmtime)
    return candidates[-1] if candidates else "day1/logs/probe_log.jsonl"


MAKER_FEE = 0.5  # adjust if game state says otherwise
TAKER_FEE = 0.5


def load(path: str) -> list[dict]:
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def event_type(r: dict) -> str:
    """Unified type label across our typed handlers and raw catch-all records."""
    k = r.get("kind")
    if k == "raw_unknown_msg":
        return r.get("type") or "raw_unknown"
    return k or "?"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(q * (len(s) - 1))))
    return s[idx]


def print_header(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze a probe_log.jsonl")
    parser.add_argument("--log", default=None,
                        help="JSONL log path. Default: newest day1/probe_log_*.jsonl")
    args = parser.parse_args()
    if args.log is None:
        args.log = _newest_default_log()
        if not os.path.exists(args.log):
            print(f"No log file found in day1/logs/ (or legacy day1/). "
                  f"Run probe.py or run_combined.py first, or pass --log.")
            sys.exit(1)

    records = load(args.log)
    if not records:
        print(f"No records in {args.log}")
        return
    print(f"Loaded {len(records):,} records from {args.log}")

    # -------- 1. event counts --------
    print_header("1. Event-type counts")
    counts: Counter = Counter(event_type(r) for r in records)
    for k, v in counts.most_common():
        print(f"  {k:<30s} {v:>7d}")

    # -------- 2. discovered raw types --------
    raw = [r for r in records if r.get("kind") == "raw_unknown_msg"]
    raw_types = Counter(r.get("type") for r in raw)
    if raw_types:
        print_header("2. Undocumented WS message types (via on_message catch-all)")
        for t, n in raw_types.most_common():
            print(f"  {t:<30s} {n:>7d}")

    # -------- 3. phase / round history --------
    print_header("3. Phase history + settlements")
    phase_seq: list[str] = []
    for r in records:
        if r.get("kind") == "game_state":
            p = r.get("phase")
            if p and (not phase_seq or phase_seq[-1] != p):
                phase_seq.append(p)
    print(f"  Phase sequence: {' -> '.join(phase_seq) or '(none)'}")
    settlements = [r for r in records if r.get("kind") == "settlement"]
    print(f"  Settlements observed: {len(settlements)}")
    for s in settlements:
        print(f"    prices={s.get('prices')}  pnl={s.get('pnl')}")

    # -------- 4. market structure --------
    print_header("4. Market structure")
    adds = [r for r in raw if r.get("type") == "quote_add"]
    cancels = [r for r in raw if r.get("type") == "quote_cancel"]
    fills_pub = [r for r in raw if r.get("type") == "quote_fill"]

    if adds:
        sizes = [a.get("qty", 0) for a in adds if a.get("qty")]
        print(f"  quote_add events: {len(adds):,}")
        print(f"    qty   median={statistics.median(sizes):.1f}  "
              f"p25={percentile(sizes, 0.25):.0f}  "
              f"p75={percentile(sizes, 0.75):.0f}  "
              f"max={max(sizes)}")

    if cancels:
        print(f"  quote_cancel events: {len(cancels):,}")
        reasons = Counter(c.get("reason") for c in cancels)
        for r_, n in reasons.most_common():
            print(f"    reason={r_}: {n}")

    if adds and cancels:
        ratio = len(adds) / max(len(cancels), 1)
        print(f"  add/cancel ratio: {ratio:.2f}  "
              f"({'pure churn' if abs(ratio - 1) < 0.1 else 'book growing' if ratio > 1.1 else 'book shrinking'})")

    # Quote lifetimes by order_id
    add_t: dict[int, int] = {}
    for a in adds:
        oid = a.get("order_id")
        if oid is not None:
            add_t[oid] = a.get("ts_ns", a.get("t_ns", 0))
    life_cancel: list[float] = []
    life_fill: list[float] = []
    for c in cancels:
        oid = c.get("order_id")
        if oid in add_t:
            life_cancel.append((c.get("ts_ns", c.get("t_ns", 0)) - add_t[oid]) / 1e6)
    for f in fills_pub:
        oid = f.get("order_id")
        if oid in add_t:
            life_fill.append((f.get("ts_ns", f.get("t_ns", 0)) - add_t[oid]) / 1e6)
    if life_cancel:
        print(f"  quote lifetime (cancelled): "
              f"median={statistics.median(life_cancel):.0f}ms  "
              f"mean={statistics.mean(life_cancel):.0f}ms  n={len(life_cancel)}")
    if life_fill:
        print(f"  quote lifetime (filled):    "
              f"median={statistics.median(life_fill):.0f}ms  "
              f"mean={statistics.mean(life_fill):.0f}ms  n={len(life_fill)}")

    # Spreads from book snapshots
    books = [r for r in records if r.get("kind") in ("book_periodic", "book_after_reveal")]
    spreads: list[int] = []
    best_bids: list[int] = []
    best_asks: list[int] = []
    for b in books:
        bids = b.get("bids") or []
        asks = b.get("asks") or []
        if bids and asks:
            bb = bids[0].get("price")
            ba = asks[0].get("price")
            if isinstance(bb, (int, float)) and isinstance(ba, (int, float)):
                spreads.append(int(ba - bb))
                best_bids.append(int(bb))
                best_asks.append(int(ba))
    if spreads:
        print(f"  spread:  median={statistics.median(spreads):.0f}  "
              f"p25={percentile(spreads, 0.25):.0f}  "
              f"p75={percentile(spreads, 0.75):.0f}  "
              f"max={max(spreads)}  n={len(spreads)}")
        print(f"  best_ask: median={statistics.median(best_asks):.0f}  "
              f"best_bid: median={statistics.median(best_bids):.0f}")

    # -------- 5. tape aggressor balance --------
    trades = [r for r in records if r.get("kind") == "trade"]
    if trades:
        print_header("5. Public-tape aggressor balance")
        agg = Counter(t.get("aggressor") for t in trades)
        total = sum(agg.values())
        for a, n in agg.most_common():
            pct = n / total * 100 if total else 0
            print(f"  aggressor={a}: {n} ({pct:.1f}%)")
        sizes = [t.get("qty", 0) for t in trades]
        print(f"  trade qty median={statistics.median(sizes):.1f}  "
              f"p75={percentile(sizes, 0.75):.0f}  max={max(sizes)}")

    # -------- 6. our activity --------
    our_fills = [r for r in records if r.get("kind") == "fill"]
    our_acks = [r for r in records if r.get("kind") == "ack"]
    print_header("6. Our activity")
    print(f"  Our fills: {len(our_fills)}")
    print(f"  Our acks (orders sent): {len(our_acks)}")
    if our_fills:
        qtys = [f.get("qty", 0) for f in our_fills]
        total_qty = sum(qtys)
        taker_qty = sum(f.get("qty", 0) for f in our_fills if f.get("liquidity") == "taker")
        maker_qty = sum(f.get("qty", 0) for f in our_fills if f.get("liquidity") == "maker")
        est_fees = taker_qty * TAKER_FEE + maker_qty * MAKER_FEE
        print(f"  Volume:   total={total_qty}  taker={taker_qty}  maker={maker_qty}")
        print(f"  Avg fill size: {statistics.mean(qtys):.1f}")
        print(f"  Est. fees (taker {TAKER_FEE} + maker {MAKER_FEE} per lot): "
              f"{est_fees:.1f}")

    # -------- 6b. per-round breakdown --------
    # Slice the log by 'running' segments (phase running -> settled boundaries)
    # so we can see how each round went.
    if settlements:
        print_header("6b. Per-round breakdown")
        # Build round boundaries by walking the records in order
        rounds: list[dict] = []
        current: dict | None = None
        for r in records:
            k = r.get("kind")
            if k == "game_state" and r.get("phase") == "running":
                if current is None or current.get("closed"):
                    current = {"fills": [], "trades": 0, "closed": False, "start_t": r.get("t_ns")}
                    rounds.append(current)
            elif k == "settlement" and current is not None and not current.get("closed"):
                current["settle"] = r.get("prices", {}).get("A")
                current["pnl"] = r.get("pnl")
                current["closed"] = True
            elif k == "fill" and current is not None and not current.get("closed"):
                current["fills"].append(r)
            elif k == "trade" and current is not None and not current.get("closed"):
                current["trades"] += 1
        print(f"  {'#':>3s} {'settle':>7s} {'pnl':>7s} {'fills':>6s} "
              f"{'vol':>5s} {'maker':>6s} {'taker':>6s} {'fees':>7s} "
              f"{'gross':>7s} {'trades':>7s}")
        for i, rd in enumerate(rounds, 1):
            if not rd.get("closed"):
                continue
            fills = rd["fills"]
            vol = sum(f.get("qty", 0) for f in fills)
            mk = sum(f.get("qty", 0) for f in fills if f.get("liquidity") == "maker")
            tk = sum(f.get("qty", 0) for f in fills if f.get("liquidity") == "taker")
            fees = mk * MAKER_FEE + tk * TAKER_FEE
            pnl = rd.get("pnl") or 0
            gross = pnl + fees  # what we'd have made with zero fees
            print(f"  {i:>3d} {str(rd.get('settle', '?')):>7s} "
                  f"{pnl:>+7.0f} {len(fills):>6d} {vol:>5d} "
                  f"{mk:>6d} {tk:>6d} {fees:>7.1f} {gross:>+7.0f} "
                  f"{rd['trades']:>7d}")

    # -------- 7. per-counterparty --------
    cp_summaries = [r for r in records if r.get("kind") == "counterparty_summary"]
    if cp_summaries:
        latest = cp_summaries[-1].get("stats") or {}
        if latest:
            print_header("7. Per-counterparty (last counterparty_summary in log)")
            print(f"  {'counterparty':<20s} {'fills':>6s} {'wbought':>8s} "
                  f"{'wsold':>7s} {'wmaker':>7s} {'wtaker':>7s} {'vwap':>8s}")
            items = sorted(latest.items(), key=lambda kv: -kv[1].get("n_fills", 0))
            for cp, st in items:
                print(f"  {cp[:20]:<20s} {st.get('n_fills', 0):>6d} "
                      f"{st.get('qty_we_bought', 0):>8d} "
                      f"{st.get('qty_we_sold', 0):>7d} "
                      f"{st.get('our_role_maker', 0):>7d} "
                      f"{st.get('our_role_taker', 0):>7d} "
                      f"{st.get('avg_price', 0):>8.2f}")

    # -------- 8. actionable findings --------
    print_header("8. Actionable findings")
    findings: list[str] = []
    if adds:
        market_med_size = statistics.median(sizes if (sizes := [a.get("qty", 0) for a in adds]) else [0])
        if market_med_size and market_med_size <= 7:
            findings.append(
                f"  - Market median quote size is {market_med_size:.0f}. If your "
                f"quote_qty exceeds this, you become the fattest target at "
                f"every best price -> oversized maker fees. Match it (qty 3-7)."
            )
    if life_cancel and statistics.median(life_cancel) <= 1500:
        findings.append(
            f"  - Median quote lifetime is {statistics.median(life_cancel):.0f}ms. "
            f"Bots reprice fast. If your strategy only reacts to reveals "
            f"(every 30s), you miss most stale-quote windows. Subscribe to "
            f"quote_add/cancel events via SDK on_message and snipe in-flight."
        )
    if spreads and statistics.median(spreads) <= 3:
        findings.append(
            f"  - Median market spread is {statistics.median(spreads):.0f} ticks. "
            f"Your snipe_min_edge must EXCEED taker_fee + 1 tick or you're "
            f"trading noise. Use >= {TAKER_FEE + 1.5:.1f} as a floor."
        )
    if our_fills:
        taker_share = (sum(1 for f in our_fills if f.get('liquidity') == 'taker')
                       / len(our_fills))
        if taker_share > 0.5:
            findings.append(
                f"  - {taker_share:.0%} of your fills are taker (you snipe more "
                f"than you make). Each taker fill pays TAKER_FEE. If snipes "
                f"don't have edge > 2*TAKER_FEE, you bleed on fees alone."
            )
    cancel_reasons = Counter(c.get("reason") for c in cancels)
    if cancel_reasons.get("self_match_prevention", 0) > 0:
        findings.append(
            f"  - {cancel_reasons['self_match_prevention']} self_match_prevention "
            f"cancels: your snipes are trying to hit your own resting quotes. "
            f"Filter own prices client-side before sending IOC."
        )
    if trades:
        agg_sell = sum(1 for t in trades if t.get("aggressor") == "sell")
        agg_buy = sum(1 for t in trades if t.get("aggressor") == "buy")
        if agg_buy + agg_sell > 0:
            ratio = agg_sell / max(agg_buy, 1)
            if ratio > 1.3:
                findings.append(
                    f"  - Sell-aggressors outnumber buy-aggressors {agg_sell}:{agg_buy}. "
                    f"Downward-pressured market - your passive bid will get "
                    f"hit more than your ask. Skew quotes accordingly."
                )
            elif ratio < 0.7:
                findings.append(
                    f"  - Buy-aggressors outnumber sell-aggressors {agg_buy}:{agg_sell}. "
                    f"Upward-pressured market - your passive ask gets hit more."
                )
    if not findings:
        findings.append("  (no notable findings -- collect more data)")
    for f in findings:
        print(f)


if __name__ == "__main__":
    main()
