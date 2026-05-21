"""Compute KEVD's truly-correct settled PnL using the admin truth oracle.

The dashboard marks each open position at *book mid*. For:
  * C (binary): mid bears little relation to the 0/100 settle once the round
    is far along — the dashboard can be off by thousands.
  * D (realized_range): mid drifts but the realized range is fixed.
  * B (next_number): the dashboard appears to mark at the next reveal only,
    not at sum(remaining reveals), so it underweights future tick credits.

Settlement formulas (verified against actual `settlement` messages in
day1/logs/combined_log_v21_20260521_125211.jsonl):
    pnl_A = (cash_A + pos_A * sum(reveals))         * 1
    pnl_B = (cash_B + pos_B * sum(remaining)) * 5    # pos liquidated to 0
    pnl_C = (cash_C + pos_C * (100 if sum>=strike else 0)) * 2
    pnl_D = (cash_D + pos_D * (max(reveals)-min(reveals))) * 10

Run: `python3 day1/correct_pnl.py [--watch]` to print once or loop every 2s.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"
ADMIN_KEY = "sean123"
TRADER = "KEVD"
MULT = {"A": 1, "B": 5, "C": 2, "D": 10}


def fetch_json(path: str, key: str) -> dict:
    r = requests.get(f"{URL}{path}", headers={"X-API-Key": key}, timeout=3)
    r.raise_for_status()
    return r.json()


def last_trade_prices_from_log(log_path: Path) -> dict[str, int]:
    last: dict[str, int] = {}
    with log_path.open() as f:
        for line in f:
            if '"kind": "trade"' not in line and '"kind":"trade"' not in line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("kind") == "trade" and "symbol" in m and "price" in m:
                last[m["symbol"]] = m["price"]
    return last


def settle_pnl(positions: dict, cash: dict, settle: dict) -> dict[str, float]:
    """Settled PnL given final settle prices. Mirrors what the server emits in
    the `settlement` message."""
    pa = positions.get("A", 0); ca = cash.get("A", 0)
    pb = positions.get("B", 0); cb = cash.get("B", 0)
    pc = positions.get("C", 0); cc = cash.get("C", 0)
    pd = positions.get("D", 0); cd = cash.get("D", 0)
    return {
        "A": (ca + pa * settle["A"]) * MULT["A"],
        "B":  cb                      * MULT["B"],   # B: pos liquidated to 0; cash carries it
        "C": (cc + pc * settle["C"]) * MULT["C"],
        "D": (cd + pd * settle["D"]) * MULT["D"],
    }


def projected_pnl(positions: dict, cash: dict, full_seq: list[int], reveals_done: list[int], c_strike: int) -> tuple[dict[str, float], dict[str, int]]:
    """Project final PnL assuming positions stay frozen for the rest of the round."""
    total_sum = sum(full_seq)
    settle = {
        "A": total_sum,
        "B": full_seq[-1],   # purely informational; B uses sum(remaining), not this
        "C": 100 if total_sum >= c_strike else 0,
        "D": max(full_seq) - min(full_seq),
    }
    remaining = full_seq[len(reveals_done):]
    pa = positions.get("A", 0); ca = cash.get("A", 0)
    pb = positions.get("B", 0); cb = cash.get("B", 0)
    pc = positions.get("C", 0); cc = cash.get("C", 0)
    pd = positions.get("D", 0); cd = cash.get("D", 0)
    pnl = {
        "A": (ca + pa * settle["A"]) * MULT["A"],
        "B": (cb + pb * sum(remaining)) * MULT["B"],
        "C": (cc + pc * settle["C"]) * MULT["C"],
        "D": (cd + pd * settle["D"]) * MULT["D"],
    }
    return pnl, settle


def print_snapshot() -> None:
    game = fetch_json("/api/game", API_KEY)
    truth = fetch_json("/api/admin/truth", ADMIN_KEY)
    lb = fetch_json("/api/leaderboard", API_KEY)

    row = next(r for r in lb["rows"] if r["trader"] == TRADER)
    positions = row["positions"]; cash = row["cash"]; fees_by = row["fees_by_symbol"]
    fees_total = row["fees"]; dash_pnl = row["pnl"]; dash_by = row["pnl_by_symbol"]

    phase = game["phase"]; reveals = game.get("reveals", []); rsum = game.get("running_sum", 0)
    full_seq = truth.get("full_sequence") or []
    reveals_so_far = truth.get("reveals_so_far") or reveals
    c_strike = truth["instruments"]["C"]["settlement_params"].get("strike")
    settle = truth.get("settle_prices") or {}

    if settle:
        pnl = settle_pnl(positions, cash, settle)
        marks = settle
        mode = "SETTLED"
    else:
        pnl, marks = projected_pnl(positions, cash, full_seq, reveals_so_far, c_strike)
        mode = "PROJECTED (positions frozen)"

    log = sorted(Path("/Users/kevin/projects/valk/day1/logs").glob("combined_log_*.jsonl"))[-1]
    last_trade = last_trade_prices_from_log(log)

    gross = sum(pnl.values()); net = gross - fees_total
    dash_net = dash_pnl - fees_total

    print(f"=== {time.strftime('%H:%M:%S')}  phase={phase} mode={mode} ===")
    print(f"  reveals so_far={reveals_so_far}  full_seq={full_seq}  C.strike={c_strike}")
    print(f"  marks(truth) A={marks.get('A')} B={marks.get('B')} C={marks.get('C')} D={marks.get('D')}")
    print(f"  last_trade   A={last_trade.get('A')} B={last_trade.get('B')} C={last_trade.get('C')} D={last_trade.get('D')}")
    print(f"  KEVD pos  A={positions.get('A',0):>4d} B={positions.get('B',0):>4d} C={positions.get('C',0):>4d} D={positions.get('D',0):>4d}")
    print(f"  KEVD cash A={cash.get('A',0):>6d} B={cash.get('B',0):>6d} C={cash.get('C',0):>6d} D={cash.get('D',0):>6d}")
    print(f"  -- per-symbol --")
    for s in ("A","B","C","D"):
        d = dash_by.get(s, 0); p = pnl[s]; delta = p - d
        print(f"    {s}: corrected={p:>+8.0f}   dashboard={d:>+8.0f}   delta={delta:>+8.0f}")
    print(f"  -- totals (fees={fees_total}) --")
    print(f"    GROSS  corrected={gross:>+8.0f}   dashboard={dash_pnl:>+8.0f}   delta={gross-dash_pnl:>+8.0f}")
    print(f"    NET    corrected={net:>+8.1f}   dashboard={dash_net:>+8.1f}   delta={net-dash_net:>+8.1f}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="loop every 2s")
    args = ap.parse_args()
    if args.watch:
        try:
            while True:
                try:
                    print_snapshot()
                except Exception as e:
                    print(f"err: {e}")
                time.sleep(2)
        except KeyboardInterrupt:
            pass
    else:
        print_snapshot()


if __name__ == "__main__":
    main()
