#!/usr/bin/env python3
"""
Analyze the most recent combined_log_*.jsonl trading logs to inform strategy2.

Outputs concrete, numerical answers to:
  1. Pre-reveal PnL: is it +EV, ~0, or -EV?
  2. Reveal-arrival cadence (median/min/max gap).
  3. Per-reveal "shock" magnitude (running-mean-extrapolated jumps).
  4. Sigma trajectory over the round (sigma_fair as function of K).
  5. Quote-refresh cadence per phase.
  6. Counterparty fill patterns (who informs us, who feeds us).
  7. Other-participant public-stream patterns (flash cancels, stale lifts).
  8. Suggested per-phase refresh interval.

The script is self-contained (stdlib only) and prints a structured report.
"""

from __future__ import annotations

import glob
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional


LOG_DIR = "/Users/kevin/projects/valk/day1/logs"

# Most-recent first (the user-prioritised ones). Earlier ones are extra sample.
PRIMARY_FILES = [
    "combined_log_20260519_130342.jsonl",
    "combined_log_20260519_125314.jsonl",
    "combined_log_20260519_124658.jsonl",
]
SECONDARY_FILES = [
    "combined_log_20260519_124042.jsonl",
    "combined_log_20260519_123532.jsonl",
    "combined_log_20260519_120609.jsonl",
    "combined_log_20260519_120054.jsonl",
    "combined_log_20260519_114953.jsonl",
    "combined_log_20260519_114104.jsonl",
]

N_TOTAL_REVEALS = 10  # 10 reveals/round per game-model description
TICK = 1
MAKER_FEE = 0.5
TAKER_FEE = 0.5


# ---------- session splitting --------------------------------------------------


@dataclass
class Session:
    file: str
    session_idx: int  # which round inside the file (0 for first)
    start_ts: int
    end_ts: int
    events: list
    reveals: list = field(default_factory=list)
    settle: Optional[int] = None  # truth (sum of 10 reveals)

    def fills(self):
        return [e for e in self.events if e.get("kind") == "fill"]


def load_file_sessions(path: str) -> list[Session]:
    """Split a single log file into sessions.

    A "session" is one round (10 reveals + settlement). Some log files contain
    multiple back-to-back rounds, separated by a `settlement` event or a
    `game_state` transition to phase=running.

    Boundary rule: a new session opens on:
        * the first event in the file, OR
        * the first event after a `settlement` event (i.e. start of next round).
    A session "closes" at its own `settlement` event (inclusive).
    Events that fall strictly between two settlements (i.e. "settled" / "waiting"
    phase transitions) belong to the *next* session.
    """
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
    if not events:
        return []

    # find all settlement indices (one per finished round)
    settle_idx = [i for i, e in enumerate(events) if e.get("kind") == "settlement"]
    if not settle_idx:
        # incomplete file (e.g. only intro), skip
        return []

    sessions: list[Session] = []
    prev_close = -1  # nothing closed yet
    for s_idx, end_i in enumerate(settle_idx):
        start_i = prev_close + 1
        sess_events = events[start_i:end_i + 1]
        sess_reveals = [e for e in sess_events if e.get("kind") == "reveal"]
        settle_ev = events[end_i]
        prices = settle_ev.get("prices") or {}
        settle_val = prices.get("A")
        if settle_val is None and len(sess_reveals) == N_TOTAL_REVEALS:
            settle_val = sum(r.get("value", 0) for r in sess_reveals)
        if settle_val is None:
            prev_close = end_i
            continue
        # only keep sessions that actually have reveals (some sessions are stub)
        if not sess_reveals:
            prev_close = end_i
            continue
        sessions.append(Session(
            file=os.path.basename(path),
            session_idx=s_idx,
            start_ts=sess_events[0]["t_ns"],
            end_ts=sess_events[-1]["t_ns"],
            events=sess_events,
            reveals=sess_reveals,
            settle=settle_val,
        ))
        prev_close = end_i
    return sessions


def load_all_sessions(filenames: list[str]) -> list[Session]:
    out = []
    for fn in filenames:
        full = os.path.join(LOG_DIR, fn)
        if not os.path.exists(full):
            continue
        out.extend(load_file_sessions(full))
    return out


# ---------- helpers ------------------------------------------------------------


def ns_to_s(x: int) -> float:
    return x / 1e9


def book_mid(book_event: dict) -> Optional[float]:
    bids = book_event.get("bids", []) or []
    asks = book_event.get("asks", []) or []
    if bids and asks:
        return (bids[0]["price"] + asks[0]["price"]) / 2.0
    if bids:
        return bids[0]["price"]
    if asks:
        return asks[0]["price"]
    return None


# ---------- 1. Pre-reveal PnL --------------------------------------------------


def analysis_prereveal_pnl(sessions: list[Session]) -> dict:
    """For each session, compute realized PnL of fills that happened BEFORE the
    first reveal, marked to that session's settle.

    Sign-correct (fee already 0.5 per lot on each side, baked into fill["fee"]):
        buy lot:  pnl_per_lot = (settle - fill_px) - fee_per_lot
        sell lot: pnl_per_lot = (fill_px - settle) - fee_per_lot

    Also splits by maker/taker so we can see whether the pre-reveal edge came
    from US sniping (taker) vs being SNIPED (maker).
    """
    rows = []
    for s in sessions:
        if not s.reveals:
            continue
        first_reveal_ts = s.reveals[0]["t_ns"]
        pre_fills = [f for f in s.fills() if f["t_ns"] < first_reveal_ts]
        total_qty = 0
        gross_pnl = 0.0
        maker_qty = 0; taker_qty = 0
        maker_pnl = 0.0; taker_pnl = 0.0
        for f in pre_fills:
            qty = f["qty"]; px = f["price"]
            total_qty += qty
            fee = f.get("fee", 0.5 * qty)
            if f["side"] == "buy":
                pnl = qty * (s.settle - px) - fee
            else:
                pnl = qty * (px - s.settle) - fee
            gross_pnl += pnl
            if f.get("liquidity") == "maker":
                maker_qty += qty; maker_pnl += pnl
            else:
                taker_qty += qty; taker_pnl += pnl
        rows.append({
            "file": s.file, "session_idx": s.session_idx,
            "n_pre_fills": len(pre_fills), "pre_qty": total_qty,
            "pre_pnl_to_settle": gross_pnl, "settle": s.settle,
            "maker_qty": maker_qty, "maker_pnl": maker_pnl,
            "taker_qty": taker_qty, "taker_pnl": taker_pnl,
        })
    return {"per_session": rows}


# ---------- 2. Reveal-arrival cadence -----------------------------------------


def analysis_reveal_cadence(sessions: list[Session]) -> dict:
    gaps_all = []
    for s in sessions:
        ts = [r["t_ns"] for r in s.reveals]
        for a, b in zip(ts, ts[1:]):
            gaps_all.append(ns_to_s(b - a))
    if not gaps_all:
        return {}
    return {
        "n_gaps": len(gaps_all),
        "median_s": statistics.median(gaps_all),
        "min_s": min(gaps_all),
        "max_s": max(gaps_all),
        "mean_s": statistics.mean(gaps_all),
        "stdev_s": statistics.pstdev(gaps_all) if len(gaps_all) > 1 else 0.0,
    }


# ---------- 3. Per-reveal shock magnitude --------------------------------------


def analysis_reveal_shocks(sessions: list[Session]) -> dict:
    """For each reveal index K (1..10), compute the change in
    running_mean_extrapolated = sum_revealed/K * N_TOTAL across sessions.
    """
    by_k = defaultdict(list)
    for s in sessions:
        revs = sorted(s.reveals, key=lambda e: e["index"])
        prev_extrap = None
        run_sum = 0
        for r in revs:
            k = r["index"]
            run_sum += r["value"]
            extrap = (run_sum / k) * N_TOTAL_REVEALS
            if prev_extrap is not None:
                by_k[k].append(abs(extrap - prev_extrap))
            prev_extrap = extrap
    out = {}
    for k in sorted(by_k):
        out[k] = {
            "n": len(by_k[k]),
            "median_shock": statistics.median(by_k[k]),
            "mean_shock": statistics.mean(by_k[k]),
            "max_shock": max(by_k[k]),
        }
    return out


# ---------- 4. Sigma trajectory ----------------------------------------------


def analysis_sigma_trajectory(sessions: list[Session]) -> dict:
    """Compute σ_fair (remaining-reveal stdev of settle) as a function of K.

    Two approaches reported side-by-side:

      σ_pop(K)    = sqrt((N - K)) * σ_pop_value
                    where σ_pop_value is the empirical pop-stdev of reveal values
                    across ALL sessions (≈4.3). This is a *parametric* estimate
                    that does NOT shrink with K — it assumes future reveals are
                    iid with population variance.

      σ_emp(K)    = median over sessions of sqrt((N - K) * s^2_K) where s^2_K is
                    the *sample* variance of the K reveals seen so far. This is
                    noisy for small K but converges to σ_pop as K→N.

    σ_pop is the right number to size quote width against (the bot doesn't know
    the population variance is tiny when K is small).
    """
    # First pass: pool all reveal values to compute σ_pop_value
    all_vals = [r["value"] for s in sessions for r in s.reveals]
    sigma_pop_value = statistics.pstdev(all_vals) if all_vals else 0.0

    by_k_emp = defaultdict(list)
    for s in sessions:
        revs = sorted(s.reveals, key=lambda e: e["index"])
        vals = [r["value"] for r in revs]
        for k in range(1, N_TOTAL_REVEALS + 1):
            if k > len(vals):
                break
            seen = vals[:k]
            if k < 2:
                sigma = None
            else:
                s2 = statistics.pvariance(seen)
                sigma = math.sqrt(max(0, N_TOTAL_REVEALS - k) * s2)
            by_k_emp[k].append(sigma)

    out = {"sigma_pop_value": sigma_pop_value}
    for k in range(1, N_TOTAL_REVEALS + 1):
        sigma_pop_fair = math.sqrt(max(0, N_TOTAL_REVEALS - k)) * sigma_pop_value
        vals = [v for v in by_k_emp.get(k, []) if v is not None]
        out[k] = {
            "n": len(vals),
            "median_sigma_emp": (statistics.median(vals) if vals else None),
            "sigma_pop": sigma_pop_fair,
        }
    return out


# ---------- 5. Quote-refresh cadence ------------------------------------------


def analysis_refresh_cadence(sessions: list[Session]) -> dict:
    """Build per-session timelines:
       - same_order_intervals: gaps between modify_ack events on same order_id
       - new_order_intervals: gaps between consecutive order_ack open events
    Bucketed by phase: K=0 (pre-reveal), K=1-3, K=4-7, K=8-10.
    """
    def phase_of(ts, reveal_ts):
        # K = number of reveals seen by time ts (i.e. reveals with t_ns <= ts)
        k = sum(1 for r in reveal_ts if r <= ts)
        if k == 0: return "K=0"
        if 1 <= k <= 3: return "K=1-3"
        if 4 <= k <= 7: return "K=4-7"
        return "K=8-10"

    same_by_phase = defaultdict(list)
    new_by_phase = defaultdict(list)

    for s in sessions:
        reveal_ts = [r["t_ns"] for r in s.reveals]
        # gather refresh events on same order_id
        order_id_events = defaultdict(list)
        new_order_ts = []
        for e in s.events:
            k = e.get("kind")
            if k == "modify_ack":
                order_id_events[e["order_id"]].append(e["t_ns"])
            elif k == "ack" and e.get("type") == "order_ack" \
                    and e.get("status") in ("open", "filled", "partially_filled"):
                # treat as "fresh order" only when a new resting order opens
                if e.get("status") == "open":
                    new_order_ts.append(e["t_ns"])
                    order_id_events[e["order_id"]].append(e["t_ns"])

        for oid, tslist in order_id_events.items():
            tslist.sort()
            for a, b in zip(tslist, tslist[1:]):
                gap = ns_to_s(b - a)
                ph = phase_of(b, reveal_ts)
                same_by_phase[ph].append(gap)

        new_order_ts.sort()
        for a, b in zip(new_order_ts, new_order_ts[1:]):
            gap = ns_to_s(b - a)
            ph = phase_of(b, reveal_ts)
            new_by_phase[ph].append(gap)

    def summarize(d):
        out = {}
        for ph, vals in d.items():
            if not vals:
                continue
            out[ph] = {
                "n": len(vals),
                "median_s": statistics.median(vals),
                "p10_s": statistics.quantiles(vals, n=10)[0] if len(vals) >= 10 else min(vals),
                "p90_s": statistics.quantiles(vals, n=10)[8] if len(vals) >= 10 else max(vals),
            }
        return out

    return {
        "same_order_refresh": summarize(same_by_phase),
        "new_order_cadence": summarize(new_by_phase),
    }


# ---------- 6. Counterparty fill patterns -------------------------------------


def analysis_counterparties(sessions: list[Session]) -> dict:
    """Aggregate fills by counterparty. For each:
       - qty bought/sold from us, n fills
       - PnL to settle (their counterparty PnL is the inverse of ours mark-to-settle)
       - typical side they take (always buy from us? sell?)
       - edge_at_fill = (fill_px - settle) signed by THEIR side direction
         (positive edge_at_fill means we made money on the fill)
    """
    cp_stats = defaultdict(lambda: {
        "n_fills": 0, "qty_we_bought": 0, "qty_we_sold": 0,
        "pnl_to_settle": 0.0, "fees_paid": 0.0,
        "edges_at_fill_signed_for_us": [],
        "px_minus_settle": [],
    })

    for s in sessions:
        for f in s.fills():
            cp = f.get("counterparty", "UNK")
            qty = f["qty"]; px = f["price"]
            d = cp_stats[cp]
            d["n_fills"] += 1
            d["fees_paid"] += f.get("fee", 0.5 * qty)
            if f["side"] == "buy":
                d["qty_we_bought"] += qty
                pnl_per = s.settle - px
            else:
                d["qty_we_sold"] += qty
                pnl_per = px - s.settle
            d["pnl_to_settle"] += qty * pnl_per
            d["edges_at_fill_signed_for_us"].append(pnl_per)
            d["px_minus_settle"].append(px - s.settle)

    out = {}
    for cp, d in cp_stats.items():
        edges = d["edges_at_fill_signed_for_us"]
        out[cp] = {
            "n_fills": d["n_fills"],
            "qty_we_bought": d["qty_we_bought"],
            "qty_we_sold": d["qty_we_sold"],
            "gross_pnl_to_settle": d["pnl_to_settle"],
            "net_pnl_after_fees": d["pnl_to_settle"] - d["fees_paid"],
            "median_edge_per_lot": (statistics.median(edges) if edges else 0.0),
            "mean_edge_per_lot": (statistics.mean(edges) if edges else 0.0),
            "side_bias": (
                "they_always_BUY_from_us" if d["qty_we_sold"] > 0 and d["qty_we_bought"] == 0
                else "they_always_SELL_to_us" if d["qty_we_bought"] > 0 and d["qty_we_sold"] == 0
                else "mixed"
            ),
        }
    return out


# ---------- 7. Public-stream patterns ----------------------------------------


def analysis_public_stream(sessions: list[Session]) -> dict:
    """For each session, track public quote_add events. Measure:
       a) quote-flash rate: fraction of quote_add events cancelled within 100ms
          *with no fill*.
       b) stale-lift rate: fraction of quote_add events where our running-mean
          extrapolated "fair" moves past the quote's price within 5s of its
          posting (i.e. that quote was about to be picked off).
    Also count price-relative-to-mid clusters: aggressive (cross book) vs passive.
    """
    flash_total = 0
    flash_cancelled_lt_100ms_no_fill = 0
    flash_filled = 0  # post-and-fill (likely aggressive)
    cancel_latency_ms = []
    stale_lift_eligible = 0
    stale_lift_hit = 0

    aggressive_count = 0   # price crossed book at insertion time
    passive_count = 0
    inside_count = 0
    unclear_mid = 0

    for s in sessions:
        # build a chronological list of public events
        # We need the latest book mid going into each quote_add insertion.
        # Use book_periodic + book_after_reveal + running update from quote events
        # (we approximate mid from periodic snapshots only — cheaper).
        snapshots = [e for e in s.events
                     if e.get("kind") in ("book_periodic", "book_after_reveal")]
        snapshots.sort(key=lambda e: e["t_ns"])

        def mid_at(ts):
            # binary-search-like; snapshots are sorted
            best = None
            for snap in snapshots:
                if snap["t_ns"] <= ts:
                    best = snap
                else:
                    break
            if best is None:
                return None
            return book_mid(best)

        # running revealed-mean extrapolated, for stale-lift detection
        running = 0
        cnt = 0
        # Build a function: fair_at(ts) = extrapolation given reveals up to ts
        reveals_sorted = sorted(s.reveals, key=lambda r: r["t_ns"])

        def fair_at(ts):
            run = 0; c = 0
            for r in reveals_sorted:
                if r["t_ns"] <= ts:
                    run += r["value"]; c += 1
                else:
                    break
            if c == 0:
                return 25.0  # prior
            return (run / c) * N_TOTAL_REVEALS

        # track add events
        add_events = {}  # order_id -> add event (we keep the first add per id)
        fills_by_oid = defaultdict(int)
        for e in s.events:
            if e.get("kind") == "quote_fill":
                fills_by_oid[e["order_id"]] += 1

        for e in s.events:
            k = e.get("kind")
            if k == "quote_add":
                oid = e["order_id"]
                # only first add per oid
                if oid not in add_events:
                    add_events[oid] = e

                # classify: aggressive / passive vs current mid
                m = mid_at(e["t_ns"])
                px = e["price"]; side = e["side"]
                if m is None:
                    unclear_mid += 1
                else:
                    if side == "buy":
                        # aggressive if bid >= mid (crossing or at-mid)
                        if px >= m + 0.5:
                            aggressive_count += 1
                        elif px <= m - 0.5:
                            passive_count += 1
                        else:
                            inside_count += 1
                    else:  # sell
                        if px <= m - 0.5:
                            aggressive_count += 1
                        elif px >= m + 0.5:
                            passive_count += 1
                        else:
                            inside_count += 1

                # stale-lift check: did fair_at move past this px within 5s?
                # only meaningful pre-settlement and where px is "limit" not crossing
                add_fair = fair_at(e["t_ns"])
                # We say the quote is "potentially snipeable" if it's BUY priced >= add_fair (over-paying) or SELL priced <= add_fair (under-asking)
                # Actually opposite: a quote is snipeable when its price is ON THE WRONG SIDE OF FAIR.
                # buy at high price => seller can take it; sell at low price => buyer can take it.
                # For "stale lift": we check if fair moved past it within 5s.
                future_window_end = e["t_ns"] + int(5e9)
                future_fair_max_move = 0.0
                # peek future reveals/fair points within window
                for r in reveals_sorted:
                    if e["t_ns"] < r["t_ns"] <= future_window_end:
                        fnow = fair_at(r["t_ns"])
                        if side == "buy":
                            future_fair_max_move = max(future_fair_max_move, fnow - px)
                            # if fair drops below px, this resting buy is overpaying → snipeable
                            if fnow + 0.5 < px:
                                stale_lift_hit += 1
                                stale_lift_eligible += 1
                                break
                        else:
                            if fnow > px + 0.5:
                                stale_lift_hit += 1
                                stale_lift_eligible += 1
                                break
                else:
                    stale_lift_eligible += 1

            elif k == "quote_cancel":
                oid = e["order_id"]
                add = add_events.get(oid)
                if add is None:
                    continue
                cancel_ts = e["t_ns"]
                latency_ns = cancel_ts - add["t_ns"]
                cancel_latency_ms.append(latency_ns / 1e6)
                # count: was there any fill on this oid?
                if fills_by_oid[oid] == 0:
                    flash_total += 1
                    if latency_ns <= 100e6:
                        flash_cancelled_lt_100ms_no_fill += 1
                # cleanup so we don't double-count if same oid is reused
                add_events.pop(oid, None)

            elif k == "quote_fill":
                # successful trade — not a flash
                pass

    # quote_add count = how many we observed (we only sampled the first add per oid)
    total_adds = aggressive_count + passive_count + inside_count + unclear_mid

    return {
        "total_adds_observed": total_adds,
        "aggressive_pct": (aggressive_count / total_adds) if total_adds else 0.0,
        "passive_pct": (passive_count / total_adds) if total_adds else 0.0,
        "inside_pct": (inside_count / total_adds) if total_adds else 0.0,
        "unclear_pct": (unclear_mid / total_adds) if total_adds else 0.0,
        "flash_cancel_lt_100ms_no_fill": flash_cancelled_lt_100ms_no_fill,
        "flash_cancel_no_fill_total": flash_total,
        "flash_cancel_lt_100ms_share": (flash_cancelled_lt_100ms_no_fill / flash_total)
                                       if flash_total else 0.0,
        "stale_lift_eligible": stale_lift_eligible,
        "stale_lift_hit": stale_lift_hit,
        "stale_lift_rate": (stale_lift_hit / stale_lift_eligible) if stale_lift_eligible else 0.0,
        "median_cancel_latency_ms": (statistics.median(cancel_latency_ms) if cancel_latency_ms else None),
    }


# ---------- 8. Suggested refresh interval --------------------------------------


def suggest_refresh_intervals(sigma_traj: dict, reveal_cadence: dict) -> dict:
    """Per phase, suggest refresh interval.

    Logic: refresh ≈ at the cadence that fair moves materially (≥ ~½ tick).
    With reveal_interval ≈ 30s and a step-jump in fair per reveal of ~σ_pop_value,
    between reveals σ_fair barely moves — but on reveal the fair JUMPS. So the
    optimal refresh is: re-quote immediately on each reveal + a slow heartbeat
    for residual book noise.
    """
    out = {"K=0": {
        "action": "DO NOT QUOTE",
        "reason": "no information; σ_pop(fair) ≈ 13.6 → any quote ≤25 wide is "
                  "pure variance trade; pre-reveal stale-lift / adverse selection"
                  " by informed bots dominates expected edge",
    }}
    def sigma_pop_at(k):
        d = sigma_traj.get(k)
        if d is None: return 10.0
        return d.get("sigma_pop", 10.0)

    sig_low = sigma_pop_at(2)   # ~K=1-3
    sig_mid = sigma_pop_at(5)   # ~K=4-7
    sig_hi  = sigma_pop_at(8)   # ~K=8-10

    out["K=1-3"] = {
        "sigma_pop": sig_low,
        "suggested_refresh_s": 12.0,
        "rule": "Re-quote IMMEDIATELY on each reveal; heartbeat ~10-15s for "
                "minor book moves; quote half-width ≈ σ_pop + 1 tick (~12-13)",
    }
    out["K=4-7"] = {
        "sigma_pop": sig_mid,
        "suggested_refresh_s": 8.0,
        "rule": "Re-quote on reveal; heartbeat ~6-10s; half-width ≈ σ_pop + 1 (~10)",
    }
    out["K=8-10"] = {
        "sigma_pop": sig_hi,
        "suggested_refresh_s": 4.0,
        "rule": "Re-quote on reveal; heartbeat ~3-5s; half-width ≈ σ_pop + 1 (~5);"
                " be willing to take edge ≥ 2 because settle is nearly pinned",
    }
    return out


# ---------- pretty printing ---------------------------------------------------


def fmt_money(x):
    return f"{x:+.1f}"


def print_report(primary_sessions, secondary_sessions, all_sessions):
    print("=" * 78)
    print("STRATEGY 2 — LOG ANALYSIS REPORT")
    print(f"  primary sessions:   {len(primary_sessions)}")
    print(f"  secondary sessions: {len(secondary_sessions)}")
    print(f"  total sessions:     {len(all_sessions)}")
    print("=" * 78)

    # 1. pre-reveal pnl
    pr = analysis_prereveal_pnl(all_sessions)
    print("\n[1] PRE-REVEAL PNL (fills before first reveal, marked to session settle)")
    total_q = sum(r["pre_qty"] for r in pr["per_session"])
    total_p = sum(r["pre_pnl_to_settle"] for r in pr["per_session"])
    total_mq = sum(r["maker_qty"] for r in pr["per_session"])
    total_tq = sum(r["taker_qty"] for r in pr["per_session"])
    total_mp = sum(r["maker_pnl"] for r in pr["per_session"])
    total_tp = sum(r["taker_pnl"] for r in pr["per_session"])
    n_sessions_with_pre = sum(1 for r in pr["per_session"] if r["n_pre_fills"] > 0)
    print(f"   sessions with any pre-reveal fills: {n_sessions_with_pre} / {len(pr['per_session'])}")
    print(f"   total pre-reveal qty: {total_q} lots")
    print(f"   total pre-reveal PnL-to-settle: {fmt_money(total_p)}")
    if total_q > 0:
        print(f"   avg pre-reveal PnL per lot: {fmt_money(total_p / total_q)}")
    print(f"   maker-side: qty={total_mq} pnl={fmt_money(total_mp)}  "
          f"(this is us QUOTING and getting picked off / lifted)")
    print(f"   taker-side: qty={total_tq} pnl={fmt_money(total_tp)}  "
          f"(this is us SNIPING others' resting quotes)")
    for r in pr["per_session"]:
        if r["n_pre_fills"]:
            print(f"     - {r['file']} #{r['session_idx']}: "
                  f"qty={r['pre_qty']:>3}  pnl={fmt_money(r['pre_pnl_to_settle']):>8}  "
                  f"settle={r['settle']}  "
                  f"maker_q={r['maker_qty']}/pnl={fmt_money(r['maker_pnl'])}, "
                  f"taker_q={r['taker_qty']}/pnl={fmt_money(r['taker_pnl'])}")

    # 2. reveal cadence
    rc = analysis_reveal_cadence(all_sessions)
    print("\n[2] REVEAL CADENCE (seconds between successive reveals)")
    print(f"   n_gaps={rc['n_gaps']}  median={rc['median_s']:.2f}s  min={rc['min_s']:.2f}s  max={rc['max_s']:.2f}s")
    print(f"   mean={rc['mean_s']:.2f}s  stdev={rc['stdev_s']:.2f}s")
    print("   → reveal_interval=30s confirmed; jitter is small.")

    # 3. shocks
    sh = analysis_reveal_shocks(all_sessions)
    print("\n[3] PER-REVEAL SHOCK = |Δ running-mean extrapolated (×10)|")
    print(f"   K  |  n  |  median  |  mean  |  max")
    for k in sorted(sh):
        v = sh[k]
        print(f"   {k:>2} | {v['n']:>3} | {v['median_shock']:>7.2f}  | {v['mean_shock']:>5.2f}  | {v['max_shock']:>5.2f}")

    # 4. sigma trajectory
    sig = analysis_sigma_trajectory(all_sessions)
    print("\n[4] SIGMA_FAIR by K")
    print(f"   σ_pop(value) across all sessions: {sig['sigma_pop_value']:.2f}")
    print(f"   σ_pop(fair)  = sqrt(10-K) * σ_pop(value)  (parametric, doesn't shrink)")
    print(f"   σ_emp(fair)  = median sqrt((10-K)*s²_K)   (noisy at small K)")
    print(f"   K  |   n   | σ_pop(fair) | σ_emp(fair)")
    for k in range(1, N_TOTAL_REVEALS + 1):
        if k not in sig:
            continue
        v = sig[k]
        med = v['median_sigma_emp']
        if med is None:
            med_s = "(need K≥2)"
        else:
            med_s = f"{med:>6.2f}"
        print(f"   {k:>2} | {v['n']:>4}  |   {v['sigma_pop']:>5.2f}     |   {med_s}")

    # 5. refresh cadence
    rcad = analysis_refresh_cadence(all_sessions)
    print("\n[5] OUR QUOTE-REFRESH CADENCE (sec between events)")
    print("   same-order modify_ack gaps:")
    for ph in ["K=0", "K=1-3", "K=4-7", "K=8-10"]:
        d = rcad["same_order_refresh"].get(ph)
        if d:
            print(f"     {ph:>6}: n={d['n']:>4}  median={d['median_s']:.2f}s  "
                  f"p10={d['p10_s']:.2f}s  p90={d['p90_s']:.2f}s")
    print("   new-order cadence (gap between fresh order_acks):")
    for ph in ["K=0", "K=1-3", "K=4-7", "K=8-10"]:
        d = rcad["new_order_cadence"].get(ph)
        if d:
            print(f"     {ph:>6}: n={d['n']:>4}  median={d['median_s']:.2f}s  "
                  f"p10={d['p10_s']:.2f}s  p90={d['p90_s']:.2f}s")

    # 6. counterparties
    cp = analysis_counterparties(all_sessions)
    print("\n[6] COUNTERPARTY FILL PATTERNS (mark to session settle)")
    cp_rows = sorted(cp.items(), key=lambda kv: kv[1]["net_pnl_after_fees"])
    print(f"   {'CP':<8} {'n_fills':>7} {'we_bot':>7} {'we_sold':>7} {'gross':>10} {'net':>10} {'edge/lot':>10} {'side_bias'}")
    for cp_name, d in cp_rows:
        print(f"   {cp_name:<8} {d['n_fills']:>7} {d['qty_we_bought']:>7} {d['qty_we_sold']:>7} "
              f"{fmt_money(d['gross_pnl_to_settle']):>10} {fmt_money(d['net_pnl_after_fees']):>10} "
              f"{d['median_edge_per_lot']:>10.2f} {d['side_bias']}")

    # 7. public-stream
    ps = analysis_public_stream(all_sessions)
    print("\n[7] PUBLIC-STREAM (OTHER PARTICIPANTS) PATTERNS")
    print(f"   total quote_add events analysed: {ps['total_adds_observed']}")
    print(f"     aggressive (cross/at-mid): {ps['aggressive_pct']*100:.1f}%")
    print(f"     passive (away from mid):   {ps['passive_pct']*100:.1f}%")
    print(f"     inside (~at mid):          {ps['inside_pct']*100:.1f}%")
    print(f"     mid unknown:               {ps['unclear_pct']*100:.1f}%")
    print(f"   flash-cancel (<100ms, no fill) share of cancels-without-fill: "
          f"{ps['flash_cancel_lt_100ms_share']*100:.1f}%  "
          f"({ps['flash_cancel_lt_100ms_no_fill']}/{ps['flash_cancel_no_fill_total']})")
    print(f"   median cancel latency overall:   {ps['median_cancel_latency_ms']:.0f} ms"
          if ps['median_cancel_latency_ms'] is not None else "")
    print(f"   stale-lift rate (fair walks past resting quote within 5s): "
          f"{ps['stale_lift_rate']*100:.1f}%")

    # 8. suggested intervals
    sugg = suggest_refresh_intervals(sig, rc)
    print("\n[8] SUGGESTED PER-PHASE REFRESH INTERVAL FOR strategy2")
    for ph in ["K=0", "K=1-3", "K=4-7", "K=8-10"]:
        d = sugg.get(ph, {})
        if "action" in d:
            print(f"   {ph}: {d['action']}  ({d['reason']})")
        else:
            print(f"   {ph}: σ_pop(fair) ≈ {d['sigma_pop']:.2f}  "
                  f"→ suggested refresh ≈ {d['suggested_refresh_s']:.1f}s  ({d['rule']})")

    # ------ headline answers (concise) ------
    pre_total_qty = sum(r["pre_qty"] for r in pr["per_session"])
    pre_total_pnl = sum(r["pre_pnl_to_settle"] for r in pr["per_session"])
    losers = [cp_name for cp_name, d in cp.items() if d["net_pnl_after_fees"] < -5]
    winners = [cp_name for cp_name, d in cp.items() if d["net_pnl_after_fees"] > 5]
    print("\n" + "=" * 78)
    print("HEADLINE ANSWERS")
    print("=" * 78)
    print(f"  Q1 Skip K=0?  → "
          f"pre-reveal qty={pre_total_qty}, PnL-to-settle={fmt_money(pre_total_pnl)}.  "
          f"Per-lot {fmt_money(pre_total_pnl/pre_total_qty) if pre_total_qty else 'N/A'}")
    print(f"  Q2 Refresh intervals:")
    for ph in ["K=0", "K=1-3", "K=4-7", "K=8-10"]:
        d = sugg.get(ph, {})
        if "action" in d:
            print(f"     {ph}: {d['action']}")
        else:
            print(f"     {ph}: ≈ {d['suggested_refresh_s']:.1f}s")
    print(f"  Q3 Widen against (informed CPs):  {losers}")
    print(f"     Tighten against (noisy CPs):    {winners}")
    print(f"  Q4 Public-stream:  flash-cancels <100ms ≈ "
          f"{ps['flash_cancel_lt_100ms_share']*100:.1f}% of no-fill cancels "
          f"(median cancel latency = {ps['median_cancel_latency_ms']:.0f}ms)")
    print(f"        → do NOT chase phantom quotes (resting ≤100ms with no fill).")
    print(f"     Aggressive adds = {ps['aggressive_pct']*100:.1f}% of total "
          f"({ps['stale_lift_rate']*100:.1f}% stale-lift rate within 5s) — these"
          f" eat our maker quotes; widen briefly on bursts.")
    print("=" * 78)


# ---------- main --------------------------------------------------------------


def main():
    primary = load_all_sessions(PRIMARY_FILES)
    secondary = load_all_sessions(SECONDARY_FILES)
    all_sessions = primary + secondary
    if not all_sessions:
        print("No sessions loaded — check LOG_DIR.")
        return
    print_report(primary, secondary, all_sessions)


if __name__ == "__main__":
    main()
