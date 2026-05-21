"""v20 log collector — thin wrapper around `probe.Probe`.

Mirrors `strategy19/probe19.py` but emits the log under a v20-specific
filename so v20 sessions don't get tangled with v19 logs during
analysis. v20 trades A/B/C/D; the probe is symbol-agnostic for logging
purposes (every WS event is captured regardless of `symbol`).

Usage from `run_combined20.py`:

    from strategy20.probe20 import make_probe20
    probe = make_probe20(client, symbol="A",
                         log_path="day1/logs/combined_log_v20_...jsonl")
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from sdk_client import GameClient  # noqa: E402
from probe_base import Probe  # noqa: E402

__all__ = ["Probe", "make_probe20", "default_log_path"]


def default_log_path() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"day1/logs/combined_log_v20_{ts}.jsonl"


def make_probe20(client: GameClient, symbol: str = "A",
                 log_path: Optional[str] = None) -> Probe:
    """Build a v20 probe wired for passive logging only.

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
    p.log("v20_session_marker", note="strategy20 runner (A/B/C/D)")
    return p
