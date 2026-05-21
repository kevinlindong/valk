"""v19 log collector — thin wrapper around `probe.Probe`.

Mirrors `strategy18/probe18.py` but emits the log under a v19-specific
filename so v19 sessions don't get tangled with v18 logs during
analysis.

Usage from `run_combined19.py`:

    from strategy19.probe19 import make_probe19
    probe = make_probe19(client, symbol="A",
                         log_path="day1/logs/combined_log_v19_...jsonl")
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

__all__ = ["Probe", "make_probe19", "default_log_path"]


def default_log_path() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"day1/logs/combined_log_v19_{ts}.jsonl"


def make_probe19(client: GameClient, symbol: str = "A",
                 log_path: Optional[str] = None) -> Probe:
    """Build a v19 probe wired for passive logging only.

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
    p.log("v19_session_marker", note="strategy19 runner")
    return p
