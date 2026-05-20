"""
Diagnose a trading loss from a JSONL session log.

Usage:
    python day1/analyze_loss.py [--log day1/logs/second_round_loss.jsonl]

This script:
  1. Splits the log into rounds (by reveal-index resets and game_state phases).
  2. Computes per-round PnL: sum over OUR fills of qty * sign * (settle - fill_px - fee),
     where sign=+1 for buy, -1 for sell. Uses the round's settle value when available,
     else extrapolates a likely settle from the observed reveals.
  3. For the losing round, builds a (ts, position, fair_estimate, reveal_count) timeline.
  4. Attributes PnL by counterparty in that round.
  5. Lists the 5 worst single fills (largest negative PnL contribution).
  6. Splits maker vs taker PnL.
  7. Checks specific v2 strategy failure modes (K=0 trading, sigma cap, stale quotes,
     non-informed counterparties, FIFO reprice guard hang, market-mid disagreement gate).

Only edits to NOTES if you re-run on a different log: nothing -- it auto-discovers
via --log argument or defaults to day1/logs/second_round_loss.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Any, Optional


MAKER_FEE = 0.5
TAKER_FEE = 0.5
DEFAULT_LOG = "day1/logs/second_round_loss.jsonl"
INFORMED_SET = {"VALKC", "VALKM", "VALKN", "VALKJ"}
SIGMA_CAP_TARGET = 2.0  # max_pos_target_sigma from strategy2
SIGMA_CAP_FLOOR = 10    # max_pos_floor from strategy2


def load(path: str) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def split_rounds(events: list[dict]) -> list[dict]:
    """Split events into rounds. A round break occurs:
      - after a settlement event (next event starts the new round).
      - OR when a reveal event's index goes DOWN (covers logs missing a
        settlement, e.g. a round we joined mid-stream after a prior one).

    Returns a list of round dicts:
      {
        'reveals': [(ts, idx, value, running_sum), ...],
        'fills': [...], 'settlement': dict_or_None,
        'counterparty_summary': dict_or_None,
        'starts_mid_round': bool, 'starting_reveal_idx': int (if mid-round join)
      }
    """
    rounds: list[dict] = []
    boundaries: list[int] = [0]  # event indices where rounds start
    # Build round boundaries
    prev_reveal_idx = 0
    for i, e in enumerate(events):
        k = e.get("kind")
        if k == "reveal":
            ridx = e.get("index", 0)
            if ridx > 0 and prev_reveal_idx > 0 and ridx <= prev_reveal_idx:
                # New round starts at THIS event index
                if boundaries[-1] != i:
                    boundaries.append(i)
            prev_reveal_idx = ridx
        elif k == "settlement":
            # Round ends at this event index (inclusive); next round starts
            # at i+1.
            if i + 1 < len(events) and boundaries[-1] != i + 1:
                boundaries.append(i + 1)
            prev_reveal_idx = 0  # reset
    # If a settlement boundary appears before a same-round reveal-reset
    # boundary, the reveal-reset is redundant; we dedupe.
    boundaries = sorted(set(boundaries))

    # Construct round dicts
    for bi, start_i in enumerate(boundaries):
        end_i = boundaries[bi + 1] - 1 if bi + 1 < len(boundaries) else len(events) - 1
        r = {"reveals": [], "fills": [], "settlement": None,
             "counterparty_summary": None,
             "start_event_i": start_i, "end_event_i": end_i,
             "starts_mid_round": False, "starting_reveal_idx": 1}
        evs = events[start_i:end_i + 1]
        r["events"] = evs
        r["start_ns"] = evs[0].get("t_ns") if evs else None
        r["end_ns"] = evs[-1].get("t_ns") if evs else None
        first_reveal_idx_in_round = None
        for ev in evs:
            k = ev.get("kind")
            if k == "reveal":
                ridx = ev.get("index", 0)
                if first_reveal_idx_in_round is None:
                    first_reveal_idx_in_round = ridx
                    r["starts_mid_round"] = ridx != 1
                    r["starting_reveal_idx"] = ridx
                r["reveals"].append((ev.get("t_ns"), ridx, ev.get("value"),
                                     ev.get("running_sum")))
            elif k == "fill":
                r["fills"].append(ev)
            elif k == "settlement":
                r["settlement"] = ev
            elif k == "counterparty_summary":
                r["counterparty_summary"] = ev
        rounds.append(r)

    # Drop empty leading rounds (e.g., session_start before any trading)
    rounds = [r for r in rounds
              if r["fills"] or r["reveals"] or r["settlement"]]
    return rounds


def online_fair_estimate(running_sum: float, k_revealed: int, n_total: int) -> float:
    """Crude online estimate of total settlement based on revealed-so-far mean."""
    if k_revealed <= 0:
        # Very rough prior: mean reveal ~6.5 by inspection of historical data
        # Without history, we just return a placeholder used as 'no-info'
        return 6.5 * n_total
    mean_so_far = running_sum / k_revealed
    return mean_so_far * n_total


def reveals_before_ts(reveals: list[tuple[int, int, int, int]],
                      ts_ns: int) -> tuple[int, float]:
    """Returns (k_revealed_before_ts, running_sum_before_ts)."""
    k = 0
    rs = 0
    for (t, _idx, v, _rs) in reveals:
        if t is None or ts_ns is None:
            continue
        if t <= ts_ns:
            k += 1
            rs += v
        else:
            break
    return k, rs


def position_at_ts(fills: list[dict], ts_ns: int) -> int:
    pos = 0
    for f in fills:
        if f.get("t_ns", 0) > ts_ns:
            break
        q = f.get("qty", 0)
        pos += q if f.get("side") == "buy" else -q
    return pos


def sigma_from_n_remaining(n_rem: int) -> float:
    """Rough sigma scaling from strategy comments: sigma_pop(fair) ~ sqrt(10-K)*4.3.
    But here we want sigma at the time the fill happens given how many reveals are
    left. The strategy uses predict_settle's law-of-total-variance, but for the
    failure-mode check we just need a rough scale: sigma scales with n_remaining.
    """
    if n_rem <= 0:
        return 0.0
    # Approximate per-reveal variance (a~3, w~4 prior => uniform width ~5,
    # variance per reveal ~ w*(w+2)/12 ~ 2.5).  Std ~ sqrt(n_rem * 2.5).
    # Plus uncertainty about a,w themselves -> rough multiplier ~1.5.
    return 1.5 * (n_rem * 2.5) ** 0.5


def compute_round_pnl(r: dict, settle: float) -> dict:
    """Returns per-fill PnL and totals.

    Per-lot fee constant (0.5 maker, 0.5 taker). The fill event's `fee` field
    is the TOTAL fee for that fill (already multiplied by qty), so we MUST NOT
    multiply by qty again. We use the per-lot constants and multiply by qty.
    """
    fills_pnl = []
    for f in r["fills"]:
        side = f.get("side")
        qty = f.get("qty", 0)
        px = f.get("price", 0)
        liq = f.get("liquidity", "?")
        cp = f.get("counterparty", "?")
        fee_per_lot = MAKER_FEE if liq == "maker" else TAKER_FEE
        sign = +1 if side == "buy" else -1
        # User formula: qty * sign * (settle - fill_px - 0.5_fee)
        # where 0.5_fee is a per-lot constant.
        pnl = qty * (sign * (settle - px) - fee_per_lot)
        fills_pnl.append({
            "ts_ns": f.get("t_ns"),
            "side": side, "qty": qty, "price": px,
            "liquidity": liq, "counterparty": cp,
            "pnl": pnl,
            "fee_per_lot": fee_per_lot,
            "fee_total": qty * fee_per_lot,
        })
    total = sum(p["pnl"] for p in fills_pnl)
    return {"fills_pnl": fills_pnl, "total_pnl": total}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--n-total", type=int, default=90)
    args = ap.parse_args()

    if not os.path.isabs(args.log):
        # Locate relative to the repo root if needed
        candidates = [args.log,
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   os.path.basename(args.log))]
        for c in candidates:
            if os.path.exists(c):
                args.log = c
                break

    print(f"Loading: {args.log}")
    events = load(args.log)
    print(f"Total events: {len(events)}")

    rounds = split_rounds(events)
    print(f"Rounds detected: {len(rounds)}")

    # Decide settle for each round. If there's no settlement event, extrapolate.
    # Use the FINAL observed running_sum * (n_total / n_observed) plus the part
    # already locked in as the central estimate.
    for i, r in enumerate(rounds):
        if r["settlement"]:
            settle = r["settlement"]["prices"].get("A")
            r["settle"] = settle
            r["settle_source"] = "settlement_event"
        else:
            if not r["reveals"]:
                r["settle"] = None
                r["settle_source"] = "no_reveals"
                continue
            # Use observed mean * n_total as point estimate
            last_t, last_idx, last_v, last_rs = r["reveals"][-1]
            # If round STARTS mid-stream (e.g. log joins existing round at idx=39),
            # the running_sum reported is for the FULL round so far, so we can
            # extrapolate from that.
            k_observed = last_idx
            mean_obs = last_rs / k_observed if k_observed > 0 else 0
            r["settle"] = mean_obs * args.n_total
            r["settle_source"] = (
                f"extrapolated from k={k_observed} mean={mean_obs:.2f}")

    # ---- Per-round PnL summary ----
    print("\n" + "=" * 78)
    print("PER-ROUND PNL")
    print("=" * 78)
    pnl_by_round = []
    for i, r in enumerate(rounds):
        n_fills = len(r["fills"])
        settle = r["settle"]
        if settle is None:
            print(f"Round {i+1}: no settle/reveals, n_fills={n_fills}")
            continue
        res = compute_round_pnl(r, settle)
        r["pnl_result"] = res
        pnl_by_round.append((i, res["total_pnl"]))
        starts_mid = r["starts_mid_round"]
        sr_idx = r["starting_reveal_idx"]
        print(f"Round {i+1}:")
        print(f"  reveals observed in this round: {len(r['reveals'])} "
              f"(starts_mid_round={starts_mid} starting_idx={sr_idx})")
        if r["settlement"]:
            sp = r["settlement"]["prices"].get("A")
            print(f"  settled (event) at {sp}")
        else:
            last_t, last_idx, last_v, last_rs = r["reveals"][-1]
            print(f"  NO settlement event; final observed reveal "
                  f"idx={last_idx} running_sum={last_rs}")
            print(f"  extrapolated settle estimate: {settle:.1f} "
                  f"({r['settle_source']})")
        print(f"  n_fills={n_fills}  total_pnl={res['total_pnl']:.2f}")
        if r["settlement"]:
            print(f"  (settlement event says trader pnl = "
                  f"{r['settlement'].get('pnl')})")

    # ---- Find the losing round ----
    losing_idx = None
    if pnl_by_round:
        losing_idx, _ = min(pnl_by_round, key=lambda x: x[1])
    if losing_idx is None:
        print("\nNo rounds with PnL computed; aborting.")
        return
    r = rounds[losing_idx]
    print("\n" + "=" * 78)
    print(f"LOSING ROUND DIAGNOSIS: Round {losing_idx + 1}")
    print("=" * 78)

    settle = r["settle"]
    print(f"Settle: {settle:.1f} ({r['settle_source']})")

    # Compare settle vs naive prior fair. Prior mean over reveal X ~ ?
    # Just compare to round's own final-reveal-extrapolation given a few reveals.
    if r["reveals"]:
        first_reveals = r["reveals"][:5]
        if r["starts_mid_round"]:
            # The running_sum already accounts for unobserved early reveals.
            # We can still see what mean per reveal looks like at the start vs end.
            first_t, first_idx, first_v, first_rs = first_reveals[0]
            mean_at_first = first_rs / first_idx if first_idx > 0 else 0
            print(f"At first observed reveal (idx={first_idx}): "
                  f"mean-per-reveal so far = {mean_at_first:.2f}; "
                  f"implied total = {mean_at_first * args.n_total:.1f}")
        else:
            mean_first5 = statistics.mean(rv[2] for rv in first_reveals)
            print(f"Mean of first 5 reveals: {mean_first5:.2f} "
                  f"(naive extrapolated settle: {mean_first5*args.n_total:.0f})")
        # Prior expected value: from POSTERIOR after no reveals, mean is ~6.5*n_total ~ 585
        prior_settle = 6.5 * args.n_total
        print(f"Prior expected settle (no info): ~{prior_settle:.0f} "
              f"vs actual/estimated settle: {settle:.1f}  -> "
              f"prior_diff={settle - prior_settle:+.1f}")
        if abs(settle - prior_settle) > 100:
            print(f"  >>> TAIL ROUND: settle is far from prior mean.")

    # ---- Reveal sequence ----
    print("\nReveal sequence in this round:")
    for (t, idx, v, rs) in r["reveals"]:
        print(f"  idx={idx:3d}  v={v}  running_sum={rs}  (t_ns={t})")

    # Reveal value distribution sanity
    vals = [rv[2] for rv in r["reveals"]]
    if vals:
        print(f"\nReveal stats this round: n={len(vals)} mean={statistics.mean(vals):.2f} "
              f"min={min(vals)} max={max(vals)}")

    # ---- Position timeline ----
    print("\n" + "-" * 78)
    print("TIMELINE: (ts_ns_offset_sec, position, fair_estimate, reveal_count)")
    print("-" * 78)
    fills_sorted = sorted(r["fills"], key=lambda f: f.get("t_ns", 0))
    start_ns = r["start_ns"]
    cum_pos = 0
    pos_traj = []
    for f in fills_sorted:
        q = f.get("qty", 0)
        side = f.get("side")
        cum_pos += q if side == "buy" else -q
        ts = f.get("t_ns")
        k, rs = reveals_before_ts(r["reveals"], ts)
        if r["starts_mid_round"]:
            # For mid-round joins, the running_sum at the time WE observed
            # the first reveal already captures earlier reveals. Use it for
            # online fair.
            # Treat k as "reveals observed since first observed reveal" -> use
            # actual reveal index instead.
            actual_idx = 0
            actual_rs = 0
            for (rt, ridx, rv, rrs) in r["reveals"]:
                if rt is not None and rt <= ts:
                    actual_idx = ridx
                    actual_rs = rrs
                else:
                    break
            fair = (online_fair_estimate(actual_rs, actual_idx, args.n_total)
                    if actual_idx > 0 else None)
            k_for_print = actual_idx
        else:
            fair = (online_fair_estimate(rs, k, args.n_total) if k > 0 else None)
            k_for_print = k
        pos_traj.append((ts, cum_pos, fair, k_for_print, f))

    # Summarize: print every 10th and big position transitions
    if pos_traj:
        peak_abs_pos = max(pos_traj, key=lambda x: abs(x[1]))
        print(f"|position| peak: pos={peak_abs_pos[1]} at t_offset_s="
              f"{(peak_abs_pos[0]-start_ns)/1e9:.2f} K={peak_abs_pos[3]} "
              f"fair_est={peak_abs_pos[2]}")
        # Print a sparse view: every 10th fill + extrema
        print("\nSparse timeline (every 5th fill + extrema):")
        last_pos = None
        for i_, (ts, p, fair, k, f) in enumerate(pos_traj):
            t_off = (ts - start_ns) / 1e9
            print_it = (i_ % 5 == 0 or i_ == len(pos_traj) - 1
                        or (last_pos is not None and abs(p - last_pos) >= 10))
            if print_it:
                fp = f"{fair:.1f}" if fair is not None else "None"
                print(f"  t+{t_off:6.2f}s  pos={p:+4d}  K={k:3d}  fair_est={fp}"
                      f"  fill: {f.get('side'):4s} {f.get('qty'):3d} @ "
                      f"{f.get('price'):3d} ({f.get('liquidity')}, "
                      f"{f.get('counterparty')})")
            last_pos = p

    # ---- Per-counterparty PnL ----
    print("\n" + "-" * 78)
    print("PER-COUNTERPARTY PnL CONTRIBUTION (this round)")
    print("-" * 78)
    cp_pnl: dict[str, float] = defaultdict(float)
    cp_qty: dict[str, int] = defaultdict(int)
    cp_maker_pnl: dict[str, float] = defaultdict(float)
    cp_taker_pnl: dict[str, float] = defaultdict(float)
    cp_buy_q: dict[str, int] = defaultdict(int)
    cp_sell_q: dict[str, int] = defaultdict(int)
    for fp in r["pnl_result"]["fills_pnl"]:
        cp = fp["counterparty"] or "?"
        cp_pnl[cp] += fp["pnl"]
        cp_qty[cp] += fp["qty"]
        if fp["liquidity"] == "maker":
            cp_maker_pnl[cp] += fp["pnl"]
        else:
            cp_taker_pnl[cp] += fp["pnl"]
        if fp["side"] == "buy":
            cp_buy_q[cp] += fp["qty"]
        else:
            cp_sell_q[cp] += fp["qty"]
    rows = sorted(cp_pnl.items(), key=lambda x: x[1])
    print(f"{'CP':10s} {'n_qty':6s} {'pnl':>10s} {'maker_pnl':>10s} "
          f"{'taker_pnl':>10s} {'we_bought':>10s} {'we_sold':>10s} "
          f"{'informed?':10s}")
    for cp, pnl in rows:
        marker = "INFORMED" if cp in INFORMED_SET else ""
        print(f"{cp:10s} {cp_qty[cp]:6d} {pnl:10.2f} {cp_maker_pnl[cp]:10.2f} "
              f"{cp_taker_pnl[cp]:10.2f} {cp_buy_q[cp]:10d} {cp_sell_q[cp]:10d} "
              f"{marker:10s}")

    # ---- Maker vs Taker split ----
    print("\n" + "-" * 78)
    maker_pnl = sum(p["pnl"] for p in r["pnl_result"]["fills_pnl"]
                    if p["liquidity"] == "maker")
    taker_pnl = sum(p["pnl"] for p in r["pnl_result"]["fills_pnl"]
                    if p["liquidity"] == "taker")
    maker_q = sum(p["qty"] for p in r["pnl_result"]["fills_pnl"]
                  if p["liquidity"] == "maker")
    taker_q = sum(p["qty"] for p in r["pnl_result"]["fills_pnl"]
                  if p["liquidity"] == "taker")
    print(f"MAKER fills: qty={maker_q}, pnl={maker_pnl:.2f}, "
          f"per-lot={maker_pnl/maker_q if maker_q else 0:.2f}")
    print(f"TAKER fills: qty={taker_q}, pnl={taker_pnl:.2f}, "
          f"per-lot={taker_pnl/taker_q if taker_q else 0:.2f}")

    # ---- 5 worst single fills ----
    print("\n" + "-" * 78)
    print("5 WORST SINGLE FILLS (largest negative PnL contribution)")
    print("-" * 78)
    fp = sorted(r["pnl_result"]["fills_pnl"], key=lambda x: x["pnl"])
    for i, p in enumerate(fp[:5]):
        ts = p["ts_ns"]
        k_at, rs_at = reveals_before_ts(r["reveals"], ts)
        if r["starts_mid_round"]:
            actual_idx = 0
            actual_rs = 0
            for (rt, ridx, rv, rrs) in r["reveals"]:
                if rt is not None and rt <= ts:
                    actual_idx = ridx
                    actual_rs = rrs
                else:
                    break
            k_at = actual_idx
            rs_at = actual_rs
        fair_at = (online_fair_estimate(rs_at, k_at, args.n_total)
                   if k_at > 0 else None)
        fair_str = f"{fair_at:.1f}" if fair_at else "n/a"
        t_off = (ts - start_ns) / 1e9
        print(f"  #{i+1} t+{t_off:6.2f}s  side={p['side']:4s} qty={p['qty']:3d}  "
              f"fill_px={p['price']:3d}  fair@fill={fair_str:6s}  "
              f"settle={settle:.0f}  cp={p['counterparty']:6s}  "
              f"liq={p['liquidity']:5s}  pnl={p['pnl']:.2f}")

    # ---- v2 failure mode checks ----
    print("\n" + "=" * 78)
    print("V2 STRATEGY FAILURE-MODE CHECKS")
    print("=" * 78)

    # 1. Trades at K=0?
    # K is the strategy's posterior knowledge.  When the round STARTS mid-round
    # (we joined mid-stream), the strategy gets reveals via game_state at
    # phase=running, so K is NOT 0 even for the first fill.
    pre_reveal_fills = []
    for f in r["fills"]:
        ts = f.get("t_ns")
        if r["starts_mid_round"]:
            # K >= starting_reveal_idx for the whole round; never 0.
            continue
        k_at, _ = reveals_before_ts(r["reveals"], ts)
        if k_at == 0:
            pre_reveal_fills.append(f)
    if r["starts_mid_round"]:
        print(f"\n[1] K=0 fills (pre-reveal trading): NOT APPLICABLE — round "
              f"joined mid-stream at idx={r['starting_reveal_idx']}, so "
              f"strategy posterior had K >= {r['starting_reveal_idx']} from "
              f"phase=running.")
    else:
        print(f"\n[1] K=0 fills (pre-reveal trading): "
              f"{len(pre_reveal_fills)} fills, "
              f"qty={sum(f['qty'] for f in pre_reveal_fills)}")
        for f in pre_reveal_fills[:5]:
            print(f"    pre-reveal fill: {f['side']} {f['qty']} @ "
                  f"{f['price']} cp={f.get('counterparty')} "
                  f"liq={f.get('liquidity')}")

    # 2. Sigma-scaled position cap breach? Plus hard position_limit breach.
    print("\n[2] Sigma-scaled + hard position cap breaches:")
    breaches = 0
    max_breach = 0
    hard_breaches = 0
    max_abs_pos = 0
    for (ts, pos, fair, k, f) in pos_traj:
        if abs(pos) > max_abs_pos:
            max_abs_pos = abs(pos)
        n_rem = max(args.n_total - k, 0) if k > 0 else args.n_total
        sigma = sigma_from_n_remaining(n_rem)
        if sigma <= SIGMA_CAP_TARGET:
            cap = 100
        else:
            cap = max(SIGMA_CAP_FLOOR, int(100 * SIGMA_CAP_TARGET / sigma))
        if abs(pos) > cap:
            breaches += 1
            if abs(pos) - cap > max_breach:
                max_breach = abs(pos) - cap
        if abs(pos) > 100:
            hard_breaches += 1
    print(f"    fills where |pos| > sigma-scaled cap: {breaches}  "
          f"(max excess: {max_breach} lots)")
    print(f"    fills where |pos| > HARD limit (100): {hard_breaches}  "
          f"(absolute peak |pos|={max_abs_pos})")
    if max_abs_pos > 100:
        print("    >>> CRITICAL: hard position limit of +/-100 was BREACHED")

    # 3. Stale maker quote across a reveal?
    # Look for: a quote was OPEN before a reveal and got FILLED right after the
    # reveal without being modified. Use quote_add/modify/cancel events plus
    # reveal events plus quote_fill events.
    # We'll check fills with liquidity=maker that occurred within 0.5s of a reveal
    # and whose underlying order_id hadn't seen a modify_ack in the prior 1s.
    print("\n[3] Stale maker quotes across reveal:")
    modify_ack_times: dict[int, list[int]] = defaultdict(list)
    quote_add_times: dict[int, int] = {}
    for ev in r["events"]:
        k = ev.get("kind")
        if k == "modify_ack":
            modify_ack_times[ev.get("order_id")].append(ev.get("t_ns"))
        elif k == "quote_add":
            quote_add_times.setdefault(ev.get("order_id"), ev.get("t_ns"))
    reveal_ts = [rv[0] for rv in r["reveals"]]
    stale_fills = []
    for f in r["fills"]:
        if f.get("liquidity") != "maker":
            continue
        ft = f.get("t_ns")
        oid = f.get("order_id")
        # Find the most recent reveal before this fill
        prior_reveal = max((t for t in reveal_ts if t < ft), default=None)
        if prior_reveal is None:
            continue
        gap_s = (ft - prior_reveal) / 1e9
        if gap_s > 2.0:
            continue
        # Was order modified between prior reveal and this fill?
        modifies = modify_ack_times.get(oid, [])
        modified_after_reveal = any(prior_reveal < m <= ft for m in modifies)
        # Was the order first quoted BEFORE the reveal?
        add_t = quote_add_times.get(oid)
        if add_t is None or add_t >= prior_reveal:
            continue
        if not modified_after_reveal:
            stale_fills.append({"ft": ft, "oid": oid, "gap_s": gap_s,
                                "side": f["side"], "qty": f["qty"],
                                "price": f["price"], "cp": f.get("counterparty")})
    print(f"    maker fills that occurred <2s after a reveal on an order "
          f"posted BEFORE the reveal and NOT modified between: "
          f"{len(stale_fills)}")
    for s in stale_fills[:8]:
        print(f"      stale_fill: oid={s['oid']} {s['side']} {s['qty']}@{s['price']} "
              f"cp={s['cp']} reveal-to-fill gap={s['gap_s']:.3f}s")

    # 4. Non-informed counterparty losses
    print("\n[4] Non-informed counterparty losses:")
    non_inf_loss_total = 0
    non_inf_loss_cps = []
    for cp, pnl in cp_pnl.items():
        if cp not in INFORMED_SET and pnl < 0:
            non_inf_loss_total += pnl
            non_inf_loss_cps.append((cp, pnl, cp_qty[cp]))
    print(f"    total loss to non-informed CPs: {non_inf_loss_total:.2f}")
    for cp, pnl, q in sorted(non_inf_loss_cps, key=lambda x: x[1]):
        print(f"      {cp}: pnl={pnl:.2f}, n_qty={q}, INFORMED={'YES' if cp in INFORMED_SET else 'no'}")

    # 5. FIFO reprice guard hung on stale inventory?
    # When a reveal moved fair and we DIDN'T cancel the resting quote because
    # _reprice skipped (rest_qty <= target). Heuristic: find maker fills where
    # the fill price was on the WRONG side of the post-reveal fair, AND
    # there's no quote_cancel for that order_id between the most recent reveal
    # and the fill.
    print("\n[5] FIFO reprice guard hangs (stale inventory across fair shift):")
    cancels_by_oid: dict[int, list[int]] = defaultdict(list)
    for ev in r["events"]:
        if ev.get("kind") in ("quote_cancel", "cancel_ack"):
            cancels_by_oid[ev.get("order_id")].append(ev.get("t_ns"))
    fifo_hangs = []
    for f in r["fills"]:
        if f.get("liquidity") != "maker":
            continue
        ft = f.get("t_ns")
        oid = f.get("order_id")
        prior_reveal = max((t for t in reveal_ts if t < ft), default=None)
        if prior_reveal is None:
            continue
        # Estimate fair right after that reveal
        actual_idx = 0
        actual_rs = 0
        for (rt, ridx, rv, rrs) in r["reveals"]:
            if rt is not None and rt <= prior_reveal:
                actual_idx = ridx
                actual_rs = rrs
        if actual_idx == 0:
            continue
        fair_after_reveal = online_fair_estimate(actual_rs, actual_idx, args.n_total)
        sign = +1 if f["side"] == "buy" else -1
        # PnL per lot vs the IMMEDIATE post-reveal fair (not settle)
        # If we sold at price below fair, that's a loss.
        per_lot_loss = sign * (fair_after_reveal - f["price"])
        if per_lot_loss < -1.5:  # tougher than 1 tick
            # Did we cancel order before fill?
            cancels = cancels_by_oid.get(oid, [])
            cancelled_before_fill = any(c <= ft for c in cancels)
            if not cancelled_before_fill:
                fifo_hangs.append({
                    "ft": ft, "oid": oid, "side": f["side"], "qty": f["qty"],
                    "price": f["price"], "cp": f.get("counterparty"),
                    "fair_after_reveal": fair_after_reveal,
                    "per_lot_loss": per_lot_loss,
                })
    print(f"    maker fills with per-lot adverse vs post-reveal fair > 1.5: "
          f"{len(fifo_hangs)}")
    fifo_hangs.sort(key=lambda x: x["per_lot_loss"])
    for h in fifo_hangs[:8]:
        print(f"      hang: oid={h['oid']} {h['side']} {h['qty']}@{h['price']} "
              f"cp={h['cp']} fair_after_reveal={h['fair_after_reveal']:.1f} "
              f"per_lot_loss={h['per_lot_loss']:.1f}")

    # 6. Market-mid disagreement gate failure
    # Detect: did we have fills where fair (online estimate) disagreed strongly
    # with the prevailing market_mid (best bid/ask midpoint in book at that
    # moment)?  We'd need book snapshots near the fill.  Use book_periodic
    # snapshots as proxy.
    print("\n[6] Market-mid disagreement gate (fair vs book_periodic mid):")
    book_snaps = []
    for ev in r["events"]:
        if ev.get("kind") == "book_periodic":
            bids = ev.get("bids", [])
            asks = ev.get("asks", [])
            if bids and asks:
                book_snaps.append((ev.get("t_ns"),
                                   (bids[0]["price"] + asks[0]["price"]) / 2.0,
                                   bids[0]["price"], asks[0]["price"]))
    def market_mid_at(ts: int) -> Optional[tuple[float, int, int]]:
        # last snapshot <= ts
        best = None
        for (t, mid, bb, ba) in book_snaps:
            if t <= ts:
                best = (mid, bb, ba)
            else:
                break
        return best

    disagreement_fills = 0
    disagreement_sum_loss = 0.0
    for fp_ in r["pnl_result"]["fills_pnl"]:
        ts = fp_["ts_ns"]
        mm = market_mid_at(ts)
        if mm is None:
            continue
        mid, _, _ = mm
        k_at, rs_at = reveals_before_ts(r["reveals"], ts)
        if r["starts_mid_round"]:
            actual_idx = 0
            actual_rs = 0
            for (rt, ridx, rv, rrs) in r["reveals"]:
                if rt is not None and rt <= ts:
                    actual_idx = ridx
                    actual_rs = rrs
            k_at = actual_idx
            rs_at = actual_rs
        if k_at == 0:
            continue
        fair_now = online_fair_estimate(rs_at, k_at, args.n_total)
        if abs(fair_now - mid) >= 5.0 and fp_["pnl"] < 0:
            disagreement_fills += 1
            disagreement_sum_loss += fp_["pnl"]
    print(f"    fills where |online_fair - book_mid| >= 5 AND fill was a "
          f"loser: {disagreement_fills}, total loss attributable: "
          f"{disagreement_sum_loss:.2f}")

    # ---- Dollar loss attribution summary ----
    print("\n" + "=" * 78)
    print("DOLLAR LOSS ATTRIBUTION SUMMARY")
    print("=" * 78)
    total_pnl = r["pnl_result"]["total_pnl"]
    print(f"Total round PnL: {total_pnl:.2f}")
    print(f"  Maker PnL: {maker_pnl:.2f} ({maker_q} lots)")
    print(f"  Taker PnL: {taker_pnl:.2f} ({taker_q} lots)")
    print(f"  Top losing CP: "
          f"{rows[0][0]} contributed {rows[0][1]:.2f}")
    print(f"  Top 3 losers sum: {sum(p for _, p in rows[:3]):.2f}")
    print(f"  Maker pickoff (stale-after-reveal) fills: {len(stale_fills)}, "
          f"FIFO-hang adverse fills: {len(fifo_hangs)}")
    print(f"  Disagreement-gate-should-have-fired losers: "
          f"{disagreement_fills} totalling {disagreement_sum_loss:.2f}")


if __name__ == "__main__":
    main()
