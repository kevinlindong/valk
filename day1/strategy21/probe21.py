"""v21 log collector — thin wrapper around `probe.Probe`.

Mirrors `strategy20/probe20.py` but emits the log under a v21-specific
filename so v21 sessions stay separated from v20 logs during analysis.
v21 trades A/B/C/D; the probe is symbol-agnostic for logging purposes
(every WS event is captured regardless of `symbol`).

Usage from `run_combined21.py`:

    from strategy21.probe21 import make_probe21
    probe = make_probe21(client, symbol="A",
                         log_path="day1/logs/combined_log_v21_...jsonl")
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_V20_DIR = os.path.join(os.path.dirname(_THIS_DIR), "strategy20")
for _p in (_THIS_DIR, _V20_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from strategy20 import GameClient  # noqa: E402
from probe20 import Probe  # noqa: E402

__all__ = ["Probe", "make_probe21", "default_log_path"]


def default_log_path() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"day1/logs/combined_log_v21_{ts}.jsonl"


def make_probe21(client: GameClient, symbol: str = "A",
                 log_path: Optional[str] = None) -> Probe:
    """Build a v21 probe wired for passive logging only.

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
    p.log("v21_session_marker", note="strategy21 runner (A/B/C/D, bot-driven)")
    return p
