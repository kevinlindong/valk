"""v18 log collector — thin wrapper around `probe.Probe`.

The base Probe class already handles the full event log (reveals,
fills, books, trades, settlements, acks, rejects) and counterparty
fill stats. We re-export it under a v18-specific factory so the runner
imports `probe18.make_probe18(...)` instead of `probe.Probe(...)` —
this keeps the strategy18 package self-contained and gives us a hook
for v18-only instrumentation later (precompute hit/miss, park timing,
sweep edge realization, etc.) without touching the shared probe.

Usage from `run_combined18.py`:

    from strategy18.probe18 import make_probe18
    probe = make_probe18(client, symbol="A",
                         log_path="day1/logs/combined_log_v18_...jsonl")
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DAY1_DIR = os.path.dirname(_THIS_DIR)
_REPO = os.path.dirname(_DAY1_DIR)
for _p in (_REPO, _DAY1_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sdk.client import GameClient  # noqa: E402
from probe import Probe  # noqa: E402

__all__ = ["Probe", "make_probe18", "default_log_path"]


def default_log_path() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"day1/logs/combined_log_v18_{ts}.jsonl"


def make_probe18(client: GameClient, symbol: str = "A",
                 log_path: Optional[str] = None) -> Probe:
    """Build a v18 probe wired for passive logging only.

    The strategy owns all order placement / cancellation; the probe is
    a pure WS-event recorder. We disable its built-in probe scheduler
    (probe_mode='passive', probe_interval huge) and stub _safety_check
    so it never tries to flatten on its own.
    """
    if log_path is None:
        log_path = default_log_path()
    p = Probe(client, symbol, log_path,
              probe_mode="passive", probe_interval=999999.0)
    p._safety_check = lambda: None  # type: ignore[assignment]
    p.log("v18_session_marker", note="strategy18 runner")
    return p
