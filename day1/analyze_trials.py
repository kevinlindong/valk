#!/usr/bin/env python3
"""
analyze_trials.py — analyze labeled trial-run logs from the day1 trading game.

Computes:
  1) per-file PnL / round counts / maker-taker / counterparty summary
  2) Strategy1 vs Strategy2 head-to-head in matched environments
  3) Bot-mix scaling at fixed interval
  4) Counterparty patterns (informed/noisy/directional, edge at fill)
  5) Repetitive-behavior fingerprints (size, lifetime, one-sided, chasing)
  6) Overfit-warning section
  7) Interval-independent recommendations (safe to adopt)

Run:
  python3 /Users/kevin/projects/valk/day1/analyze_trials.py
"""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from glob import glob
from typing import Optional

LOG_DIR = "/Users/kevin/projects/valk/day1/logs"

# Labeled trial files only. Skip raw combined_log_*.jsonl unless asked.
TRIAL_FILES = [
    "no_bots.jsonl",
    "random_bots_strategy1.jsonl",
    "random_bots_strategy2.jsonl",
    "random_crossing_bot_5second_intervals_strategy2.jsonl",
    "random_crossing_bot_5second_intervals_strategy2_2.jsonl",
    "some_bots_5second_intervals_strategy1.jsonl",
    "some_bots_5second_intervals_strategy1_2.jsonl",
    "some_bots_1.5second_intervals_strategy1.jsonl",
    "some_bots_1.5second_intervals_strategy1_3.jsonl",
    "some_bots_1.5second_intervals_strategy2_2.jsonl",
    "all_bots_5second_intervals.jsonl",
]

FEE_PER_LOT = 0.5


# ----------------------------- per-round analysis ------------------------------


@dataclass
class FillRec:
    t_ns: int
    side: str           # buy / sell
    qty: int
    price: float
    liquidity: str      # maker / taker
    counterparty: str
    order_id: int
    fair_at_fill: Optional[float] = None  # running_sum + remaining_expected_value


@dataclass
class QuoteRec:
    """Public-stream quote (any author). Used for fingerprinting."""
    order_id: int
    side: str
    price: float
    qty: int
    add_t_ns: int
    end_t_ns: Optional[int] = None
    end_kind: Optional[str] = None     # cancel / fill / open-at-eor
    fills: list = field(default_factory=list)
    modify_count: int = 0


@dataclass
class RoundRec:
    round_idx: int
    reveals: list = field(default_factory=list)         # (t_ns, value, index)
    reveal_interval_label: Optional[float] = None
    duration: Optional[float] = None
    n_reveals_total: Optional[int] = None
    settle: Optional[float] = None
    our_fills: list = field(default_factory=list)       # FillRec
    public_quotes: dict = field(default_factory=dict)   # order_id -> QuoteRec
    our_order_ids: set = field(default_factory=set)
    start_t_ns: Optional[int] = None
    end_t_ns: Optional[int] = None

    def fair_at(self, t_ns: int) -> Optional[float]:
        """Best-effort fair = running_sum + expected_remaining where
        expected_remaining = (n_reveals_total - revealed_count) * 5 (uniform 1..10 -> mean 5.5;
        but we use 5.5 since reveals are uniform 1..10).
        Actually use 5.5 for an unbiased estimator; see game.
        """
        if self.n_reveals_total is None:
            return None
        # Revealed so far at time t
        running = 0
        revealed = 0
        for rt, rv, _ in self.reveals:
            if rt <= t_ns:
                running += rv
                revealed += 1
        remaining = self.n_reveals_total - revealed
        # die values 1..10 average to 5.5
        return running + remaining * 5.5

    def reveal_index_at(self, t_ns: int) -> int:
        k = 0
        for rt, _, _ in self.reveals:
            if rt <= t_ns:
                k += 1
        return k


def infer_reveal_interval_label(path: str) -> Optional[float]:
    name = os.path.basename(path)
    if "1.5second" in name:
        return 1.5
    if "5second" in name:
        return 5.0
    return None


def infer_reveal_interval_measured(rounds: list) -> Optional[float]:
    gaps = []
    for r in rounds:
        ts = [t for t, _, _ in r.reveals]
        ts.sort()
        for a, b in zip(ts, ts[1:]):
            gaps.append((b - a) / 1e9)
    if not gaps:
        return None
    return round(statistics.median(gaps), 2)


def parse_file(path: str) -> tuple[list, dict]:
    """Return list[RoundRec] and a file-level meta dict."""
    rounds: list[RoundRec] = []
    cur: Optional[RoundRec] = None
    meta = {"path": path, "reveal_interval_label": infer_reveal_interval_label(path)}
    # We also want declared reveal_interval from the first game_state.
    declared_iv = None
    declared_duration = None

    def new_round():
        nonlocal cur
        cur = RoundRec(round_idx=len(rounds))

    with open(path, "r") as f:
        for raw in f:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            kind = ev.get("kind")
            t_ns = ev.get("t_ns", 0)
            if kind == "game_state":
                if declared_iv is None and ev.get("reveal_interval"):
                    declared_iv = ev.get("reveal_interval")
                    declared_duration = ev.get("duration")
                phase = ev.get("phase")
                if phase == "running":
                    if cur is None:
                        new_round()
                        cur.start_t_ns = t_ns
                        cur.duration = declared_duration
                        cur.reveal_interval_label = declared_iv
                        cur.n_reveals_total = (
                            int(round(declared_duration / declared_iv))
                            if (declared_duration and declared_iv)
                            else None
                        )
                elif phase == "settled":
                    if cur is not None:
                        cur.end_t_ns = t_ns
                        rounds.append(cur)
                        cur = None
                # waiting -> ignore
                continue

            if cur is None:
                # Allow bootstrap: if we see the first reveal/quote/fill without
                # a "running" transition, start a round defensively.
                if kind in ("reveal", "quote_add", "quote_fill", "trade", "fill"):
                    new_round()
                    cur.start_t_ns = t_ns
                    cur.duration = declared_duration
                    cur.reveal_interval_label = declared_iv
                    cur.n_reveals_total = (
                        int(round(declared_duration / declared_iv))
                        if (declared_duration and declared_iv)
                        else None
                    )
                else:
                    continue

            if kind == "reveal":
                cur.reveals.append((t_ns, ev["value"], ev.get("index", len(cur.reveals) + 1)))
            elif kind == "settlement":
                cur.settle = ev.get("underlying")
                cur.end_t_ns = t_ns
                rounds.append(cur)
                cur = None
            elif kind == "quote_add":
                oid = ev.get("order_id")
                if oid is not None:
                    cur.public_quotes[oid] = QuoteRec(
                        order_id=oid,
                        side=ev.get("side"),
                        price=ev.get("price"),
                        qty=ev.get("qty"),
                        add_t_ns=t_ns,
                    )
            elif kind == "quote_modify":
                oid = ev.get("order_id")
                q = cur.public_quotes.get(oid)
                if q:
                    q.modify_count += 1
                    if "price" in ev:
                        q.price = ev["price"]
                    if "qty" in ev:
                        q.qty = ev["qty"]
            elif kind == "quote_cancel":
                oid = ev.get("order_id")
                q = cur.public_quotes.get(oid)
                if q and q.end_t_ns is None:
                    q.end_t_ns = t_ns
                    q.end_kind = "cancel"
            elif kind == "quote_fill":
                oid = ev.get("order_id")
                q = cur.public_quotes.get(oid)
                if q:
                    q.fills.append((t_ns, ev.get("price"), ev.get("qty")))
                    if ev.get("remaining", 1) == 0 and q.end_t_ns is None:
                        q.end_t_ns = t_ns
                        q.end_kind = "fill"
            elif kind == "order_ack":
                oid = ev.get("order_id")
                if oid is not None:
                    cur.our_order_ids.add(oid)
            elif kind == "fill":
                fr = FillRec(
                    t_ns=t_ns,
                    side=ev["side"],
                    qty=ev["qty"],
                    price=ev["price"],
                    liquidity=ev.get("liquidity"),
                    counterparty=ev.get("counterparty", "?"),
                    order_id=ev.get("order_id"),
                )
                fr.fair_at_fill = cur.fair_at(t_ns)
                cur.our_fills.append(fr)
                if ev.get("order_id") is not None:
                    cur.our_order_ids.add(ev["order_id"])

    if cur is not None:
        rounds.append(cur)

    meta["declared_reveal_interval"] = declared_iv
    meta["declared_duration"] = declared_duration
    meta["n_rounds"] = len(rounds)
    return rounds, meta


# ----------------------------- aggregations ------------------------------


def pnl_for_fill(f: FillRec, settle: float) -> float:
    sign = 1 if f.side == "buy" else -1
    # Fee is per lot, taken on both maker and taker in this game.
    return f.qty * sign * (settle - f.price) - FEE_PER_LOT * f.qty


def edge_at_fill(f: FillRec) -> Optional[float]:
    if f.fair_at_fill is None:
        return None
    # For our buy: we earn (fair - price). For our sell: (price - fair).
    return (f.fair_at_fill - f.price) if f.side == "buy" else (f.price - f.fair_at_fill)


def file_summary(path: str) -> dict:
    rounds, meta = parse_file(path)
    settled = [r for r in rounds if r.settle is not None]
    total_pnl = 0.0
    maker_lots = taker_lots = 0
    by_cp = defaultdict(lambda: {"fills": 0, "lots": 0, "pnl": 0.0, "edges": []})
    cp_buy_lots = Counter()
    cp_sell_lots = Counter()
    fills_count = 0
    edges_all = []

    for r in settled:
        for f in r.our_fills:
            fills_count += 1
            pnl = pnl_for_fill(f, r.settle)
            total_pnl += pnl
            if f.liquidity == "maker":
                maker_lots += f.qty
            elif f.liquidity == "taker":
                taker_lots += f.qty
            cp = f.counterparty
            by_cp[cp]["fills"] += 1
            by_cp[cp]["lots"] += f.qty
            by_cp[cp]["pnl"] += pnl
            e = edge_at_fill(f)
            if e is not None:
                by_cp[cp]["edges"].append(e)
                edges_all.append(e)
            if f.side == "buy":
                cp_buy_lots[cp] += f.qty
            else:
                cp_sell_lots[cp] += f.qty

    measured_iv = infer_reveal_interval_measured(settled)
    return {
        "path": path,
        "name": os.path.basename(path),
        "n_rounds": len(settled),
        "n_fills": fills_count,
        "total_pnl": total_pnl,
        "pnl_per_round": (total_pnl / len(settled)) if settled else None,
        "maker_lots": maker_lots,
        "taker_lots": taker_lots,
        "reveal_interval_label": meta["reveal_interval_label"],
        "declared_reveal_interval": meta.get("declared_reveal_interval"),
        "measured_reveal_interval": measured_iv,
        "by_cp": by_cp,
        "cp_buy_lots": cp_buy_lots,
        "cp_sell_lots": cp_sell_lots,
        "mean_edge_at_fill": (statistics.mean(edges_all) if edges_all else None),
        "rounds": settled,
    }


def classify_cp(d: dict) -> str:
    """informed / noisy / directional / mixed."""
    pnl = d["pnl"]
    lots = max(d["lots"], 1)
    pnl_per_lot = pnl / lots
    if pnl_per_lot <= -1.0:
        return "informed"
    if pnl_per_lot >= 1.0:
        return "noisy"
    return "mixed"


def directional_tag(buy_lots: int, sell_lots: int) -> str:
    tot = buy_lots + sell_lots
    if tot == 0:
        return "-"
    r = buy_lots / tot
    if r > 0.8:
        return "buyer"
    if r < 0.2:
        return "seller"
    return "two-sided"


# ----------------------------- fingerprint clustering ------------------------------


def quote_fingerprints(summary: dict, top_k: int = 8) -> list:
    """Aggregate (size, side, lifetime_bucket, end_kind) clusters across all public quotes
    that are NOT ours. Return top clusters with example order_ids.
    """
    clusters = Counter()
    examples = defaultdict(list)
    lifetimes_by_size = defaultdict(list)
    for r in summary["rounds"]:
        for oid, q in r.public_quotes.items():
            if oid in r.our_order_ids:
                continue
            lt = None
            if q.end_t_ns:
                lt = (q.end_t_ns - q.add_t_ns) / 1e9
            lt_bucket = (
                "fill" if q.end_kind == "fill"
                else "<0.5s" if (lt is not None and lt < 0.5)
                else "0.5-2s" if (lt is not None and lt < 2)
                else "2-10s" if (lt is not None and lt < 10)
                else ">10s" if lt is not None
                else "still-open"
            )
            key = (q.side, q.qty, lt_bucket, q.end_kind or "open")
            clusters[key] += 1
            if len(examples[key]) < 3:
                examples[key].append(oid)
            if lt is not None:
                lifetimes_by_size[(q.side, q.qty)].append(lt)
    out = []
    for key, n in clusters.most_common(top_k):
        out.append({"key": key, "count": n, "examples": examples[key]})
    return out


def detect_repetitive_patterns(summary: dict) -> dict:
    """Look for: same-size quotes by side, very short lifetimes (anti-snipers / chasers),
    one-sided pressure, mid-chasing modify chains.
    """
    rep = {
        "size_histogram_by_side": defaultdict(Counter),
        "very_short_lifetime_count": 0,
        "very_short_lifetime_examples": [],
        "modify_chain_max": 0,
        "modify_chain_examples": [],
        "one_sided_round_ratio": [],
    }
    for r in summary["rounds"]:
        buy_lots = sell_lots = 0
        for oid, q in r.public_quotes.items():
            if oid in r.our_order_ids:
                continue
            rep["size_histogram_by_side"][q.side][q.qty] += 1
            if q.end_kind == "cancel" and q.end_t_ns is not None:
                lt = (q.end_t_ns - q.add_t_ns) / 1e9
                if lt < 0.2:
                    rep["very_short_lifetime_count"] += 1
                    if len(rep["very_short_lifetime_examples"]) < 5:
                        rep["very_short_lifetime_examples"].append((oid, lt, q.side, q.price))
            if q.modify_count >= 3:
                if q.modify_count > rep["modify_chain_max"]:
                    rep["modify_chain_max"] = q.modify_count
                if len(rep["modify_chain_examples"]) < 5:
                    rep["modify_chain_examples"].append((oid, q.modify_count, q.side))
            if q.side == "buy":
                buy_lots += q.qty
            else:
                sell_lots += q.qty
        tot = buy_lots + sell_lots
        if tot > 0:
            rep["one_sided_round_ratio"].append(buy_lots / tot)
    return rep


# ----------------------------- reporting ------------------------------


def fmt(x, w=8, prec=1):
    if x is None:
        return " " * w
    if isinstance(x, float):
        return f"{x:>{w}.{prec}f}"
    return f"{x:>{w}}"


def print_per_file(summaries: list[dict]):
    print("=" * 95)
    print("1) PER-FILE SUMMARY")
    print("=" * 95)
    print(
        f"{'file':56s} {'rds':>4s} {'fills':>5s} {'pnl':>9s} {'pnl/rd':>8s} "
        f"{'mk':>4s} {'tk':>4s} {'iv':>4s}"
    )
    for s in summaries:
        print(
            f"{s['name']:56s} {s['n_rounds']:>4d} {s['n_fills']:>5d} "
            f"{s['total_pnl']:>9.1f} {fmt(s['pnl_per_round'])} "
            f"{s['maker_lots']:>4d} {s['taker_lots']:>4d} "
            f"{fmt(s['measured_reveal_interval'], 4, 1)}"
        )
    print()


def print_strategy_head_to_head(summaries: list[dict]):
    print("=" * 95)
    print("2) STRATEGY1 vs STRATEGY2 HEAD-TO-HEAD")
    print("=" * 95)

    def pick(predicate):
        return [s for s in summaries if predicate(s["name"])]

    matched_envs = [
        ("some_bots_5s",     lambda n: "some_bots_5second" in n),
        ("some_bots_1.5s",   lambda n: "some_bots_1.5second" in n),
        ("random_bots",      lambda n: n.startswith("random_bots_") or "random_crossing" in n),
    ]
    for label, pred in matched_envs:
        s1 = [s for s in pick(pred) if "strategy1" in s["name"]]
        s2 = [s for s in pick(pred) if "strategy2" in s["name"]]
        if not (s1 and s2):
            print(f"  [{label}] missing strategy1 or strategy2 trials; skip")
            continue

        def agg(group):
            rounds = sum(s["n_rounds"] for s in group)
            pnl = sum(s["total_pnl"] for s in group)
            mk = sum(s["maker_lots"] for s in group)
            tk = sum(s["taker_lots"] for s in group)
            fills = sum(s["n_fills"] for s in group)
            edges = [
                e for s in group for r in s["rounds"]
                for f in r.our_fills
                for e in [edge_at_fill(f)] if e is not None
            ]
            adverse = sum(1 for e in edges if e < 0)
            return {
                "rounds": rounds, "pnl": pnl, "mk": mk, "tk": tk,
                "fills": fills,
                "adverse_rate": adverse / len(edges) if edges else 0,
                "mean_edge": statistics.mean(edges) if edges else 0,
            }

        a1 = agg(s1)
        a2 = agg(s2)
        print(f"  [{label}]")
        for tag, a, group in (("strat1", a1, s1), ("strat2", a2, s2)):
            print(
                f"    {tag}: rds={a['rounds']:>3d} pnl={a['pnl']:>8.1f} "
                f"pnl/rd={a['pnl']/max(a['rounds'],1):>7.1f} "
                f"fills={a['fills']:>3d} mk/tk={a['mk']}/{a['tk']} "
                f"adverse={a['adverse_rate']:.2%} mean_edge={a['mean_edge']:+.2f}"
            )
    print()


def print_bot_mix(summaries: list[dict]):
    print("=" * 95)
    print("3) BOT-MIX SCALING")
    print("=" * 95)
    rows = []
    for s in summaries:
        n = s["name"]
        if "no_bots" in n:
            mix = "none"
        elif "random_crossing" in n:
            mix = "random_crossing"
        elif n.startswith("random_bots_"):
            mix = "random"
        elif "some_bots" in n:
            mix = "some"
        elif "all_bots" in n:
            mix = "all"
        else:
            mix = "?"
        strat = "s2" if "strategy2" in n else ("s1" if "strategy1" in n else "?")
        iv = s.get("measured_reveal_interval")
        rows.append((mix, strat, iv, s["n_rounds"], s["total_pnl"], s["pnl_per_round"], n))
    rows.sort(key=lambda r: (r[0], r[1], r[2] if r[2] is not None else 1e9))
    print(f"  {'mix':16s} {'strat':5s} {'iv':>5s} {'rds':>4s} {'pnl':>9s} {'pnl/rd':>8s} file")
    for mix, strat, iv, rds, pnl, prd, name in rows:
        print(
            f"  {mix:16s} {strat:5s} {fmt(iv, 5, 1)} {rds:>4d} "
            f"{pnl:>9.1f} {fmt(prd, 8, 1)} {name}"
        )
    print()


def print_counterparties(summaries: list[dict]):
    print("=" * 95)
    print("4) COUNTERPARTY PATTERNS")
    print("=" * 95)
    # Aggregate across all bot-arena files (skip no_bots).
    agg = defaultdict(lambda: {"fills": 0, "lots": 0, "pnl": 0.0, "edges": [],
                                "buy_lots": 0, "sell_lots": 0, "files": set()})
    for s in summaries:
        if "no_bots" in s["name"]:
            continue
        for cp, d in s["by_cp"].items():
            agg[cp]["fills"] += d["fills"]
            agg[cp]["lots"] += d["lots"]
            agg[cp]["pnl"] += d["pnl"]
            agg[cp]["edges"].extend(d["edges"])
            agg[cp]["buy_lots"] += s["cp_buy_lots"].get(cp, 0)
            agg[cp]["sell_lots"] += s["cp_sell_lots"].get(cp, 0)
            agg[cp]["files"].add(s["name"])

    print(f"  {'cp':6s} {'fills':>5s} {'lots':>5s} {'pnl':>9s} {'$/lot':>7s} "
          f"{'edge':>6s} {'buy/sell':>10s} {'cls':>10s} {'dir':>10s}")
    sorted_cps = sorted(agg.items(), key=lambda kv: kv[1]["pnl"])
    for cp, d in sorted_cps:
        lots = max(d["lots"], 1)
        edge = (statistics.mean(d["edges"]) if d["edges"] else 0)
        cls = classify_cp(d)
        dirt = directional_tag(d["buy_lots"], d["sell_lots"])
        print(
            f"  {cp:6s} {d['fills']:>5d} {d['lots']:>5d} {d['pnl']:>9.1f} "
            f"{d['pnl']/lots:>7.2f} {edge:>6.2f} "
            f"{str(d['buy_lots'])+'/'+str(d['sell_lots']):>10s} "
            f"{cls:>10s} {dirt:>10s}"
        )
    print()


def print_repetitive(summaries: list[dict]):
    print("=" * 95)
    print("5) REPETITIVE BEHAVIOR FINGERPRINTS")
    print("=" * 95)
    for s in summaries:
        if "no_bots" in s["name"]:
            continue
        fps = quote_fingerprints(s, top_k=6)
        rep = detect_repetitive_patterns(s)
        size_buy = rep["size_histogram_by_side"]["buy"].most_common(3)
        size_sell = rep["size_histogram_by_side"]["sell"].most_common(3)
        print(f"  -- {s['name']}")
        print(f"     top quote clusters (side,qty,lifetime,end): "
              f"{[(k, n) for k, n in [(c['key'], c['count']) for c in fps]]}")
        print(f"     bid size top: {size_buy} | ask size top: {size_sell}")
        print(f"     very-short-lifetime cancels (<0.2s): {rep['very_short_lifetime_count']}")
        if rep["modify_chain_max"]:
            print(f"     max modify chain: {rep['modify_chain_max']} examples={rep['modify_chain_examples'][:3]}")
        if rep["one_sided_round_ratio"]:
            mean_buy_share = statistics.mean(rep["one_sided_round_ratio"])
            extreme = sum(1 for x in rep["one_sided_round_ratio"] if x < 0.1 or x > 0.9)
            print(f"     mean buy_lots_share/round={mean_buy_share:.2f} extreme_rounds={extreme}/{len(rep['one_sided_round_ratio'])}")
    print()


def print_exploits_and_recommendations(summaries: list[dict]):
    print("=" * 95)
    print("6) OVERFIT WARNINGS + 7) INTERVAL-INDEPENDENT RECOMMENDATIONS")
    print("=" * 95)

    # Cross-file counterparty stats (re-use of print_counterparties data)
    agg = defaultdict(lambda: {"fills": 0, "lots": 0, "pnl": 0.0, "edges": [],
                                "buy_lots": 0, "sell_lots": 0, "files": set()})
    for s in summaries:
        if "no_bots" in s["name"]:
            continue
        for cp, d in s["by_cp"].items():
            agg[cp]["fills"] += d["fills"]
            agg[cp]["lots"] += d["lots"]
            agg[cp]["pnl"] += d["pnl"]
            agg[cp]["edges"].extend(d["edges"])
            agg[cp]["buy_lots"] += s["cp_buy_lots"].get(cp, 0)
            agg[cp]["sell_lots"] += s["cp_sell_lots"].get(cp, 0)
            agg[cp]["files"].add(s["name"])
    sorted_cps = sorted(agg.items(), key=lambda kv: kv[1]["pnl"])

    informed_set_v2 = {"VALKC", "VALKM", "VALKN", "VALKJ"}
    observed_informed = {cp for cp, d in agg.items()
                         if d["lots"] >= 4 and d["pnl"] / max(d["lots"], 1) <= -1.0}
    new_informed = observed_informed - informed_set_v2
    missing_informed = informed_set_v2 - observed_informed

    print("  Counterparty 'informed' set audit:")
    print(f"    v2-hardcoded: {sorted(informed_set_v2)}")
    print(f"    empirically informed (>=4 lots, pnl/lot<=-1.0): {sorted(observed_informed)}")
    print(f"    -> ADD to informed: {sorted(new_informed)}")
    print(f"    -> REMOVE/audit: {sorted(missing_informed)}")
    print()

    directional = []
    for cp, d in agg.items():
        if d["lots"] < 4:
            continue
        dt = directional_tag(d["buy_lots"], d["sell_lots"])
        if dt in ("buyer", "seller"):
            directional.append((cp, dt, d["buy_lots"], d["sell_lots"],
                               d["pnl"] / max(d["lots"], 1), len(d["files"])))
    directional.sort(key=lambda x: -abs(x[2] - x[3]))
    print("  Directional counterparties (>=4 lots; threshold 80/20):")
    for cp, dt, b, s, pnl_per_lot, nf in directional:
        print(f"    {cp:6s} {dt:7s} buy/sell={b}/{s} pnl/lot={pnl_per_lot:+.2f} in_files={nf}")
    print()

    print("  -- worst counterparties (most $ lost to):")
    for cp, d in sorted_cps[:5]:
        files = ", ".join(sorted(d["files"]))[:60]
        print(f"    {cp:6s} pnl={d['pnl']:>7.1f} lots={d['lots']:>3d} files={files}")
    print()
    print("  -- best counterparties (most $ won from):")
    for cp, d in reversed(sorted_cps[-5:]):
        files = ", ".join(sorted(d["files"]))[:60]
        print(f"    {cp:6s} pnl={d['pnl']:>7.1f} lots={d['lots']:>3d} files={files}")
    print()


# ----------------------------- driver ------------------------------


def main():
    paths = []
    for name in TRIAL_FILES:
        p = os.path.join(LOG_DIR, name)
        if os.path.exists(p):
            paths.append(p)
        else:
            print(f"WARN: missing {p}")
    summaries = [file_summary(p) for p in paths]
    summaries.sort(key=lambda s: s["name"])
    print_per_file(summaries)
    print_strategy_head_to_head(summaries)
    print_bot_mix(summaries)
    print_counterparties(summaries)
    print_repetitive(summaries)
    print_exploits_and_recommendations(summaries)


if __name__ == "__main__":
    main()
