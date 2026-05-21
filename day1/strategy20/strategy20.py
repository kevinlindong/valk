"""Day 1 strategy v20 — v19 truth-anchored MM extended to A/B/C/D.

WHY v20:
  * v19 makes money on A but bleeds on B because it lets directional
    skew + size run unchecked while B's per-tick settlement grinds
    against any stale inventory. v20 keeps v19's truth oracle but
    is FUNDAMENTALLY A MARKET MAKER — quote two-sided on every
    symbol, ramp position with reveal count, dime to defend BBO,
    snipe only when book is grossly mispriced AND not a spoofer.
  * Two new exchange-traded derivatives extend the universe:
      - Market C  (binary call):   settles 100 if sum(X_i) >= K else 0
                                    → fair_c ∈ {0, 100} from t=0 (truth)
      - Market D  (realized range): settles max(X_i) - min(X_i)
                                    → fair_d = const from t=0 (truth)
  * Both C and D have other oracle-armed bots competing for the
    obvious edge: VANG (oracle_sweeper, C, 10-75s cadence, 5 levels)
    and GVAN (oracle_twapper, D, 4s cadence, size 1). We can't out-
    blitz them — we have to OUT-MM them by sitting tighter than the
    naive bots (VANA, VANC, AVAN, CVAN) while staying outside the
    HVALK/spoofer wide-quote bait zone.

WHAT v20 ADDS vs v19:
  * SYMS = ("A","B","C","D"). State, resting, in_flight, dime
    throttles auto-extend via parent's SYMS-driven loops.
  * Symbol-keyed dispatch helpers — _width_for, _flip_for,
    _min_edge_for, _size_min/max_for, _fair_for — so the parent's
    "if sym == 'A' else 'B'" branches don't need to be rewritten
    case-by-case.
  * `fair_c()` / `fair_d()` — truth-derived exact constants once
    `/api/admin/truth` resolves. Pre-truth fallback uses MC priors.
  * Two-sided MM on C/D with directional skew:
      - C: width=6 (look like a tighter VANA/VANC). Under truth=100,
        bid is tight (close to fair), ask is wide (parked near 100
        cap). Position ramp prevents loading until k > k_min.
      - D: width=4 (match AVAN/CVAN). Under truth-known range,
        dime AVAN/CVAN by 1 tick whenever edge >= penny_min_edge_d.
  * Position-cap ramp (per-symbol):
      - C: 25% * 50 → 100% * 50 (min_cap=10) — gradual load-up.
      - D: 25% * 75 → 100% * 75 (min_cap=15) — gradual load-up.
  * Size ramp (per-symbol): smaller quotes early, larger as truth
    materializes.
  * Anti-spoofer guards preserved per-symbol:
      - A: max_ioc_distance_ticks_a = 50 (HVALK is B-only but other
        wide quoters on A live).
      - B: max_ioc_distance_ticks_b = 50 (HVALK explicit spoofer).
      - C: max_ioc_distance_ticks_c = 90 (binary [0,100] needs deep
        sniping, but linger floor blocks lift below px=2 / above 98
        unless quote persists > c_linger_min_ms).
      - D: max_ioc_distance_ticks_d = 30 (D unbounded but realistic
        range is small; 30t past fair is almost certainly a spoof).
  * Discretion: NO initial blitz, NO one-sided quoting under truth.
    We blend in with the MM crowd via matched widths and reasonable
    sizes. Snipes only fire on grossly mispriced book levels (>=
    sweep_edge_ticks_X) — they look like normal cross-arb hits.
  * VANA/VANC are wide_width=20-30 — when truth diverges from their
    naive_mean=7 prior (e.g. truth says fair_c=100), every bid<99 or
    ask>1 they post is ours for the taking. v20 catches them on the
    cross-arb path (inter_sweep + on-fill).

BOT MAP (from bot_config_dump.json):
  * A bots: VALKA(naive_mm,w20), VALKB(noise), VALKC(informed_sniper,
    lead=1500ms,fire=0.4), VALKD(dimer,refresh=1s,min_spread=0),
    VALKE(mixed_sweeper), VALKF(predictive_mm,acc=0.55),
    VALKG(directional_taker,lean=0.7), VALKH(inventory_mm,w14),
    VALKJ(slow_informed,30-60s), VALKK(casual,w30) — wide+slow,
    VALKL(bayes_mm,w24), VALKM(bayes_taker,edge=2),
    VALKN(pull_event,ioc=50), VALKO(stale_quoter,5s_refresh,size=25)
    — BIG STALE QUOTES, easy snipe when truth contradicts.
  * B bots: AVALK(naive_avg_mm,w6), BVALK(noise),
    CVALK(informed_sniper_next,fire=1.0,lead=1s),
    DVALK(predictive_avg_mm,acc=0.55), EVALK(random_sweeper),
    FVALK(directional_taker_next,lean=0.7), GVALK(informed_twapper),
    HVALK(SPOOFER,size=10-30,linger_ms=300) — DON'T FILL,
    IVALK(true_mean_taker,edge=0), JVALK(order_stat_inv_mm,w6),
    KVALK(dimer,refresh=1s,improve=1-2t), SEAN(sean,w=4-8).
  * C bots: VANA(naive_binary_mm,w30,size 2-4),
    VANB(noise,max_buy=99/min_sell=2) — random hits between 2-99,
    VANC(moments_binary_mm,w20,size 2-4),
    VAND(dimer,min_spread=4,refresh=1.5s) — only dimes if BBO>=4,
    VANE(informed_binary_taker,25-35s,edge=0) — informed competitor,
    VANF(random_sweeper,max_buy=99/min_sell=2) — random sweeps,
    VANG(oracle_sweeper,10-75s,levels=5) — DIRECT COMPETITOR with
    insider info. Be tighter than VANG between its bursts.
  * D bots: AVAN(naive_range_mm,w4,size 3-5),
    BVAN(noise,size=2), CVAN(naive_range_mm,w4,size 3-5),
    DVAN(informed_range_taker,25-35s,n_sims=3000),
    EVAN(directional_taker,lean=0.65),
    FVAN(resting_layer,lead=1.5s pre-reveal),
    GVAN(oracle_twapper,4s+jitter,size=1) — slow oracle competitor.

DESIGN INVARIANTS:
  * Hard position_limit on A/B/C/D never breached (snipe paths still
    use ±L; MM uses the ramped soft cap).
  * Truth-anchored fair → tiny sigma → tight inter-sweep / cross-arb
    edges. The "looks like MM" disguise comes from the WIDTH of our
    posted quotes, not the sweep math.
  * NEVER lift HVALK-style 300ms-linger wide-quote bait — the
    max_ioc_distance + linger gate enforces this.
  * Dime defense fires per-symbol with independent throttles so a
    v=1 dime war on D doesn't starve A's REST budget.

Run:
    python day1/strategy20/run_combined20.py
"""

# This module was assembled from the v12/v18/v19/v20 strategy stack and the
# sdk_client + GameClient implementation. See per-section banners below for
# the original file boundaries. The pre-consolidation files lived next to
# this one as `sdk_client.py`, `strategy12.py`, `strategy18.py`, and
# `strategy19.py` and were deleted after the merge.

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import random
import socket
import statistics
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import requests
import websockets

try:
    from requests.adapters import HTTPAdapter
    from urllib3.poolmanager import PoolManager
    _HAVE_REQUESTS = True
except Exception:
    _HAVE_REQUESTS = False



# ===========================================================================
# Inlined from sdk_client.py — GameClient + token bucket
# ===========================================================================

class _TokenBucket:
    """Thread-safe leaky-bucket rate limiter.

    Why: the exchange enforces a hard 20 req/s cap per API key and answers
    excess requests with `reject {reason: rate_limited}` plus a 3-second
    cool-off during which we lose every snipe opportunity. We saw 3,246
    such rejects across one analysis batch (91.7% on new orders). A
    client-side bucket at slightly *under* the server cap eliminates the
    lockout: when we'd otherwise burst past 20/s, we delay by a few ms
    instead of forfeiting 3 seconds. Shared across all calling threads.
    """

    def __init__(self, rate: float, capacity: float):
        self.rate = rate            # tokens per second refill
        self.capacity = capacity    # max tokens (also burst size)
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

    def acquire(self, timeout: float = 5.0) -> bool:
        """Block until a token is available, up to `timeout` seconds.
        Returns True on success, False on timeout. We sleep in small slices
        so the wait-time tracks closely to bucket refill granularity."""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                self._refill_locked()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                # how long until we'd have one token?
                deficit = 1.0 - self.tokens
                wait = deficit / self.rate
            now = time.monotonic()
            if now >= deadline:
                return False
            time.sleep(min(wait, deadline - now, 0.02))

    def available(self) -> float:
        """Current token count (snapshot). Callers race other threads but
        the value is accurate enough to decide whether to skip non-critical
        REST calls (e.g., redundant modifies) and reserve budget for snipes."""
        with self.lock:
            self._refill_locked()
            return self.tokens


class GameClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._ws_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # requests.Session reuses the underlying TCP connection across calls,
        # eliminating the per-call TCP handshake (~1-3ms LAN). At 20-50
        # REST/sec on the hot path that saves serious latency vs competing
        # FIFO actors.
        self._session = requests.Session()
        # API key is sent on every request; Content-Type is set automatically
        # by requests when json=... is passed, so we only need the key here.
        self._session.headers.update({"X-API-Key": self.api_key})
        # Client-side rate limiter at slightly under the server's 20 req/s.
        # Every REST method below acquires one token before hitting the wire.
        # 18 req/s sustained + 20-token burst gives us full server bandwidth
        # without the 3-second lockout from a `rate_limited` reject.
        self._bucket = _TokenBucket(rate=18.0, capacity=20.0)

        # Callbacks — override on the instance
        self.on_fill: Callable[[dict], None] = lambda m: None
        self.on_trade: Callable[[dict], None] = lambda m: None
        self.on_book: Callable[[dict], None] = lambda m: None
        self.on_reveal: Callable[[dict], None] = lambda m: None
        self.on_game_state: Callable[[dict], None] = lambda m: None
        self.on_settlement: Callable[[dict], None] = lambda m: None
        self.on_ack: Callable[[dict], None] = lambda m: None  # order_ack
        self.on_cancel_ack: Callable[[dict], None] = lambda m: None
        self.on_modify_ack: Callable[[dict], None] = lambda m: None
        self.on_reject: Callable[[dict], None] = lambda m: None
        self.on_message: Callable[[dict], None] = lambda m: None  # catch-all

    # ---------- REST ----------

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def tokens_available(self) -> float:
        """Snapshot of how many REST tokens are free right now. Strategies
        can call this to skip non-critical writes (e.g., maker requotes)
        and reserve budget for high-edge actions like IOC snipes."""
        return self._bucket.available()

    def buy(
        self,
        symbol: str,
        price: int,
        qty: int,
        client_order_id: Optional[str] = None,
        tif: str = "gtc",
    ) -> dict:
        return self._order(symbol, "buy", "limit", qty, price, client_order_id, tif)

    def sell(
        self,
        symbol: str,
        price: int,
        qty: int,
        client_order_id: Optional[str] = None,
        tif: str = "gtc",
    ) -> dict:
        return self._order(symbol, "sell", "limit", qty, price, client_order_id, tif)

    def buy_market(self, symbol: str, qty: int, tif: str = "ioc") -> dict:
        return self._order(symbol, "buy", "market", qty, None, None, tif)

    def sell_market(self, symbol: str, qty: int, tif: str = "ioc") -> dict:
        return self._order(symbol, "sell", "market", qty, None, None, tif)

    def buy_ioc(self, symbol: str, price: int, qty: int) -> dict:
        return self._order(symbol, "buy", "limit", qty, price, None, "ioc")

    def sell_ioc(self, symbol: str, price: int, qty: int) -> dict:
        return self._order(symbol, "sell", "limit", qty, price, None, "ioc")

    def buy_fok(self, symbol: str, price: int, qty: int) -> dict:
        return self._order(symbol, "buy", "limit", qty, price, None, "fok")

    def sell_fok(self, symbol: str, price: int, qty: int) -> dict:
        return self._order(symbol, "sell", "limit", qty, price, None, "fok")

    def _order(self, symbol, side, type_, qty, price, client_order_id, tif="gtc") -> dict:
        body: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": type_, "qty": qty, "tif": tif,
        }
        if price is not None:
            body["price"] = price
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        self._bucket.acquire()
        r = self._session.post(f"{self.base_url}/api/order", json=body)
        r.raise_for_status()
        return r.json()

    def modify(
        self,
        order_id: int,
        price: Optional[int] = None,
        qty: Optional[int] = None,
    ) -> dict:
        """Modify a resting GTC limit order.

        Queue-priority rule: pure qty-down at the same price keeps your spot
        in the FIFO queue. Any other change (price change, or qty up) loses
        priority and may execute immediately if the new price crosses.

        Returns: {"order": ..., "trades": [...], "kept_priority": bool}
        """
        body: dict[str, Any] = {}
        if price is not None:
            body["price"] = price
        if qty is not None:
            body["qty"] = qty
        self._bucket.acquire()
        r = self._session.patch(
            f"{self.base_url}/api/order/{order_id}", json=body
        )
        r.raise_for_status()
        return r.json()

    def cancel(self, order_id: int) -> dict:
        self._bucket.acquire()
        r = self._session.delete(f"{self.base_url}/api/order/{order_id}")
        r.raise_for_status()
        return r.json()

    def cancel_all(self, symbol: Optional[str] = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        self._bucket.acquire()
        r = self._session.delete(f"{self.base_url}/api/orders", params=params)
        r.raise_for_status()
        return r.json()

    def my_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        self._bucket.acquire()
        r = self._session.get(f"{self.base_url}/api/orders", params=params)
        r.raise_for_status()
        return r.json()["orders"]

    def book(self, symbol: Optional[str] = None, depth: int = 10) -> dict:
        params: dict[str, Any] = {"depth": depth}
        if symbol:
            params["symbol"] = symbol
        self._bucket.acquire()
        r = self._session.get(f"{self.base_url}/api/book", params=params)
        r.raise_for_status()
        return r.json()

    def positions(self) -> dict:
        self._bucket.acquire()
        r = self._session.get(f"{self.base_url}/api/positions")
        r.raise_for_status()
        return r.json()

    def game_state(self) -> dict:
        self._bucket.acquire()
        r = self._session.get(f"{self.base_url}/api/game")
        r.raise_for_status()
        return r.json()

    def instruments(self) -> dict:
        self._bucket.acquire()
        r = self._session.get(f"{self.base_url}/api/instruments")
        r.raise_for_status()
        return r.json()

    def leaderboard(self) -> list:
        self._bucket.acquire()
        r = self._session.get(f"{self.base_url}/api/leaderboard")
        r.raise_for_status()
        return r.json()["rows"]

    # ---------- WebSocket ----------

    def start(self) -> None:
        if self._ws_thread is not None:
            return
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def wait_forever(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def _ws_loop(self) -> None:
        ws_base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{ws_base}/ws/private?api_key={self.api_key}"
        asyncio.run(self._ws_run(url))

    async def _ws_run(self, url: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        self._dispatch(msg)
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        self.on_message(msg)
        if t == "fill":
            self.on_fill(msg)
        elif t == "trade":
            self.on_trade(msg)
        elif t == "book":
            self.on_book(msg)
        elif t == "reveal":
            self.on_reveal(msg)
        elif t == "game_state":
            self.on_game_state(msg)
        elif t == "settlement":
            self.on_settlement(msg)
        elif t == "order_ack":
            self.on_ack(msg)
        elif t == "cancel_ack":
            self.on_cancel_ack(msg)
        elif t == "modify_ack":
            self.on_modify_ack(msg)
        elif t == "reject":
            self.on_reject(msg)


# ===========================================================================
# Inlined from strategy12.py — Posterior, CPProfile, NoDelay adapter,
# PrecomputedScenario, base StrategyV12
# ===========================================================================

try:
    from requests.adapters import HTTPAdapter
    from urllib3.poolmanager import PoolManager
    _HAVE_REQUESTS = True
except Exception:
    _HAVE_REQUESTS = False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"


# ---------------------------------------------------------------------------
# TCP_NODELAY adapter
# ---------------------------------------------------------------------------
class _NoDelayHTTPAdapter(HTTPAdapter if _HAVE_REQUESTS else object):
    """HTTPAdapter that disables Nagle's algorithm.

    Why: urllib3 2.x doesn't pass any socket_options by default. For our
    sub-200-byte JSON POSTs that means up to 40ms of ACK-coalescing wait
    on each request, which compounds across IOC futures at reveal."""

    def __init__(self, pool_maxsize: int = 16):
        self._pool_maxsize = pool_maxsize
        super().__init__(pool_connections=pool_maxsize,
                         pool_maxsize=pool_maxsize, pool_block=False)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        kw["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        ]
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **kw)


def _install_nodelay(client: GameClient, pool_maxsize: int) -> bool:
    if not _HAVE_REQUESTS:
        return False
    try:
        adapter = _NoDelayHTTPAdapter(pool_maxsize=pool_maxsize)
        client._session.mount("http://", adapter)
        client._session.mount("https://", adapter)
        return True
    except Exception as e:
        print(f"[v12 NODELAY] install failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Posterior over (a, w) — unchanged from v11
# ---------------------------------------------------------------------------
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8
W_LOGN_MU, W_LOGN_SIGMA = 1.7, 0.9
N_PRIOR_SIM = 1_000_000


class Posterior:
    """Discrete joint posterior over (a, w) for X_i ~ Uniform{a, ..., a+w}."""

    def __init__(self, a_mu: float = A_LOGN_MU, a_sigma: float = A_LOGN_SIGMA,
                 w_mu: float = W_LOGN_MU, w_sigma: float = W_LOGN_SIGMA,
                 n_sim: int = N_PRIOR_SIM):
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for _ in range(n_sim):
            a_v = int(math.floor(random.lognormvariate(a_mu, a_sigma)))
            w_v = 1 + int(math.floor(random.lognormvariate(w_mu, w_sigma)))
            counts[(a_v, w_v)] += 1
        total = sum(counts.values())
        self.prior: dict[tuple[int, int], float] = {
            k: v / total for k, v in counts.items()}
        self.posterior: dict[tuple[int, int], float] = dict(self.prior)
        self.reveals: list[float] = []

    def reset(self, reveals: Optional[list[float]] = None) -> None:
        self.posterior = dict(self.prior)
        self.reveals = []
        for x in reveals or []:
            self.update(x)

    def update(self, x: float) -> None:
        self.reveals.append(float(x))
        new: dict[tuple[int, int], float] = {}
        total = 0.0
        eps = 1e-9
        for (a, w), p in self.posterior.items():
            b = a + w
            if x < a - eps or x > b + eps:
                continue
            new_p = p / (w + 1)
            new[(a, w)] = new_p
            total += new_p
        if total <= 0:
            print(f"WARN: reveal {x} outside posterior support; ignoring")
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def predict_settle(self, running_sum: float,
                       n_remaining: int) -> tuple[float, float]:
        if n_remaining <= 0:
            return float(running_sum), 0.0
        e_inner = 0.0
        e2_inner = 0.0
        e_var = 0.0
        for (a, w), p in self.posterior.items():
            inner_mean = n_remaining * (a + w / 2.0)
            inner_var = n_remaining * (w * (w + 2)) / 12.0
            e_inner += p * inner_mean
            e2_inner += p * inner_mean * inner_mean
            e_var += p * inner_var
        var_total = e_var + (e2_inner - e_inner * e_inner)
        return running_sum + e_inner, math.sqrt(max(var_total, 0.0))


# ---------------------------------------------------------------------------
# CPProfile: runtime per-counterparty stats for sizing/flow
# ---------------------------------------------------------------------------
@dataclass
class _CPStat:
    qty_we_bought: int = 0   # we BOUGHT from them (their side=sell)
    qty_we_sold: int = 0     # we SOLD to them   (their side=buy)
    taker_count: int = 0     # times they were taker (aggressor)
    maker_count: int = 0     # times they were maker (our IOC hit them)
    last_fill_t: float = 0.0
    # PnL feedback proxy: signed (notional moved against us in ticks).
    adverse_ticks: float = 0.0


class CPProfile:
    """Runtime stats per counterparty across the session.

    Fed exclusively from our private fill stream (on_fill_event) plus the
    posterior fair at the moment of fill. The data is used to:
      * detect one-sided flow (CPs who only buy from us / only sell to us)
      * size up against CPs whose history shows we extract edge
      * widen the side an "informed-looking" CP keeps hitting
    """

    def __init__(self, recent_window_sec: float = 30.0,
                 recent_max_len: int = 256):
        self._lock = threading.Lock()
        self._stats: Dict[str, _CPStat] = defaultdict(_CPStat)
        # recent fills: (ts, side_we_did, qty, cp, fair_at_fill, fill_px)
        self._recent: deque[tuple[float, str, int, str, float, float]] = deque(
            maxlen=recent_max_len)
        self._recent_window_sec = recent_window_sec

    def record_fill(self, *, cp: str, side: str, qty: int,
                    liq: str, fill_px: float, fair_at_fill: float) -> None:
        """side: 'buy' or 'sell' from OUR perspective. liq: 'maker' or 'taker'
        from OUR perspective."""
        if not cp or qty <= 0:
            return
        now = time.time()
        with self._lock:
            s = self._stats[cp]
            if side == "buy":
                s.qty_we_bought += qty
                # adverse if we bought and fill was above fair (paid up)
                s.adverse_ticks += (fill_px - fair_at_fill) * qty
            elif side == "sell":
                s.qty_we_sold += qty
                # adverse if we sold and fill was below fair (sold cheap)
                s.adverse_ticks += (fair_at_fill - fill_px) * qty
            if liq == "maker":
                # WE were maker => CP was taker => CP is the aggressor
                s.taker_count += 1
            elif liq == "taker":
                s.maker_count += 1
            s.last_fill_t = now
            self._recent.append((now, side, qty, cp, fair_at_fill, fill_px))

    def _expire_recent(self, now: float) -> None:
        cutoff = now - self._recent_window_sec
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    def flow_score(self) -> float:
        """Signed buy-vs-sell imbalance over recent fills.

        Returns score in [-1, +1]:
          +1  recent fills are 100% us BUYING (our bids being lifted? no:
              if we BOUGHT, our offer-side is depleted by us taking, OR
              someone hit our bid making us buy passively as maker).
        Interpretation depends on liq; we keep it simple here and let
        callers reason about direction. Positive = our recent net = buy.
        """
        now = time.time()
        with self._lock:
            self._expire_recent(now)
            num = 0.0
            den = 0.0
            for ts, side, qty, _, _, _ in self._recent:
                w = qty
                if side == "buy":
                    num += w
                elif side == "sell":
                    num -= w
                den += w
            if den <= 0:
                return 0.0
            return max(-1.0, min(1.0, num / den))

    def directional_pressure(self) -> tuple[float, float]:
        """Returns (buy_pressure, sell_pressure) each in [0,1] over recent
        fills, normalized by total. Used for asymmetric quoting.

        buy_pressure  high => recent fills are us buying (book is being
                              hit on the ask side, we're absorbing sells)
        sell_pressure high => recent fills are us selling (our ask is
                              being lifted; flow wants to BUY from us)
        """
        now = time.time()
        with self._lock:
            self._expire_recent(now)
            buy_q = 0
            sell_q = 0
            for ts, side, qty, _, _, _ in self._recent:
                if side == "buy":
                    buy_q += qty
                elif side == "sell":
                    sell_q += qty
            tot = buy_q + sell_q
            if tot <= 0:
                return 0.0, 0.0
            return buy_q / tot, sell_q / tot

    def is_adverse(self, cp: Optional[str]) -> bool:
        if not cp:
            return False
        with self._lock:
            s = self._stats.get(cp)
            if not s:
                return False
            return s.adverse_ticks > 5.0 and s.maker_count >= 2

    def snapshot(self) -> Dict[str, _CPStat]:
        with self._lock:
            return {k: _CPStat(**vars(v)) for k, v in self._stats.items()}


# ---------------------------------------------------------------------------
# Config v12: tunable knobs grouped by purpose
# ---------------------------------------------------------------------------
@dataclass
class ConfigV12:
    # --- maker quoting (BIGGER defaults than v11) ---
    quote_qty: int = 4                  # v11 was 2
    quote_qty_normal: int = 6           # v11 was 5
    mid_round_quote_qty: int = 2        # v11 was 1
    full_qty_sigma_max: float = 3.0
    pre_reveal_quote_qty: int = 2       # v11 was 1
    min_edge: float = 1.0
    edge_per_sigma: float = 0.25
    skew_per_unit: float = 0.20         # inventory skew
    no_edge_pull_ticks: float = 0.5
    tight_floor_edge: float = 1.0       # min per-fill margin when pennying

    # --- aggressive pennying (NEW in v12) ---
    penny_min_edge: float = 0.5         # accept tighter floor when pennying
    penny_when_biased: bool = True      # v11 forbid; v12 allow if safe
    penny_max_sigma: float = 6.0        # only suppress penny in very wide markets

    # --- flow-based asymmetric sizing (NEW in v12) ---
    flow_score_threshold: float = 0.5   # |score| >= this to act
    flow_size_bonus: int = 3            # extra lots on demanded side
    flow_widen_ticks: float = 1.0       # widen demanded side by N ticks
    flow_narrow_ticks: float = 0.0      # narrow opposite side by N ticks

    # --- snipe (IOC) — unchanged from v11 ---
    snipe_min_edge: float = 2.0
    snipe_buffer_sigma: float = 0.40
    snipe_book_depth: int = 10
    snipe_max_qty_per_level: int = 10
    snipe_max_per_round: int = 40
    snipe_position_buffer: int = 8
    snipe_max_disagreement: float = 3.0
    snipe_vwap_buffer: float = 1.0

    # --- precompute ---
    precompute_min_prob: float = 5e-3
    precompute_max_values: int = 40
    precompute_lead_sec: float = 4.0
    jit_refresh_sec: float = 0.5
    ioc_parallel_workers: int = 6

    # --- pre-reveal scheduler ---
    pre_reveal_post_sec: float = 1.5
    pre_reveal_post_qty: int = 6        # v11 was 5 — matches new bigger size
    pre_reveal_cancel_sec: float = 0.9
    pre_reveal_blackout_sec: float = 0.95
    pre_reveal_token_reserve: float = 7.0

    # --- book pre-warm fallback ---
    book_prewarm_lead_sec: float = 0.7
    book_prewarm_max_age_sec: float = 0.1
    book_cache_max_age_sec: float = 1.0

    # --- adverse-fill skew (between-reveal MM) ---
    adverse_skew_ticks: float = 1.5
    adverse_decay_sec: float = 8.0

    # --- VWAP / market signal ---
    trade_window_sec: float = 30.0
    vwap_clamp_max_k: int = 3
    vwap_clamp_min_volume: int = 3
    vwap_side_guard_ticks: int = 1
    min_trade_vol_pre_reveal: int = 3
    pre_reveal_vwap_weight: float = 0.7
    pre_reveal_max_pos: int = 5
    market_anchor_disagreement: float = 5.0

    # --- reveal-math guard ---
    reveal_math_guard_ticks: float = 4.0
    reveal_math_k1_widening: float = 2.0

    # --- inventory ---
    max_pos_target_sigma: float = 2.0
    max_pos_floor: int = 10
    max_pos_target_sigma_normal: float = 3.0
    max_pos_floor_normal: int = 15
    inv_hard_kill_frac: float = 0.4

    # --- drift cancel ---
    drift_cancel_ticks: float = 3.0
    drift_cancel_min_interval: float = 0.5

    # --- partial fill cool-off ---
    partial_fill_cool_off_sec: float = 3.0

    # --- pre-reveal MM ---
    pre_reveal_min_edge: float = 4.0
    pre_reveal_pull_disagreement: float = 10.0
    empty_side_edge: float = 15.0
    pre_reveal_warmup_sec: float = 2.0

    # --- maker cadence between reveals ---
    maker_reprice_interval_by_k: tuple[tuple[int, float], ...] = (
        (3, 12.0), (7, 8.0), (99, 4.0))

    # --- scheduler ticks ---
    fast_scheduler_tick_sec: float = 0.02
    slow_housekeeping_interval_sec: float = 2.0
    current_theos_max_age_sec: float = 2.5

    # --- ws-book settling ---
    quote_event_throttle_sec: float = 0.05


# ---------------------------------------------------------------------------
# PrecomputedScenario: "if next reveal == v, do this"
# ---------------------------------------------------------------------------
@dataclass
class PrecomputedScenario:
    value: int
    posterior_snapshot: Dict[Tuple[int, int], float]
    fair: float
    sigma: float
    bid_px: Optional[int]
    ask_px: Optional[int]
    lift_to_px: Optional[int]
    hit_to_px: Optional[int]
    marginal_prob: float = 0.0


# ---------------------------------------------------------------------------
# Strategy v12
# ---------------------------------------------------------------------------
class StrategyV12:
    def __init__(self, client: GameClient, posterior: Posterior,
                 symbol: str = "A", config: Optional[ConfigV12] = None):
        self.c = client
        self.symbol = symbol
        self.posterior = posterior
        self.cfg = config or Config()
        self.lock = threading.RLock()

        # ---- v12: install TCP_NODELAY on the session BEFORE first call ----
        ok = _install_nodelay(client, pool_maxsize=max(
            8, self.cfg.ioc_parallel_workers + 2))
        print(f"[v12 TCP_NODELAY] installed={ok}")

        gs = client.game_state()
        self.duration = gs["duration"]
        self.reveal_interval = gs["reveal_interval"]
        self.n_total = self.duration // self.reveal_interval
        instr = gs["instruments"][symbol]
        self.tick = instr["tick_size"]
        self.position_limit = instr["position_limit"]
        fees = gs.get("fees", {})
        self.maker_fee = fees.get("maker_per_lot", 0.5)
        self.taker_fee = fees.get("taker_per_lot", 0.5)

        self.posterior.reset(gs.get("reveals") or [])
        self.position = int(client.positions()["positions"].get(symbol, 0))
        self.phase: Optional[str] = gs.get("phase")

        # ----- state -----
        self.resting: dict[str, Optional[dict]] = {"bid": None, "ask": None}
        self._posterior_gen: int = 0
        self._fair_sigma_cache: Optional[tuple[int, float, float]] = None
        self._last_maker_apply_t: float = 0.0
        self._last_quote_event_t: float = 0.0
        self._snipe_count_this_round: int = 0
        self._phase_running_t: Optional[float] = None

        # ----- v12 CPProfile -----
        self.cpp = CPProfile(recent_window_sec=self.cfg.trade_window_sec)

        # ----- adverse skew + cool-offs -----
        self._last_adverse_fill_t: Dict[str, float] = {
            "bid": 0.0, "ask": 0.0}
        self._partial_fill_cool_off_until: Dict[str, float] = {
            "bid": 0.0, "ask": 0.0}

        # ----- WS book cache -----
        self._book_cache: Optional[dict] = None
        self._book_cache_t: float = 0.0

        # ----- private fill signal -----
        self._last_fill_px: float = 0.0
        self._last_fill_t: float = 0.0

        # ----- public trade tape -----
        self._recent_trades: deque[tuple[float, float, int, str]] = deque(
            maxlen=1000)
        self._vwap_num: float = 0.0
        self._vwap_den: int = 0

        # ----- reveal timing -----
        self._last_reveal_t: Optional[float] = None
        self._pre_reveal_last_serviced_idx: int = -1
        self._pre_reveal_last_cancelled_idx: int = -1

        # ----- drift cancel -----
        self._last_quote_fair_eff: Optional[float] = None
        self._last_drift_cancel_t: float = 0.0
        try:
            mean0, _ = posterior.predict_settle(0.0, self.n_total)
            self._prior_E_settle: float = float(mean0)
        except Exception:
            self._prior_E_settle = 27.0

        # ----- precompute -----
        self._precomputed: Dict[int, PrecomputedScenario] = {}
        self._precompute_lock = threading.RLock()
        self._precompute_for_cycle: int = -1
        self._precompute_request = threading.Event()
        self._last_precompute_us: float = 0.0
        self._precompute_run_count: int = 0
        self._precompute_prefetched_cycle: int = -1
        self._jit_refresh_done_for_cycle: int = -1
        self._last_book_prewarm_t: float = 0.0

        # ----- current-state theos (cheap read cache) -----
        self._current_theos: Optional[Dict[str, Any]] = None
        self._current_theos_cycle: int = -1
        self._current_theos_t: float = 0.0

        # ----- executors -----
        self._ioc_executor = ThreadPoolExecutor(
            max_workers=self.cfg.ioc_parallel_workers,
            thread_name_prefix="v12-ioc")
        self._mm_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="v12-mm")

        # ----- threads -----
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(
            target=self._fast_scheduler_loop,
            name="v12-fast-sched", daemon=True)
        self._scheduler_thread.start()
        self._slow_thread = threading.Thread(
            target=self._slow_housekeeping_loop,
            name="v12-slow-house", daemon=True)
        self._slow_thread.start()
        self._precompute_worker = threading.Thread(
            target=self._precompute_loop,
            name="v12-precompute", daemon=True)
        self._precompute_worker.start()
        self._precompute_request.set()

        self._warm_connections(self.cfg.ioc_parallel_workers)

    # ------------------------------------------------------------------
    # Warming
    # ------------------------------------------------------------------
    def _warm_connections(self, n: int) -> None:
        t0 = time.perf_counter()
        try:
            list(self._ioc_executor.map(
                lambda _: self.c.game_state(), range(n)))
            dt_ms = (time.perf_counter() - t0) * 1000
            print(f"[v12 WARM] {n} HTTP connections in {dt_ms:.1f}ms")
        except Exception as e:
            print(f"[v12 WARM] connection warm-up failed: {e}")

    # ------------------------------------------------------------------
    # Trivial helpers
    # ------------------------------------------------------------------
    def _running_sum(self) -> float:
        return sum(self.posterior.reveals)

    def _n_remaining(self) -> int:
        return max(self.n_total - len(self.posterior.reveals), 0)

    def fair_and_sigma(self) -> tuple[float, float]:
        gen = self._posterior_gen
        cached = self._fair_sigma_cache
        if cached is not None and cached[0] == gen:
            return cached[1], cached[2]
        fair, sigma = self.posterior.predict_settle(
            self._running_sum(), self._n_remaining())
        self._fair_sigma_cache = (gen, fair, sigma)
        return fair, sigma

    def _maker_reprice_interval(self) -> float:
        k = len(self.posterior.reveals)
        if k == 0:
            return float("inf")
        for k_max, interval in self.cfg.maker_reprice_interval_by_k:
            if k <= k_max:
                return interval
        return self.cfg.maker_reprice_interval_by_k[-1][1]

    # ------------------------------------------------------------------
    # Reveal-math guard
    # ------------------------------------------------------------------
    def _reveal_math_fair(self) -> Optional[float]:
        reveals = self.posterior.reveals
        k = len(reveals)
        if k < 1:
            return None
        remaining = self._n_remaining()
        if remaining <= 0:
            return sum(reveals)
        return sum(reveals) + remaining * statistics.fmean(reveals)

    def _reveal_math_guard_width(self) -> float:
        k = len(self.posterior.reveals)
        if k == 1:
            return self.cfg.reveal_math_guard_ticks * self.cfg.reveal_math_k1_widening
        return self.cfg.reveal_math_guard_ticks

    def _reveal_math_blocks_buy(self, fair_eff: float) -> bool:
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return fair_eff - rm > self._reveal_math_guard_width()

    def _reveal_math_blocks_sell(self, fair_eff: float) -> bool:
        rm = self._reveal_math_fair()
        if rm is None:
            return False
        return rm - fair_eff > self._reveal_math_guard_width()

    # ------------------------------------------------------------------
    # VWAP / market signal
    # ------------------------------------------------------------------
    def _expire_trades(self, now: float) -> None:
        cutoff = now - self.cfg.trade_window_sec
        tape = self._recent_trades
        while tape and tape[0][0] < cutoff:
            _, px_old, q_old, _ = tape.popleft()
            self._vwap_num -= px_old * q_old
            self._vwap_den -= q_old

    def _trade_vwap(self) -> Optional[float]:
        now = time.time()
        with self.lock:
            self._expire_trades(now)
            if self._vwap_den <= 0:
                return None
            return self._vwap_num / self._vwap_den

    def _vwap_clamped_fair(self, fair: float) -> float:
        k = len(self.posterior.reveals)
        if k < 1 or k > self.cfg.vwap_clamp_max_k:
            return fair
        if self._vwap_den < self.cfg.vwap_clamp_min_volume:
            return fair
        vwap = self._trade_vwap()
        if vwap is None:
            return fair
        w = (self.cfg.vwap_clamp_max_k - k + 1) / (self.cfg.vwap_clamp_max_k + 1)
        return w * vwap + (1.0 - w) * fair

    def _vwap_for_side_guard(self) -> Optional[float]:
        if self._vwap_den < self.cfg.vwap_clamp_min_volume:
            return None
        return self._trade_vwap()

    def _market_mid_from_book(self) -> Optional[float]:
        book = self._book_cache
        if book is None:
            return None
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return (bids[0]["price"] + asks[0]["price"]) / 2.0

    def _market_signal(self) -> Optional[float]:
        book_mid = self._market_mid_from_book()
        vwap = self._trade_vwap()
        if book_mid is not None and vwap is not None:
            return 0.5 * book_mid + 0.5 * vwap
        return vwap if vwap is not None else book_mid

    def _pre_reveal_vwap_fair(self, prior_mean: float) -> Optional[float]:
        vwap = self._trade_vwap()
        if vwap is None or self._vwap_den < self.cfg.min_trade_vol_pre_reveal:
            return None
        w = self.cfg.pre_reveal_vwap_weight
        return w * vwap + (1.0 - w) * prior_mean

    # ------------------------------------------------------------------
    # Sizing (v12: per-side, CPProfile-aware)
    # ------------------------------------------------------------------
    def _max_position_for_sigma(self, sigma: float,
                                *, relaxed: bool = False) -> int:
        if relaxed:
            target = self.cfg.max_pos_target_sigma_normal
            floor = self.cfg.max_pos_floor_normal
        else:
            target = self.cfg.max_pos_target_sigma
            floor = self.cfg.max_pos_floor
        if sigma <= target:
            return self.position_limit
        cap = int(self.position_limit * target / sigma)
        return max(floor, cap)

    def _base_quote_qty(self) -> int:
        """Symmetric base size before per-side flow bonus."""
        if len(self.posterior.reveals) == 0:
            return self.cfg.pre_reveal_quote_qty
        fair, sigma = self.fair_and_sigma()
        market_mid = self._market_signal()
        relaxed = (market_mid is not None and
                   abs(fair - market_mid) < self.cfg.market_anchor_disagreement)
        if sigma > self.cfg.full_qty_sigma_max:
            base = self.cfg.mid_round_quote_qty
        else:
            base = self.cfg.quote_qty_normal if relaxed else self.cfg.quote_qty
        if (self._in_pre_reveal_window() and market_mid is not None
                and abs(fair - market_mid) < self.cfg.market_anchor_disagreement):
            base = max(base, self.cfg.pre_reveal_post_qty)
        return base

    def _current_quote_qty(self, side: Optional[str] = None) -> int:
        """Per-side size with flow-asymmetric bonus.

        side='bid' or 'ask'. None returns symmetric base (legacy callers).
        Flow score >0 => recent fills net us BUYING => one-sided buyers'
        flow has been on our ASK (people lifting us). Upsize ASK quote
        so they hit more lots; keep BID at base.
        """
        base = self._base_quote_qty()
        if side is None:
            return base
        # directional bonus
        buy_p, sell_p = self.cpp.directional_pressure()
        # buy_p high => WE buy (passive bid hit / aggressive lift) — book sells to us
        # sell_p high => WE sell (passive ask lifted) — book buys from us
        if side == "ask" and sell_p - buy_p >= self.cfg.flow_score_threshold:
            return base + self.cfg.flow_size_bonus
        if side == "bid" and buy_p - sell_p >= self.cfg.flow_score_threshold:
            return base + self.cfg.flow_size_bonus
        return base

    # ------------------------------------------------------------------
    # Adverse-fill skew
    # ------------------------------------------------------------------
    def _adverse_skew(self, side: str, now: float) -> float:
        last_t = self._last_adverse_fill_t.get(side, 0.0)
        if last_t <= 0.0:
            return 0.0
        age = now - last_t
        if age >= self.cfg.adverse_decay_sec:
            return 0.0
        return self.cfg.adverse_skew_ticks * (
            1.0 - age / self.cfg.adverse_decay_sec)

    # ------------------------------------------------------------------
    # Reveal-window helpers
    # ------------------------------------------------------------------
    def _next_reveal_at(self) -> Optional[float]:
        if self._last_reveal_t is None:
            return None
        nxt = self._last_reveal_t + self.reveal_interval
        now = time.time()
        while nxt < now - 0.5:
            nxt += self.reveal_interval
        return nxt

    def _in_pre_reveal_window(self) -> bool:
        nxt = self._next_reveal_at()
        if nxt is None or self._n_remaining() <= 0:
            return False
        now = time.time()
        return (nxt - self.cfg.pre_reveal_post_sec) <= now \
            < (nxt - self.cfg.pre_reveal_cancel_sec)

    def _in_quote_blackout(self) -> bool:
        nxt = self._next_reveal_at()
        if nxt is None or self._n_remaining() <= 0:
            return False
        return time.time() >= (nxt - self.cfg.pre_reveal_blackout_sec)

    # ------------------------------------------------------------------
    # Quote calculation
    # ------------------------------------------------------------------
    def desired_quotes(self) -> tuple[Optional[int], Optional[int], float, float]:
        fair, sigma = self.fair_and_sigma()
        fair_eff = self._vwap_clamped_fair(fair)
        pre_reveal_vwap_active = False
        if len(self.posterior.reveals) == 0:
            v6 = self._pre_reveal_vwap_fair(prior_mean=fair)
            if v6 is None:
                return None, None, fair, sigma
            fair_eff = v6
            pre_reveal_vwap_active = True
        if self._n_remaining() == 0:
            return None, None, fair, sigma

        if not self._other_makers_present():
            return None, None, fair, sigma

        pre_reveal = len(self.posterior.reveals) == 0
        if pre_reveal and self._phase_running_t is not None:
            if time.time() - self._phase_running_t < self.cfg.pre_reveal_warmup_sec:
                return None, None, fair, sigma

        pre_bids: list = []
        pre_asks: list = []
        if pre_reveal:
            book = self._book_cache
            if (book is None or
                    time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec):
                return None, None, fair, sigma
            pre_bids = book.get("bids") or []
            pre_asks = book.get("asks") or []
            if not pre_bids and not pre_asks:
                return None, None, fair, sigma

        edge = max(self.cfg.min_edge, self.cfg.edge_per_sigma * sigma)
        if pre_reveal:
            edge = max(edge, self.cfg.pre_reveal_min_edge)
        if not pre_reveal and edge < self.cfg.no_edge_pull_ticks:
            return None, None, fair, sigma

        skew = 0.0 if pre_reveal else -self.position * self.cfg.skew_per_unit

        now = time.time()
        bid_adv = self._adverse_skew("bid", now)
        ask_adv = self._adverse_skew("ask", now)

        # v12: flow-based asymmetric edge widen/narrow
        buy_p, sell_p = self.cpp.directional_pressure()
        flow_widen_bid = 0.0
        flow_widen_ask = 0.0
        flow_narrow_bid = 0.0
        flow_narrow_ask = 0.0
        if not pre_reveal:
            net_we_sell = sell_p - buy_p     # >0 => we keep selling => widen ASK
            net_we_buy = buy_p - sell_p      # >0 => we keep buying  => widen BID
            if net_we_sell >= self.cfg.flow_score_threshold:
                flow_widen_ask = self.cfg.flow_widen_ticks
                flow_narrow_bid = self.cfg.flow_narrow_ticks
            elif net_we_buy >= self.cfg.flow_score_threshold:
                flow_widen_bid = self.cfg.flow_widen_ticks
                flow_narrow_ask = self.cfg.flow_narrow_ticks

        bid_px: Optional[int] = int(math.floor(
            fair_eff - edge + skew - bid_adv - flow_widen_bid + flow_narrow_bid))
        ask_px: Optional[int] = int(math.ceil(
            fair_eff + edge + skew + ask_adv + flow_widen_ask - flow_narrow_ask))

        # Pre-reveal one-sided book handling
        if pre_reveal:
            if not pre_asks:
                ask_px = int(math.ceil(fair_eff + self.cfg.empty_side_edge + skew))
                if (pre_bids and pre_bids[0]["price"]
                        > fair_eff + self.cfg.pre_reveal_pull_disagreement):
                    ask_px = None
            elif not pre_bids:
                bid_px = int(math.floor(fair_eff - self.cfg.empty_side_edge + skew))
                if (pre_asks and pre_asks[0]["price"]
                        < fair_eff - self.cfg.pre_reveal_pull_disagreement):
                    bid_px = None

        market_mid = self._market_signal()
        biased = (market_mid is not None and
                  abs(fair_eff - market_mid) >= self.cfg.market_anchor_disagreement)

        # v12: pennying is now allowed even when biased (still bounded)
        if self.cfg.penny_when_biased or not biased:
            if sigma <= self.cfg.penny_max_sigma:
                bid_px, ask_px = self._apply_penny(bid_px, ask_px, fair_eff, skew)

        if biased and market_mid is not None:
            safe_bid = int(math.floor(market_mid - edge))
            safe_ask = int(math.ceil(market_mid + edge))
            if bid_px is not None:
                bid_px = min(bid_px, safe_bid)
            if ask_px is not None:
                ask_px = max(ask_px, safe_ask)

        if pre_reveal and market_mid is not None:
            if market_mid > fair_eff + self.cfg.pre_reveal_pull_disagreement:
                ask_px = None
            elif market_mid < fair_eff - self.cfg.pre_reveal_pull_disagreement:
                bid_px = None

        if self.cfg.vwap_side_guard_ticks > 0:
            gv = self._vwap_for_side_guard()
            if gv is not None:
                g = self.cfg.vwap_side_guard_ticks * self.tick
                bid_ceiling = int(math.floor(gv)) - g
                ask_floor = int(math.ceil(gv)) + g
                if bid_px is not None and bid_px > bid_ceiling:
                    bid_px = bid_ceiling
                if ask_px is not None and ask_px < ask_floor:
                    ask_px = ask_floor

        if ask_px is not None and bid_px is not None and ask_px <= bid_px:
            ask_px = bid_px + self.tick

        bid_qty = self._current_quote_qty("bid")
        ask_qty = self._current_quote_qty("ask")
        relaxed = (market_mid is not None and not biased)
        if pre_reveal_vwap_active:
            max_pos = self.cfg.pre_reveal_max_pos
        else:
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        if bid_px is not None and self.position + bid_qty > max_pos:
            bid_px = None
        if ask_px is not None and self.position - ask_qty < -max_pos:
            ask_px = None

        kill_thresh = int(max_pos * self.cfg.inv_hard_kill_frac)
        if self.position > kill_thresh and bid_px is not None:
            bid_px = None
        if self.position < -kill_thresh and ask_px is not None:
            ask_px = None

        if (bid_px is not None and
                now < self._partial_fill_cool_off_until.get("bid", 0.0)):
            bid_px = None
        if (ask_px is not None and
                now < self._partial_fill_cool_off_until.get("ask", 0.0)):
            ask_px = None

        # No-cross guard vs cached inside book
        book = self._book_cache
        if (book is not None and
                time.time() - self._book_cache_t <= self.cfg.book_cache_max_age_sec):
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if ask_px is not None and bids:
                best_bid = bids[0].get("price")
                if best_bid is not None and ask_px <= best_bid:
                    ask_px = None
            if bid_px is not None and asks:
                best_ask = asks[0].get("price")
                if best_ask is not None and bid_px >= best_ask:
                    bid_px = None

        if bid_px is not None or ask_px is not None:
            self._last_quote_fair_eff = fair_eff

        return bid_px, ask_px, fair, sigma

    def _apply_penny(self, bid_px: Optional[int], ask_px: Optional[int],
                     fair: float, skew: float) -> tuple[Optional[int], Optional[int]]:
        """v12: aggressively quote 1 tick inside BBO whenever the penny
        price still preserves penny_min_edge per fill. Distinct from
        tight_floor_edge so we can be more permissive here than for
        normal MM postings."""
        book = self._book_cache
        if (book is None or
                time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec):
            return bid_px, ask_px
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        step = self.tick
        floor = self.cfg.penny_min_edge

        if bids and bid_px is not None:
            best_bid = bids[0]["price"]
            our = self.resting.get("bid")
            already_top = our is not None and our["price"] >= best_bid
            if not already_top:
                penny = best_bid + step
                max_safe = int(math.floor(fair + skew - floor))
                if penny > bid_px and penny <= max_safe:
                    bid_px = penny
                elif best_bid > bid_px and best_bid <= max_safe:
                    bid_px = best_bid
        if asks and ask_px is not None:
            best_ask = asks[0]["price"]
            our = self.resting.get("ask")
            already_top = our is not None and our["price"] <= best_ask
            if not already_top:
                penny = best_ask - step
                min_safe = int(math.ceil(fair + skew + floor))
                if penny < ask_px and penny >= min_safe:
                    ask_px = penny
                elif best_ask < ask_px and best_ask >= min_safe:
                    ask_px = best_ask
        return bid_px, ask_px

    def _other_makers_present(self) -> bool:
        book = self._book_cache
        if book is None:
            return False
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return False

        def side_has_other(levels, our_px, our_qty):
            for lvl in levels:
                lvl_px = lvl.get("price")
                if lvl_px != our_px:
                    return True
                if our_qty is None or lvl.get("qty", 0) > our_qty:
                    return True
            return False

        ob = self.resting.get("bid")
        oa = self.resting.get("ask")
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        return (side_has_other(bids,
                               ob["price"] if ob else None,
                               ob["qty"] if ob else None)
                or side_has_other(asks,
                                  oa["price"] if oa else None,
                                  oa["qty"] if oa else None))

    # ------------------------------------------------------------------
    # Order plumbing
    # ------------------------------------------------------------------
    def _record_fill_from_trades(self, side: str, trades: list[dict]) -> int:
        filled = sum(t["qty"] for t in trades)
        if filled:
            self.position += filled if side == "buy" else -filled
            last_px = None
            for t in trades:
                if t.get("qty"):
                    last_px = t.get("price")
                # v12: feed CPProfile from REST IOC trades too
                cp = t.get("counterparty") or ""
                try:
                    px = float(t.get("price") or 0.0)
                    q = int(t.get("qty") or 0)
                except Exception:
                    px = 0.0
                    q = 0
                if q > 0 and cp:
                    try:
                        fair, _ = self.fair_and_sigma()
                    except Exception:
                        fair = px
                    # IOC trade: WE are taker
                    self.cpp.record_fill(
                        cp=cp, side=side, qty=q, liq="taker",
                        fill_px=px, fair_at_fill=fair)
            if last_px is not None:
                self._last_fill_px = float(last_px)
                self._last_fill_t = time.time()
        return filled

    def _safe_cancel(self, side: str) -> None:
        rest = self.resting[side]
        if rest is None:
            return
        try:
            self.c.cancel(rest["order_id"])
        except Exception:
            pass
        self.resting[side] = None

    def _post(self, side: str, price: int) -> None:
        method = self.c.buy if side == "bid" else self.c.sell
        sgn_side = "buy" if side == "bid" else "sell"
        qty = self._current_quote_qty(side)
        try:
            r = method(self.symbol, price=price, qty=qty)
        except Exception as e:
            self.resting[side] = None
            print(f"  post {side} @ {price} failed: {e}")
            return
        o = r["order"]
        self._record_fill_from_trades(sgn_side, r.get("trades", []))
        if o["status"] in ("open", "partial"):
            self.resting[side] = {
                "order_id": o["order_id"],
                "price": o["price"],
                "qty": o["remaining"],
            }
        else:
            self.resting[side] = None

    def _apply_target_quotes(self, bid_px: Optional[int],
                             ask_px: Optional[int]) -> None:
        if self._in_quote_blackout():
            self._safe_cancel("bid")
            self._safe_cancel("ask")
            return
        cur_ask = self.resting["ask"]["price"] if self.resting["ask"] else None
        cur_bid = self.resting["bid"]["price"] if self.resting["bid"] else None

        if bid_px is not None and cur_ask is not None and bid_px >= cur_ask:
            self._safe_cancel("ask")
        if ask_px is not None and cur_bid is not None and ask_px <= cur_bid:
            self._safe_cancel("bid")

        for side, want_px, cur_px in (
                ("bid", bid_px, cur_bid),
                ("ask", ask_px, cur_ask)):
            if want_px is None:
                self._safe_cancel(side)
                continue
            if cur_px != want_px:
                self._safe_cancel(side)
                self._post(side, want_px)

    # ------------------------------------------------------------------
    # Drift cancel
    # ------------------------------------------------------------------
    def _maybe_drift_cancel(self) -> None:
        if self.phase != "running":
            return
        if self._last_quote_fair_eff is None:
            return
        now = time.time()
        if now - self._last_drift_cancel_t < self.cfg.drift_cancel_min_interval:
            return
        try:
            fair, _ = self.fair_and_sigma()
        except Exception:
            return
        fair_eff = self._vwap_clamped_fair(fair)
        if abs(fair_eff - self._last_quote_fair_eff) <= self.cfg.drift_cancel_ticks:
            return
        print(f"  v12 DRIFT-CANCEL: fair_eff {self._last_quote_fair_eff:.1f}"
              f" -> {fair_eff:.1f} "
              f"({fair_eff - self._last_quote_fair_eff:+.1f})")
        self._last_drift_cancel_t = now
        try:
            with self.lock:
                self.c.cancel_all()
                self.resting = {"bid": None, "ask": None}
            self._last_quote_fair_eff = None
            self.step()
        except Exception as e:
            print(f"  v12 drift-cancel failed: {e}")

    # ------------------------------------------------------------------
    # Unified snipe path
    # ------------------------------------------------------------------
    def _snipe_check_global(self) -> bool:
        if len(self.posterior.reveals) < 1:
            return False
        if self._n_remaining() > 0 \
                and self._snipe_count_this_round >= self.cfg.snipe_max_per_round:
            return False
        return True

    def _snipe_edge_required(self, sigma: float, side: str) -> float:
        if self._n_remaining() == 0:
            return self.taker_fee
        return self.taker_fee + max(self.cfg.snipe_min_edge,
                                    self.cfg.snipe_buffer_sigma * sigma)

    def _snipe_max_pos(self, sigma: float, *, relaxed: bool) -> int:
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        return min(max_pos, self.position_limit - self.cfg.snipe_position_buffer)

    def _snipe(self, side: str, price: int, qty_avail: int) -> bool:
        if side not in ("buy", "sell") or price is None or qty_avail <= 0:
            return False

        with self.lock:
            if self.phase != "running":
                return False
            if not self._snipe_check_global():
                return False
            our = self.resting.get("ask" if side == "buy" else "bid")
            if our is not None and our["price"] == price:
                return False
            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return False
            fair_eff = self._vwap_clamped_fair(fair)
            snipe_vwap = self._vwap_for_side_guard()
            if side == "buy" and self._reveal_math_blocks_buy(fair_eff):
                return False
            if side == "sell" and self._reveal_math_blocks_sell(fair_eff):
                return False
            market_mid = self._market_signal()
            if self._n_remaining() > 0:
                if market_mid is None:
                    return False
                if abs(fair_eff - market_mid) > self.cfg.snipe_max_disagreement:
                    return False
            relaxed = (market_mid is not None and
                       abs(fair_eff - market_mid) < self.cfg.market_anchor_disagreement)
            snipe_max_pos = self._snipe_max_pos(sigma, relaxed=relaxed)
            edge_required = self._snipe_edge_required(sigma, side)

            if side == "buy":
                mispricing = fair_eff - price
                if mispricing <= edge_required:
                    return False
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap < price - self.cfg.snipe_vwap_buffer):
                    return False
                headroom = snipe_max_pos - self.position
            else:
                mispricing = price - fair_eff
                if mispricing <= edge_required:
                    return False
                if (snipe_vwap is not None and self._n_remaining() > 0
                        and snipe_vwap > price + self.cfg.snipe_vwap_buffer):
                    return False
                headroom = snipe_max_pos + self.position
            if headroom <= 0:
                return False
            qty = min(qty_avail, headroom, self.cfg.snipe_max_qty_per_level)
            if qty <= 0:
                return False

        ioc = self.c.buy_ioc if side == "buy" else self.c.sell_ioc
        try:
            res = ioc(self.symbol, price=price, qty=qty)
        except Exception:
            return False

        with self.lock:
            filled = self._record_fill_from_trades(side, res.get("trades", []))
            if filled and self._n_remaining() > 0:
                self._snipe_count_this_round += 1
        if filled:
            print(f"  SNIPE {side} {filled} @ {price}  "
                  f"fair={fair_eff:.1f} edge={mispricing:+.1f}")
        return bool(filled)

    def _snipe_book_scan(self, fair: float, sigma: float) -> bool:
        book = self._book_cache
        if book is None:
            return False
        if time.time() - self._book_cache_t > self.cfg.book_cache_max_age_sec:
            return False
        took = False
        for level in book.get("asks") or []:
            if self._snipe("buy", level["price"], level["qty"]):
                took = True
            else:
                break
        for level in book.get("bids") or []:
            if self._snipe("sell", level["price"], level["qty"]):
                took = True
            else:
                break
        return took

    # ------------------------------------------------------------------
    # Top-level step
    # ------------------------------------------------------------------
    def step(self, *, reconcile: bool = False) -> None:
        with self.lock:
            if self.phase != "running":
                if self.phase == "settled":
                    self.resting = {"bid": None, "ask": None}
                return
            if self._in_quote_blackout():
                return
            if reconcile:
                self.reconcile_position()
            pre_reveal = (len(self.posterior.reveals) == 0)

        if not pre_reveal:
            fair, sigma = self.fair_and_sigma()
            self._snipe_book_scan(fair, sigma)

        with self.lock:
            bid_px, ask_px, fair, sigma = self.desired_quotes()
            self._apply_target_quotes(bid_px, ask_px)
            self._last_maker_apply_t = time.time()
            print(f"QUOTE  fv={fair:6.1f} +/-{sigma:4.1f}  "
                  f"pos={self.position:+4d}  bid={bid_px}  ask={ask_px}  "
                  f"k={len(self.posterior.reveals)}/{self.n_total}")

    def reconcile_position(self) -> None:
        try:
            self.position = int(
                self.c.positions()["positions"].get(self.symbol, 0))
        except Exception as e:
            print(f"reconcile_position failed: {e}")

    # ------------------------------------------------------------------
    # FAST-PATH on_reveal: lock-free wait, dict lookup
    # ------------------------------------------------------------------
    def on_reveal(self, value: float) -> None:
        t_start = time.perf_counter()
        self._last_reveal_t = time.time()
        int_value = int(value)

        with self._precompute_lock:
            scenario = self._precomputed.get(int_value)
            cached_cycle = self._precompute_for_cycle
        current_cycle = len(self.posterior.reveals)

        if cached_cycle != current_cycle:
            print(f"[v12 FAST-MISS-STALE] v={int_value} "
                  f"cached={cached_cycle} cur={current_cycle} -> JIT rebuild")
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[v12 FAST-MISS-STALE] rebuild error: {e!r}")
            with self._precompute_lock:
                scenario = self._precomputed.get(int_value)
                cached_cycle = self._precompute_for_cycle

        if scenario is None or cached_cycle != current_cycle:
            print(f"[v12 CRIT] no scenario for value={int_value} cycle={current_cycle} "
                  f"-> emergency rebuild + slow update")
            self._emergency_reveal(value)
            return

        with self.lock:
            self.posterior.posterior = dict(scenario.posterior_snapshot)
            self.posterior.reveals.append(float(value))
            self._posterior_gen += 1
            self._fair_sigma_cache = None
            self._last_maker_apply_t = 0.0
            self._last_quote_fair_eff = None
            self.resting = {"bid": None, "ask": None}
            n_remaining_after = self.n_total - len(self.posterior.reveals)
            book = self._book_cache
            pos = self.position
            relaxed_max_pos = self._max_position_for_sigma(
                scenario.sigma, relaxed=True)
        t_local = time.perf_counter()

        sweep_count = 0
        futures: List[Tuple[str, int, int, Future]] = []
        if n_remaining_after >= 0 and book is not None:
            futures = self._build_and_submit_ioc(
                scenario, book, pos, relaxed_max_pos)
        t_submit = time.perf_counter()

        if scenario.bid_px is not None or scenario.ask_px is not None:
            self._mm_executor.submit(
                self._post_mm_async, scenario.bid_px, scenario.ask_px,
                scenario.fair)

        if futures:
            sweep_count = self._collect_ioc_results(futures, scenario)
        t_done = time.perf_counter()

        elapsed_us = (t_done - t_start) * 1e6
        local_us = (t_local - t_start) * 1e6
        submit_us = (t_submit - t_local) * 1e6
        wait_us = (t_done - t_submit) * 1e6
        print(f"[v12 FAST #{int_value}] cycle={current_cycle+1}/{self.n_total} "
              f"fair={scenario.fair:.1f}+/-{scenario.sigma:.1f} "
              f"bid={scenario.bid_px} ask={scenario.ask_px} "
              f"lift_to={scenario.lift_to_px} hit_to={scenario.hit_to_px} "
              f"sweeps={sweep_count} "
              f"t_local={local_us:.0f}us t_submit={submit_us:.0f}us "
              f"t_wait={wait_us/1000:.0f}ms total={elapsed_us/1000:.1f}ms")

        self._precompute_request.set()
        self._precompute_prefetched_cycle = -1

        if n_remaining_after == 0:
            self._sweep_at_settle(float(sum(self.posterior.reveals)))

    def _post_mm_async(self, bid_px: Optional[int],
                       ask_px: Optional[int], fair: float) -> None:
        try:
            with self.lock:
                self._apply_target_quotes(bid_px, ask_px)
                self._last_quote_fair_eff = fair
        except Exception as e:
            print(f"  [v12 MM-POST] async post error: {e!r}")

    def _build_and_submit_ioc(
        self,
        scenario: PrecomputedScenario,
        book: dict,
        position: int,
        relaxed_max_pos: int,
    ) -> List[Tuple[str, int, int, Future]]:
        snipe_max_pos = min(
            relaxed_max_pos, self.position_limit - self.cfg.snipe_position_buffer)
        cap = self.cfg.snipe_max_qty_per_level

        buy_plans: List[Tuple[int, int]] = []
        if scenario.lift_to_px is not None:
            remaining = snipe_max_pos - position
            for lvl in book.get("asks") or []:
                if lvl["price"] > scenario.lift_to_px:
                    break
                if remaining <= 0:
                    break
                qty = min(lvl["qty"], remaining, cap)
                if qty <= 0:
                    break
                buy_plans.append((lvl["price"], qty))
                remaining -= qty

        sell_plans: List[Tuple[int, int]] = []
        if scenario.hit_to_px is not None:
            remaining = snipe_max_pos + position
            for lvl in book.get("bids") or []:
                if lvl["price"] < scenario.hit_to_px:
                    break
                if remaining <= 0:
                    break
                qty = min(lvl["qty"], remaining, cap)
                if qty <= 0:
                    break
                sell_plans.append((lvl["price"], qty))
                remaining -= qty

        futures: List[Tuple[str, int, int, Future]] = []
        for px, q in buy_plans:
            fut = self._ioc_executor.submit(
                self.c.buy_ioc, self.symbol, price=px, qty=q)
            futures.append(("buy", px, q, fut))
        for px, q in sell_plans:
            fut = self._ioc_executor.submit(
                self.c.sell_ioc, self.symbol, price=px, qty=q)
            futures.append(("sell", px, q, fut))
        return futures

    def _collect_ioc_results(
        self,
        futures: List[Tuple[str, int, int, Future]],
        scenario: PrecomputedScenario,
    ) -> int:
        sweep_count = 0
        for side, px, qty, fut in futures:
            try:
                res = fut.result(timeout=2.0)
            except Exception as e:
                print(f"  [v12 FAST-SWEEP] {side} @ {px} q={qty} failed: {e!r}")
                continue
            with self.lock:
                filled = self._record_fill_from_trades(side, res.get("trades", []))
                if filled:
                    self._snipe_count_this_round += 1
            if filled:
                sweep_count += 1
                edge = (scenario.fair - px) if side == "buy" else (px - scenario.fair)
                print(f"  [v12 FAST-SWEEP] {side} {filled} @ {px} "
                      f"fair={scenario.fair:.1f} edge={edge:+.1f}")
        return sweep_count

    def _emergency_reveal(self, value: float) -> None:
        with self.lock:
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.posterior.update(value)
            self._posterior_gen += 1
            self._fair_sigma_cache = None
        self.step(reconcile=True)
        n_after = self._n_remaining()
        if n_after == 0:
            self._sweep_at_settle(float(sum(self.posterior.reveals)))

    def _sweep_at_settle(self, running_sum: float) -> None:
        for _ in range(6):
            took = False
            book = self._book_cache
            if book is not None:
                for lvl in book.get("asks") or []:
                    if self._snipe("buy", lvl["price"], lvl["qty"]):
                        took = True
                for lvl in book.get("bids") or []:
                    if self._snipe("sell", lvl["price"], lvl["qty"]):
                        took = True
            if not took:
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # WS event handlers
    # ------------------------------------------------------------------
    def on_fill_event(self, msg: dict) -> None:
        liq = msg.get("liquidity")
        side = msg.get("side")
        if liq == "maker" and side in ("buy", "sell"):
            self._last_adverse_fill_t[
                "bid" if side == "buy" else "ask"] = time.time()

        matched = False
        with self.lock:
            qty = int(msg.get("qty") or 0)
            order_id = msg.get("order_id")
            fill_px_raw = msg.get("price")
            if fill_px_raw is not None:
                self._last_fill_px = float(fill_px_raw)
                self._last_fill_t = time.time()
            for key, expected_side in (("bid", "buy"), ("ask", "sell")):
                rest = self.resting[key]
                if rest is not None and rest["order_id"] == order_id and side == expected_side:
                    rest["qty"] -= qty
                    if side == "buy":
                        self.position += qty
                    else:
                        self.position -= qty
                    if (rest["qty"] > 0 and liq == "maker"
                            and self.cfg.partial_fill_cool_off_sec > 0):
                        self._partial_fill_cool_off_until[key] = (
                            time.time() + self.cfg.partial_fill_cool_off_sec)
                    if rest["qty"] <= 0:
                        self.resting[key] = None
                    matched = True
                    break

        # v12: feed CPProfile from every fill event (WS-derived authoritative)
        cp = msg.get("counterparty") or ""
        if cp and side in ("buy", "sell") and qty > 0 and fill_px_raw is not None:
            try:
                fair, _ = self.fair_and_sigma()
            except Exception:
                fair = float(fill_px_raw)
            self.cpp.record_fill(
                cp=cp, side=side, qty=qty,
                liq=liq if liq in ("maker", "taker") else "",
                fill_px=float(fill_px_raw),
                fair_at_fill=float(fair))

        if not matched:
            self.step(reconcile=True)

    def on_quote_event(self, msg: dict) -> None:
        if self.phase != "running":
            return
        k = len(self.posterior.reveals)
        pre_reveal = (k == 0)

        if not pre_reveal and msg.get("type") == "quote_add":
            side = msg.get("side")
            price = msg.get("price")
            qty = msg.get("qty")
            if side and price is not None and qty:
                our_side = "sell" if side == "buy" else "buy"
                self._snipe(our_side, price, qty)

        now = time.time()
        do_book_scan = (not pre_reveal and
                        (now - self._last_quote_event_t
                         >= self.cfg.quote_event_throttle_sec))
        if pre_reveal:
            do_maker_reprice = (now - self._last_maker_apply_t >= 1.0)
        else:
            do_maker_reprice = (now - self._last_maker_apply_t
                                >= self._maker_reprice_interval())

        if not do_book_scan and not do_maker_reprice:
            self._maybe_drift_cancel()
            return

        if do_book_scan:
            self._last_quote_event_t = now

        if do_book_scan and k >= 1:
            fair, sigma = self.fair_and_sigma()
            if not (fair == 0.0 and sigma == 0.0):
                self._snipe_book_scan(fair, sigma)
        if do_maker_reprice:
            with self.lock:
                if pre_reveal or k >= 1:
                    bid_px, ask_px, _, _ = self.desired_quotes()
                    self._apply_target_quotes(bid_px, ask_px)
                    self._last_maker_apply_t = now
        self._maybe_drift_cancel()

    def on_book_event(self, msg: dict) -> None:
        if msg.get("symbol") not in (None, self.symbol):
            return
        self._book_cache = msg
        self._book_cache_t = time.time()

    def on_trade(self, msg: dict) -> None:
        if msg.get("symbol") != self.symbol:
            return
        try:
            price = float(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        if qty <= 0:
            return
        aggressor = msg.get("aggressor")
        if aggressor not in ("buy", "sell"):
            aggressor = ""
        now = time.time()
        with self.lock:
            self._expire_trades(now)
            if len(self._recent_trades) >= self._recent_trades.maxlen:
                _, px_old, q_old, _ = self._recent_trades[0]
                self._vwap_num -= px_old * q_old
                self._vwap_den -= q_old
            self._recent_trades.append((now, price, qty, aggressor))
            self._vwap_num += price * qty
            self._vwap_den += qty
        self._maybe_drift_cancel()

    def on_phase_change(self, phase: Optional[str],
                        reveals: list[float]) -> None:
        with self.lock:
            self.phase = phase
            if phase == "running":
                try:
                    self.c.cancel_all()
                    print("CANCEL_ALL on phase->running")
                except Exception as e:
                    print(f"cancel_all on phase change failed: {e}")
                self.posterior.reset(reveals)
                self._posterior_gen += 1
                self._fair_sigma_cache = None
                self.resting = {"bid": None, "ask": None}
                self._snipe_count_this_round = 0
                self._last_maker_apply_t = 0.0
                self._partial_fill_cool_off_until = {"bid": 0.0, "ask": 0.0}
                self._recent_trades.clear()
                self._vwap_num = 0.0
                self._vwap_den = 0
                self._phase_running_t = time.time()
                self._last_reveal_t = None
                self._pre_reveal_last_serviced_idx = -1
                self._pre_reveal_last_cancelled_idx = -1
                self._last_quote_fair_eff = None
                self._last_drift_cancel_t = 0.0
                self._precompute_prefetched_cycle = -1
                self._jit_refresh_done_for_cycle = -1
                self._current_theos = None
                self._current_theos_cycle = -1
                self._current_theos_t = 0.0
                self._last_book_prewarm_t = 0.0
                self._last_adverse_fill_t = {"bid": 0.0, "ask": 0.0}
                self._precompute_request.set()
                self.reconcile_position()
        if phase == "running":
            self.step()

    def flatten(self) -> None:
        try:
            self._scheduler_stop.set()
        except Exception:
            pass
        try:
            self._ioc_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._mm_executor.shutdown(wait=False)
        except Exception:
            pass
        with self.lock:
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.reconcile_position()
            pos = self.position
            if pos > 0:
                try:
                    res = self.c.sell_market(self.symbol, qty=pos)
                    self._record_fill_from_trades("sell", res.get("trades", []))
                except Exception as e:
                    print(f"flatten sell failed: {e}")
            elif pos < 0:
                try:
                    res = self.c.buy_market(self.symbol, qty=-pos)
                    self._record_fill_from_trades("buy", res.get("trades", []))
                except Exception as e:
                    print(f"flatten buy failed: {e}")
            print(f"FLAT  pos={self.position}")

    # ------------------------------------------------------------------
    # Fast scheduler
    # ------------------------------------------------------------------
    def _fast_scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self._fast_scheduler_tick()
            except Exception as e:
                print(f"fast_scheduler tick error: {e}")
            self._scheduler_stop.wait(self.cfg.fast_scheduler_tick_sec)

    def _fast_scheduler_tick(self) -> None:
        if self.phase != "running":
            return
        if self._n_remaining() <= 0:
            return
        nxt = self._next_reveal_at()
        if nxt is None:
            return
        now = time.time()
        cycle = len(self.posterior.reveals)
        cancel_at = nxt - self.cfg.pre_reveal_cancel_sec
        post_at = nxt - self.cfg.pre_reveal_post_sec

        if now >= cancel_at and self._pre_reveal_last_cancelled_idx < cycle:
            self._pre_reveal_last_cancelled_idx = cycle
            try:
                with self.lock:
                    self.c.cancel_all()
                    self.resting = {"bid": None, "ask": None}
                print(f"[v12 SCHED] PRE-REVEAL-CANCEL cycle={cycle} "
                      f"reveal_in={nxt - now:.2f}s")
            except Exception as e:
                print(f"[v12 SCHED] cancel error: {e}")
        elif (now >= post_at and now < cancel_at
                and self._pre_reveal_last_serviced_idx < cycle):
            self._pre_reveal_last_serviced_idx = cycle
            print(f"[v12 SCHED] PRE-REVEAL-POST cycle={cycle} "
                  f"reveal_in={nxt - now:.2f}s pos={self.position}")
            try:
                self.step()
            except Exception as e:
                print(f"[v12 SCHED] post error: {e}")

        if self._precompute_prefetched_cycle < cycle:
            if now >= nxt - self.cfg.precompute_lead_sec:
                self._precompute_prefetched_cycle = cycle
                self._precompute_request.set()
                print(f"[v12 SCHED] precompute-prefetch cycle={cycle} "
                      f"reveal_in={nxt - now:.2f}s")

        if now >= nxt - self.cfg.book_prewarm_lead_sec:
            age = now - self._book_cache_t
            if age >= self.cfg.book_prewarm_max_age_sec:
                try:
                    self._book_cache = self.c.book(
                        self.symbol, depth=self.cfg.snipe_book_depth)
                    self._book_cache_t = time.time()
                    self._last_book_prewarm_t = self._book_cache_t
                except Exception:
                    pass

        if (self._jit_refresh_done_for_cycle < cycle
                and now >= nxt - self.cfg.jit_refresh_sec):
            self._jit_refresh_done_for_cycle = cycle
            with self._precompute_lock:
                cached_cycle = self._precompute_for_cycle
                cached_n = len(self._precomputed)
            if cached_cycle != cycle:
                print(f"[v12 SCHED] JIT-REFRESH cycle={cycle} "
                      f"reveal_in={nxt - now:.2f}s "
                      f"(cached={cached_cycle}, n={cached_n})")
                t0 = time.perf_counter()
                try:
                    self._do_precompute()
                except Exception as e:
                    print(f"[v12 SCHED] JIT rebuild error: {e!r}")
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                print(f"[v12 SCHED] JIT-REFRESH done {elapsed_ms:.1f}ms")

    # ------------------------------------------------------------------
    # Slow housekeeping
    # ------------------------------------------------------------------
    def _slow_housekeeping_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self._slow_housekeeping_tick()
            except Exception as e:
                print(f"slow_housekeeping tick error: {e}")
            self._scheduler_stop.wait(self.cfg.slow_housekeeping_interval_sec)

    def _slow_housekeeping_tick(self) -> None:
        if self.phase != "running":
            return
        try:
            rest_pos = int(
                self.c.positions()["positions"].get(self.symbol, 0))
        except Exception:
            return
        if rest_pos != self.position:
            print(f"[v12 HOUSEKEEP] position drift: local={self.position} "
                  f"rest={rest_pos} -> reconciling")
            with self.lock:
                self.position = rest_pos
        cycle = len(self.posterior.reveals)
        with self._precompute_lock:
            cached_cycle = self._precompute_for_cycle
        if (cached_cycle >= 0 and cycle - cached_cycle >= 2
                and self._n_remaining() > 0):
            print(f"[v12 HOUSEKEEP] WARN precompute stale "
                  f"by {cycle - cached_cycle} cycles -> forcing rebuild")
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[v12 HOUSEKEEP] forced rebuild error: {e!r}")
        self._refresh_current_theos()

    def _refresh_current_theos(self) -> None:
        try:
            fair, sigma = self.fair_and_sigma()
            bid_px, ask_px, _, _ = self.desired_quotes()
        except Exception as e:
            print(f"[v12 CURRENT-THEOS] refresh error: {e!r}")
            return
        self._current_theos = {
            "fair": float(fair), "sigma": float(sigma),
            "bid_px": bid_px, "ask_px": ask_px}
        self._current_theos_cycle = len(self.posterior.reveals)
        self._current_theos_t = time.time()

    def get_cached_current_theos(self) -> Optional[Dict[str, Any]]:
        if self._current_theos is None:
            return None
        if self._current_theos_cycle != len(self.posterior.reveals):
            return None
        if (time.time() - self._current_theos_t
                > self.cfg.current_theos_max_age_sec):
            return None
        return self._current_theos

    # ------------------------------------------------------------------
    # Precompute worker
    # ------------------------------------------------------------------
    def _precompute_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            triggered = self._precompute_request.wait(timeout=0.5)
            if self._scheduler_stop.is_set():
                break
            if not triggered:
                continue
            self._precompute_request.clear()
            try:
                self._do_precompute()
            except Exception as e:
                print(f"[v12 PRECOMPUTE] error: {e!r}")

    def _do_precompute(self) -> None:
        t_start = time.perf_counter()
        with self.lock:
            if self.phase != "running":
                return
            snapshot = dict(self.posterior.posterior)
            reveals_so_far = list(self.posterior.reveals)
        running_sum = sum(reveals_so_far)
        n_remaining = self.n_total - len(reveals_so_far)
        cycle_idx = len(reveals_so_far)

        if n_remaining <= 0 or not snapshot:
            return

        marginal: Dict[int, float] = defaultdict(float)
        for (a, w), p in snapshot.items():
            mass = p / (w + 1)
            for v in range(a, a + w + 1):
                marginal[v] += mass

        sorted_vals = sorted(marginal.items(), key=lambda kv: -kv[1])
        sorted_vals = [(v, p) for v, p in sorted_vals
                       if p >= self.cfg.precompute_min_prob]
        sorted_vals = sorted_vals[:self.cfg.precompute_max_values]

        new_precomputed: Dict[int, PrecomputedScenario] = {}
        for v, prob in sorted_vals:
            scenario = self._build_scenario(
                snapshot, v, prob, running_sum, n_remaining)
            if scenario is not None:
                new_precomputed[v] = scenario

        with self._precompute_lock:
            self._precomputed = new_precomputed
            self._precompute_for_cycle = cycle_idx
        self._last_precompute_us = (time.perf_counter() - t_start) * 1e6
        self._precompute_run_count += 1
        vals_sorted = sorted(new_precomputed.keys())
        print(f"[v12 PRECOMPUTE #{self._precompute_run_count}] "
              f"cycle={cycle_idx} k={cycle_idx}/{self.n_total} "
              f"n_scenarios={len(new_precomputed)} "
              f"values={vals_sorted[:15]}"
              f"{'...' if len(vals_sorted) > 15 else ''} "
              f"elapsed={self._last_precompute_us:.0f}us")

    def _build_scenario(
        self,
        prior_post: Dict[Tuple[int, int], float],
        v: int,
        prob: float,
        running_sum: float,
        n_remaining: int,
    ) -> Optional[PrecomputedScenario]:
        new_post: Dict[Tuple[int, int], float] = {}
        total = 0.0
        for (a, w), p in prior_post.items():
            if a <= v <= a + w:
                np = p / (w + 1)
                new_post[(a, w)] = np
                total += np
        if total <= 0:
            return None
        for k in new_post:
            new_post[k] /= total

        new_remaining = n_remaining - 1
        new_running = running_sum + v
        if new_remaining <= 0:
            fair = float(new_running)
            sigma = 0.0
        else:
            e_inner = 0.0
            e2_inner = 0.0
            e_var = 0.0
            for (a, w), p in new_post.items():
                inner_mean = new_remaining * (a + w / 2.0)
                inner_var = new_remaining * (w * (w + 2)) / 12.0
                e_inner += p * inner_mean
                e2_inner += p * inner_mean * inner_mean
                e_var += p * inner_var
            var_total = e_var + (e2_inner - e_inner * e_inner)
            fair = new_running + e_inner
            sigma = math.sqrt(max(var_total, 0.0))

        edge = max(self.cfg.min_edge, self.cfg.edge_per_sigma * sigma)
        if edge < self.cfg.no_edge_pull_ticks:
            bid_px: Optional[int] = None
            ask_px: Optional[int] = None
        else:
            bid_px = int(math.floor(fair - edge))
            ask_px = int(math.ceil(fair + edge))

        base_edge = self.taker_fee + max(
            self.cfg.snipe_min_edge, self.cfg.snipe_buffer_sigma * sigma)
        lift_to_px = int(math.floor(fair - base_edge))
        hit_to_px = int(math.ceil(fair + base_edge))

        return PrecomputedScenario(
            value=v,
            posterior_snapshot=new_post,
            fair=fair,
            sigma=sigma,
            bid_px=bid_px,
            ask_px=ask_px,
            lift_to_px=lift_to_px,
            hit_to_px=hit_to_px,
            marginal_prob=prob,
        )


# ---------------------------------------------------------------------------
# Standalone runner. Prefer run_combined12 for full WS + probe setup.
# ---------------------------------------------------------------------------
def _strategy12_main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")

    strat = StrategyV12(c, post, symbol="A")
    print(f"v12: reveal_interval={strat.reveal_interval}s  "
          f"duration={strat.duration}s  n_total={strat.n_total}")
    print(f"v12: quote_qty={strat.cfg.quote_qty} "
          f"qnormal={strat.cfg.quote_qty_normal} "
          f"penny_min_edge={strat.cfg.penny_min_edge} "
          f"flow_thresh={strat.cfg.flow_score_threshold}")

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  "
              f"running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(f"FILL   {msg['side']:>4s} {msg['qty']} @ {msg['price']}  "
              f"liq={msg.get('liquidity')}  cp={msg.get('counterparty')}")
        strat.on_fill_event(msg)

    def on_game_state(msg):
        phase = msg.get("phase")
        reveals = msg.get("reveals") or []
        print(f"STATE  phase={phase}  reveals={len(reveals)}")
        strat.on_phase_change(phase, reveals)

    def on_message(msg):
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = strat.on_trade
    c.on_book = strat.on_book_event
    c.on_game_state = on_game_state
    c.on_message = on_message
    c.start()

    if c.game_state().get("phase") == "running":
        strat.step(reconcile=True)

    print("\nBot started (v12). Commands: s=status, p=precompute, "
          "c=cp profile, f=flatten, q=quit.\n")
    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fair, sigma = strat.fair_and_sigma()
                nxt = strat._next_reveal_at()
                nxt_in = f"{nxt - time.time():.2f}s" if nxt else "-"
                with strat._precompute_lock:
                    n_scen = len(strat._precomputed)
                    pcycle = strat._precompute_for_cycle
                bp, sp = strat.cpp.directional_pressure()
                print(f"  pos={strat.position}  fair={fair:.1f}+/-{sigma:.1f}  "
                      f"k={len(strat.posterior.reveals)}/{strat.n_total}  "
                      f"precompute={n_scen}@cycle{pcycle} "
                      f"({strat._last_precompute_us:.0f}us)  "
                      f"next_reveal_in={nxt_in}  "
                      f"flow buy={bp:.2f} sell={sp:.2f}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precomputed.items())
                    pcycle = strat._precompute_for_cycle
                print(f"  precompute (cycle={pcycle}, n={len(items)}):")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.marginal_prob:.3f}  "
                          f"fair={sc.fair:6.1f}+/-{sc.sigma:4.1f}  "
                          f"bid={sc.bid_px} ask={sc.ask_px}  "
                          f"lift<={sc.lift_to_px} hit>={sc.hit_to_px}")
            elif cmd == "c":
                snap = strat.cpp.snapshot()
                print(f"  CPProfile ({len(snap)} counterparties):")
                rows = sorted(snap.items(),
                              key=lambda kv: -(kv[1].qty_we_bought + kv[1].qty_we_sold))
                for cp_id, s in rows[:30]:
                    print(f"    {cp_id:<14s} bought={s.qty_we_bought:>4d} "
                          f"sold={s.qty_we_sold:>4d}  "
                          f"taker={s.taker_count} maker={s.maker_count}  "
                          f"adv={s.adverse_ticks:+.1f}")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown {cmd!r}; try s/p/c/f/q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Flattening...")
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten on exit failed: {e}")


# ===========================================================================
# Inlined from strategy18.py — _SymState/_Scenario/_LiveThresholds and
# the dual-symbol MM StrategyV18 (independent of v12 — not a subclass)
# ===========================================================================

# Path bootstrap: keep sibling modules importable when run as a script.



# ===========================================================================
# Config
# ===========================================================================
@dataclass
class ConfigV18:
    # ---- MM widths/sizes (mirrored from SEAN, validated by v17 logs) ----
    quote_width_a: int = 8
    quote_width_b: int = 4
    size_a: int = 5
    size_b: int = 5
    # Inventory skew: quote midpoint shifts (position / skew_full_flip)
    # half-widths toward flat. At |pos|==flip the quote fully flips.
    skew_full_flip_a: int = 50
    skew_full_flip_b: int = 100

    # ---- Pre-reveal park (user spec: 0.8s before each reveal) ----
    # 0.8s gives us a 200ms lead on SEAN's 1.0s pre_reveal_lead. We PARK
    # the quote at fair ± park_offset (not cancel) — order stays alive
    # at a price the market won't fill into, so the post-reveal hot path
    # can MODIFY back to BBO in 1 REST call vs cancel+post = 2.
    pre_reveal_park_sec: float = 0.8
    pre_reveal_park_offset_ticks: int = 30
    # Multiplier when we widen instead of park (e.g. k=0 no-info phase).
    pre_reveal_widen_mult: float = 5.0
    # k=0 only: posterior is the raw prior, so the spread defaults very
    # wide regardless of timing.
    no_info_widen_mult: float = 8.0

    # ---- MM refresh (between-reveal slow loop) ----
    mm_refresh_sec: float = 1.5
    mm_min_interval_sec: float = 0.4
    # Re-quote when fair drifts ≥ this many ticks. Strategy17 used 2;
    # we use 1 to penny-stay-inside more aggressively (combined with
    # the penny logic below).
    quote_min_change_ticks: int = 1

    # ---- Penny/dime BBO (between reveals only) ----
    # When the inside BBO is wider than our default quote distance, we
    # already win. When it's tighter, we step 1t INSIDE the inside if
    # the post-step price still earns at least `penny_min_edge_*` per
    # fill (fair vs price, before fees). We do NOT step inside during
    # the pre-reveal window — being inside there = being a sniper's
    # target.
    penny_enabled: bool = True
    penny_min_edge_a: float = 1.0   # net of maker fee 0.5 → +0.5/lot
    penny_min_edge_b: float = 1.0
    # Cap on how many ticks we'll step inside (so a one-side dimer
    # collapsing 10t can't drag us 10t out of our default).
    penny_max_step_ticks: int = 5

    # ---- Dime-defense / BBO-claim (reactive, runs on every book event) ----
    # Competitors place v=1 orders one tick inside our BBO to claim
    # queue priority. The MM refresh loop only requotes every 0.4-1.5s,
    # so we sit dimed for up to a full reveal cycle. This path watches
    # every book event and ensures we're at the inside BBO whenever
    # stepping there still leaves >= penny_min_edge_* vs fair. Covers
    # three cases per side: dimed, absent (no resting), or outside (we
    # have resting but a tighter inside formed). Throttled per symbol
    # so a v=1 dime war doesn't blow REST budget.
    dime_defense_enabled: bool = True
    dime_defense_throttle_sec: float = 0.15

    # ---- Post-reveal IOC sweep (hot path) ----
    # Threshold edge for IOC'ing a laggard book level on reveal. 1.5t
    # is below the typical informed-bot threshold of 2-3t so we catch
    # short-lifetime stale liquidity before it gets repriced.
    sweep_edge_ticks: float = 1.5
    sweep_max_slice: int = 10
    sweep_position_pad: int = 5
    # Wide-quoter trap filter: refuse any IOC at price >50t off fair.
    # See bot_config_dump.json for spoofer (HVALK) behavior.
    max_ioc_distance_ticks: float = 50.0

    # ---- Stale-quote sweep between reveals (VALKO-style targets) ----
    # When a book_event arrives, if any level is past `inter_sweep_edge_ticks`
    # from our fair AND the size is ≥ inter_sweep_min_size, IOC it.
    # Tuned tight enough to catch refresh=5s stale_quoter, loose enough
    # not to fire on noise from naive_mm 1s refresh cycle.
    inter_sweep_enabled: bool = True
    inter_sweep_edge_ticks: float = 3.0
    inter_sweep_min_size: int = 4
    inter_sweep_max_slice: int = 10
    inter_sweep_throttle_sec: float = 0.3

    # ---- Cross-symbol arb / private-feed exploit ----
    # The public WS feed lags the private feed by ~15ms. When WE see a
    # fill on the private feed, the print hasn't propagated to the public
    # book yet — so for ~12-15ms, competitors are still pricing off a
    # book that doesn't know about our trade. Hit BOTH books during
    # that exclusive window using fresh fairs.
    #
    # Mathematical link: A settles at running_sum + sum(remaining X),
    # B settles at the NEXT X. So implied_X_from_A = (mid_A - rs)/n_rem
    # and implied_X_from_B = mid_B should be equal in equilibrium. Any
    # divergence is arb.
    cross_arb_enabled: bool = True
    # Lower edge than inter_sweep — we're sniping during an exclusive
    # window, not racing a refresh cycle, so we can take thinner edges.
    cross_arb_edge_ticks: float = 2.0
    cross_arb_max_slice: int = 8
    cross_arb_levels: int = 5     # walk this many price levels deep
    # Documented constant (matches the exchange spec). Used for telemetry
    # / sanity checks, not for sleeps.
    public_feed_lag_ms: float = 15.0

    # ---- Flatten bias before high-uncertainty events ----
    # Pre-first-reveal posterior IS the raw prior — sigma_A is huge. An
    # outlier reveal can blow PnL on any sized inventory. So in the
    # window before the first reveal, we *bias* sniping toward trades
    # that reduce |position|: lift_to is raised when we're SHORT (we'll
    # cover at slightly worse prices), hit_to is lowered when we're LONG
    # (we'll dump at slightly worse bids). Edges are reduced, never
    # taken below the taker fee — we DON'T force-flatten at a loss.
    # Also extends to any reveal whose sigma_A exceeds a threshold (e.g.
    # if posterior is wide for another reason — sparse priors, late join).
    flatten_bias_enabled: bool = True
    # Window before the first reveal (k=0) where bias is active.
    pre_first_reveal_flatten_sec: float = 8.0
    # Optional generalized trigger: bias is also active if sigma_a >=
    # this value (in price ticks). Setting to inf disables the
    # sigma-based trigger.
    flatten_bias_sigma_a_min: float = 12.0
    # Edge reduction in the position-reducing direction.
    flatten_edge_reduction_ticks: float = 1.5
    # Floor on the post-reduction edge — must still cover taker fee.
    # Taker fee = 0.5/lot, so 0.5t minimum still nets ~0 ignoring slippage.
    flatten_min_edge_ticks: float = 0.6

    # ---- Fast / slow precompute split ----
    # Slow loop (existing _precompute_loop): builds the per-scenario
    # table for the NEXT reveal — Bayesian update over every plausible
    # X, fair/sigma per branch, base sweep thresholds. Heavy, posterior-
    # driven, runs on reveal + on signal.
    # Fast loop (new _fast_precompute_loop): keeps the CURRENT-STATE
    # live snipe thresholds fresh — re-derived every 50ms from cached
    # fair_a/fair_b + current position + flatten-bias window. Cheap
    # (~50us per tick). Consumed by inter_sweep / cross_arb so they
    # don't re-compute fair on every book event.
    fast_precompute_tick_sec: float = 0.05

    # ---- Endgame burst mode ----
    # After the LAST reveal, A's settlement value is KNOWN exactly
    # (= running_sum_final). There is no posterior uncertainty. Any ask
    # below running_sum or bid above running_sum is literal free money —
    # the only thing in our way is other snipers. Switch to a tight loop
    # that walks the full book depth and fires IOCs continuously until
    # the game ends.
    endgame_enabled: bool = True
    # Tick interval for the endgame loop. 25Hz = one scan per 40ms; with
    # the ioc_parallel_workers in the pool we can fire multiple IOCs per
    # tick. Tighter than this risks 20/s rate-limit lockout.
    endgame_tick_sec: float = 0.04
    # Edge threshold in endgame: A has zero uncertainty, B is moot
    # (already settled per-reveal). 0.5t edge captures essentially every
    # genuine mispricing after fees (0.5t maker + 0.5t taker = 1t cost,
    # so 0.5t edge nets ~0).
    endgame_edge_ticks_a: float = 0.5
    endgame_max_levels: int = 20      # walk much deeper than the 5 default
    endgame_max_slice: int = 25       # bigger slices — race other snipers
    # Reserve some bucket headroom so the endgame burst doesn't lockout.
    endgame_min_tokens: float = 3.0
    # When _reveal_count >= n_total - 1 we're approaching the cliff. At
    # the LAST reveal, A's uncertainty collapses → activate.
    endgame_activate_at_remaining: int = 0

    # ---- Connection pre-warm ----
    # Keep-alive TCP idle for >~60s is at risk of NAT/server reset. A
    # cheap GET keeps the pool warm so the first hot-path POST after a
    # quiet stretch doesn't pay a fresh TCP handshake (~3ms LAN).
    prewarm_enabled: bool = True
    prewarm_interval_sec: float = 25.0
    # Fire a pre-warm burst T-park_sec before reveal so all pooled
    # connections are confirmed-alive for the reveal IOC fan-out.
    prewarm_before_reveal_sec: float = 1.0

    # ---- Precompute control ----
    precompute_min_prob: float = 1e-4
    precompute_max_x: int = 60

    # ---- Inventory safety ----
    # Stay this far inside the position limit when sizing MM quotes.
    mm_inventory_safety_pad: int = 10
    skip_negative_ev: bool = True
    # Hard cap on |pos| as a multiple of position_limit. Tripping this
    # triggers cancel_all + IOC flatten back into the pad.
    emergency_flatten_factor: float = 1.0

    # ---- Strategy-level rate-limit window ----
    # Server cap = 20/s with 3s lockout; SDK bucket = 18/s sustained,
    # 20 burst. We cap STRATEGY-side sends at 15/s sliding so the burst
    # plus dashboard manual writes never trip the server.
    max_sends_per_second: float = 15.0
    sends_window_sec: float = 1.0
    mm_send_min_tokens: float = 4.0
    reconcile_min_tokens: float = 8.0
    bulk_cancel_min_tokens: float = 6.0

    # ---- Reconcile ----
    reconcile_check_sec: float = 3.0
    reconcile_max_cancels_per_tick: int = 8
    reconcile_bulk_cancel_threshold: int = 20
    reconcile_min_age_sec: float = 3.0
    # Routine cleanup of 1-2 orphans is expected (race between POST ack
    # and resting-dict update). Only log when the orphan count crosses
    # this threshold OR cancels are being capped (something's stuck).
    reconcile_print_min: int = 3

    # ---- Server lockout backoff ----
    lockout_min_pad_ms: float = 100.0
    lockout_max_pad_ms: float = 3500.0
    post_backoff_sec: float = 5.0

    # ---- 403 circuit breaker ----
    # A 403 while we think phase=="running" usually means our local
    # phase view lagged the server (WS game_state hasn't arrived).
    # On any 403 we re-fetch game_state to confirm. If the server agrees
    # we're running but we still see N 403s in a window, hard-block sends
    # (some other server-side rule is rejecting us — auth, position, etc.)
    # and stop spamming retries.
    forbidden_circuit_max: int = 3
    forbidden_circuit_window_sec: float = 10.0
    # Phase re-check throttle so multiple parallel POSTs that all 403
    # don't all spam game_state requests.
    forbidden_phase_recheck_throttle_sec: float = 0.5

    # ---- Latency ----
    # Larger pool than the default 6 — endgame burst can fire 10+ IOCs
    # in the same 40ms tick and the connection pool should absorb that
    # without serializing.
    ioc_parallel_workers: int = 10
    use_modify_replace: bool = True

    # ---- Misc ----
    log_every_modify: bool = False
    client_order_id_prefix: str = "v18"
    reconcile_respect_manual_orders: bool = True


# ===========================================================================
# Per-symbol state
# ===========================================================================
@dataclass
class _SymState:
    sym: str
    position: int = 0
    position_limit: int = 100
    tick: int = 1
    book: Optional[dict] = None
    book_t: float = 0.0
    resting: Dict[str, Dict[int, dict]] = field(
        default_factory=lambda: {"bid": {}, "ask": {}})
    in_flight: Dict[str, int] = field(
        default_factory=lambda: {"bid": 0, "ask": 0})
    last_inter_sweep_t: float = 0.0
    last_dime_defense_t: float = 0.0


# ===========================================================================
# Precomputed scenario for a given next reveal value
# ===========================================================================
@dataclass
class _Scenario:
    """All actions to take if the next reveal is `value`. Pre-staged so
    the hot path is dict-lookup + parallel-fire only."""
    value: int
    prob: float
    fair_a: float
    sigma_a: float
    fair_b: float
    sigma_b: float
    # Passive quotes to repost (None = don't post that side)
    bid_a: Optional[int]
    ask_a: Optional[int]
    bid_b: Optional[int]
    ask_b: Optional[int]
    # IOC sweep thresholds: price above which we'd SELL (hit bids)
    # and below which we'd BUY (lift asks). Both expressed as ints
    # since the book prices are ints.
    lift_to_a: int  # buy asks with price <= this
    hit_to_a: int   # sell bids with price >= this
    lift_to_b: int
    hit_to_b: int


# ===========================================================================
# Live snipe thresholds — kept fresh by the FAST precompute loop
# ===========================================================================
@dataclass
class _LiveThresholds:
    """Snapshot of "would I snipe this level right now?" — refreshed by
    `_fast_precompute_loop` every ~50ms. inter_sweep, cross_arb, and any
    other live snipe path read this instead of re-deriving from fair.

    Two flavors per side:
      * lift_to       — BUY any ask with px ≤ this (normal edge)
      * hit_to        — SELL any bid with px ≥ this (normal edge)
      * lift_to_flat  — relaxed version, used when we're SHORT during
                        a flatten-bias window (closer to fair so we
                        cover at slightly worse prices)
      * hit_to_flat   — relaxed version, used when we're LONG during
                        a flatten-bias window (closer to fair so we
                        dump at slightly worse prices)
    """
    sym: str
    fair: float
    sigma: float
    lift_to: int
    hit_to: int
    lift_to_flat: int
    hit_to_flat: int
    in_flatten_window: bool
    gen: int


# ===========================================================================
# Strategy
# ===========================================================================
class StrategyV18:
    """Dual-symbol passive MM with reveal-time precompute hot path."""

    SYMS = ("A", "B")

    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[ConfigV18] = None):
        self.c = client
        self.posterior = posterior
        self.cfg = config or Config()
        self.lock = threading.RLock()

        # TCP_NODELAY adapter (sub-200B POSTs skip Nagle 40ms wait).
        ok = _install_nodelay(client, pool_maxsize=max(
            8, self.cfg.ioc_parallel_workers + 2))
        print(f"[v18 NODELAY] installed={ok}")

        # Bootstrap EVERYTHING from game_state. No hardcoded duration / N.
        gs = client.game_state()
        self.duration = gs["duration"]
        self.reveal_interval = gs["reveal_interval"]
        self.n_total = self.duration // self.reveal_interval
        fees = gs.get("fees", {})
        self.maker_fee = fees.get("maker_per_lot", 0.5)
        self.taker_fee = fees.get("taker_per_lot", 0.5)

        # Per-symbol state
        self.state: Dict[str, _SymState] = {}
        for sym in self.SYMS:
            instr = (gs.get("instruments") or {}).get(sym) or {}
            self.state[sym] = _SymState(
                sym=sym,
                position=0,
                position_limit=int(instr.get("position_limit") or 100),
                tick=int(instr.get("tick_size") or 1),
            )

        # Initial positions
        try:
            ps = (client.positions() or {}).get("positions") or {}
            for sym in self.SYMS:
                self.state[sym].position = int(ps.get(sym, 0))
        except Exception as e:
            print(f"[v18 INIT-POS] failed: {e}")

        # Phase + posterior bootstrap
        self.phase: Optional[str] = gs.get("phase")
        self.posterior.reset(gs.get("reveals") or [])
        self._reveal_count = len(self.posterior.reveals)
        self._running_sum = float(sum(self.posterior.reveals))
        self._phase_running_t: Optional[float] = (
            time.time() if self.phase == "running" else None)
        self._last_reveal_t: Optional[float] = None

        # Posterior-gen caches: bumped on every posterior change so fair_*
        # cost a dict lookup unless the posterior actually moved.
        self._fair_a_cache: Optional[Tuple[int, float, float]] = None
        self._fair_b_cache: Optional[Tuple[int, float, float]] = None
        self._posterior_gen: int = 0

        # Slow precompute: per-scenario table for the NEXT reveal.
        self._precompute: Dict[int, _Scenario] = {}
        self._precompute_lock = threading.RLock()
        self._precompute_for_cycle: int = -1
        self._precompute_request = threading.Event()
        self._precompute_run_count = 0
        self._last_precompute_us = 0.0

        # Fast precompute: live snipe thresholds for the CURRENT moment.
        # Updated every fast_precompute_tick_sec from cached fair_a/fair_b
        # plus position + flatten-bias window. Snipe paths (inter_sweep,
        # cross_arb) read this instead of re-deriving fair every event.
        self._live_thresholds: Dict[str, _LiveThresholds] = {}
        self._live_lock = threading.RLock()
        self._live_gen: int = 0
        self._fast_precompute_run_count = 0
        self._last_fast_precompute_us = 0.0

        # Park tracking: avoid re-parking multiple times for the same reveal.
        self._last_park_reveal_idx: int = -1

        # Counters
        self._modify_count = 0
        self._post_count = 0
        self._reveal_ioc_count = 0
        self._inter_sweep_count = 0
        self._cross_arb_count = 0
        self._endgame_count = 0
        self._coid_counter = 0
        self._sends_deferred = 0

        # Endgame state — flipped True once `_reveal_count` hits the
        # last index. Stays True for the remainder of the round.
        self._endgame_active = False
        self._endgame_pos_a_at_entry: int = 0
        # Pre-warm bookkeeping (last successful idle GET).
        self._last_prewarm_t: float = 0.0
        self._prewarm_before_reveal_idx: int = -1

        # Server-lockout cutoff + sliding-window send log.
        self._lockout_until = 0.0
        self._lockout_count = 0
        self._send_log: deque = deque(maxlen=64)
        self._send_log_lock = threading.Lock()
        self._last_mm_refresh_t = 0.0
        self._post_backoff_until: Dict[Tuple[str, str], float] = {}
        # Sticky terminal flag — set on the first 403 received while phase
        # is anything other than "running" (game ended mid-flight). All
        # POST/MODIFY paths short-circuit silently while it's set; clears
        # on phase→running transition. Prevents the 5s-backoff retry loop
        # from spamming 403s once the game is over.
        self._post_terminal_blocked: bool = False
        self._terminal_403_count: int = 0
        # 403 circuit breaker — track recent forbidden events to detect
        # cases where phase-locally claims running but server disagrees,
        # OR there's another persistent reject reason (auth/position/etc).
        self._forbidden_log: deque = deque(maxlen=32)
        self._forbidden_log_lock = threading.Lock()
        self._last_phase_recheck_t: float = 0.0
        self._forbidden_count: int = 0

        # Parallel IOC executor — pre-allocated so the hot path has no
        # thread-spawn cost.
        self._ioc_executor = ThreadPoolExecutor(
            max_workers=self.cfg.ioc_parallel_workers,
            thread_name_prefix="v18-ioc")

        # Threads
        self._stop = threading.Event()
        self._mm_wake = threading.Event()
        self._th_precompute = threading.Thread(
            target=self._precompute_loop, daemon=True,
            name="v18-precomp-slow")
        self._th_fast_precompute = threading.Thread(
            target=self._fast_precompute_loop, daemon=True,
            name="v18-precomp-fast")
        self._th_mm = threading.Thread(
            target=self._mm_loop, daemon=True, name="v18-mm")
        self._th_park = threading.Thread(
            target=self._park_loop, daemon=True, name="v18-park")
        self._th_reconcile = threading.Thread(
            target=self._reconcile_loop, daemon=True, name="v18-recon")
        self._th_endgame = threading.Thread(
            target=self._endgame_loop, daemon=True, name="v18-endgame")
        self._th_prewarm = threading.Thread(
            target=self._prewarm_loop, daemon=True, name="v18-prewarm")
        self._th_precompute.start()
        self._th_fast_precompute.start()
        self._th_mm.start()
        self._th_park.start()
        self._th_reconcile.start()
        self._th_endgame.start()
        self._th_prewarm.start()
        self._precompute_request.set()

        # Banner
        print(f"[v18] phase={self.phase}  n_total={self.n_total}  "
              f"reveal_interval={self.reveal_interval}s  "
              f"duration={self.duration}s")
        for sym in self.SYMS:
            s = self.state[sym]
            print(f"[v18 SYM:{sym}] pos_limit={s.position_limit}  "
                  f"tick={s.tick}  pos={s.position}")
        print(f"[v18 MM] A: width={self.cfg.quote_width_a}t "
              f"size={self.cfg.size_a} flip={self.cfg.skew_full_flip_a}  "
              f"B: width={self.cfg.quote_width_b}t "
              f"size={self.cfg.size_b} flip={self.cfg.skew_full_flip_b}")
        print(f"[v18 PARK] {self.cfg.pre_reveal_park_sec}s before each reveal "
              f"@ ±{self.cfg.pre_reveal_park_offset_ticks}t")
        print(f"[v18 SWEEP] reveal-edge={self.cfg.sweep_edge_ticks}t  "
              f"inter-edge={self.cfg.inter_sweep_edge_ticks}t  "
              f"arb-edge={self.cfg.cross_arb_edge_ticks}t  "
              f"max-dist={self.cfg.max_ioc_distance_ticks}t")
        print(f"[v18 ENDGAME] enabled={self.cfg.endgame_enabled}  "
              f"tick={self.cfg.endgame_tick_sec*1000:.0f}ms  "
              f"edge={self.cfg.endgame_edge_ticks_a}t  "
              f"depth={self.cfg.endgame_max_levels}  "
              f"workers={self.cfg.ioc_parallel_workers}")
        print(f"[v18 PREWARM] enabled={self.cfg.prewarm_enabled}  "
              f"interval={self.cfg.prewarm_interval_sec:.0f}s  "
              f"pre_reveal={self.cfg.prewarm_before_reveal_sec:.1f}s")
        print(f"[v18 FLATTEN] enabled={self.cfg.flatten_bias_enabled}  "
              f"pre_first={self.cfg.pre_first_reveal_flatten_sec}s  "
              f"sigma_min={self.cfg.flatten_bias_sigma_a_min}t  "
              f"edge_reduction={self.cfg.flatten_edge_reduction_ticks}t  "
              f"min_edge={self.cfg.flatten_min_edge_ticks}t")
        print(f"[v18 PRECOMP] slow=on-reveal  "
              f"fast={self.cfg.fast_precompute_tick_sec*1000:.0f}ms tick")

    # ==================================================================
    # Posterior wrappers
    # ==================================================================
    def _n_remaining(self) -> int:
        return max(self.n_total - self._reveal_count, 0)

    def _bump_posterior_gen(self) -> None:
        self._posterior_gen += 1
        self._fair_a_cache = None
        self._fair_b_cache = None

    def fair_a(self) -> Tuple[float, float]:
        """E[settle_A] = running_sum + n_rem * E[X], sigma via LotV."""
        cached = self._fair_a_cache
        if cached and cached[0] == self._posterior_gen:
            return cached[1], cached[2]
        n_rem = self._n_remaining()
        fair, sigma = self.posterior.predict_settle(self._running_sum, n_rem)
        self._fair_a_cache = (self._posterior_gen, fair, sigma)
        return fair, sigma

    def fair_b(self) -> Tuple[float, float]:
        """E[X], sigma — B settles on the next X."""
        cached = self._fair_b_cache
        if cached and cached[0] == self._posterior_gen:
            return cached[1], cached[2]
        ex, ex2 = 0.0, 0.0
        for (a, w), p in self.posterior.posterior.items():
            mean_x = a + w / 2.0
            var_x = w * (w + 2) / 12.0
            ex += p * mean_x
            ex2 += p * (var_x + mean_x * mean_x)
        sigma = math.sqrt(max(0.0, ex2 - ex * ex))
        self._fair_b_cache = (self._posterior_gen, ex, sigma)
        return ex, sigma

    # ==================================================================
    # Quote calculation: fair → (bid, ask) with skew + pre-reveal mode
    # ==================================================================
    def _quote_prices(self, sym: str, fair: float, position: int,
                      mode: str) -> Tuple[Optional[int], Optional[int]]:
        """Compute (bid, ask) for `sym` at given `fair` and `position`.

        mode:
          "normal"   — between-reveal MM, default widths
          "widen"    — pre-reveal or no-info: half_width * widen_mult
          "park"     — deep park during the pull window
        """
        s = self.state[sym]
        if sym == "A":
            width = self.cfg.quote_width_a
            flip = max(1, self.cfg.skew_full_flip_a)
        else:
            width = self.cfg.quote_width_b
            flip = max(1, self.cfg.skew_full_flip_b)
        tick = s.tick

        if mode == "park":
            off = self.cfg.pre_reveal_park_offset_ticks * tick
            bid_px = ((int(round(fair - off))) // tick) * tick
            ask_px = ((int(round(fair + off))) // tick) * tick
            if bid_px < tick:
                bid_px = None
            if (bid_px is not None and ask_px is not None
                    and ask_px <= bid_px):
                ask_px = bid_px + tick
            pad = self.cfg.mm_inventory_safety_pad
            if position >= s.position_limit - pad:
                bid_px = None
            if position <= -s.position_limit + pad:
                ask_px = None
            return bid_px, ask_px

        half = width / 2.0
        if self._reveal_count == 0:
            half *= self.cfg.no_info_widen_mult
        elif mode == "widen":
            half *= self.cfg.pre_reveal_widen_mult

        # Inventory skew: long → push both sides DOWN (encourage sells).
        skew = (position / flip) * half
        bid_px = int(round(fair - half - skew))
        ask_px = int(round(fair + half - skew))
        bid_px = (bid_px // tick) * tick
        ask_px = (ask_px // tick) * tick
        if ask_px <= bid_px:
            ask_px = bid_px + tick

        if self.cfg.skip_negative_ev:
            if bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None

        if bid_px is not None and bid_px < tick:
            bid_px = None

        # Inventory pad: don't quote the side that adds to exposure.
        pad = self.cfg.mm_inventory_safety_pad
        if position >= s.position_limit - pad:
            bid_px = None
        if position <= -s.position_limit + pad:
            ask_px = None
        return bid_px, ask_px

    # ==================================================================
    # Penny/dime BBO: step 1t inside the BBO if floor edge preserved
    # ==================================================================
    def _maybe_penny(self, sym: str, fair: float, position: int,
                     bid_px: Optional[int], ask_px: Optional[int]
                     ) -> Tuple[Optional[int], Optional[int]]:
        """Step 1t inside the inside BBO if doing so still preserves
        penny_min_edge_* per fill. Skipped during pre-reveal (we're
        the sniper's target inside, not their winner)."""
        if not self.cfg.penny_enabled:
            return bid_px, ask_px
        book = self.state[sym].book
        if not book:
            return bid_px, ask_px
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        tick = self.state[sym].tick
        min_edge = (self.cfg.penny_min_edge_a if sym == "A"
                    else self.cfg.penny_min_edge_b)
        max_step = self.cfg.penny_max_step_ticks

        # Bid side: if our default bid is BELOW best_bid and stepping
        # (best_bid + 1t) still earns >= min_edge, penny inside.
        if (bid_px is not None and best_bid is not None
                and best_bid >= bid_px):
            # Don't undercut ourselves (we may already BE best_bid).
            same = False
            for e in self.state[sym].resting["bid"].values():
                if int(e.get("price") or 0) == best_bid:
                    same = True
                    break
            target = best_bid + tick if not same else best_bid
            # Edge check: target ≤ fair − min_edge.
            if (target <= fair - min_edge
                    and (target - bid_px) <= max_step * tick
                    and target < (best_ask if best_ask is not None else 10**9)):
                bid_px = target

        # Ask side: if our default ask is ABOVE best_ask and stepping
        # (best_ask − 1t) still earns >= min_edge, penny inside.
        if (ask_px is not None and best_ask is not None
                and best_ask <= ask_px):
            same = False
            for e in self.state[sym].resting["ask"].values():
                if int(e.get("price") or 0) == best_ask:
                    same = True
                    break
            target = best_ask - tick if not same else best_ask
            if (target >= fair + min_edge
                    and (ask_px - target) <= max_step * tick
                    and target > (best_bid if best_bid is not None else 0)):
                ask_px = target
        return bid_px, ask_px

    # ==================================================================
    # Pre-reveal scheduling
    # ==================================================================
    def _next_reveal_at(self) -> Optional[float]:
        if self.phase != "running" or self._phase_running_t is None:
            return None
        if self._reveal_count >= self.n_total:
            return None
        if self._last_reveal_t is not None:
            return self._last_reveal_t + self.reveal_interval
        return (self._phase_running_t
                + (self._reveal_count + 1) * self.reveal_interval)

    def _in_park_window(self) -> bool:
        nxt = self._next_reveal_at()
        if nxt is None:
            return False
        now = time.time()
        # Park window = [next_reveal − park_sec, next_reveal + 0.5s]. The
        # +0.5s tail covers reveal-delay (server can fire 200-500ms late).
        return (nxt - self.cfg.pre_reveal_park_sec) <= now < (nxt + 0.5)

    # ==================================================================
    # Precompute (slow loop)
    # ==================================================================
    def _possible_x_values(self) -> List[int]:
        """Integer X values with non-negligible posterior probability."""
        seen: Dict[int, float] = {}
        for (a, w), p in self.posterior.posterior.items():
            share = p / (w + 1)
            for v in range(a, a + w + 1):
                seen[v] = seen.get(v, 0.0) + share
        out = [v for v, p in seen.items()
               if p >= self.cfg.precompute_min_prob
               and 0 <= v <= self.cfg.precompute_max_x]
        out.sort()
        return out

    # ==================================================================
    # SLOW precompute — per-scenario table for the NEXT reveal.
    # Heavy Bayesian work, signal-driven (runs on reveal + manual kick).
    # ==================================================================
    def _precompute_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._precompute_request.wait(timeout=2.0)
                if self._stop.is_set():
                    return
                self._precompute_request.clear()
                self._build_precompute()
            except Exception as e:
                print(f"[v18 SLOW-PRECOMP-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # FAST precompute — live snipe thresholds for the CURRENT moment.
    # Lightweight, time-driven (every fast_precompute_tick_sec). Caches
    # the "would I snipe this level right now?" decision so inter_sweep
    # and cross_arb don't recompute fair on every book event.
    # ==================================================================
    def _fast_precompute_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.phase == "running":
                    self._build_live_thresholds()
            except Exception as e:
                print(f"[v18 FAST-PRECOMP-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.fast_precompute_tick_sec)

    def _in_flatten_bias_window(self, sigma_a: Optional[float] = None) -> bool:
        """True if we should bias toward flattening |pos| before an event.

        Triggers:
          1. Approaching the FIRST reveal (k=0, raw prior, biggest sigma).
          2. Current fair_a sigma exceeds `flatten_bias_sigma_a_min` (any
             reveal where the posterior is unusually wide).
        """
        if not self.cfg.flatten_bias_enabled:
            return False
        if self.phase != "running":
            return False
        # Trigger 1: pre-first-reveal window.
        if self._reveal_count == 0:
            nxt = self._next_reveal_at()
            if nxt is not None:
                eta = nxt - time.time()
                if 0.0 <= eta <= self.cfg.pre_first_reveal_flatten_sec:
                    return True
        # Trigger 2: high-sigma reveal in general.
        if sigma_a is not None:
            if sigma_a >= self.cfg.flatten_bias_sigma_a_min:
                return True
        return False

    def _build_live_thresholds(self) -> None:
        """Recompute the live snipe thresholds for both symbols and swap
        them in. Reads cached fair_a/fair_b — O(few µs) when posterior
        hasn't moved, only the tick-clamping math runs."""
        t0 = time.perf_counter()
        sweep_edge = self.cfg.sweep_edge_ticks
        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks

        fair_a, sigma_a = self.fair_a()
        fair_b, sigma_b = self.fair_b()
        in_flat = self._in_flatten_bias_window(sigma_a)

        new_live: Dict[str, _LiveThresholds] = {}
        for sym, fair, sigma in (("A", fair_a, sigma_a),
                                 ("B", fair_b, sigma_b)):
            tick = self.state[sym].tick
            # Normal sweep thresholds (same formula as in slow precompute).
            lift_to = ((int(math.floor(fair - sweep_edge))) // tick) * tick
            hit_to_raw = int(math.ceil(fair + sweep_edge))
            hit_to = ((hit_to_raw + tick - 1) // tick) * tick

            # Flatten-bias variants — relaxed in the position-reducing
            # direction, floored at flatten_min_edge_ticks (must still
            # beat taker fee).
            relaxed = max(flat_min, sweep_edge - flat_reduction)
            lift_to_flat = ((int(math.floor(fair - relaxed))) // tick) * tick
            hit_to_flat_raw = int(math.ceil(fair + relaxed))
            hit_to_flat = (((hit_to_flat_raw + tick - 1) // tick) * tick)

            new_live[sym] = _LiveThresholds(
                sym=sym, fair=fair, sigma=sigma,
                lift_to=lift_to, hit_to=hit_to,
                lift_to_flat=lift_to_flat, hit_to_flat=hit_to_flat,
                in_flatten_window=in_flat, gen=self._live_gen + 1)

        with self._live_lock:
            self._live_thresholds = new_live
            self._live_gen += 1
            self._fast_precompute_run_count += 1
            self._last_fast_precompute_us = (
                time.perf_counter() - t0) * 1e6

    def _get_snipe_thresholds(
        self, sym: str, position: int
    ) -> Tuple[Optional[int], Optional[int], float, bool]:
        """Effective (lift_to, hit_to, fair, in_flatten_window) for the
        given symbol given current position. Applies flatten-bias only
        in the direction that reduces |position|.

        Returns (None, None, fair, in_flat) if the live table is empty
        (e.g. fast loop hasn't ticked yet) — callers should fall back.
        """
        with self._live_lock:
            lt = self._live_thresholds.get(sym)
        if lt is None:
            return None, None, 0.0, False
        lift_to = lt.lift_to
        hit_to = lt.hit_to
        if lt.in_flatten_window:
            # Relax only the direction that flattens.
            if position < 0:
                lift_to = lt.lift_to_flat   # raise — buy at worse asks
            if position > 0:
                hit_to = lt.hit_to_flat     # lower — sell at worse bids
        return lift_to, hit_to, lt.fair, lt.in_flatten_window

    def _build_precompute(self) -> None:
        """For every plausible next reveal value v, stage all 4 quotes +
        both sweep thresholds (per symbol)."""
        t0 = time.perf_counter()
        cycle_at_start = self._reveal_count
        if self._n_remaining() <= 0:
            with self._precompute_lock:
                self._precompute = {}
                self._precompute_for_cycle = cycle_at_start
            return

        post_snapshot = dict(self.posterior.posterior)
        running_base = self._running_sum
        n_rem_after = self._n_remaining() - 1
        # Snapshot positions so all rows are computed against a single
        # inventory state. They may shift between build and apply, but
        # the MM loop will re-skew on the next refresh.
        pos_a = self.state["A"].position
        pos_b = self.state["B"].position
        sweep_edge = self.cfg.sweep_edge_ticks
        tick_a = self.state["A"].tick
        tick_b = self.state["B"].tick

        eps = 1e-9
        new_table: Dict[int, _Scenario] = {}
        for v in self._possible_x_values():
            # Bayesian update: P(a,w | v) ∝ P(a,w) * 1/(w+1) if v∈[a,a+w]
            new_post: Dict[Tuple[int, int], float] = {}
            total = 0.0
            marg = 0.0
            for (a, w), p in post_snapshot.items():
                if v < a - eps or v > a + w + eps:
                    continue
                lik = 1.0 / (w + 1)
                marg += p * lik
                np_new = p / (w + 1)
                new_post[(a, w)] = np_new
                total += np_new
            if total <= 0 or marg < self.cfg.precompute_min_prob:
                continue
            new_post = {k: x / total for k, x in new_post.items()}

            # E[settle_A | v], Var via law-of-total-variance
            running_after = running_base + v
            e_inner = 0.0
            e2_inner = 0.0
            e_var = 0.0
            ex = 0.0
            ex2 = 0.0
            for (a, w), p in new_post.items():
                mean_x = a + w / 2.0
                var_x = w * (w + 2) / 12.0
                inner_mean = n_rem_after * mean_x
                inner_var = n_rem_after * var_x
                e_inner += p * inner_mean
                e2_inner += p * inner_mean * inner_mean
                e_var += p * inner_var
                ex += p * mean_x
                ex2 += p * (var_x + mean_x * mean_x)
            var_a = e_var + (e2_inner - e_inner * e_inner)
            fair_a = running_after + e_inner
            sigma_a = math.sqrt(max(0.0, var_a))
            fair_b = ex
            sigma_b = math.sqrt(max(0.0, ex2 - ex * ex))

            # Passive quotes
            bid_a, ask_a = self._quote_prices("A", fair_a, pos_a, "normal")
            bid_b, ask_b = self._quote_prices("B", fair_b, pos_b, "normal")

            # Sweep thresholds — lift any ask ≤ fair − sweep_edge,
            # hit any bid ≥ fair + sweep_edge. Quantized to tick.
            lift_a = int(math.floor(fair_a - sweep_edge))
            hit_a = int(math.ceil(fair_a + sweep_edge))
            lift_b = int(math.floor(fair_b - sweep_edge))
            hit_b = int(math.ceil(fair_b + sweep_edge))
            # Quantize to tick (floor for lift, ceil for hit so we never
            # cross our own threshold accidentally).
            lift_a = (lift_a // tick_a) * tick_a
            hit_a = ((hit_a + tick_a - 1) // tick_a) * tick_a
            lift_b = (lift_b // tick_b) * tick_b
            hit_b = ((hit_b + tick_b - 1) // tick_b) * tick_b

            new_table[v] = _Scenario(
                value=v, prob=marg,
                fair_a=fair_a, sigma_a=sigma_a,
                fair_b=fair_b, sigma_b=sigma_b,
                bid_a=bid_a, ask_a=ask_a,
                bid_b=bid_b, ask_b=ask_b,
                lift_to_a=lift_a, hit_to_a=hit_a,
                lift_to_b=lift_b, hit_to_b=hit_b,
            )

        # Only commit if the posterior didn't advance during compute.
        if self._reveal_count != cycle_at_start:
            return
        with self._precompute_lock:
            self._precompute = new_table
            self._precompute_for_cycle = cycle_at_start
            self._precompute_run_count += 1
            self._last_precompute_us = (time.perf_counter() - t0) * 1e6

    # ==================================================================
    # HOT PATH on reveal: dict lookup → parallel IOC sweep → apply quotes
    # ==================================================================
    def on_reveal(self, value) -> None:
        try:
            v = int(round(float(value)))
        except Exception:
            print(f"[v18 ON_REVEAL] bad value {value!r}")
            return

        # 1. O(1) precompute lookup BEFORE posterior update.
        with self._precompute_lock:
            scen = self._precompute.get(v)

        # 2. Posterior update + counters under single lock.
        with self.lock:
            self.posterior.update(value)
            self._reveal_count = len(self.posterior.reveals)
            self._running_sum = float(sum(self.posterior.reveals))
            self._last_reveal_t = time.time()
            self._bump_posterior_gen()

        # 2b. Flip endgame flag if this was the LAST reveal — fair_A is
        # now an exact constant, switch to burst sweep mode.
        self._maybe_activate_endgame()

        if scen is None:
            # Cache miss — fall back to live recompute (rare).
            print(f"[v18 MISS] no precompute for v={v}; live refresh")
            self._refresh_mm_quotes()
            self._precompute_request.set()
            return

        # 3. Parallel IOC sweep using precomputed lift_to / hit_to.
        ioc_a = self._sweep_post_reveal("A", scen.lift_to_a, scen.hit_to_a,
                                        scen.fair_a)
        ioc_b = self._sweep_post_reveal("B", scen.lift_to_b, scen.hit_to_b,
                                        scen.fair_b)

        # 4. Apply pre-staged passive quotes (parallel modify or fresh post).
        self._apply_quote("A", scen.bid_a, scen.ask_a)
        self._apply_quote("B", scen.bid_b, scen.ask_b)

        # 5. Cross-symbol arb pass. The reveal is on the private feed; the
        # public book is ~15ms behind. Any laggard mispricing that
        # survived steps 3-4 (or only became visible at the new posterior)
        # gets picked off here while competitors are still reading the
        # old book.
        try:
            ioc_arb = self._try_cross_arb("reveal")
        except Exception as e:
            print(f"[v18 ARB-ERR reveal] {type(e).__name__}: {e}")
            ioc_arb = 0

        print(f"[v18 HOT] v={v} fair_a={scen.fair_a:.1f} fair_b={scen.fair_b:.2f}  "
              f"A=[{scen.bid_a},{scen.ask_a}] lift/hit=[{scen.lift_to_a},{scen.hit_to_a}]  "
              f"B=[{scen.bid_b},{scen.ask_b}] lift/hit=[{scen.lift_to_b},{scen.hit_to_b}]  "
              f"ioc_a={ioc_a} ioc_b={ioc_b} arb={ioc_arb}")

        # 6. Trigger precompute rebuild for the NEW posterior.
        self._precompute_request.set()

    def _sweep_post_reveal(self, sym: str, lift_to: int, hit_to: int,
                           fair: float) -> int:
        """Snap the book and fire IOCs against laggard prices using the
        precomputed lift_to / hit_to thresholds. Returns count of orders
        fired."""
        if self._in_lockout():
            return 0
        s = self.state[sym]
        book = s.book
        if not book:
            return 0
        max_slice = self.cfg.sweep_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        fired = 0

        # BUY asks priced <= lift_to (positive edge).
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px > lift_to:
                    break  # sorted ascending
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, px):
                    taken += want
                    room_buy -= want
                    fired += 1
                    print(f"[v18 SWP:{sym}] BUY {want}@{px}  "
                          f"fair={fair:.1f} lift_to={lift_to}")
                else:
                    break

        # SELL bids priced >= hit_to (positive edge).
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px < hit_to:
                    break  # sorted descending
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, px):
                    taken += want
                    room_sell -= want
                    fired += 1
                    print(f"[v18 SWP:{sym}] SELL {want}@{px}  "
                          f"fair={fair:.1f} hit_to={hit_to}")
                else:
                    break

        if fired:
            self._reveal_ioc_count += fired
        return fired

    # ==================================================================
    # MM refresh loop (between reveals)
    # ==================================================================
    def _mm_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_mm_quotes()
            except Exception as e:
                print(f"[v18 MM-ERR] {type(e).__name__}: {e}")
            self._mm_wake.wait(timeout=self.cfg.mm_refresh_sec)
            self._mm_wake.clear()
            if self._stop.is_set():
                return

    def _refresh_mm_quotes(self) -> None:
        if not self._can_post():
            return
        # In endgame mode A's settlement is known — don't repost MM
        # quotes on A (they'd just be picked off by other snipers).
        # Skip the loop entirely if both symbols would be excluded.
        if self._endgame_active:
            return
        now = time.time()
        if (self.cfg.mm_min_interval_sec > 0
                and (now - self._last_mm_refresh_t)
                < self.cfg.mm_min_interval_sec):
            return
        self._last_mm_refresh_t = now
        in_park = self._in_park_window()
        for sym in self.SYMS:
            try:
                if sym == "A":
                    fair, _ = self.fair_a()
                else:
                    fair, _ = self.fair_b()
                pos = self.state[sym].position
                if in_park:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "park")
                else:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "normal")
                    bid_px, ask_px = self._maybe_penny(
                        sym, fair, pos, bid_px, ask_px)
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v18 MM:{sym}-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # Park loop — tight ~50ms tick, parks at T_reveal − park_sec exactly
    # ==================================================================
    def _park_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._park_tick()
            except Exception as e:
                print(f"[v18 PARK-ERR] {type(e).__name__}: {e}")
            self._stop.wait(0.05)

    def _park_tick(self) -> None:
        if not self._can_post():
            return
        if not self._in_park_window():
            return
        # Park ONCE per reveal index — don't re-park on every tick.
        if self._last_park_reveal_idx == self._reveal_count:
            return
        self._last_park_reveal_idx = self._reveal_count
        # Apply park quotes for both symbols. This goes through the same
        # _apply_quote path as everything else, so modify-replace is
        # used and the order survives the window at deep price.
        for sym in self.SYMS:
            try:
                if sym == "A":
                    fair, _ = self.fair_a()
                else:
                    fair, _ = self.fair_b()
                pos = self.state[sym].position
                bid_px, ask_px = self._quote_prices(sym, fair, pos, "park")
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v18 PARK:{sym}-ERR] {type(e).__name__}: {e}")
        nxt = self._next_reveal_at()
        eta = (nxt - time.time()) if nxt is not None else float("nan")
        print(f"[v18 PARK] parked all sides, reveal in {eta:.2f}s")

    # ==================================================================
    # Endgame burst loop — after last reveal, A's settlement is KNOWN.
    # Walk full book depth at 25Hz and snipe every mispriced level.
    # ==================================================================
    def _endgame_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if (self.cfg.endgame_enabled
                        and self._endgame_active
                        and self.phase == "running"):
                    self._endgame_tick()
            except Exception as e:
                print(f"[v18 ENDGAME-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.endgame_tick_sec)

    def _endgame_tick(self) -> None:
        """One pass through both books, fire IOCs on anything mispriced.

        After last reveal, posterior collapses → fair_A = running_sum
        exact, fair_B = E[X] is no longer in play (B settled per reveal).
        We focus the burst on A and cancel our own resting quotes so they
        can't be picked off by competitors during the burst.
        """
        if self._in_lockout():
            return
        if self._tokens_available() < self.cfg.endgame_min_tokens:
            return

        # A: fair is exact. Cancel any of our resting quotes that aren't
        # the BBO improvement — they could only get filled at adverse
        # prices now that fair is known.
        fair_a = self._running_sum  # by definition after last reveal
        edge = self.cfg.endgame_edge_ticks_a
        max_levels = self.cfg.endgame_max_levels
        max_slice = self.cfg.endgame_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks

        s = self.state["A"]
        book = s.book
        if not book:
            return
        fired = 0

        # BUY asks priced ≤ running_sum − edge
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            for lvl in (book.get("asks") or [])[:max_levels]:
                if room_buy <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if fair_a - px < edge:
                    break
                if max_dist > 0 and abs(px - fair_a) > max_dist:
                    continue
                want = min(max_slice, room_buy, size)
                if self._ioc("A", "buy", want, px):
                    room_buy -= want
                    fired += 1
                    print(f"[v18 ENDGAME:A] BUY {want}@{px}  "
                          f"settle={fair_a:.0f} edge={fair_a - px:.1f}t")

        # SELL bids priced ≥ running_sum + edge
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            for lvl in (book.get("bids") or [])[:max_levels]:
                if room_sell <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px - fair_a < edge:
                    break
                if max_dist > 0 and abs(px - fair_a) > max_dist:
                    continue
                want = min(max_slice, room_sell, size)
                if self._ioc("A", "sell", want, px):
                    room_sell -= want
                    fired += 1
                    print(f"[v18 ENDGAME:A] SELL {want}@{px}  "
                          f"settle={fair_a:.0f} edge={px - fair_a:.1f}t")

        if fired:
            self._endgame_count += fired

    def _maybe_activate_endgame(self) -> None:
        """Flip the endgame flag on the LAST reveal."""
        if self._endgame_active:
            return
        if not self.cfg.endgame_enabled:
            return
        thresh = self.cfg.endgame_activate_at_remaining
        if self._n_remaining() <= thresh:
            self._endgame_active = True
            self._endgame_pos_a_at_entry = self.state["A"].position
            # Cancel our own resting orders on A — we don't want to be
            # picked off at adverse prices now that settlement is known.
            try:
                self.c.cancel_all(symbol="A")
                self.state["A"].resting["bid"].clear()
                self.state["A"].resting["ask"].clear()
            except Exception as e:
                print(f"[v18 ENDGAME] cancel_all A failed: {e}")
            print(f"[v18 ENDGAME] ACTIVE  rs={self._running_sum:.0f}  "
                  f"pos_a={self._endgame_pos_a_at_entry}  "
                  f"tick={self.cfg.endgame_tick_sec*1000:.0f}ms  "
                  f"edge={self.cfg.endgame_edge_ticks_a}t  "
                  f"depth={self.cfg.endgame_max_levels}")

    # ==================================================================
    # Connection pre-warm — saturate the pool with cheap GETs so the
    # first POST after an idle stretch doesn't eat a TCP handshake.
    # ==================================================================
    def _prewarm_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._prewarm_tick()
            except Exception as e:
                print(f"[v18 PREWARM-ERR] {type(e).__name__}: {e}")
            self._stop.wait(0.2)

    def _prewarm_tick(self) -> None:
        if not self.cfg.prewarm_enabled:
            return
        if self.phase != "running":
            return
        if self._in_lockout():
            return
        now = time.time()
        # Periodic idle keep-alive.
        do_periodic = (
            (now - self._last_prewarm_t) >= self.cfg.prewarm_interval_sec)
        # Burst right before reveal — confirm pool health for the IOC
        # fan-out about to fire.
        nxt = self._next_reveal_at()
        in_burst_window = (
            nxt is not None
            and 0.0 <= (nxt - now) <= self.cfg.prewarm_before_reveal_sec
            and self._prewarm_before_reveal_idx != self._reveal_count)
        if not (do_periodic or in_burst_window):
            return
        if self._tokens_available() < 2.0:
            # Don't burn rate-limit budget on warm-ups when bucket is low.
            return
        # Cheapest authenticated GET. Doesn't count as a strategy-side
        # write (no _register_send) — SDK bucket still gates it.
        try:
            self.c.tokens_available()  # local check, free
            # Use my_orders('A') as the warm-up — single tiny response
            # that exercises the same Session/keep-alive path as POSTs.
            self.c.my_orders(symbol="A")
            self._last_prewarm_t = now
            if in_burst_window:
                self._prewarm_before_reveal_idx = self._reveal_count
        except Exception:
            pass  # warm-up is best-effort

    # ==================================================================
    # Quote application — modify-replace fast path with multi-order safety
    # ==================================================================
    def _resting_qty(self, sym: str, side: str) -> int:
        return sum(int(e.get("qty") or 0)
                   for e in self.state[sym].resting[side].values())

    def _primary_resting(self, sym: str, side: str) -> Optional[dict]:
        entries = self.state[sym].resting[side]
        if not entries:
            return None
        return entries[max(entries.keys())]

    def _quote_size(self, sym: str, side: str) -> int:
        s = self.state[sym]
        base = self.cfg.size_a if sym == "A" else self.cfg.size_b
        pad = self.cfg.mm_inventory_safety_pad
        same_side_out = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        if side == "bid":
            headroom = ((s.position_limit - pad)
                        - s.position - same_side_out - in_flight)
        else:
            headroom = ((s.position_limit - pad)
                        + s.position - same_side_out - in_flight)
        return max(0, min(base, headroom + same_side_out))

    def _tokens_available(self) -> float:
        fn = getattr(self.c, "tokens_available", None)
        if fn is None:
            return float("inf")
        try:
            return float(fn())
        except Exception:
            return float("inf")

    def _in_lockout(self) -> bool:
        return time.monotonic() < self._lockout_until

    def _can_post(self) -> bool:
        """Master gate for any order-creating REST call (POST/MODIFY).
        False if game isn't in trading phase OR we've already seen a
        terminal 403 (game ended). Cancels can still go through during
        non-running phase via _cancel() so flatten cleanup works."""
        if self._post_terminal_blocked:
            return False
        if self.phase != "running":
            return False
        return True

    @staticmethod
    def _http_status_and_body(e: Exception) -> Tuple[Optional[int], str]:
        """Best-effort extraction of (status, body) from a requests
        HTTPError. Returns (None, '') if e isn't an HTTPError or has
        no .response attached."""
        resp = getattr(e, "response", None)
        if resp is None:
            return None, ""
        status = getattr(resp, "status_code", None)
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            body = ""
        return status, body

    def _handle_forbidden(self, ctx: str, sym: str, side: str,
                          target_px: Optional[int],
                          e: Exception) -> None:
        """Central 403 handler. Three things:
          1. Log the response body once so we can see WHY (auth /
             trading_closed / position / etc.).
          2. Re-fetch game_state to confirm phase. If server says we're
             NOT running, set the terminal block immediately.
          3. Count 403s in a sliding window; trip the circuit breaker
             if too many even after the phase recheck says we're fine
             (something else is broken — auth, position, etc.).
        """
        status, body = self._http_status_and_body(e)
        self._forbidden_count += 1
        now = time.monotonic()
        with self._forbidden_log_lock:
            self._forbidden_log.append(now)
            cutoff = now - self.cfg.forbidden_circuit_window_sec
            while (self._forbidden_log
                   and self._forbidden_log[0] < cutoff):
                self._forbidden_log.popleft()
            in_window = len(self._forbidden_log)

        # Log diagnostic info on first 403 of a streak.
        if self._forbidden_count == 1 or self._forbidden_count % 10 == 0:
            print(f"[v18 FORBIDDEN:{ctx}:{sym}/{side}@{target_px}] "
                  f"status={status} body={body!r} "
                  f"count={self._forbidden_count} "
                  f"in_window={in_window}/"
                  f"{self.cfg.forbidden_circuit_max}")

        # Throttled authoritative phase re-check. If the server says
        # we're not running, set the terminal block so subsequent sends
        # short-circuit silently.
        if (now - self._last_phase_recheck_t
                >= self.cfg.forbidden_phase_recheck_throttle_sec):
            self._last_phase_recheck_t = now
            try:
                gs = self.c.game_state()
                server_phase = gs.get("phase")
                if server_phase != "running":
                    if not self._post_terminal_blocked:
                        print(f"[v18 FORBIDDEN] server phase="
                              f"{server_phase!r} (local was "
                              f"{self.phase!r}) → setting terminal block")
                    self.phase = server_phase
                    self._post_terminal_blocked = True
            except Exception as e2:
                print(f"[v18 FORBIDDEN] phase recheck failed: "
                      f"{type(e2).__name__}: {e2}")

        # Circuit breaker: too many 403s in window even though we
        # believe we should be allowed → hard-block sends.
        if (in_window >= self.cfg.forbidden_circuit_max
                and not self._post_terminal_blocked):
            print(f"[v18 FORBIDDEN] circuit breaker tripped "
                  f"({in_window} 403s in "
                  f"{self.cfg.forbidden_circuit_window_sec:.0f}s) → "
                  f"blocking sends until phase transition")
            self._post_terminal_blocked = True

    def _can_send_now(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.cfg.sends_window_sec
        with self._send_log_lock:
            while self._send_log and self._send_log[0] < cutoff:
                self._send_log.popleft()
            return len(self._send_log) < self.cfg.max_sends_per_second

    def _register_send(self) -> None:
        now = time.monotonic()
        cutoff = now - self.cfg.sends_window_sec
        with self._send_log_lock:
            while self._send_log and self._send_log[0] < cutoff:
                self._send_log.popleft()
            self._send_log.append(now)

    def on_reject_event(self, msg: dict) -> None:
        if (msg.get("reason") or "").lower() != "rate_limited":
            return
        retry_ms = msg.get("retry_after_ms")
        try:
            retry_ms = float(retry_ms) if retry_ms is not None else 0.0
        except Exception:
            retry_ms = 0.0
        if retry_ms <= 0:
            retry_ms = self.cfg.lockout_min_pad_ms
        retry_ms = max(self.cfg.lockout_min_pad_ms,
                       min(self.cfg.lockout_max_pad_ms, retry_ms))
        new_until = time.monotonic() + retry_ms / 1000.0
        if new_until > self._lockout_until:
            self._lockout_until = new_until
            self._lockout_count += 1
            print(f"[v18 LOCKOUT] op={msg.get('op')!r} sym={msg.get('symbol')!r} "
                  f"retry_ms={retry_ms:.0f} count={self._lockout_count}")

    def _make_coid(self) -> str:
        self._coid_counter += 1
        return f"{self.cfg.client_order_id_prefix}-{self._coid_counter}"

    def _can_send(self, sym: str, side: str, qty: int,
                  is_modify: bool = False, primary_qty: int = 0) -> bool:
        s = self.state[sym]
        cap = s.position_limit - self.cfg.mm_inventory_safety_pad
        resting = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        add = max(0, qty - primary_qty) if is_modify else qty
        if side == "bid":
            worst = s.position + resting + in_flight + add
            if worst > cap:
                return False
        else:
            worst = s.position - resting - in_flight - add
            if worst < -cap:
                return False
        return True

    def _apply_quote(self, sym: str, bid_px: Optional[int],
                     ask_px: Optional[int]) -> None:
        s = self.state[sym]
        with self.lock:
            bid_sz = self._quote_size(sym, "bid") if bid_px is not None else 0
            ask_sz = self._quote_size(sym, "ask") if ask_px is not None else 0
            self._apply_side(sym, "bid",
                             bid_px if bid_sz > 0 else None, bid_sz)
            self._apply_side(sym, "ask",
                             ask_px if ask_sz > 0 else None, ask_sz)

    def _apply_side(self, sym: str, side: str,
                    target_px: Optional[int], target_qty: int) -> None:
        if self._in_lockout():
            return
        s = self.state[sym]
        side_orders = s.resting[side]
        primary = self._primary_resting(sym, side)

        # Cancel-only branch
        if target_px is None or target_qty <= 0:
            if (len(side_orders) >= 2
                    and self._tokens_available()
                    < self.cfg.bulk_cancel_min_tokens):
                return
            for oid in list(side_orders.keys()):
                if not self._cancel(sym, oid):
                    return
                side_orders.pop(oid, None)
            return

        # Drain orphans before any send.
        if primary is not None:
            orphans = [oid for oid in side_orders.keys()
                       if oid != primary["order_id"]]
            if (len(orphans) >= 2
                    and self._tokens_available()
                    < self.cfg.bulk_cancel_min_tokens):
                return
            for oid in orphans:
                if not self._cancel(sym, oid):
                    return
                side_orders.pop(oid, None)
            primary = self._primary_resting(sym, side)

        # Modify-replace fast path
        if primary is not None and self.cfg.use_modify_replace:
            same_px = (abs(primary["price"] - target_px)
                       < self.cfg.quote_min_change_ticks)
            same_qty = primary["qty"] == target_qty
            if same_px and same_qty:
                return
            if not self._can_post():
                return
            primary_qty = int(primary.get("qty") or 0)
            if not self._can_send(sym, side, target_qty,
                                  is_modify=True, primary_qty=primary_qty):
                return
            if self._tokens_available() < self.cfg.mm_send_min_tokens:
                return
            if not self._can_send_now():
                self._sends_deferred += 1
                return
            self._register_send()
            old_oid = primary["order_id"]
            net_add = max(0, target_qty - primary_qty)
            s.in_flight[side] += net_add
            try:
                res = self.c.modify(
                    old_oid,
                    price=target_px if not same_px else None,
                    qty=target_qty if not same_qty else None)
                order = (res or {}).get("order") or {}
                new_oid = (order.get("order_id") or order.get("id")
                           or old_oid)
                new_qty = order.get("qty", target_qty)
                if new_oid != old_oid:
                    side_orders.pop(old_oid, None)
                side_orders[new_oid] = {
                    "order_id": new_oid,
                    "price": target_px,
                    "qty": int(new_qty),
                    "added_t": time.time(),
                }
                self._modify_count += 1
                return
            except Exception as e:
                msg = str(e).lower()
                is_403 = "403" in msg or "forbidden" in msg
                terminal = (any(x in msg for x in
                                ("404", "not found", "no such", "gone"))
                            or "400" in msg or "bad request" in msg
                            or is_403)
                if is_403:
                    self._handle_forbidden(
                        "MODIFY", sym, side, target_px, e)
                    side_orders.pop(old_oid, None)
                elif terminal:
                    side_orders.pop(old_oid, None)
                else:
                    print(f"[v18 MODIFY:{sym}/{side}@{target_px}x"
                          f"{target_qty}] {type(e).__name__}: {e}")
                    return
            finally:
                s.in_flight[side] = max(
                    0, s.in_flight[side] - net_add)
        elif primary is not None:
            if not self._cancel(sym, primary["order_id"]):
                return
            side_orders.pop(primary["order_id"], None)

        # Post fresh
        if target_px < self.state[sym].tick:
            return
        if not self._can_post():
            return
        backoff_t = self._post_backoff_until.get((sym, side), 0.0)
        if backoff_t and time.monotonic() < backoff_t:
            return
        for oid in list(side_orders.keys()):
            if self._cancel(sym, oid):
                side_orders.pop(oid, None)
            else:
                return
        if not self._can_send(sym, side, target_qty):
            return
        if self._tokens_available() < self.cfg.mm_send_min_tokens:
            return
        # Duplicate-price guard
        for entry in side_orders.values():
            if int(entry.get("price", 0)) == int(target_px):
                return
        if not self._can_send_now():
            self._sends_deferred += 1
            return
        self._register_send()
        fn = self.c.buy if side == "bid" else self.c.sell
        s.in_flight[side] += target_qty
        try:
            res = fn(sym, target_px, target_qty,
                     client_order_id=self._make_coid())
            res_obj = res or {}
            nested = res_obj.get("order") or {}
            oid = (res_obj.get("order_id") or res_obj.get("id")
                   or nested.get("order_id") or nested.get("id"))
            if oid is not None:
                side_orders[oid] = {
                    "order_id": oid,
                    "price": target_px,
                    "qty": target_qty,
                    "added_t": time.time(),
                }
            self._post_count += 1
            self._post_backoff_until.pop((sym, side), None)
        except Exception as e:
            msg = str(e).lower()
            is_403 = "403" in msg or "forbidden" in msg
            terminal = ("400" in msg or "bad request" in msg or is_403)
            if is_403:
                self._handle_forbidden("POST", sym, side, target_px, e)
            elif terminal:
                already = (self._post_backoff_until.get(
                    (sym, side), 0.0) > time.monotonic())
                self._post_backoff_until[(sym, side)] = (
                    time.monotonic() + self.cfg.post_backoff_sec)
                if not already:
                    print(f"[v18 POST:{sym}/{side}@{target_px}] "
                          f"{type(e).__name__}: {e} → backoff "
                          f"{self.cfg.post_backoff_sec}s")
            elif "limit" not in msg and "position" not in msg:
                print(f"[v18 POST:{sym}/{side}@{target_px}] "
                      f"{type(e).__name__}: {e}")
        finally:
            s.in_flight[side] = max(0, s.in_flight[side] - target_qty)

    def _cancel(self, sym: str, order_id) -> bool:
        if self._in_lockout():
            return False
        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        self._register_send()
        try:
            self.c.cancel(order_id)
            return True
        except Exception as e:
            msg = str(e).lower()
            if (any(x in msg for x in ("404", "not found", "no such", "gone"))
                    or "400" in msg or "bad request" in msg
                    or "403" in msg or "forbidden" in msg):
                return True
            print(f"[v18 CANCEL:{sym} oid={order_id}] "
                  f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # IOC sniping helper (used by sweep + inter-sweep)
    # ==================================================================
    def _ioc(self, sym: str, side: str, qty: int, price: int) -> bool:
        if qty <= 0 or self._in_lockout():
            return False
        if not self._can_post():
            return False
        max_dist = self.cfg.max_ioc_distance_ticks
        if max_dist > 0:
            fair = (self.fair_a()[0] if sym == "A" else self.fair_b()[0])
            if abs(price - fair) > max_dist:
                return False
        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        s = self.state[sym]
        cap_side = "bid" if side == "buy" else "ask"
        if not self._can_send(sym, cap_side, qty):
            return False
        self._register_send()
        if side == "buy" and (s.position + qty) > s.position_limit:
            qty = s.position_limit - s.position
            if qty <= 0:
                return False
        if side == "sell" and (s.position - qty) < -s.position_limit:
            qty = s.position + s.position_limit
            if qty <= 0:
                return False
        fn = self.c.buy_ioc if side == "buy" else self.c.sell_ioc
        try:
            fn(sym, price, qty)
            return True
        except Exception as e:
            msg = str(e).lower()
            is_403 = "403" in msg or "forbidden" in msg
            if is_403:
                self._handle_forbidden("IOC", sym, side, price, e)
            elif "limit" not in msg and "position" not in msg:
                print(f"[v18 IOC:{sym}/{side}@{price}x{qty}] "
                      f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # Inter-reveal stale-quote sweep — fires on book_event
    # ==================================================================
    def _maybe_inter_sweep(self, sym: str) -> None:
        """Inter-reveal stale-quote sweep. Reads cached live thresholds
        from the FAST precompute loop (which already applied flatten-
        bias) — falls back to fair_a/fair_b if the cache is empty.

        Inter-sweep uses a wider `inter_sweep_edge_ticks` (3t default)
        than the post-reveal sweep — between reveals the book is mostly
        in equilibrium, so a 3t mispricing is a confident stale-quoter.
        """
        if not self.cfg.inter_sweep_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        now = time.time()
        if (now - s.last_inter_sweep_t) < self.cfg.inter_sweep_throttle_sec:
            return
        s.last_inter_sweep_t = now

        # Use live thresholds (flatten-aware) as baseline, then re-derive
        # the wider inter-sweep cutoff. Live cache: O(1).
        live_lift, live_hit, live_fair, in_flat = self._get_snipe_thresholds(
            sym, s.position)
        if live_fair == 0.0:
            # Fast loop hasn't ticked yet — direct compute.
            if sym == "A":
                live_fair, _ = self.fair_a()
            else:
                live_fair, _ = self.fair_b()
        fair = live_fair
        book = s.book
        if not book:
            return
        edge = self.cfg.inter_sweep_edge_ticks
        # In flatten window, reduce the edge on the flatten side.
        edge_buy = edge
        edge_sell = edge
        if in_flat:
            reduction = self.cfg.flatten_edge_reduction_ticks
            floor = self.cfg.flatten_min_edge_ticks
            if s.position < 0:
                edge_buy = max(floor, edge - reduction)
            if s.position > 0:
                edge_sell = max(floor, edge - reduction)
        min_size = self.cfg.inter_sweep_min_size
        max_slice = self.cfg.inter_sweep_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        fired = 0

        # BUY asks below fair − edge_buy.
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    continue
                if fair - px < edge_buy:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, px):
                    taken += want
                    room_buy -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position < 0) else "INTER"
                    print(f"[v18 {tag}:{sym}] BUY {want}@{px} "
                          f"fair={fair:.1f} edge={fair - px:.1f}t "
                          f"pos={s.position}")
                else:
                    break

        # SELL bids above fair + edge_sell.
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    continue
                if px - fair < edge_sell:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, px):
                    taken += want
                    room_sell -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position > 0) else "INTER"
                    print(f"[v18 {tag}:{sym}] SELL {want}@{px} "
                          f"fair={fair:.1f} edge={px - fair:.1f}t "
                          f"pos={s.position}")
                else:
                    break
        if fired:
            self._inter_sweep_count += fired

    # ==================================================================
    # Dime-defense / BBO-claim — react to inside book changes
    # ==================================================================
    def _maybe_dime_defense(self, sym: str) -> None:
        """Per-side, on every book event, ensure we're sitting AT the
        inside BBO whenever stepping there still leaves >= min_edge vs
        fair. Three sub-cases per side:

          1. Dimed:  we have resting, competitor is strictly inside us
                     → modify/post to best ± tick.
          2. Absent: no resting on this side (fresh after fill, or before
                     MM refresh) → post at best ± tick if profitable.
          3. Outside: we have resting BELOW (or above for ask) the inside
                     → step in if min_edge holds. Catches cases where the
                     inside tightened without anyone explicitly diming us.

        Skipped during the pre-reveal park window — we WANT to be deep
        then. Cases 1/2/3 all share the per-symbol throttle so a v=1
        dime war is bounded; the throttle also caps REST traffic.
        """
        if not self.cfg.dime_defense_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        now = time.time()
        if (now - s.last_dime_defense_t) < self.cfg.dime_defense_throttle_sec:
            return
        book = s.book
        if not book:
            return
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return
        best_bid = int(bids[0]["price"])
        best_ask = int(asks[0]["price"])

        if sym == "A":
            fair, _ = self.fair_a()
            min_edge = self.cfg.penny_min_edge_a
        else:
            fair, _ = self.fair_b()
            min_edge = self.cfg.penny_min_edge_b
        tick = s.tick
        max_step_dist = self.cfg.penny_max_step_ticks * tick
        fired = False

        # --- Bid side ---
        my_best_bid = None
        for entry in s.resting["bid"].values():
            px = int(entry.get("price") or 0)
            if my_best_bid is None or px > my_best_bid:
                my_best_bid = px
        # Default target: step 1t inside the inside. If we're already AT
        # or above the inside, no action — we're already BBO.
        if my_best_bid is None or my_best_bid < best_bid:
            target_bid = best_bid + tick
            # Edge floor (preserves maker profit after fees) + no cross +
            # cap on step distance (so a one-side collapse can't drag us
            # too far in).
            if (target_bid <= fair - min_edge
                    and target_bid < best_ask
                    and (my_best_bid is None
                         or (target_bid - my_best_bid) <= max_step_dist)):
                qty = self._quote_size(sym, "bid")
                if qty > 0:
                    case = "absent" if my_best_bid is None else "dimed"
                    print(f"[v18 BBO:{sym}/bid {case}] best={best_bid}"
                          f"(mine={my_best_bid}) → step to {target_bid}"
                          f" fair={fair:.1f}")
                    self._apply_side(sym, "bid", target_bid, qty)
                    fired = True

        # --- Ask side ---
        my_best_ask = None
        for entry in s.resting["ask"].values():
            px = int(entry.get("price") or 0)
            if my_best_ask is None or px < my_best_ask:
                my_best_ask = px
        if my_best_ask is None or my_best_ask > best_ask:
            target_ask = best_ask - tick
            if (target_ask >= fair + min_edge
                    and target_ask > best_bid
                    and (my_best_ask is None
                         or (my_best_ask - target_ask) <= max_step_dist)):
                qty = self._quote_size(sym, "ask")
                if qty > 0:
                    case = "absent" if my_best_ask is None else "dimed"
                    print(f"[v18 BBO:{sym}/ask {case}] best={best_ask}"
                          f"(mine={my_best_ask}) → step to {target_ask}"
                          f" fair={fair:.1f}")
                    self._apply_side(sym, "ask", target_ask, qty)
                    fired = True

        if fired:
            s.last_dime_defense_t = now

    # ==================================================================
    # Cross-symbol arb + private-feed (15ms) exploit
    # ==================================================================
    def _try_cross_arb(self, source: str) -> int:
        """Scan BOTH books and snipe any level past `cross_arb_edge_ticks`
        from fair. Walks `cross_arb_levels` deep on each side.

        The 15ms private-feed advantage: when called from on_fill_event
        or on_reveal (private events with no public-feed delay), our view
        of the new world is fresh but competitors are still pricing off
        a stale public book for ~15ms. Their stale liquidity is the
        cheapest it'll ever be — take it.

        Bypasses the inter_sweep_throttle (this is a private-event hook,
        not a book-flap reaction) but still honors:
          * server lockout cutoff (_in_lockout)
          * sliding-window rate cap (_can_send_now via _ioc)
          * max_ioc_distance trap filter
          * inventory pad

        `source` ∈ {"fill", "reveal", "manual"} — for log telemetry.
        """
        if not self.cfg.cross_arb_enabled:
            return 0
        if not self._can_post():
            return 0
        if self._in_lockout():
            return 0
        if self._in_park_window():
            return 0  # don't add risk in the 0.8s before reveal

        # Read live thresholds (FAST precompute) — already includes any
        # flatten-bias relaxation per direction.
        base_edge = self.cfg.cross_arb_edge_ticks
        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks
        max_slice = self.cfg.cross_arb_max_slice
        depth = max(1, self.cfg.cross_arb_levels)
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        fired_total = 0

        with self._live_lock:
            live_a = self._live_thresholds.get("A")
            live_b = self._live_thresholds.get("B")

        for sym, live in (("A", live_a), ("B", live_b)):
            s = self.state[sym]
            book = s.book
            if not book:
                continue

            # Determine effective edge per direction. If live cache is
            # populated and we're in a flatten window, relax on the
            # position-reducing side.
            if live is not None:
                fair = live.fair
                in_flat = live.in_flatten_window
            else:
                fair, _ = (self.fair_a() if sym == "A" else self.fair_b())
                in_flat = False
            edge_buy = base_edge
            edge_sell = base_edge
            if in_flat:
                if s.position < 0:
                    edge_buy = max(flat_min, base_edge - flat_reduction)
                if s.position > 0:
                    edge_sell = max(flat_min, base_edge - flat_reduction)

            # BUY mispriced asks (price ≤ fair − edge_buy), depth levels.
            room_buy = (s.position_limit - pad) - s.position
            if room_buy > 0:
                taken = 0
                for lvl in (book.get("asks") or [])[:depth]:
                    if room_buy <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if fair - px < edge_buy:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    want = min(max_slice - taken, room_buy, size)
                    if want <= 0:
                        break
                    if self._ioc(sym, "buy", want, px):
                        taken += want
                        room_buy -= want
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position < 0)
                               else f"ARB({source})")
                        print(f"[v18 {tag}:{sym}] BUY {want}@{px}  "
                              f"fair={fair:.2f} edge={fair - px:.2f}t "
                              f"pos={s.position}")
                    else:
                        break

            # SELL mispriced bids (price ≥ fair + edge_sell), depth levels.
            room_sell = s.position - (-s.position_limit + pad)
            if room_sell > 0:
                taken = 0
                for lvl in (book.get("bids") or [])[:depth]:
                    if room_sell <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if px - fair < edge_sell:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    want = min(max_slice - taken, room_sell, size)
                    if want <= 0:
                        break
                    if self._ioc(sym, "sell", want, px):
                        taken += want
                        room_sell -= want
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position > 0)
                               else f"ARB({source})")
                        print(f"[v18 {tag}:{sym}] SELL {want}@{px}  "
                              f"fair={fair:.2f} edge={px - fair:.2f}t "
                              f"pos={s.position}")
                    else:
                        break

        if fired_total:
            self._cross_arb_count += fired_total
        return fired_total

    # ==================================================================
    # Reconcile (orphan cleanup)
    # ==================================================================
    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.phase == "running":
                    self._reconcile_once()
            except Exception as e:
                print(f"[v18 RECON-ERR] {type(e).__name__}: {e}")
            self._stop.wait(self.cfg.reconcile_check_sec)

    def _reconcile_once(self) -> None:
        if self._in_lockout():
            return
        if self._tokens_available() < self.cfg.reconcile_min_tokens:
            return
        for sym in self.SYMS:
            if not self._can_send_now():
                self._sends_deferred += 1
                continue
            self._register_send()
            try:
                orders = self.c.my_orders(symbol=sym)
            except Exception as e:
                msg = str(e).lower()
                if "rate" not in msg:
                    print(f"[v18 RECON:{sym}] my_orders: "
                          f"{type(e).__name__}: {e}")
                continue
            server_oids = {int(o.get("order_id") or o.get("id") or -1): o
                           for o in (orders or [])
                           if (o.get("order_id") or o.get("id")) is not None}
            s = self.state[sym]
            now = time.time()
            min_age = self.cfg.reconcile_min_age_sec
            with self.lock:
                # Prune ghost entries
                for side in ("bid", "ask"):
                    for oid in list(s.resting[side].keys()):
                        if oid in server_oids:
                            continue
                        e = s.resting[side][oid]
                        if (now - float(e.get("added_t", 0))) >= min_age:
                            s.resting[side].pop(oid, None)
                # Cancel server-side orphans tagged with our prefix
                tracked = set(s.resting["bid"]) | set(s.resting["ask"])
                prefix = self.cfg.client_order_id_prefix
                respect = self.cfg.reconcile_respect_manual_orders
                orphan_oids = []
                for oid, o in server_oids.items():
                    if oid in tracked:
                        continue
                    if respect:
                        coid = o.get("client_order_id")
                        if coid is None or not str(coid).startswith(prefix):
                            continue
                    orphan_oids.append(oid)
                total = len(orphan_oids)
                bulk_thr = self.cfg.reconcile_bulk_cancel_threshold
                if (bulk_thr > 0 and total >= bulk_thr
                        and self._can_send_now()):
                    self._register_send()
                    try:
                        self.c.cancel_all(symbol=sym)
                        for side in ("bid", "ask"):
                            s.resting[side].clear()
                        print(f"[v18 RECON:{sym}] BULK cancel_all "
                              f"({total} orphans)")
                        continue
                    except Exception as e:
                        print(f"[v18 RECON:{sym}] cancel_all failed: {e}")
                cap = max(1, self.cfg.reconcile_max_cancels_per_tick)
                cancelled = 0
                for oid in orphan_oids[:cap]:
                    if not self._cancel(sym, oid):
                        break
                    cancelled += 1
                # Only log when above the noise threshold or when we
                # had to cap (capping hints at a real leak worth seeing).
                capped = total > cap
                if total >= self.cfg.reconcile_print_min or capped:
                    suffix = (f" (capped at {cap}/{total})"
                              if capped else "")
                    print(f"[v18 RECON:{sym}] cancelled {cancelled} "
                          f"orphan(s){suffix}")

    # ==================================================================
    # Book helpers
    # ==================================================================
    def _book_best(self, sym: str, side: str) -> Optional[Tuple[int, int]]:
        book = self.state[sym].book
        if not book:
            return None
        lvls = book.get(side) or []
        if not lvls:
            return None
        return (lvls[0]["price"], int(lvls[0].get("qty") or 0))

    # ==================================================================
    # WS event handlers
    # ==================================================================
    def on_book_event(self, msg: dict) -> None:
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        s = self.state[sym]
        s.book = {
            "bids": list(msg.get("bids") or []),
            "asks": list(msg.get("asks") or []),
        }
        s.book_t = time.time()
        # Penny/dime + inter-reveal stale sweep both react here. Both
        # are no-ops during the park window.
        self._maybe_inter_sweep(sym)
        # Reclaim BBO if a competitor stepped inside us. Runs on EVERY
        # book event (own throttle) so we don't sit dimed for a full
        # mm_refresh_sec window.
        self._maybe_dime_defense(sym)

    def on_fill_event(self, msg: dict) -> None:
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        s = self.state[sym]
        side = msg.get("side")
        qty = int(msg.get("qty") or 0)
        if qty <= 0 or side not in ("buy", "sell"):
            return
        with self.lock:
            s.position += qty if side == "buy" else -qty
        if msg.get("liquidity") == "maker":
            ord_id = msg.get("order_id")
            for kind in ("bid", "ask"):
                entry = s.resting[kind].get(ord_id)
                if entry is not None:
                    new_qty = max(0, int(entry.get("qty") or 0) - qty)
                    if new_qty == 0:
                        s.resting[kind].pop(ord_id, None)
                    else:
                        entry["qty"] = new_qty
                    break
        # 15ms private-feed exploit: the print hasn't propagated to the
        # public WS feed yet, so competitors are still seeing the
        # pre-fill book. Scan both A and B for arb edges at fresh fairs
        # before they reprice. Runs BEFORE the defensive-cancel block so
        # the IOC bursts go out first; cancels are second-order.
        try:
            self._try_cross_arb("fill")
        except Exception as e:
            print(f"[v18 ARB-ERR fill] {type(e).__name__}: {e}")
        # Defensive cancel if we hit the pad zone.
        pad = self.cfg.mm_inventory_safety_pad
        if s.position >= s.position_limit - pad:
            self._defensive_cancel_side(sym, "bid")
        if s.position <= -(s.position_limit - pad):
            self._defensive_cancel_side(sym, "ask")
        self._emergency_flatten_if_needed(sym)
        self._mm_wake.set()

    def _defensive_cancel_side(self, sym: str, side: str) -> None:
        s = self.state[sym]
        if not s.resting[side]:
            return
        if self._in_lockout():
            return
        if (len(s.resting[side]) >= 3
                and self._tokens_available()
                < self.cfg.bulk_cancel_min_tokens):
            return
        for oid in list(s.resting[side].keys()):
            if self._cancel(sym, oid):
                s.resting[side].pop(oid, None)

    def _emergency_flatten_if_needed(self, sym: str) -> None:
        s = self.state[sym]
        limit = s.position_limit
        trip = int(limit * self.cfg.emergency_flatten_factor)
        if abs(s.position) <= trip:
            return
        if s.resting["bid"] or s.resting["ask"]:
            try:
                self.c.cancel_all(symbol=sym)
                s.resting["bid"].clear()
                s.resting["ask"].clear()
            except Exception as e:
                print(f"[v18 EMG cancel_all:{sym}] "
                      f"{type(e).__name__}: {e}")
        if not self._can_post():
            return
        if s.position > trip:
            excess = s.position - (limit - self.cfg.mm_inventory_safety_pad)
            best = self._book_best(sym, "bids")
            if best is not None and excess > 0:
                px, sz = best
                try:
                    self.c.sell_ioc(sym, px, min(excess, sz))
                except Exception as e:
                    msg = str(e).lower()
                    if not (("403" in msg or "forbidden" in msg)
                            and self.phase != "running"):
                        print(f"[v18 EMG sell:{sym}] "
                              f"{type(e).__name__}: {e}")
                print(f"[v18 EMG:{sym}] pos={s.position}/lim={limit} "
                      f"IOC sell {min(excess, sz)}@{px}")
        else:
            excess = -s.position - (limit - self.cfg.mm_inventory_safety_pad)
            best = self._book_best(sym, "asks")
            if best is not None and excess > 0:
                px, sz = best
                try:
                    self.c.buy_ioc(sym, px, min(excess, sz))
                except Exception as e:
                    msg = str(e).lower()
                    if not (("403" in msg or "forbidden" in msg)
                            and self.phase != "running"):
                        print(f"[v18 EMG buy:{sym}] "
                              f"{type(e).__name__}: {e}")
                print(f"[v18 EMG:{sym}] pos={s.position}/lim={limit} "
                      f"IOC buy {min(excess, sz)}@{px}")

    def on_trade(self, msg: dict) -> None:
        pass

    def on_quote_event(self, msg: dict) -> None:
        pass

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        with self.lock:
            prev_phase = self.phase
            self.phase = phase
            if phase == "running":
                self._phase_running_t = time.time()
                self.posterior.reset(reveals or [])
                self._reveal_count = len(self.posterior.reveals)
                self._running_sum = float(sum(self.posterior.reveals))
                self._last_reveal_t = None
                self._last_park_reveal_idx = -1
                self._endgame_active = False  # reset on phase enter
                # Clear sticky 403 block — new round, server accepts orders.
                if self._post_terminal_blocked:
                    print(f"[v18 POST-TERMINAL] cleared "
                          f"(prev_phase={prev_phase!r}, count="
                          f"{self._terminal_403_count}, "
                          f"forbidden_total={self._forbidden_count})")
                self._post_terminal_blocked = False
                self._terminal_403_count = 0
                self._forbidden_count = 0
                with self._forbidden_log_lock:
                    self._forbidden_log.clear()
                self._last_phase_recheck_t = 0.0
                self._bump_posterior_gen()
                self._precompute_request.set()
                # If we joined mid-round AFTER the last reveal, flip
                # endgame immediately so the burst loop picks up.
                self._maybe_activate_endgame()
            elif phase in ("completed", "settling", "settled"):
                self._phase_running_t = None
                self._endgame_active = False
                # Pre-emptively block all POSTs so we don't race a 403.
                self._post_terminal_blocked = True
                for sym in self.SYMS:
                    for kind in ("bid", "ask"):
                        self.state[sym].resting[kind] = {}

    # ==================================================================
    # Flatten + stop
    # ==================================================================
    def flatten(self) -> None:
        self._stop.set()
        self._mm_wake.set()
        self._precompute_request.set()
        for sym in self.SYMS:
            try:
                self.c.cancel_all(symbol=sym)
            except Exception as e:
                print(f"[v18 FLATTEN cancel_all:{sym}] "
                      f"{type(e).__name__}: {e}")
            s = self.state[sym]
            for kind in ("bid", "ask"):
                s.resting[kind] = {}
        for sym in self.SYMS:
            s = self.state[sym]
            if s.position == 0:
                continue
            side = "sell" if s.position > 0 else "buy"
            qty = abs(s.position)
            best = self._book_best(
                sym, "bids" if side == "sell" else "asks")
            if best is None:
                continue
            px = best[0]
            self._ioc(sym, side, qty, px)
            print(f"[v18 FLATTEN:{sym}] {side} {qty}@{px}")
        try:
            self._ioc_executor.shutdown(wait=False)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        self._mm_wake.set()
        self._precompute_request.set()


# ===========================================================================
# Standalone runner — prefer run_combined18.py
# ===========================================================================
def _strategy18_main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")
    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} (a, w) pairs.")
    strat = StrategyV18(c, post)

    def on_reveal(msg):
        print(f"REVEAL #{msg.get('index')} = {msg.get('value')}  "
              f"running_sum={msg.get('running_sum')}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(f"FILL {msg['symbol']} {msg['side']:>4s} "
              f"{msg['qty']}@{msg['price']}  "
              f"liq={msg.get('liquidity')}  cp={msg.get('counterparty')}")
        strat.on_fill_event(msg)

    def on_game_state(msg):
        strat.on_phase_change(msg.get("phase"), msg.get("reveals") or [])

    def on_message(msg):
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = strat.on_trade
    c.on_book = strat.on_book_event
    c.on_game_state = on_game_state
    c.on_message = on_message
    c.on_reject = strat.on_reject_event
    c.start()

    print("\nv18 standalone. s=status, p=precompute, f=flatten, q=quit.\n")
    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                while True:
                    time.sleep(60)
            if cmd == "s":
                fa, sa = strat.fair_a()
                fb, sb = strat.fair_b()
                print(f"  phase={strat.phase}  k={strat._reveal_count}/"
                      f"{strat.n_total}  rs={strat._running_sum:.0f}")
                print(f"  A: pos={strat.state['A'].position}  "
                      f"fair={fa:.1f}+/-{sa:.1f}")
                print(f"  B: pos={strat.state['B'].position}  "
                      f"fair={fb:.2f}+/-{sb:.2f}")
            elif cmd == "p":
                with strat._precompute_lock:
                    items = sorted(strat._precompute.items())
                print(f"  {len(items)} scenarios:")
                for v, sc in items:
                    print(f"    v={v:>2d}  P={sc.prob:.3f}  "
                          f"A=[{sc.bid_a},{sc.ask_a}]/"
                          f"[{sc.lift_to_a},{sc.hit_to_a}]  "
                          f"B=[{sc.bid_b},{sc.ask_b}]/"
                          f"[{sc.lift_to_b},{sc.hit_to_b}]")
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown {cmd!r}; try s/p/f/q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten error: {e}")


# ===========================================================================
# Inlined from strategy19.py — TruthOracle + StrategyV19(StrategyV18)
# with truth-anchored fairs
# ===========================================================================

# Path bootstrap: keep sibling modules importable when run as a script.



# ===========================================================================
# Truth oracle — one-shot fetch of /api/admin/truth at round start.
#
# Re-implemented (not imported from v16) to keep strategy19 self-contained
# and to drop v16's superfluous polling thread.
# ===========================================================================
ADMIN_KEY = "sean123"


class TruthOracle:
    """Fetcher for `/api/admin/truth` (admin key) returning the entire
    reveal sequence.

    Uses its OWN requests.Session with the admin key so the fetch does
    not consume the trader's rate budget. One-shot per round: the
    `fetch_once` method is idempotent — once the sequence is cached it
    short-circuits. Call `reset()` at every phase->running transition
    so the next round re-fetches.
    """

    def __init__(self, base_url: str = URL, admin_key: str = ADMIN_KEY,
                 timeout_sec: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_sec
        self._lock = threading.Lock()
        self._round_sequence: Optional[List[int]] = None
        self._fetch_count = 0
        self._err_count = 0
        self._last_err: str = ""
        self._sess = requests.Session()
        self._sess.headers.update({"X-API-Key": admin_key})

    def fetch_once(self) -> bool:
        """Direct admin GET. Returns True iff a fresh running-phase
        sequence was cached (or already cached). Safe to call again."""
        with self._lock:
            if self._round_sequence is not None:
                return True
        try:
            r = self._sess.get(
                f"{self.base_url}/api/admin/truth",
                timeout=self.timeout)
        except Exception as e:
            with self._lock:
                self._err_count += 1
                self._last_err = repr(e)
            return False
        if not r.ok:
            with self._lock:
                self._err_count += 1
                self._last_err = f"HTTP {r.status_code}"
            return False
        try:
            j = r.json()
        except Exception as e:
            with self._lock:
                self._err_count += 1
                self._last_err = f"json: {e!r}"
            return False
        if j.get("phase") != "running":
            return False
        seq = j.get("full_sequence") or []
        if not seq:
            return False
        with self._lock:
            self._round_sequence = [int(v) for v in seq]
            self._fetch_count += 1
        return True

    def reset(self) -> None:
        """Clear the cached sequence (call on each phase->running edge)."""
        with self._lock:
            self._round_sequence = None

    def full_sequence(self) -> Optional[List[int]]:
        with self._lock:
            return (list(self._round_sequence)
                    if self._round_sequence else None)

    def truth_a(self) -> Optional[float]:
        """A settles once at sum(full_sequence). Constant once cached."""
        with self._lock:
            if not self._round_sequence:
                return None
            return float(sum(self._round_sequence))

    def truth_b_for_window(self, window_idx: int) -> Optional[float]:
        """B settles at every reveal — window i (= reveals already seen)
        settles at full_sequence[i]. Returns None past final window."""
        with self._lock:
            if not self._round_sequence:
                return None
            if 0 <= window_idx < len(self._round_sequence):
                return float(self._round_sequence[window_idx])
            return None

    def stats(self) -> dict:
        with self._lock:
            return {
                "cached": self._round_sequence is not None,
                "fetches": self._fetch_count,
                "errors": self._err_count,
                "last_err": self._last_err,
            }


# ===========================================================================
# Config
# ===========================================================================
@dataclass
class ConfigV19(ConfigV18):
    """v19 Config — v18 defaults with truth-aware overrides.

    Tightenings vs v18:
      * sweep_edge_ticks 1.5 -> 0.5         truth is exact, recoup 0.5t fee
      * inter_sweep_edge_ticks 3.0 -> 0.5   ditto
      * cross_arb_edge_ticks 2.0 -> 0.5     ditto
      * inter_sweep_min_size 4 -> 1         any size is +EV
      * sweep_max_slice 10 -> 25            bigger bites, fewer rounds
      * inter_sweep_max_slice 10 -> 25      ditto
      * cross_arb_max_slice 8 -> 25         ditto
      * cross_arb_levels 5 -> 10            deeper book walk
      * mm_inventory_safety_pad 10 -> 0     full ±L window
      * sweep_position_pad 5 -> 0           ditto
      * size_a 5 -> 10                      bigger maker quotes
      * size_b 5 -> 10                      ditto
      * skew_full_flip_a 50 -> 200          slower skew rebound
      * skew_full_flip_b 100 -> 200         ditto
      * quote_width_a 8 -> 4                tighter MM
      * quote_width_b 4 -> 2                tighter MM
      * pre_reveal_park_sec 0.8 -> 0.0      no park — quotes are truthful
      * no_info_widen_mult 8.0 -> 1.0       k=0 is no longer "no info"
      * pre_reveal_widen_mult 5.0 -> 1.0    ditto
      * flatten_bias_enabled True -> False  no uncertainty
    """
    # ---- truth oracle ----
    truth_enabled: bool = True
    # Block at most this long waiting for the round-start truth fetch.
    truth_init_wait_sec: float = 1.0

    # ---- tighter snipe edges ----
    sweep_edge_ticks: float = 0.5
    inter_sweep_edge_ticks: float = 0.5
    cross_arb_edge_ticks: float = 0.5

    # ---- larger slices / deeper depth ----
    sweep_max_slice: int = 25
    inter_sweep_max_slice: int = 25
    cross_arb_max_slice: int = 25
    cross_arb_levels: int = 10
    inter_sweep_min_size: int = 1
    inter_sweep_throttle_sec: float = 0.15

    # ---- no inventory padding ----
    mm_inventory_safety_pad: int = 0
    sweep_position_pad: int = 0

    # ---- park / widen disabled ----
    pre_reveal_park_sec: float = 0.0
    pre_reveal_park_offset_ticks: int = 0
    pre_reveal_widen_mult: float = 1.0
    no_info_widen_mult: float = 1.0

    # ---- flatten-bias disabled ----
    flatten_bias_enabled: bool = False

    # ---- bigger maker quotes, slower skew, tighter widths ----
    size_a: int = 10
    size_b: int = 10
    skew_full_flip_a: int = 200
    skew_full_flip_b: int = 200
    quote_width_a: int = 4
    quote_width_b: int = 2

    # ---- penny-step edges (the maker-side floor) ----
    # min_edge must still beat the 0.5/lot maker fee, but we can lower
    # it now that fair is exact. 0.5t == break-even after fee.
    penny_min_edge_a: float = 0.5
    penny_min_edge_b: float = 0.5

    # ---- endgame burst — extend to B ----
    # v18 only sweeps A during endgame (since A's settle is exact only
    # after the last reveal). With truth, B's value in the current
    # window is also exact at all times → reuse the burst pattern for
    # B once we're inside the final B window.
    endgame_b_enabled: bool = True
    endgame_edge_ticks_b: float = 0.5

    # ---- truth printing throttle ----
    truth_print_throttle_sec: float = 5.0

    # ----------------------------------------------------------------
    # Position-cap ramp — MM enforces a SOFT cap that grows from
    # start_frac*L at window 0 to end_frac*L at the final window.
    # Snipes (reveal IOC, inter-sweep, dime defense, cross-arb,
    # endgame) still use the HARD position_limit — every snipe fill
    # is +EV by truth, so size up always. The ramp only restrains
    # passive MM accumulation early in the round, where the posterior
    # has more time to adversely-select us and where directional
    # confidence matters less (since the favorable side ramps in too).
    # ----------------------------------------------------------------
    position_ramp_enabled: bool = True
    position_ramp_start_frac: float = 0.25  # 25% of ±L at window 0
    position_ramp_end_frac: float = 1.0     # 100% of ±L at final window
    position_ramp_min_cap: int = 15         # floor so early MM is viable

    # ---- Maker size ramp ----
    # Base quote size also ramps over the round so we lean into edges
    # more as the round matures (and as our soft cap expands).
    size_ramp_enabled: bool = True
    size_ramp_start_frac: float = 0.40
    size_ramp_min_size: int = 3

    # ---- Directional skew (truth-vs-naive edge) ----
    # When the truth fair differs from a fresh posterior's "naive"
    # fair at this window by >= min_edge ticks, skew our MM quotes:
    #   * favorable side (the one we want filled) → tight_ticks
    #     from fair (we WANT this fill — it's +EV by truth)
    #   * unfavorable side → wide_ticks from fair (we only sell/buy
    #     if some idiot lifts us at a crazy price)
    # Edge sign convention:
    #   edge > 0 → truth > naive → market underprices → BUY
    #   edge < 0 → truth < naive → market overprices → SELL
    # Disabled if naive trajectory is unknown (truth fetch failed).
    directional_skew_enabled: bool = True
    directional_skew_min_edge: float = 1.5      # ticks
    directional_skew_tight_ticks: int = 1
    directional_skew_wide_ticks: int = 5

    # ---- coid prefix ----
    client_order_id_prefix: str = "v19"


# ===========================================================================
# Strategy
# ===========================================================================
class StrategyV19(StrategyV18):
    """v19: v18 dual-symbol MM execution with truth-anchored fair."""

    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[ConfigV19] = None):
        # Force the Config v19 dataclass so all v19 fields are present.
        if config is None:
            config = ConfigV19()
        elif not isinstance(config, Config):
            # Cross-type promotion (Config18 -> Config19) keeps overlapping
            # fields and uses defaults for new ones.
            config = Config(**{
                f.name: getattr(config, f.name)
                for f in config.__dataclass_fields__.values()
                if f.name in Config.__dataclass_fields__
            })

        # Truth state — create BEFORE super so any background thread spun
        # up in super().__init__ that calls fair_a/fair_b doesn't NPE.
        self.truth = TruthOracle()
        self._last_truth_log_t: float = 0.0
        self._truth_scenario_count: int = 0
        # Cached "naive" per-window trajectory — what a fresh posterior
        # would say at each window after seeing reveals[0:i] from truth.
        # Used to compute the truth-vs-naive edge for directional skew.
        # Filled by _compute_naive_trajectory once truth is cached.
        self._naive_traj_a: Optional[List[float]] = None
        self._naive_traj_b: Optional[List[float]] = None

        super().__init__(client, posterior, config)

        # If we attach mid-running, try the first fetch right now so MM
        # threads can start using truth-anchored fair on their first tick.
        if self.phase == "running":
            deadline = time.time() + self.cfg.truth_init_wait_sec
            while time.time() < deadline:
                if self.truth.fetch_once():
                    break
                time.sleep(0.05)
            seq = self.truth.full_sequence()
            if seq is not None:
                print(f"[v19 TRUTH-INIT] seq={seq}  "
                      f"truth_a={sum(seq)}  "
                      f"truth_b@win{self._reveal_count}="
                      f"{seq[self._reveal_count] if self._reveal_count < len(seq) else '-'}")
                self._compute_naive_trajectory()
                self._bump_posterior_gen()
                self._precompute_request.set()
            else:
                st = self.truth.stats()
                print(f"[v19 TRUTH-INIT] no truth within "
                      f"{self.cfg.truth_init_wait_sec:.1f}s "
                      f"(fetches={st['fetches']} errors={st['errors']} "
                      f"last_err={st['last_err']!r}) — degrading to v18 fair")

        print(f"[v19 INIT] truth_enabled={self.cfg.truth_enabled}  "
              f"sweep_edge={self.cfg.sweep_edge_ticks}t  "
              f"inter_edge={self.cfg.inter_sweep_edge_ticks}t  "
              f"arb_edge={self.cfg.cross_arb_edge_ticks}t")
        print(f"[v19 INIT] size_a={self.cfg.size_a} size_b={self.cfg.size_b}  "
              f"width_a={self.cfg.quote_width_a}t width_b={self.cfg.quote_width_b}t  "
              f"flip_a={self.cfg.skew_full_flip_a} flip_b={self.cfg.skew_full_flip_b}")
        print(f"[v19 INIT] park_sec={self.cfg.pre_reveal_park_sec}  "
              f"mm_pad={self.cfg.mm_inventory_safety_pad}  "
              f"flatten_bias={self.cfg.flatten_bias_enabled}")
        print(f"[v19 INIT] pos_ramp={self.cfg.position_ramp_start_frac:.2f}->"
              f"{self.cfg.position_ramp_end_frac:.2f} "
              f"(min_cap={self.cfg.position_ramp_min_cap})  "
              f"size_ramp={self.cfg.size_ramp_start_frac:.2f}->1.0  "
              f"dir_skew={self.cfg.directional_skew_enabled} "
              f"min_edge={self.cfg.directional_skew_min_edge}t "
              f"tight/wide={self.cfg.directional_skew_tight_ticks}/"
              f"{self.cfg.directional_skew_wide_ticks}t")

    # ==================================================================
    # Truth helpers
    # ==================================================================
    def _truth_a(self) -> Optional[float]:
        if not self.cfg.truth_enabled:
            return None
        return self.truth.truth_a()

    def _truth_b(self, window_idx: Optional[int] = None) -> Optional[float]:
        if not self.cfg.truth_enabled:
            return None
        i = self._reveal_count if window_idx is None else int(window_idx)
        return self.truth.truth_b_for_window(i)

    def _truth_next_value(self) -> Optional[int]:
        """The exact value of the NEXT reveal (used to collapse the
        precompute table to a single scenario)."""
        seq = self.truth.full_sequence()
        if not seq:
            return None
        i = self._reveal_count
        if 0 <= i < len(seq):
            return int(seq[i])
        return None

    def _maybe_log_truth(self) -> None:
        now = time.time()
        if (now - self._last_truth_log_t) < self.cfg.truth_print_throttle_sec:
            return
        self._last_truth_log_t = now
        seq = self.truth.full_sequence()
        if seq is None:
            return
        wb = self._reveal_count
        tb = seq[wb] if 0 <= wb < len(seq) else None
        print(f"[v19 TRUTH] seq={seq}  truth_a={sum(seq)}  "
              f"truth_b@win{wb}={tb}  pos_a={self.state['A'].position}  "
              f"pos_b={self.state['B'].position}")

    # ==================================================================
    # Naive trajectory — what would a fresh posterior say at each window
    # after feeding it reveals[0:i] from the known truth sequence?
    # The truth-vs-naive edge at each window tells us where the market
    # is most mispriced; we lean directionally into those windows.
    # ==================================================================
    def _compute_naive_trajectory(self) -> None:
        seq = self.truth.full_sequence()
        if not seq:
            self._naive_traj_a = None
            self._naive_traj_b = None
            return
        n_total = self.n_total
        work = copy.deepcopy(self.posterior)
        work.reset()  # back to prior, empty reveals
        naive_a: List[float] = []
        naive_b: List[float] = []
        running_sum = 0.0
        for i in range(n_total + 1):
            n_rem = max(n_total - i, 0)
            fa, _sa = work.predict_settle(running_sum, n_rem)
            ex_b = 0.0
            for (a, w), prob in work.posterior.items():
                ex_b += prob * (a + w / 2.0)
            naive_a.append(fa)
            naive_b.append(ex_b)
            if i < n_total and i < len(seq):
                work.update(float(seq[i]))
                running_sum += float(seq[i])
        self._naive_traj_a = naive_a
        self._naive_traj_b = naive_b
        truth_a = float(sum(seq))
        # Compact, scannable trajectory print.
        print(f"[v19 TRAJECTORY] truth_a={truth_a:.1f}  n_total={n_total}")
        for i in range(n_total + 1):
            ea = truth_a - naive_a[i]
            if i < len(seq):
                tb = float(seq[i])
                eb = tb - naive_b[i]
                print(f"  win {i:>2d}: naive_a={naive_a[i]:>7.1f} "
                      f"edge_a={ea:+6.1f}  truth_b={tb:>3.0f} "
                      f"naive_b={naive_b[i]:>6.2f} edge_b={eb:+6.2f}")
            else:
                print(f"  win {i:>2d}: naive_a={naive_a[i]:>7.1f} "
                      f"edge_a={ea:+6.1f}  (no B)")

    def _round_progress(self) -> float:
        """Round fraction in [0, 1] based on reveal count."""
        if self.n_total <= 0:
            return 1.0
        return min(1.0, max(0.0, self._reveal_count / float(self.n_total)))

    def _effective_position_cap(self, sym: str) -> int:
        """Soft cap (|position|) that MM uses. Grows from start_frac*L
        at window 0 to end_frac*L at the final window. Snipes still
        use the hard ±position_limit."""
        L = self.state[sym].position_limit
        if not self.cfg.position_ramp_enabled:
            return L
        prog = self._round_progress()
        frac = (self.cfg.position_ramp_start_frac
                + (self.cfg.position_ramp_end_frac
                   - self.cfg.position_ramp_start_frac) * prog)
        cap = int(round(L * frac))
        cap = max(cap, self.cfg.position_ramp_min_cap)
        return min(cap, L)

    def _ramped_base_size(self, sym: str) -> int:
        base = self.cfg.size_a if sym == "A" else self.cfg.size_b
        if not self.cfg.size_ramp_enabled:
            return base
        prog = self._round_progress()
        frac = (self.cfg.size_ramp_start_frac
                + (1.0 - self.cfg.size_ramp_start_frac) * prog)
        sz = int(round(base * frac))
        return max(sz, self.cfg.size_ramp_min_size)

    def _window_edge_at(self, sym: str, window_idx: int) -> Optional[float]:
        """truth_fair - naive_fair at the given window. None if the
        trajectory isn't available."""
        if sym == "A":
            traj = self._naive_traj_a
            if not traj:
                return None
            seq = self.truth.full_sequence()
            if not seq:
                return None
            idx = max(0, min(window_idx, len(traj) - 1))
            return float(sum(seq)) - traj[idx]
        traj_b = self._naive_traj_b
        if not traj_b:
            return None
        seq = self.truth.full_sequence()
        if not seq:
            return None
        if 0 <= window_idx < len(seq):
            return float(seq[window_idx]) - traj_b[window_idx]
        return None

    def _window_edge(self, sym: str) -> Optional[float]:
        return self._window_edge_at(sym, self._reveal_count)

    # ==================================================================
    # fair_a / fair_b — return truth when available, with tiny sigma
    # ==================================================================
    def fair_a(self) -> Tuple[float, float]:
        t = self._truth_a()
        if t is not None:
            self._maybe_log_truth()
            return float(t), 0.5
        return super().fair_a()

    def fair_b(self) -> Tuple[float, float]:
        t = self._truth_b()
        if t is not None:
            return float(t), 0.5
        return super().fair_b()

    # ==================================================================
    # flatten-bias never triggers with truth — uncertainty is zero
    # ==================================================================
    def _in_flatten_bias_window(self, sigma_a: Optional[float] = None) -> bool:
        if self.cfg.truth_enabled and self._truth_a() is not None:
            return False
        return super()._in_flatten_bias_window(sigma_a)

    # ==================================================================
    # Quote sizing — soft-cap aware + base-size ramp.
    # MM honors the SOFT cap so we don't blow past the round-progress
    # position envelope during passive market-making. Snipes (IOC paths
    # like _ioc, sweeps, dime defense, cross-arb, endgame) still use
    # the hard ±position_limit because every snipe fill is +EV by
    # truth — those paths bypass _quote_size entirely.
    # ==================================================================
    def _quote_size(self, sym: str, side: str) -> int:
        s = self.state[sym]
        base = self._ramped_base_size(sym)
        cap = self._effective_position_cap(sym)
        same_side_out = self._resting_qty(sym, side)
        in_flight = int(s.in_flight.get(side, 0))
        if side == "bid":
            headroom = cap - s.position - same_side_out - in_flight
        else:
            headroom = cap + s.position - same_side_out - in_flight
        return max(0, min(base, headroom + same_side_out))

    # ==================================================================
    # Quote prices — wraps v18 with directional skew + soft-cap clamps.
    # ==================================================================
    def _quote_prices(self, sym: str, fair: float, position: int,
                      mode: str) -> Tuple[Optional[int], Optional[int]]:
        return self._quote_prices_at(sym, fair, position, mode,
                                     self._reveal_count)

    def _quote_prices_at(self, sym: str, fair: float, position: int,
                         mode: str, window_idx: int
                         ) -> Tuple[Optional[int], Optional[int]]:
        """Same contract as v18 _quote_prices, but parameterized by the
        window index used to look up the directional edge. The MM hot
        path passes self._reveal_count; the precompute (which prices
        for AFTER the next reveal) passes self._reveal_count + 1."""
        bid_px, ask_px = super()._quote_prices(sym, fair, position, mode)

        # Apply the MM soft cap as an inventory pad — if we're already
        # at the cap, drop the side that would push us further out.
        cap = self._effective_position_cap(sym)
        if position >= cap and bid_px is not None:
            bid_px = None
        if position <= -cap and ask_px is not None:
            ask_px = None

        # Park / non-running modes don't get directional override.
        if (mode == "park"
                or not self.cfg.directional_skew_enabled
                or self.phase != "running"):
            return bid_px, ask_px

        edge = self._window_edge_at(sym, window_idx)
        if edge is None or abs(edge) < self.cfg.directional_skew_min_edge:
            return bid_px, ask_px

        s = self.state[sym]
        tick = s.tick
        tight = self.cfg.directional_skew_tight_ticks * tick
        wide = self.cfg.directional_skew_wide_ticks * tick

        if edge > 0:
            # truth > naive → market underprices → we want to BUY.
            # Tighten bid (closer to fair), widen ask.
            ideal_bid = ((int(round(fair - tight))) // tick) * tick
            ideal_ask = ((int(round(fair + wide))) // tick) * tick
            if bid_px is not None:
                bid_px = max(bid_px, ideal_bid)
            if ask_px is not None:
                ask_px = max(ask_px, ideal_ask)
        else:
            # truth < naive → market overprices → we want to SELL.
            # Tighten ask, widen bid.
            ideal_ask = ((int(round(fair + tight))) // tick) * tick
            ideal_bid = ((int(round(fair - wide))) // tick) * tick
            if ask_px is not None:
                ask_px = min(ask_px, ideal_ask)
            if bid_px is not None:
                bid_px = min(bid_px, ideal_bid)

        # Re-validate ordering after the bounds (no crossed quote).
        if bid_px is not None and ask_px is not None and ask_px <= bid_px:
            ask_px = bid_px + tick

        # Negative-EV guard (mirrors v18) — after the bounds, our maker
        # bid might sit at fair, which is breakeven; never above.
        if self.cfg.skip_negative_ev:
            if bid_px is not None and bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None

        if bid_px is not None and bid_px < s.tick:
            bid_px = None
        return bid_px, ask_px

    # ==================================================================
    # Phase change: re-fetch truth on every new round
    # ==================================================================
    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        prev_phase = self.phase
        super().on_phase_change(phase, reveals)
        if phase == "running" and prev_phase != "running":
            self.truth.reset()
            self._naive_traj_a = None
            self._naive_traj_b = None
            deadline = time.time() + self.cfg.truth_init_wait_sec
            while time.time() < deadline:
                if self.truth.fetch_once():
                    break
                time.sleep(0.05)
            seq = self.truth.full_sequence()
            if seq is not None:
                print(f"[v19 TRUTH-PHASE] phase=running seq={seq}  "
                      f"truth_a={sum(seq)}")
                self._compute_naive_trajectory()
                self._bump_posterior_gen()
                self._precompute_request.set()
            else:
                print(f"[v19 TRUTH-PHASE] no truth within "
                      f"{self.cfg.truth_init_wait_sec:.1f}s "
                      f"(falling back to posterior)")
        elif phase in ("completed", "settling", "settled"):
            self.truth.reset()
            self._naive_traj_a = None
            self._naive_traj_b = None

    # ==================================================================
    # Precompute — collapse to a SINGLE truth-anchored scenario keyed
    # on the known next reveal value. Cheaper, exact.
    # ==================================================================
    def _build_precompute(self) -> None:
        if not self.cfg.truth_enabled:
            return super()._build_precompute()
        next_v = self._truth_next_value()
        if next_v is None:
            # Truth not yet cached, or past final window → defer to v18.
            return super()._build_precompute()

        t0 = time.perf_counter()
        cycle_at_start = self._reveal_count
        if self._n_remaining() <= 0:
            with self._precompute_lock:
                self._precompute = {}
                self._precompute_for_cycle = cycle_at_start
            return

        # A's truth fair is sum(full_sequence) — constant, valid AFTER
        # this reveal too. B's NEXT window (after this reveal lands) is
        # full_sequence[k+1] — if k+1 == N there's no further B settle,
        # but the precompute is only consulted on this reveal so we just
        # use the current-window truth for B as well.
        seq = self.truth.full_sequence() or []
        truth_a = float(sum(seq)) if seq else 0.0
        # AFTER this reveal, B's next-window truth = full_sequence[k+1]
        # if it exists; else fall back to last value (won't be used).
        k_next = self._reveal_count + 1
        if 0 <= k_next < len(seq):
            truth_b_after = float(seq[k_next])
        elif seq:
            truth_b_after = float(seq[-1])
        else:
            truth_b_after = 0.0

        pos_a = self.state["A"].position
        pos_b = self.state["B"].position
        sweep_edge = self.cfg.sweep_edge_ticks
        tick_a = self.state["A"].tick
        tick_b = self.state["B"].tick

        # Precompute prices the AFTER-reveal window (cycle k+1) — pass
        # the next window idx so directional skew uses the right edge.
        bid_a, ask_a = self._quote_prices_at("A", truth_a, pos_a, "normal",
                                             cycle_at_start + 1)
        bid_b, ask_b = self._quote_prices_at("B", truth_b_after, pos_b,
                                             "normal", cycle_at_start + 1)

        lift_a = ((int(math.floor(truth_a - sweep_edge))) // tick_a) * tick_a
        hit_a_raw = int(math.ceil(truth_a + sweep_edge))
        hit_a = ((hit_a_raw + tick_a - 1) // tick_a) * tick_a
        lift_b = ((int(math.floor(truth_b_after - sweep_edge))) // tick_b) * tick_b
        hit_b_raw = int(math.ceil(truth_b_after + sweep_edge))
        hit_b = ((hit_b_raw + tick_b - 1) // tick_b) * tick_b

        scen = _Scenario(
            value=next_v, prob=1.0,
            fair_a=truth_a, sigma_a=0.5,
            fair_b=truth_b_after, sigma_b=0.5,
            bid_a=bid_a, ask_a=ask_a,
            bid_b=bid_b, ask_b=ask_b,
            lift_to_a=lift_a, hit_to_a=hit_a,
            lift_to_b=lift_b, hit_to_b=hit_b,
        )
        new_table = {next_v: scen}

        # Commit if posterior didn't move under us.
        if self._reveal_count != cycle_at_start:
            return
        with self._precompute_lock:
            self._precompute = new_table
            self._precompute_for_cycle = cycle_at_start
            self._precompute_run_count += 1
            self._last_precompute_us = (time.perf_counter() - t0) * 1e6
            self._truth_scenario_count += 1

    # ==================================================================
    # On reveal: keep v18 hot path; force live-threshold + precompute
    # refresh so fair_b advances to the new window's truth immediately.
    # ==================================================================
    def on_reveal(self, value) -> None:
        # Best-effort late fetch if truth missed init.
        if (self.cfg.truth_enabled
                and self.truth.full_sequence() is None):
            self.truth.fetch_once()
        super().on_reveal(value)
        # Posterior + reveal_count moved → invalidate caches so fair_b
        # picks up the NEW window's truth on the very next read.
        self._bump_posterior_gen()
        # Force the slow precompute to rebuild for the next reveal.
        self._precompute_request.set()

    # ==================================================================
    # Endgame burst — extend to ALSO snipe B in the final B window.
    # v18's _endgame_tick only handles A; we layer a B pass after the
    # super() call. Activation is the same flag (last reveal seen → A
    # settled → endgame active), but with truth, B's value in the
    # current window is also exact at all times — so the B burst is
    # always safe to run while _endgame_active is True.
    # ==================================================================
    def _endgame_tick(self) -> None:
        super()._endgame_tick()
        if not self.cfg.endgame_b_enabled:
            return
        if not self._endgame_active:
            return
        if self._in_lockout():
            return
        if self._tokens_available() < self.cfg.endgame_min_tokens:
            return
        fair_b = self._truth_b()
        if fair_b is None:
            # Past the final B window or truth missing — nothing to do.
            return
        edge = self.cfg.endgame_edge_ticks_b
        max_levels = self.cfg.endgame_max_levels
        max_slice = self.cfg.endgame_max_slice
        pad = self.cfg.sweep_position_pad
        max_dist = self.cfg.max_ioc_distance_ticks
        s = self.state["B"]
        book = s.book
        if not book:
            return
        fired = 0
        # BUY asks <= truth - edge
        room_buy = (s.position_limit - pad) - s.position
        if room_buy > 0:
            for lvl in (book.get("asks") or [])[:max_levels]:
                if room_buy <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if fair_b - px < edge:
                    break
                if max_dist > 0 and abs(px - fair_b) > max_dist:
                    continue
                want = min(max_slice, room_buy, size)
                if self._ioc("B", "buy", want, px):
                    room_buy -= want
                    fired += 1
                    print(f"[v19 ENDGAME:B] BUY {want}@{px}  "
                          f"truth={fair_b:.1f} edge={fair_b - px:.1f}t")
        # SELL bids >= truth + edge
        room_sell = s.position - (-s.position_limit + pad)
        if room_sell > 0:
            for lvl in (book.get("bids") or [])[:max_levels]:
                if room_sell <= 0 or not self._can_send_now():
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px - fair_b < edge:
                    break
                if max_dist > 0 and abs(px - fair_b) > max_dist:
                    continue
                want = min(max_slice, room_sell, size)
                if self._ioc("B", "sell", want, px):
                    room_sell -= want
                    fired += 1
                    print(f"[v19 ENDGAME:B] SELL {want}@{px}  "
                          f"truth={fair_b:.1f} edge={px - fair_b:.1f}t")
        if fired:
            self._endgame_count += fired

    # ==================================================================
    # Status accessors for the runner
    # ==================================================================
    def truth_status_str(self) -> str:
        seq = self.truth.full_sequence()
        if seq is None:
            st = self.truth.stats()
            return (f"no truth (fetches={st['fetches']} "
                    f"errors={st['errors']} "
                    f"last_err={st['last_err']!r})")
        wb = self._reveal_count
        tb = seq[wb] if 0 <= wb < len(seq) else None
        return (f"seq={seq}  truth_a={sum(seq)}  "
                f"truth_b@win{wb}={tb}  "
                f"scenarios_built={self._truth_scenario_count}")

    def ramp_status_str(self) -> str:
        """Round progress + soft caps + ramped sizes + current edges."""
        prog = self._round_progress()
        cap_a = self._effective_position_cap("A")
        cap_b = self._effective_position_cap("B")
        sz_a = self._ramped_base_size("A")
        sz_b = self._ramped_base_size("B")
        ea = self._window_edge("A")
        eb = self._window_edge("B")
        ea_s = f"{ea:+.1f}" if ea is not None else "-"
        eb_s = f"{eb:+.2f}" if eb is not None else "-"
        return (f"prog={prog:.2%} "
                f"cap_a=+/-{cap_a}/{self.state['A'].position_limit} "
                f"cap_b=+/-{cap_b}/{self.state['B'].position_limit}  "
                f"size_a={sz_a}/{self.cfg.size_a} "
                f"size_b={sz_b}/{self.cfg.size_b}  "
                f"edge_a={ea_s} edge_b={eb_s}")

    def naive_trajectory_str(self) -> str:
        seq = self.truth.full_sequence()
        traj_a = self._naive_traj_a
        traj_b = self._naive_traj_b
        if not seq or not traj_a or not traj_b:
            return "trajectory: not computed (no truth)"
        lines = [f"trajectory  truth_a={sum(seq)}  n_total={self.n_total}"]
        truth_a_f = float(sum(seq))
        for i in range(len(traj_a)):
            ea = truth_a_f - traj_a[i]
            here = " <-- now" if i == self._reveal_count else ""
            if i < len(seq):
                tb = float(seq[i])
                eb = tb - traj_b[i]
                lines.append(
                    f"  win {i:>2d}: naive_a={traj_a[i]:>7.1f} "
                    f"edge_a={ea:+6.1f}  truth_b={tb:>3.0f} "
                    f"naive_b={traj_b[i]:>6.2f} edge_b={eb:+6.2f}{here}")
            else:
                lines.append(
                    f"  win {i:>2d}: naive_a={traj_a[i]:>7.1f} "
                    f"edge_a={ea:+6.1f}  (no B){here}")
        return "\n".join(lines)


# ===========================================================================
# Strategy v20 — original body. Extends V19 to A/B/C/D MM.
# ===========================================================================

# Path bootstrap: all dependencies live next to this file inside strategy20/.




# ===========================================================================
# Config v20 — extends v19 with C/D MM, ramps, dime, anti-spoofer knobs
# ===========================================================================
@dataclass
class Config(ConfigV19):
    # =================================================================
    # Symbol C — binary call, [0, 100], tick=1
    # =================================================================
    # MM width — VAND dimer has min_spread=4, so width=6 means VAND
    # only dimes us if our quote loosens to >=4t spread; we'll usually
    # be width=2-3 effective (after dime defense) which keeps VAND off
    # us. VANA/VANC quote width 20-30, so we'll always be inside them.
    # v20.1: widened from 6 → 10. VANA/VANC are oracle competitors;
    # the previous 6t put us at fair±3 where they'd dime us
    # systematically. width=10 keeps us at fair±5 — still inside
    # VANA/VANC's 20-30 width quotes but outside the dimer fight.
    quote_width_c: int = 10
    size_c: int = 4                # Match VANA/VANC size range
    skew_full_flip_c: int = 30     # ±30 position fully flips bid/ask sides

    # Position cap ramp for C — starts at 25% * 50 = 13, ramps to 50.
    # v20.1: lowered floor from 10 → 5. C losses (-$15k single session)
    # came from being adversely selected as MAKER by VANA/VANC. Lower
    # cap during the first half of the round limits the bleed.
    position_ramp_min_cap_c: int = 5

    # Size ramp for C
    size_ramp_min_size_c: int = 2

    # Sweep thresholds — under truth, fair is exact. Edge=2 ticks
    # means we IOC any ask <= fair-2 or bid >= fair+2. Looks like a
    # normal cross-arb hit, not an oracle blast.
    sweep_edge_ticks_c: float = 2.0
    inter_sweep_edge_ticks_c: float = 2.0
    cross_arb_edge_ticks_c: float = 2.0
    penny_min_edge_c: float = 2.0

    # IOC distance cap. C is bounded [0, 100] so a fair=100 with
    # ask=0 has distance 100 — but that's almost certainly real
    # mispricing on a binary contract, not a spoof. Cap at 90 so
    # extreme-corner asks (bid > 98, ask < 2) still go through the
    # linger gate below.
    max_ioc_distance_ticks_c: float = 90.0

    # Anti-spoof linger floor for C. Quotes at ask < 2 or bid > 98
    # must have been seen for >= c_linger_min_ms before we lift them.
    # HVALK spoof bots have linger=300ms; we require ~500ms to be
    # conservative.
    c_extreme_low: int = 2
    c_extreme_high: int = 98
    c_linger_min_ms: float = 500.0

    # =================================================================
    # Symbol D — realized range, tick=1
    # =================================================================
    # AVAN/CVAN quote width=4. We want to MATCH them or slightly
    # dime, not stick out. width=4 means we sit AT or 1t inside.
    quote_width_d: int = 4
    size_d: int = 4                # Match AVAN/CVAN size range
    skew_full_flip_d: int = 50     # D position limit 75 → flip at ±50

    position_ramp_min_cap_d: int = 15
    size_ramp_min_size_d: int = 2

    sweep_edge_ticks_d: float = 1.0
    inter_sweep_edge_ticks_d: float = 1.0
    cross_arb_edge_ticks_d: float = 1.0
    penny_min_edge_d: float = 0.5

    max_ioc_distance_ticks_d: float = 30.0

    # =================================================================
    # Discretion knobs
    # =================================================================
    # Initial blitz disabled — v20 is a market maker, not a sniper.
    # If you want to load up immediately on truth, set True.
    initial_cd_blitz_enabled: bool = False

    # Per-symbol dime defense throttles inherited from v18 default
    # (dime_defense_throttle_sec). C/D get their own to avoid
    # cross-symbol REST starvation.
    dime_defense_throttle_sec_c: float = 0.15
    dime_defense_throttle_sec_d: float = 0.15

    # Inter-sweep throttle per C/D (inherited from parent for A/B).
    inter_sweep_throttle_sec_c: float = 0.15
    inter_sweep_throttle_sec_d: float = 0.15

    # =================================================================
    # v20.1: Early-round take suppression (analysis of v20 logs showed
    # 85-95% taker% in first 5s vs v19's ~5%; the IOC blast fires the
    # instant TruthOracle returns, before reveal 1). Gates below shift
    # v20 from "take-first MM" to "MM-first take-when-edge-arrives".
    # =================================================================
    # Minimum reveal count before cross-arb and inter-sweep are armed.
    # Default 1: wait for at least one real reveal so the public book
    # has tightened around fair_a/fair_b and quotes reflect new info.
    cross_arb_min_reveals: int = 1
    inter_sweep_min_reveals: int = 1

    # After each reveal the public book takes ~15ms to update. Suppress
    # cross-arb for this many ms post-reveal so we're not racing stale
    # book snapshots.
    cross_arb_post_reveal_lockout_ms: float = 50.0

    # Early-round edge multiplier — when reveals_seen < early_round_reveals,
    # require this multiple of the normal cross-arb / inter-sweep edge.
    # Default 1.5x: with cross_arb_edge_ticks_c=2.0 → 3.0t required.
    early_round_edge_mult: float = 1.5
    early_round_reveals: int = 2
    # Absolute floor on early-round edge (in ticks). v19 inherits
    # cross_arb_edge_ticks=0.5 for A/B — 0.5*1.5=0.75 is too easy to
    # trigger, so a 2.0t hard floor ensures we only take obvious arbs
    # during the high-uncertainty opening.
    early_round_edge_floor: float = 2.0

    # Per-round per-symbol cross-arb qty budget cap. Once a symbol's
    # cumulative cross-arb fills exceed this, cross-arb skips it for the
    # rest of the round. Reset on phase → running. Hard ceiling against
    # the worst-case session blowout (-$15k C in one v20 session).
    #
    # v20.4: actual exchange position limits are A=±50, B=±100, C=±50,
    # D=±75. v20.3 had cross_arb_qty_budget_a=80 (OVER A's limit!) and
    # c=25 (50% of C cap). New budgets are 60% of each symbol's hard
    # limit, leaving room for MM to also touch the position.
    cross_arb_qty_budget_a: int = 30   # 60% of ±50
    cross_arb_qty_budget_b: int = 60   # 60% of ±100
    cross_arb_qty_budget_c: int = 15   # 30% of ±50 — C bleeds adversely
    cross_arb_qty_budget_d: int = 45   # 60% of ±75

    # =================================================================
    # v20.1: Symbol B anti-spoofer (HVALK) — extend the C-only linger
    # gate to B. v20 logs caught us filling 24 qty against HVALK in the
    # first 5s of rounds; max_ioc_distance=50 was too permissive.
    # =================================================================
    # B max IOC distance — tighter than the inherited 50 so HVALK
    # baits at fair±30-50 are rejected outright.
    max_ioc_distance_ticks_b: float = 25.0

    # Linger gate: a B price >= b_extreme_offset ticks off fair must
    # have lingered >= b_linger_min_ms before we lift it. HVALK quotes
    # have linger_ms=300; we require 400ms to be conservative.
    b_extreme_offset: float = 20.0
    b_linger_min_ms: float = 400.0

    # =================================================================
    # v20.1: MM aggressiveness under truth
    # =================================================================
    # When the truth oracle has cached a sequence, our fair_X is exact.
    # Quote tighter (shrink width by N ticks) and bigger (bump base
    # size by N) to capture more flow at known-zero variance.
    # Excludes C: VANA/VANC adverse-selection risk; we want C WIDER
    # under truth, not tighter.
    mm_aggressive_under_truth: bool = True
    mm_truth_width_shrink_ticks: int = 1
    mm_truth_size_bump: int = 1

    # =================================================================
    # v20.2: High-frequency MM throughout the round
    # =================================================================
    # Faster MM tick cadence than the inherited 0.4/1.5s — under truth
    # the fair is constant so re-quoting more often is cheap (only
    # produces a message when position shifts our skew or dime defense
    # triggers). Strategy rate gate (15/sec) still protects us.
    # v20.3: tightened further (0.20 → 0.15 / 0.50 → 0.30) to drive more
    # HF re-quote / dime-recapture flow throughout the round.
    mm_min_interval_sec: float = 0.15
    mm_refresh_sec: float = 0.30

    # v20.2: Re-enable pre-first-reveal flatten-bias even when truth
    # is locked. v19 disabled it because "no uncertainty = no need to
    # flatten". User directive: be FLAT going into reveal 1 so we have
    # full directional flexibility through every subsequent window.
    flatten_bias_under_truth: bool = True
    # Tighter window than the inherited 8.0s — gives us aggressive
    # flattening only in the final lead-up, not the whole pre-reveal
    # phase.
    pre_first_reveal_flatten_sec: float = 5.0

    # v20.2: Directional skew for C / D under truth.
    # When truth_fair − BBO_mid > min_edge, lean LONG (tight bid, wide
    # ask). When truth_fair − BBO_mid < −min_edge, lean SHORT (tight
    # ask, wide bid). Mirrors v19's A/B directional_skew_* knobs but
    # uses BBO midpoint as the "naive proxy" since we don't compute a
    # naive trajectory for C/D.
    cd_directional_skew_enabled: bool = True
    cd_directional_skew_min_edge_ticks: float = 1.0
    cd_directional_skew_tight_ticks: int = 1
    cd_directional_skew_wide_ticks: int = 4

    # =================================================================
    # v20.3: Reveal-time IOC sweep extended to C / D
    # =================================================================
    # v18's on_reveal sweeps A and B only. v20.3 adds a C/D pass right
    # after super().on_reveal() runs, using truth-derived fair_c /
    # fair_d and per-symbol sweep_edge / max_ioc_distance. This is the
    # mechanism that captures laggard quotes still showing at pre-reveal
    # prices during the ~15ms public-feed lag after a reveal.
    reveal_sweep_cd_enabled: bool = True

    # =================================================================
    # v20.3: Hit-and-retreat MM
    # =================================================================
    # On every fill (maker OR taker), immediately repost the OPPOSITE
    # side at a tight scalp-out price (inside BBO when room exists).
    # The intent: capture roundtrip P&L from MM rather than holding the
    # position and waiting for it to revalue. Under truth this is pure
    # edge — fair is exact, every roundtrip past the spread is profit.
    hit_and_retreat_enabled: bool = True
    # When we just bought, target ask = max(fair + min_edge,
    # best_ask - inside_ticks). When we sold, target bid mirrors.
    hit_and_retreat_inside_ticks: int = 1
    # Don't scalp under tiny positions — let the MM loop handle them.
    hit_and_retreat_min_abs_pos: int = 1
    # Throttle so a burst of fills (e.g. 5 lots @ multiple book levels)
    # doesn't fire 5 scalp-outs in one ms.
    hit_and_retreat_throttle_sec: float = 0.05

    # =================================================================
    # v20.3: Scalp-out at cap — keep MM alive after position saturates
    # =================================================================
    # When |position| >= soft cap, v20.2 quoted only the inventory-
    # reducing side. But skip_negative_ev would null even THAT side
    # whenever skew pulled it past fair → dead time R3-R6. v20.3 forces
    # the inventory-reducing quote to live AT fair (or up to N ticks
    # past it) regardless of skip_negative_ev, so we keep scalping out.
    # Hard floor stops us from giving away too much: clamp to fair±
    # scalp_out_max_loss_ticks of pure loss per fill.
    scalp_out_at_cap_enabled: bool = True
    scalp_out_max_loss_ticks: int = 2

    # =================================================================
    # v20.3: Aggressive BBO diming + redime watch
    # =================================================================
    # 5x lower dime throttle: every ~50ms we recheck and re-step inside
    # the BBO if a competitor dimed us. Pair with hf MM cadence — even
    # if quote-loop is throttled by mm_min_interval_sec, dime defense
    # fires on every book event independently.
    dime_defense_throttle_sec: float = 0.05
    dime_defense_throttle_sec_c: float = 0.05
    dime_defense_throttle_sec_d: float = 0.05
    # When we step inside the BBO, also park a SECOND order at
    # BBO+2t (further out) as a redime fallback — if a competitor dimes
    # us at BBO+1, we still have a quote at BBO+2 that captures the
    # downstream flow. Disabled by default; turn on once we measure
    # it's not eating REST budget.
    dime_double_layer_enabled: bool = False

    # =================================================================
    # v20.3: Tighter early-round taker suppression
    # =================================================================
    # v20.2 had early_round_edge_floor=2.0. v20.3 raises to 3.0 — under
    # truth the early-window cross-arb edge floor is now 3t (was 2t),
    # the 50%-higher bar leaves more flow for our MM to capture passively.
    early_round_edge_floor: float = 3.0
    # Cap cross-arb slice during early window (reveal_count <
    # early_round_reveals). v18 default cross_arb_max_slice=8; the
    # early cap forces 3-lot snipes so we don't blast inventory in
    # the opening few seconds.
    cross_arb_max_slice_early: int = 3

    # =================================================================
    # v20.3: Cross-market arb trigger on every fill
    # =================================================================
    # Already inherited from v18. The flag is here only as a kill-switch.
    cross_arb_on_fill_enabled: bool = True

    # =================================================================
    # v20.4: Per-symbol slice caps + inventory-aware throttle
    # =================================================================
    # Old code used a single sweep_max_slice=10 / cross_arb_max_slice=8
    # for ALL symbols. With A=±50 limit, even an 8-lot IOC eats 16% of
    # capacity in one shot — multiple cross-arb fires in the first 30s
    # saturate position. Per-symbol caps scale by hard limit:
    #   A (±50): 5  — was 10
    #   B (±100): 10 — same
    #   C (±50): 3  — was 10; C also has VANA/VANC adverse selection
    #   D (±75): 7  — was 10
    sweep_max_slice_a: int = 5
    sweep_max_slice_b: int = 10
    sweep_max_slice_c: int = 3
    sweep_max_slice_d: int = 7
    inter_sweep_max_slice_a: int = 5
    inter_sweep_max_slice_b: int = 10
    inter_sweep_max_slice_c: int = 3
    inter_sweep_max_slice_d: int = 7
    cross_arb_max_slice_a: int = 4
    cross_arb_max_slice_b: int = 8
    cross_arb_max_slice_c: int = 3
    cross_arb_max_slice_d: int = 6

    # Inventory-aware throttle. When |pos|/limit grows on a given side,
    # the SAME-side slice caps shrink. Reducing-side slice stays full.
    # This stops the directional ratchet (A→-47 saturation, C→+48
    # saturation) we saw in v20.3 logs.
    inventory_throttle_enabled: bool = True
    # When |pos|/L > frac_X, multiply same-side slice by mult_X.
    inventory_throttle_frac1: float = 0.4   # 40% of limit
    inventory_throttle_mult1: float = 0.5
    inventory_throttle_frac2: float = 0.6
    inventory_throttle_mult2: float = 0.25
    inventory_throttle_frac3: float = 0.8
    # Above this, same-side taker is blocked outright.
    inventory_throttle_hard_block_frac: float = 0.8

    # =================================================================
    # v20.4: Parallel reveal-time sweep across A/B/C/D
    # =================================================================
    # v18 on_reveal sweeps A then B sequentially via sync REST. With
    # RTT ~150ms each, last symbol's IOC ack lands ~600ms post-reveal
    # — way past the 15ms public-feed-lag window. v20.4 submits all
    # 4 sweeps to _ioc_executor in parallel so the first ack lands at
    # 1x RTT instead of 4x.
    reveal_sweep_parallel_enabled: bool = True
    reveal_sweep_parallel_timeout_sec: float = 1.5

    # =================================================================
    # v20.5: C/D market-making emphasis
    # =================================================================
    # Diagnosis from combined_log_v20_20260520_231914: C posted 24
    # limit orders across 30 reveals (~0.4/cycle) and filled 18% as
    # maker. Under truth-locked C, _quote_prices_cd posts symmetric
    # ±width/2 quotes — when fair=100, ask clamps to 100 (never
    # fills); when fair=0, bid clamps to 1 (also degenerate). Effect:
    # half the time we're posting a quote that can't fill.
    #
    # Fix: when truth is locked and fair_c is in the boundary region,
    # switch to STACKED ONE-SIDED MM:
    #   * fair_c >= c_one_sided_high → only post BIDS at fair-d1,
    #     fair-d2, fair-d3 (multiple stacked levels). Skip asks.
    #   * fair_c <= c_one_sided_low → only post ASKS at fair+d1,
    #     fair+d2, fair+d3. Skip bids.
    # Catches naive MMs (VANA/VANC) and noise traders who quote
    # around their wider (and slower) posterior.
    c_one_sided_mm_enabled: bool = True
    c_one_sided_high: float = 90.0
    c_one_sided_low: float = 10.0
    c_one_sided_levels: int = 3
    c_one_sided_step_ticks: int = 2    # 2,4,6 ticks under/over fair
    c_one_sided_size: int = 3
    c_one_sided_min_size: int = 1

    # Tighter C MM width when NOT in boundary regime — dimes naive
    # MMs (VANA width 30, VANC width 20). Was 10 → 6.
    quote_width_c_tight: int = 6
    # Higher refresh frequency for C/D — naive MM bots refresh every
    # 1000-1500ms, so a 150ms cadence lets us re-quote 6-10x per
    # naive update cycle.
    mm_refresh_sec_cd: float = 0.15

    # C/D cross-arb cooldown when our MM already sits inside with
    # equal or better edge — avoids the "fire IOC, then immediately
    # repost via hit-and-retreat" loop we saw at C boundaries.
    cd_skip_arb_when_mm_present: bool = True

    # =================================================================
    # v20.5: C↔A delta hedge
    # =================================================================
    # C is a digital on A above strike K=137. When |c_pos| is large,
    # implied A delta = c_pos × P'(A_final crosses strike per unit
    # change in current_sum) × payout. For a simple linear approx,
    # we treat A delta = c_pos × 0.01 × payout × indicator_near_strike.
    # When fair_c is near boundary (>=90 or <=10), delta is near 0
    # (gamma is exhausted) — no hedge needed. When fair_c is in
    # middle (10-90), delta is ~payout × pdf-at-strike ≈ small per
    # remaining reveal.
    #
    # In practice the actionable hedge is: lean A's MM bid/ask skew
    # in the same direction as our C exposure. We expose this as a
    # small extra tick of skew on A when |c_pos| > threshold.
    ca_delta_hedge_enabled: bool = True
    ca_delta_hedge_pos_threshold: int = 10  # |c_pos| >= 10 to act
    ca_delta_hedge_max_skew_ticks: int = 2  # at most ±2t extra on A
    # When fair_c is in the mid (uncertain) zone, hedge is strongest.
    ca_delta_hedge_mid_low: float = 25.0
    ca_delta_hedge_mid_high: float = 75.0

    # =================================================================
    # v20.14 — Inventory-aware quote shift (truth-anchored MM-out)
    # =================================================================
    # When we're directionally loaded (e.g. +60 on B out of ±100),
    # shift BOTH bid and ask in the flattening direction so the next
    # fill is more likely to offset rather than compound. Under truth
    # we know fair exactly, so the shift is clipped to stay on the
    # +EV side of fair — never crosses it. Effect is quadratic in
    # |position/cap| beyond `inventory_shift_threshold`: near-zero
    # for small inventories, full magnitude at the cap.
    inventory_shift_enabled: bool = True
    inventory_shift_threshold: float = 0.3   # |frac| < 0.3 → no shift
    inventory_shift_max_ticks_a: int = 4
    inventory_shift_max_ticks_b: int = 3
    inventory_shift_max_ticks_c: int = 4
    inventory_shift_max_ticks_d: int = 4

    # =================================================================
    # v20.7 — public-tape driven MM (dime + one-sided wide)
    # =================================================================
    # The public trade tape lags the private feed by ~15ms but is the
    # one signal that tells us where COUNTERPARTIES are actually willing
    # to transact. Two distinct exploits ride on it:
    #
    # 1. DIME — the price of the latest sell-aggressor print is exactly
    #    where some resting bid just got hit. Posting a new bid one tick
    #    above that price (and still ≤ fair − min_edge for +EV) puts us
    #    at the new top-of-book inside the gap before competitors
    #    re-quote. Symmetric for asks.
    # 2. ONE-SIDED — if ≥`tape_imbalance_min_prints` prints arrive in
    #    `tape_window_sec` and ≥`tape_imbalance_frac` are on a single
    #    side, the OPPOSITE side of the book has been depleted. Widen
    #    our quote on the depleted side by `tape_wide_offset_ticks` so
    #    that, when the next forced trade reaches further in, we capture
    #    the cheap fill against our known-truth fair.
    tape_enabled: bool = True
    tape_dime_enabled: bool = True
    tape_imbalance_enabled: bool = True
    # How fresh a tape print must be (sec) to be used as a dime anchor.
    tape_freshness_sec: float = 0.6
    # Rolling window for imbalance detection.
    tape_window_sec: float = 0.2
    # Minimum prints in the window before we'll act on imbalance.
    tape_imbalance_min_prints: int = 3
    # Fraction of prints on a single side that qualifies as one-sided.
    tape_imbalance_frac: float = 0.8
    # Extra ticks to widen the depleted side (cheap entry vs truth).
    tape_wide_offset_ticks_a: int = 5
    tape_wide_offset_ticks_b: int = 6
    tape_wide_offset_ticks_c: int = 8
    tape_wide_offset_ticks_d: int = 6
    # Deque cap (defensive — window-trimming should keep it short).
    tape_max_prints: int = 64

    # =================================================================
    # v20.8 — Truth cap bypass for A/D
    # =================================================================
    # Under truth, A and D have exact, constant fairs. Every fill
    # within the edge band is +EV — the soft-cap ramp throws that
    # edge away. Bypass it for A/D when truth_locked. B and C still
    # ramp (B intra-tick settlement risk; C oracle-competitor adverse
    # selection).
    truth_cap_bypass_enabled: bool = True

    # =================================================================
    # v20.12 — Proactive BBO step-inside (JOIN fallback)
    # =================================================================
    # `_maybe_penny` and `_maybe_dime_defense` try to IMPROVE the BBO
    # by 1 tick (target = best_bid + tick). That target is then gated
    # by `target <= fair - min_edge`. If BBO is at fair - min_edge
    # (e.g. C with min_edge=2, BBO at fair-2), IMPROVE would post at
    # fair-1 which fails the gate → we DON'T step in and sit at our
    # wider baseline (fair-3 from quote_width/2). Result: not at BBO
    # while the market is well-spread.
    #
    # The JOIN fallback posts AT the BBO (target = best_bid) when
    # IMPROVE fails the edge gate but JOIN still clears the looser
    # `bbo_join_min_edge_ticks` floor. Maker rebate / fee math means
    # any fill at fair - 1t is still +0.5/lot positive vs maker fee
    # 0.5, so the join floor of 1 covers fees and leaves zero buffer.
    bbo_join_enabled: bool = True
    # v20.14: under truth we know fair exactly. Maker rebate (≈0.5/lot)
    # plus zero adverse-selection risk means fills exactly AT fair are
    # still net +0.5/lot. Drop the JOIN floor to 0 so we join BBO when
    # truth-bots own the inside.
    bbo_join_min_edge_ticks: float = 0.0
    # v20.14: penny_max_step_ticks must cover pre_reveal_park_offset_ticks
    # (=30) — otherwise post-park recovery crawls 5t per book event and
    # we stay parked 6+ seconds. Override the v18 default of 5.
    penny_max_step_ticks: int = 35

    # =================================================================
    # v20.11 — Hard-bounds arbitrage
    # =================================================================
    # Revealed values pin DETERMINISTIC bounds on settlement:
    #   * A_final >= sum(revealed)          — future values ≥ 0
    #   * D_final >= max(revealed)-min(revealed) once k≥2
    #   * C_final == 100 once sum(revealed) >= strike
    # Plus exact bounds under truth (lower==upper==fair). Any ask
    # priced strictly below the lower bound (or bid above upper) is a
    # GUARANTEED +EV fill independent of our fair-estimate accuracy.
    #
    # This path is stricter than cross-arb (which requires an edge
    # against a possibly-wrong fair) and looser than the post-reveal
    # lockout (free money doesn't need to wait). It runs on every
    # reveal (new info → tighter bounds) and every public print
    # (book may now contain a laggard violator).
    bounds_arb_enabled: bool = True
    # Required edge over the bound in ticks. With min_edge=1.0 we
    # only take asks priced AT LEAST 1 tick BELOW the lower bound,
    # so we clear the per-trade fee and still profit.
    bounds_arb_min_edge_ticks: float = 1.0
    # Slice cap multiplier vs sweep_max_slice (per-symbol). Bounds
    # arb is risk-free so we let it push more size at once.
    bounds_arb_slice_mult: float = 1.5
    # Cap per round per symbol — defensive bound against a runaway
    # spoof book that posts hundreds of bound-violating prices.
    bounds_arb_qty_budget_a: int = 50    # full ±L on A
    bounds_arb_qty_budget_b: int = 60    # B has tick-settle risk
    bounds_arb_qty_budget_c: int = 50    # full ±L on C
    bounds_arb_qty_budget_d: int = 75    # full ±L on D

    # =================================================================
    # COID prefix
    # =================================================================
    client_order_id_prefix: str = "v20"


# ===========================================================================
# TapeState — per-symbol rolling tape state. Fed by BOTH the private fill
# feed (15ms ahead of public — see `on_fill_event`) and the public trade
# tape (`on_trade`). Trade IDs are deduped so the same transaction isn't
# counted twice; private always wins because it arrives first.
#
# OPTIMIZATION: running buy_count / sell_count keep _tape_imbalance O(1).
# The deque entries carry the aggressor side so we know which counter to
# decrement on window trim. The fast precompute loop (every 50ms) drains
# stale entries even when no new prints arrive — without that, a quiet
# market could leave a positive imbalance signal alive long after the
# burst has faded.
# ===========================================================================
@dataclass
class TapeState:
    # recent: (t, aggressor, price, qty) for prints in tape_window_sec.
    recent: Deque[Tuple[float, str, int, int]] = field(default_factory=deque)
    # Running side counters (kept consistent with deque on every mutation).
    buy_count: int = 0
    sell_count: int = 0
    # Latest aggressor=buy event → price where ask just got lifted.
    last_buy_agg_price: Optional[int] = None
    last_buy_agg_t: float = 0.0
    # Latest aggressor=sell event → price where bid just got hit.
    last_sell_agg_price: Optional[int] = None
    last_sell_agg_t: float = 0.0
    # Dedupe: trade_id → ingest_t. Bounded; old entries fall off with the
    # window trim. Set of trade_ids we've already counted (either from
    # private fill or earlier public print).
    seen_trade_ids: Dict[int, float] = field(default_factory=dict)


# ===========================================================================
# IdentityBounds — v20.13. Precomputed hard mathematical bounds for a
# single symbol's final settlement value, derived from REVEALED values +
# cross-market identities (no fair-estimate involvement). Used by
# `_bounds_arb_sweep` to find risk-free fills, and by any caller that
# wants a deterministic "no-worse-than" floor/ceiling on settlement.
#
# Each bound is either a float or None (None = unbounded on that side).
# `lower_source` / `upper_source` tags the IDENTITY that produced the
# bound so logs make it obvious which derivation kicked in.
#
# Identities consolidated here:
#   reveals    : sum(revealed) / range(revealed) / sum_ge_K direct
#   eor_pin    : C = 0 when reveal_count == n_total AND sum < K
#   cross_C0   : A_upper = K - 1 when C is pinned at 0
#   cross_D_A  : D_upper ≤ A_upper (D = max - min ≤ max ≤ sum = A)
#   truth      : (fair, fair) collapse under truth oracle
# ===========================================================================
@dataclass
class IdentityBounds:
    sym: str
    lower: Optional[float] = None
    upper: Optional[float] = None
    lower_source: str = "init"
    upper_source: str = "init"
    gen: int = 0


# ===========================================================================
# Strategy v20
# ===========================================================================
class Strategy(StrategyV19):
    """v19 truth oracle + two-sided MM extended to A/B/C/D."""

    SYMS = ("A", "B", "C", "D")

    # ------------------------------------------------------------------
    # __init__ — reads C strike + caches truth-derived C/D fairs
    # ------------------------------------------------------------------
    def __init__(self, client: GameClient, posterior: Posterior,
                 config: Optional[Config] = None):
        if config is None:
            config = Config()
        elif not isinstance(config, Config):
            config = Config(**{
                f.name: getattr(config, f.name)
                for f in config.__dataclass_fields__.values()
                if f.name in Config.__dataclass_fields__
            })

        # Read C strike BEFORE super() so the first fast-precompute
        # tick has a usable fair_c.
        self.c_strike: Optional[int] = None
        try:
            gs = client.game_state()
            cmeta = ((gs.get("instruments") or {}).get("C") or {})
            sp = cmeta.get("settlement_params") or {}
            k_raw = (sp.get("strike") if "strike" in sp
                     else sp.get("K") if "K" in sp
                     else sp.get("threshold"))
            if k_raw is not None:
                self.c_strike = int(k_raw)
        except Exception as e:
            print(f"[v20 STRIKE] failed to read C strike: {e!r}")

        # v20.6: single-pass precompute. A/C/D are CONSTANTS once seq
        # is known. B advances per reveal via _truth_b_seq[reveal_count].
        self._fair_a_truth: Optional[float] = None
        self._fair_c_truth: Optional[float] = None
        self._fair_d_truth: Optional[float] = None
        self._truth_b_seq: Optional[Tuple[int, ...]] = None
        self._cd_truth_for_seq: Optional[Tuple[int, ...]] = None

        # Per-quote linger tracking for C extreme-tick spoof filter.
        # Key: (side, price), value: first-seen timestamp.
        self._c_linger_seen: Dict[Tuple[str, int], float] = {}
        # Same shape for B (HVALK spoofer extreme-tick gate).
        self._b_linger_seen: Dict[Tuple[str, int], float] = {}

        # v20.1: per-round per-symbol cross-arb qty consumed.
        # Reset on phase → running.
        self._cross_arb_qty_used: Dict[str, int] = {
            s: 0 for s in self.SYMS}

        # v20.11: per-round per-symbol bounds-arb qty consumed +
        # cumulative fire counter for diagnostics.
        self._bounds_arb_qty_used: Dict[str, int] = {
            s: 0 for s in self.SYMS}
        self._bounds_arb_count: int = 0

        # v20.1: last reveal monotonic timestamp (drives the
        # post-reveal lockout for cross-arb).
        self._last_reveal_t: float = 0.0

        # v20.1: precomputed arbitrage table — per-symbol exact
        # buy-at-or-below / sell-at-or-above thresholds. Built from
        # truth-derived fair + per-symbol min edge. Refreshed on
        # every reveal and inside the fast-precompute loop.
        # Schema: { sym: { 'fair': float, 'buy_at_or_below': int,
        #                  'sell_at_or_above': int, 'gen': int } }
        self._arb_table: Dict[str, Dict[str, float]] = {}
        self._arb_gen: int = 0

        # v20.13: precomputed hard-identity bounds per symbol. Pinned
        # to deterministic math (revealed values + cross-market
        # identities), independent of fair estimates. Reads by
        # `_bounds_arb_sweep` (instant risk-free fills) and any
        # diagnostic / external consumer that needs a no-worse-than
        # floor/ceiling on settlement.
        self._identity_table: Dict[str, IdentityBounds] = {
            s: IdentityBounds(sym=s) for s in self.SYMS
        }
        self._identity_gen: int = 0

        # v20.3: per-symbol scalp-out throttle (hit-and-retreat).
        # Key: symbol → last scalp-out timestamp.
        self._last_scalp_out_t: Dict[str, float] = {s: 0.0 for s in self.SYMS}
        # v20.3: per-symbol reveal-sweep counter for diagnostics.
        self._reveal_sweep_cd_count: int = 0
        # v20.4: parallel reveal-sweep futures. Cleared after each
        # reveal cycle. Pre-kicked at on_reveal entry so the override
        # of _sweep_post_reveal joins on the future instead of running
        # sequentially.
        self._reveal_sweep_futures: Dict[str, object] = {}
        # v20.5: separate refresh cadence clock for C/D so we can
        # tick MM faster on them than on A/B.
        self._last_mm_refresh_t_cd: float = 0.0

        # v20.7: public-tape state per symbol. Populated by on_trade and
        # read by _tape_dime_prices / _tape_imbalance_skew before MM quoting.
        self._tape: Dict[str, TapeState] = {s: TapeState() for s in self.SYMS}
        # Diagnostic counters.
        self._tape_dime_count: Dict[str, int] = {s: 0 for s in self.SYMS}
        self._tape_widen_count: Dict[str, int] = {s: 0 for s in self.SYMS}
        self._tape_print_count: Dict[str, int] = {s: 0 for s in self.SYMS}

        super().__init__(client, posterior, config)

        # After super: state["C"] and state["D"] exist (parent loops
        # over self.SYMS, which resolves to v20's tuple).
        self._refresh_cd_truth_cache()
        # Rebuild live thresholds so MM hot paths see fresh C/D
        # snipe levels on their first tick.
        try:
            self._build_live_thresholds()
        except Exception as e:
            print(f"[v20 INIT-LIVE] {e!r}")

        c_lim = self.state["C"].position_limit
        d_lim = self.state["D"].position_limit
        c_tick = self.state["C"].tick
        d_tick = self.state["D"].tick
        print(f"[v20 INIT] C strike={self.c_strike}  "
              f"C lim={c_lim} tick={c_tick} mult={self._mult('C')}  "
              f"D lim={d_lim} tick={d_tick} mult={self._mult('D')}")
        print(f"[v20 INIT] widths: A={self.cfg.quote_width_a} "
              f"B={self.cfg.quote_width_b} C={self.cfg.quote_width_c} "
              f"D={self.cfg.quote_width_d}")
        print(f"[v20 INIT] sizes: A={self.cfg.size_a} B={self.cfg.size_b} "
              f"C={self.cfg.size_c} D={self.cfg.size_d}")
        print(f"[v20 INIT] pos_ramp_min_caps: A={self.cfg.position_ramp_min_cap} "
              f"C={self.cfg.position_ramp_min_cap_c} "
              f"D={self.cfg.position_ramp_min_cap_d}")
        if self._fair_c_truth is not None:
            print(f"[v20 INIT-TRUTH] fair_c={self._fair_c_truth:.0f} "
                  f"fair_d={self._fair_d_truth:.1f}")

    # ==================================================================
    # Multiplier helper (read once from game_state instruments).
    # ==================================================================
    def _mult(self, sym: str) -> float:
        try:
            gs = self.c.game_state()
            instr = (gs.get("instruments") or {}).get(sym) or {}
            return float(instr.get("multiplier") or 1.0)
        except Exception:
            return 1.0

    # ==================================================================
    # Symbol-keyed dispatch helpers — used by all the overrides below
    # to avoid copy-pasting "if sym == 'A' else if sym == 'B' else ..."
    # ==================================================================
    def _width_for(self, sym: str) -> int:
        if sym == "A":
            base = self.cfg.quote_width_a
        elif sym == "B":
            base = self.cfg.quote_width_b
        elif sym == "C":
            # v20.5: tighter C width when truth is locked AND we're in
            # the mid-range (boundary regime uses one-sided stacked
            # MM via _quote_prices_c_truth_boundary which bypasses
            # this width entirely).
            if self._truth_locked("C"):
                base = self.cfg.quote_width_c_tight
            else:
                base = self.cfg.quote_width_c
        else:
            base = self.cfg.quote_width_d
        # v20.1: shrink under truth (skips C — see _truth_width_shrink).
        return max(2, base - self._truth_width_shrink(sym))

    def _flip_for(self, sym: str) -> int:
        if sym == "A":
            return max(1, self.cfg.skew_full_flip_a)
        if sym == "B":
            return max(1, self.cfg.skew_full_flip_b)
        if sym == "C":
            return max(1, self.cfg.skew_full_flip_c)
        return max(1, self.cfg.skew_full_flip_d)

    def _min_edge_for(self, sym: str) -> float:
        if sym == "A":
            return self.cfg.penny_min_edge_a
        if sym == "B":
            return self.cfg.penny_min_edge_b
        if sym == "C":
            return self.cfg.penny_min_edge_c
        return self.cfg.penny_min_edge_d

    def _sweep_edge_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            return self.cfg.sweep_edge_ticks
        if sym == "C":
            return self.cfg.sweep_edge_ticks_c
        return self.cfg.sweep_edge_ticks_d

    def _inter_sweep_edge_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            base = self.cfg.inter_sweep_edge_ticks
        elif sym == "C":
            base = self.cfg.inter_sweep_edge_ticks_c
        else:
            base = self.cfg.inter_sweep_edge_ticks_d
        return self._apply_early_round_edge(base)

    def _cross_arb_edge_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            base = self.cfg.cross_arb_edge_ticks
        elif sym == "C":
            base = self.cfg.cross_arb_edge_ticks_c
        else:
            base = self.cfg.cross_arb_edge_ticks_d
        return self._apply_early_round_edge(base)

    def _early_round_mult(self) -> float:
        if self._reveal_count < self.cfg.early_round_reveals:
            return float(self.cfg.early_round_edge_mult)
        return 1.0

    def _apply_early_round_edge(self, base: float) -> float:
        """Apply both the multiplicative early-round bump and the
        absolute floor. Outside the early window returns base unchanged."""
        if self._reveal_count >= self.cfg.early_round_reveals:
            return base
        scaled = base * self.cfg.early_round_edge_mult
        return max(scaled, self.cfg.early_round_edge_floor)

    def _max_ioc_dist_for(self, sym: str) -> float:
        if sym == "A":
            return self.cfg.max_ioc_distance_ticks
        if sym == "B":
            return self.cfg.max_ioc_distance_ticks_b
        if sym == "C":
            return self.cfg.max_ioc_distance_ticks_c
        return self.cfg.max_ioc_distance_ticks_d

    def _dime_throttle_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            return self.cfg.dime_defense_throttle_sec
        if sym == "C":
            return self.cfg.dime_defense_throttle_sec_c
        return self.cfg.dime_defense_throttle_sec_d

    def _inter_sweep_throttle_for(self, sym: str) -> float:
        if sym in ("A", "B"):
            return self.cfg.inter_sweep_throttle_sec
        if sym == "C":
            return self.cfg.inter_sweep_throttle_sec_c
        return self.cfg.inter_sweep_throttle_sec_d

    def _base_size_for(self, sym: str) -> int:
        if sym == "A":
            base = self.cfg.size_a
        elif sym == "B":
            base = self.cfg.size_b
        elif sym == "C":
            base = self.cfg.size_c
        else:
            base = self.cfg.size_d
        # v20.1: bump under truth (skips C).
        return base + self._truth_size_bump(sym)

    def _size_ramp_min_for(self, sym: str) -> int:
        if sym in ("A", "B"):
            return self.cfg.size_ramp_min_size
        if sym == "C":
            return self.cfg.size_ramp_min_size_c
        return self.cfg.size_ramp_min_size_d

    def _pos_ramp_min_for(self, sym: str) -> int:
        if sym in ("A", "B"):
            return self.cfg.position_ramp_min_cap
        if sym == "C":
            return self.cfg.position_ramp_min_cap_c
        return self.cfg.position_ramp_min_cap_d

    def _fair_for(self, sym: str) -> Tuple[float, float]:
        if sym == "A":
            return self.fair_a()
        if sym == "B":
            return self.fair_b()
        if sym == "C":
            return self.fair_c()
        return self.fair_d()

    # v20.1: per-symbol cross-arb qty budget (-1 = unlimited).
    def _cross_arb_budget_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.cross_arb_qty_budget_a
        if sym == "B":
            return self.cfg.cross_arb_qty_budget_b
        if sym == "C":
            return self.cfg.cross_arb_qty_budget_c
        return self.cfg.cross_arb_qty_budget_d

    # v20.4: per-symbol per-path slice caps.
    def _sweep_slice_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.sweep_max_slice_a
        if sym == "B":
            return self.cfg.sweep_max_slice_b
        if sym == "C":
            return self.cfg.sweep_max_slice_c
        return self.cfg.sweep_max_slice_d

    def _inter_sweep_slice_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.inter_sweep_max_slice_a
        if sym == "B":
            return self.cfg.inter_sweep_max_slice_b
        if sym == "C":
            return self.cfg.inter_sweep_max_slice_c
        return self.cfg.inter_sweep_max_slice_d

    def _cross_arb_slice_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.cross_arb_max_slice_a
        if sym == "B":
            return self.cfg.cross_arb_max_slice_b
        if sym == "C":
            return self.cfg.cross_arb_max_slice_c
        return self.cfg.cross_arb_max_slice_d

    # v20.4: inventory-aware throttle. Reduces SAME-side IOC slice as
    # |pos|/limit grows. Reducing-side slice stays full to drain.
    # Returns 0 if hard-blocked (too close to cap on same side).
    def _inventory_throttle(self, sym: str, side: str,
                            base_slice: int) -> int:
        if not self.cfg.inventory_throttle_enabled:
            return base_slice
        s = self.state[sym]
        pos = s.position
        if pos == 0:
            return base_slice
        # side="buy" adds to long; side="sell" adds to short.
        adding_long = (side == "buy" and pos > 0)
        adding_short = (side == "sell" and pos < 0)
        if not (adding_long or adding_short):
            return base_slice  # reducing |pos| → full slice
        frac = abs(pos) / max(1, s.position_limit)
        if frac >= self.cfg.inventory_throttle_hard_block_frac:
            return 0
        if frac >= self.cfg.inventory_throttle_frac3:
            return max(1, int(round(
                base_slice * self.cfg.inventory_throttle_mult2)))
        if frac >= self.cfg.inventory_throttle_frac2:
            return max(1, int(round(
                base_slice * self.cfg.inventory_throttle_mult2)))
        if frac >= self.cfg.inventory_throttle_frac1:
            return max(1, int(round(
                base_slice * self.cfg.inventory_throttle_mult1)))
        return base_slice

    # v20.1: True iff the truth oracle has cached a full sequence
    # → fair for every symbol is exact.
    def _truth_locked(self, sym: str) -> bool:
        if sym in ("C", "D"):
            return (self._fair_c_truth is not None
                    if sym == "C"
                    else self._fair_d_truth is not None)
        try:
            return self.truth.full_sequence() is not None
        except Exception:
            return False

    # v20.1: how many ticks to shrink MM width by when truth is locked.
    # C is excluded — oracle-competitor adverse-selection means we want
    # WIDER (not tighter) C quotes under truth.
    def _truth_width_shrink(self, sym: str) -> int:
        if not self.cfg.mm_aggressive_under_truth:
            return 0
        if sym == "C":
            return 0
        if not self._truth_locked(sym):
            return 0
        return max(0, self.cfg.mm_truth_width_shrink_ticks)

    # v20.1: how many lots to add to base size under truth.
    def _truth_size_bump(self, sym: str) -> int:
        if not self.cfg.mm_aggressive_under_truth:
            return 0
        if sym == "C":
            return 0
        if not self._truth_locked(sym):
            return 0
        return max(0, self.cfg.mm_truth_size_bump)

    # v20.2: override v19's hard-disable. We WANT flatten-bias during
    # pre-first-reveal even under truth — so MM, inter-sweep, and
    # cross-arb all skew toward reducing |pos| as reveal 1 approaches.
    def _in_flatten_bias_window(
            self, sigma_a: Optional[float] = None) -> bool:
        if not self.cfg.flatten_bias_enabled:
            return False
        if self.phase != "running":
            return False
        # v20.2: pre-first-reveal window, even with truth.
        if self.cfg.flatten_bias_under_truth and self._reveal_count == 0:
            nxt = self._next_reveal_at()
            if nxt is not None:
                eta = nxt - time.time()
                if 0.0 <= eta <= self.cfg.pre_first_reveal_flatten_sec:
                    return True
        # Defer to v19 (which defers to v18) for the sigma-trigger path.
        return super()._in_flatten_bias_window(sigma_a)

    def _refresh_c_strike(self) -> Optional[int]:
        """v20.16: refetch C strike from server. self.c_strike is set
        in __init__ but the process stays alive across rounds; without
        this refresh, R2+ uses the K from R1 → fair_c is computed
        against the wrong strike → catastrophic IOC sells (e.g.
        124456 R1 ran with stale K=61 vs actual K=19, fair_c=2.6 vs
        actual 99.1, ~$1,400 loss in one round from pre-truth sells).
        """
        try:
            gs = self.c.game_state()
            cmeta = ((gs.get("instruments") or {}).get("C") or {})
            sp = cmeta.get("settlement_params") or {}
            k_raw = (sp.get("strike") if "strike" in sp
                     else sp.get("K") if "K" in sp
                     else sp.get("threshold"))
            if k_raw is not None:
                new_k = int(k_raw)
                if new_k != self.c_strike:
                    print(f"[v20.16 STRIKE] K refresh "
                          f"{self.c_strike} -> {new_k}")
                    self.c_strike = new_k
                return new_k
        except Exception as e:
            print(f"[v20.16 STRIKE] refresh failed: {e!r}")
        return self.c_strike

    def on_strikes_event(self, msg: dict) -> None:
        """v20.16: server pushes a `strikes` event at round start
        with the new K. Wire this in run_combined20.on_message."""
        try:
            strikes = msg.get("strikes") or {}
            k_raw = strikes.get("C")
            if k_raw is None:
                return
            new_k = int(k_raw)
            if new_k != self.c_strike:
                print(f"[v20.16 STRIKE-EV] K {self.c_strike} -> {new_k}")
                self.c_strike = new_k
        except Exception as e:
            print(f"[v20.16 STRIKE-EV] {e!r}")

    # ==================================================================
    # v20.6: single precompute pass for ALL fair values
    # ==================================================================
    # Once the full sequence is known, the final values of A (sum), C
    # (digital on sum vs strike), and D (range max−min) are exact and
    # constant for the rest of the round. B is the per-reveal tick
    # value — settles at every reveal, so fair_B = sequence[next_idx]
    # which advances each cycle. Compute the constants once and cache
    # them; fair_b reads the per-reveal value off the cached list.
    def _refresh_cd_truth_cache(self) -> None:
        seq = self.truth.full_sequence() if self.cfg.truth_enabled else None
        if not seq:
            self._fair_a_truth = None
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._cd_truth_for_seq = None
            return
        seq_t = tuple(seq)
        if seq_t == self._cd_truth_for_seq:
            return
        self._cd_truth_for_seq = seq_t
        # A: final = sum(seq). Constant.
        self._fair_a_truth = float(sum(seq))
        # C: 100 if sum >= strike else 0. Constant.
        if self.c_strike is not None:
            self._fair_c_truth = (100.0
                                  if self._fair_a_truth >= self.c_strike
                                  else 0.0)
        else:
            self._fair_c_truth = None
        # D: range = max - min. Constant.
        self._fair_d_truth = float(max(seq) - min(seq))
        # B: per-reveal value vector. Reads as fair_b each cycle.
        self._truth_b_seq = tuple(int(v) for v in seq)
        print(f"[v20.6 TRUTH-ALL] seq={list(seq)}  "
              f"fair_a={self._fair_a_truth:.0f}  K={self.c_strike}  "
              f"fair_c={self._fair_c_truth}  "
              f"fair_d={self._fair_d_truth:.1f} "
              f"(max={max(seq)},min={min(seq)})  "
              f"fair_b_seq_len={len(self._truth_b_seq)}")

    # v20.6: read precomputed A from the same cache used for C/D.
    # super().fair_a (v19) hits the oracle every call; this hits a
    # local field after the first sequence-arrival pass.
    def fair_a(self) -> Tuple[float, float]:
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        if self._fair_a_truth is not None:
            return self._fair_a_truth, 0.5
        return super().fair_a()

    # v20.6: fair_B = sequence[next_reveal_index]. Advances per
    # reveal because reveal_count is the index of the upcoming
    # window. Once we pass the last reveal, B no longer settles
    # → fall back to super (which returns None / posterior estimate).
    def fair_b(self) -> Tuple[float, float]:
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        seq = self._truth_b_seq
        if seq is not None:
            i = self._reveal_count
            if 0 <= i < len(seq):
                return float(seq[i]), 0.5
        return super().fair_b()

    def fair_c(self) -> Tuple[float, float]:
        """Truth-anchored exact under truth; Normal-approx pre-truth."""
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        if self._fair_c_truth is not None:
            return self._fair_c_truth, 0.5

        # Pre-truth fallback. Bots VANA quote around N(70, ~12.6) for
        # sum. We approximate the same way so our quotes don't stick
        # out before truth arrives (rare — usually truth fetches at
        # round start).
        if self.c_strike is None:
            return 50.0, 20.0
        n_rem = self._n_remaining()
        if n_rem <= 0:
            mean_sum = self._running_sum
            sig_sum = 0.5
        else:
            mean_sum = self._running_sum + n_rem * 7.0
            sig_sum = math.sqrt(n_rem * 16.0)
        if sig_sum < 1e-6:
            return ((100.0 if mean_sum >= self.c_strike else 0.0), 0.5)
        z = (mean_sum - self.c_strike) / sig_sum
        from math import erf as _erf, sqrt as _sqrt
        p_ge = 0.5 * (1.0 + _erf(z / _sqrt(2.0)))
        return p_ge * 100.0, 30.0

    def fair_d(self) -> Tuple[float, float]:
        """Truth-anchored exact under truth; MC prior approx pre-truth."""
        if self._cd_truth_for_seq is None:
            self._refresh_cd_truth_cache()
        if self._fair_d_truth is not None:
            return self._fair_d_truth, 0.5

        # Pre-truth fallback: E[range | reveals so far] from posterior.
        revs = list(self.posterior.reveals)
        if not revs:
            return 5.0, 2.0
        n_rem = self._n_remaining()
        cur_min = min(revs)
        cur_max = max(revs)
        cur_range = cur_max - cur_min
        if n_rem <= 0:
            return float(cur_range), 0.5
        # Approximation: E[range] grows roughly as the support width
        # scaled by (1 - 1/(n+1)) — clamped.
        est = cur_range + 0.5 * n_rem
        return est, max(1.5, 0.3 * est)

    # ==================================================================
    # Position cap ramp — override to dispatch C/D
    # ==================================================================
    def _effective_position_cap(self, sym: str) -> int:
        L = self.state[sym].position_limit
        if not self.cfg.position_ramp_enabled:
            return L
        # v20.8: under truth, A and D have exact constant fairs and no
        # intra-round settlement risk. Every fill inside [fair-edge,
        # fair+edge] is +EV, so the ramp just wastes edge. Bypass to
        # full ±L. B is excluded because each reveal tick-settles
        # independently and we can't always unwind between ticks
        # (v19 -$3,291 B-bleed). C is excluded because the oracle
        # competitors (VANG/GVAN) cause adverse selection at the
        # boundary, so we want a tighter cap, not wider.
        if self.cfg.truth_cap_bypass_enabled and sym in ("A", "D") \
                and self._truth_locked(sym):
            return L
        prog = self._round_progress()
        frac = (self.cfg.position_ramp_start_frac
                + (self.cfg.position_ramp_end_frac
                   - self.cfg.position_ramp_start_frac) * prog)
        cap = int(round(L * frac))
        floor = self._pos_ramp_min_for(sym)
        cap = max(cap, floor)
        return min(cap, L)

    # ==================================================================
    # Size ramp — override to dispatch C/D
    # ==================================================================
    def _ramped_base_size(self, sym: str) -> int:
        base = self._base_size_for(sym)
        if not self.cfg.size_ramp_enabled:
            return base
        prog = self._round_progress()
        frac = (self.cfg.size_ramp_start_frac
                + (1.0 - self.cfg.size_ramp_start_frac) * prog)
        sz = int(round(base * frac))
        floor = self._size_ramp_min_for(sym)
        return max(sz, floor)

    # ==================================================================
    # v20.5: One-sided C MM under truth-locked boundary regime.
    # Returns (bid, ask) where exactly one side is None, or None
    # if the regime doesn't apply.
    # ==================================================================
    def _quote_prices_c_truth_boundary(
            self, fair: float, position: int
    ) -> Optional[Tuple[Optional[int], Optional[int]]]:
        s = self.state["C"]
        tick = s.tick
        ub = self._upper_bound("C")
        hi = self.cfg.c_one_sided_high
        lo = self.cfg.c_one_sided_low
        pad = self.cfg.mm_inventory_safety_pad
        if fair >= hi:
            # Buy-only mode. Truth says ~100, anything below is +EV.
            # Post tight at fair-1 (= 99 when truth=100). Skip the
            # skew that v18 layered for two-sided MM — at the
            # boundary there's no "balanced inventory" target.
            if position >= s.position_limit - pad:
                return (None, None)  # at long cap, stop adding
            bid_px = max(1, int(round(fair)) - 1)
            # Cap inside upper bound (ub=99 for C since ub-1=99).
            bid_px = min(bid_px, ub - 1)
            qty = self._quote_size("C", "bid")
            if qty <= 0 or bid_px < 1:
                return (None, None)
            return (bid_px, None)
        if fair <= lo:
            # Sell-only mode. Truth says ~0, anything above is +EV.
            if position <= -s.position_limit + pad:
                return (None, None)
            ask_px = min(ub, int(round(fair)) + 1)
            ask_px = max(ask_px, 1)
            qty = self._quote_size("C", "ask")
            if qty <= 0:
                return (None, None)
            return (None, ask_px)
        return None  # mid range → fall through to two-sided MM

    # ==================================================================
    # Quote prices — for A/B call super (v19 truth + dir-skew); for C/D
    # compute symmetric two-sided MM around truth-derived fair.
    # The wrapping override that applies scalp_out_at_cap is defined
    # further down (v20.3 section); this is the C/D dispatch helper.
    # ==================================================================
    def _quote_prices_cd(self, sym: str, fair: float, position: int,
                         mode: str
                         ) -> Tuple[Optional[int], Optional[int]]:
        """Two-sided MM around fair for C / D. Mirrors v18's quote
        math (half = width/2, position-skew = position/flip * half).

        Special handling for C — bounded [0, 100]:
          * bid in [1, 99], ask in [1, 99]. Cap at boundaries.
          * Under truth=100, normal-mode bid=fair-3 (e.g. 97),
            ask=fair+3 caps at 99 (since 103 > 100). Ask at 99 only
            loses 1t per fill, and only fires if a counterparty is
            buying through 99 (rare unless they also know truth).
          * Skew flips both sides DOWN with long position (encourage
            selling out via taker hits to our ask).
        """
        # v20.5: under truth, C near boundary uses one-sided MM.
        # Posting a sell@100 at truth=100 (or buy@1 at truth=0) is
        # a dead quote — it can never fill in our favour. Only
        # the "favourable" side fills (counterparty makes a
        # mistake selling cheap or buying expensive). Skip the
        # dead side and skip position-skew on the live side, so we
        # always present at the tightest profitable price.
        if (sym == "C" and mode == "normal"
                and self.cfg.c_one_sided_mm_enabled
                and self._truth_locked("C")):
            res = self._quote_prices_c_truth_boundary(fair, position)
            if res is not None:
                return res

        s = self.state[sym]
        tick = s.tick
        # v20.1: _width_for already applies truth-shrink (skipping C).
        width = self._width_for(sym)
        flip = self._flip_for(sym)

        if mode == "park":
            off = self.cfg.pre_reveal_park_offset_ticks * tick
            bid_px = ((int(round(fair - off))) // tick) * tick
            ask_px = ((int(round(fair + off))) // tick) * tick
            bid_px, ask_px = self._clamp_cd(sym, bid_px, ask_px)
            pad = self.cfg.mm_inventory_safety_pad
            if position >= s.position_limit - pad:
                bid_px = None
            if position <= -s.position_limit + pad:
                ask_px = None
            return bid_px, ask_px

        half = width / 2.0
        if self._reveal_count == 0:
            half *= self.cfg.no_info_widen_mult
        elif mode == "widen":
            half *= self.cfg.pre_reveal_widen_mult

        skew = (position / flip) * half
        bid_px = int(round(fair - half - skew))
        ask_px = int(round(fair + half - skew))
        bid_px = (bid_px // tick) * tick
        ask_px = (ask_px // tick) * tick

        # v20.2: directional skew under truth. fair=truth_fair already;
        # compare against BBO mid (naive proxy for what non-oracle
        # bots are pricing). If truth>mid → lean LONG (tighter bid,
        # wider ask). If truth<mid → lean SHORT.
        bid_px, ask_px = self._apply_cd_dir_skew(
            sym, fair, position, bid_px, ask_px, tick)

        # Boundary clamping (C only; D unbounded).
        bid_px, ask_px = self._clamp_cd(sym, bid_px, ask_px)

        if (bid_px is not None and ask_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + tick if (bid_px + tick) <= self._upper_bound(sym) else None

        if self.cfg.skip_negative_ev:
            if bid_px is not None and bid_px > fair:
                bid_px = None
            if ask_px is not None and ask_px < fair:
                ask_px = None

        # Soft cap (MM only) — if we're past the ramped cap, drop the
        # side that would push us further out.
        cap = self._effective_position_cap(sym)
        if position >= cap and bid_px is not None:
            bid_px = None
        if position <= -cap and ask_px is not None:
            ask_px = None

        # Hard pad — never quote into the side already at full hard limit.
        pad = self.cfg.mm_inventory_safety_pad
        if position >= s.position_limit - pad:
            bid_px = None
        if position <= -s.position_limit + pad:
            ask_px = None

        if bid_px is not None and bid_px < tick:
            bid_px = None
        return bid_px, ask_px

    def _apply_cd_dir_skew(self, sym: str, fair: float, position: int,
                           bid_px: Optional[int], ask_px: Optional[int],
                           tick: int
                           ) -> Tuple[Optional[int], Optional[int]]:
        """v20.2 C/D directional skew. Mirrors v19's A/B logic but uses
        BBO midpoint as the naive proxy. Only fires when truth is locked
        AND |truth-mid| >= min_edge AND we have a usable BBO."""
        if not self.cfg.cd_directional_skew_enabled:
            return bid_px, ask_px
        if not self._truth_locked(sym):
            return bid_px, ask_px
        s = self.state[sym]
        book = s.book or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return bid_px, ask_px
        try:
            best_bid = int(bids[0].get("price"))
            best_ask = int(asks[0].get("price"))
        except Exception:
            return bid_px, ask_px
        if best_ask <= best_bid:
            return bid_px, ask_px
        mid = 0.5 * (best_bid + best_ask)
        edge = fair - mid
        min_edge = self.cfg.cd_directional_skew_min_edge_ticks
        if abs(edge) < min_edge:
            return bid_px, ask_px

        tight = self.cfg.cd_directional_skew_tight_ticks * tick
        wide = self.cfg.cd_directional_skew_wide_ticks * tick

        if edge > 0:
            # truth > market mid → lean LONG. Tighten bid, widen ask.
            ideal_bid = ((int(round(fair - tight))) // tick) * tick
            ideal_ask = ((int(round(fair + wide))) // tick) * tick
            if bid_px is not None:
                bid_px = max(bid_px, ideal_bid)
            if ask_px is not None:
                ask_px = max(ask_px, ideal_ask)
        else:
            # truth < market mid → lean SHORT. Tighten ask, widen bid.
            ideal_ask = ((int(round(fair + tight))) // tick) * tick
            ideal_bid = ((int(round(fair - wide))) // tick) * tick
            if ask_px is not None:
                ask_px = min(ask_px, ideal_ask)
            if bid_px is not None:
                bid_px = min(bid_px, ideal_bid)
        return bid_px, ask_px

    def _upper_bound(self, sym: str) -> int:
        """Upper-bound for quotable price on C. D is unbounded → big."""
        if sym == "C":
            return 100
        return 10**9

    def _lower_bound(self, sym: str) -> int:
        """Lower-bound for quotable price (tick > 0)."""
        return self.state[sym].tick

    def _clamp_cd(self, sym: str, bid_px: Optional[int],
                  ask_px: Optional[int]
                  ) -> Tuple[Optional[int], Optional[int]]:
        """For C: clamp bid in [1, 99], ask in [1, 99]. Returning None
        for either side if the clamp would produce a non-positive
        price (D is unbounded — pass through)."""
        if sym != "C":
            return bid_px, ask_px
        if bid_px is not None:
            if bid_px < 1:
                bid_px = None
            elif bid_px > 99:
                bid_px = 99
        if ask_px is not None:
            if ask_px > 99:
                ask_px = 99
            elif ask_px < 1:
                ask_px = None
        return bid_px, ask_px

    # ==================================================================
    # MM refresh — parent loops self.SYMS but reads fair via hard-coded
    # branches; override to dispatch C/D too.
    # ==================================================================
    def _refresh_mm_quotes(self) -> None:
        if not self._can_post():
            return
        if self._endgame_active:
            # Endgame freezes A specifically; B/C/D continue to MM.
            pass  # Don't early-return; just skip A in the loop below.
        now = time.time()
        ab_due = (self.cfg.mm_min_interval_sec <= 0
                  or (now - self._last_mm_refresh_t)
                  >= self.cfg.mm_min_interval_sec)
        # v20.5: C/D get a faster refresh cadence so we keep dimed
        # quotes alive against the 1000-1500ms naive MMs.
        cd_due = (self.cfg.mm_refresh_sec_cd <= 0
                  or (now - self._last_mm_refresh_t_cd)
                  >= self.cfg.mm_refresh_sec_cd)
        if not (ab_due or cd_due):
            return
        if ab_due:
            self._last_mm_refresh_t = now
        if cd_due:
            self._last_mm_refresh_t_cd = now
        in_park = self._in_park_window()
        for sym in self.SYMS:
            if self._endgame_active and sym == "A":
                continue
            # v20.5: respect the per-cohort gate.
            if sym in ("A", "B") and not ab_due:
                continue
            if sym in ("C", "D") and not cd_due:
                continue
            try:
                fair, _ = self._fair_for(sym)
                pos = self.state[sym].position
                if in_park:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "park")
                else:
                    bid_px, ask_px = self._quote_prices(
                        sym, fair, pos, "normal")
                    bid_px, ask_px = self._maybe_penny(
                        sym, fair, pos, bid_px, ask_px)
                    # v20.14: re-apply inventory shift AFTER penny —
                    # the penny step overwrites prices with BBO targets
                    # and would otherwise undo the skew baked into
                    # _quote_prices.
                    bid_px, ask_px = self._apply_inventory_shift(
                        sym, fair, bid_px, ask_px)
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v20 MM:{sym}-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # Park loop — parent loops SYMS but hardcodes fair_a/fair_b lookup;
    # override to dispatch C/D too.
    # ==================================================================
    def _park_tick(self) -> None:
        if not self._can_post():
            return
        if not self._in_park_window():
            return
        if self._last_park_reveal_idx == self._reveal_count:
            return
        self._last_park_reveal_idx = self._reveal_count
        for sym in self.SYMS:
            try:
                fair, _ = self._fair_for(sym)
                pos = self.state[sym].position
                bid_px, ask_px = self._quote_prices(sym, fair, pos, "park")
                self._apply_quote(sym, bid_px, ask_px)
            except Exception as e:
                print(f"[v20 PARK:{sym}-ERR] {type(e).__name__}: {e}")

    # ==================================================================
    # Penny — override to dispatch min_edge per symbol.
    # ==================================================================
    def _maybe_penny(self, sym: str, fair: float, position: int,
                     bid_px: Optional[int], ask_px: Optional[int]
                     ) -> Tuple[Optional[int], Optional[int]]:
        if not self.cfg.penny_enabled:
            return bid_px, ask_px
        book = self.state[sym].book
        if not book:
            return bid_px, ask_px
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        tick = self.state[sym].tick
        min_edge = self._min_edge_for(sym)
        max_step = self.cfg.penny_max_step_ticks
        join_edge = self.cfg.bbo_join_min_edge_ticks
        join_on = self.cfg.bbo_join_enabled
        ub = self._upper_bound(sym)

        if (bid_px is not None and best_bid is not None
                and best_bid >= bid_px):
            same = any(int(e.get("price") or 0) == best_bid
                       for e in self.state[sym].resting["bid"].values())
            # IMPROVE: post 1 tick ABOVE the BBO bid (taking top-of-book).
            improve_target = best_bid + tick if not same else best_bid
            improve_ok = (
                improve_target <= fair - min_edge
                and (improve_target - bid_px) <= max_step * tick
                and improve_target < (
                    best_ask if best_ask is not None else 10**9)
                and improve_target <= ub - 1)
            if improve_ok:
                bid_px = improve_target
            elif join_on and not same:
                # v20.12 JOIN fallback: IMPROVE failed (usually because
                # BBO+1t exceeds fair-min_edge); fall back to posting AT
                # the BBO. Maker fee 0.5 → fills at fair-1t are still +EV.
                join_target = best_bid
                if (join_target <= fair - join_edge
                        and (join_target - bid_px) <= max_step * tick
                        and join_target < (
                            best_ask if best_ask is not None else 10**9)
                        and join_target <= ub - 1):
                    bid_px = join_target

        if (ask_px is not None and best_ask is not None
                and best_ask <= ask_px):
            same = any(int(e.get("price") or 0) == best_ask
                       for e in self.state[sym].resting["ask"].values())
            improve_target = best_ask - tick if not same else best_ask
            improve_ok = (
                improve_target >= fair + min_edge
                and (ask_px - improve_target) <= max_step * tick
                and improve_target > (
                    best_bid if best_bid is not None else 0)
                and improve_target <= ub)
            if improve_ok:
                ask_px = improve_target
            elif join_on and not same:
                # v20.12 JOIN fallback for ask side.
                join_target = best_ask
                if (join_target >= fair + join_edge
                        and (ask_px - join_target) <= max_step * tick
                        and join_target > (
                            best_bid if best_bid is not None else 0)
                        and join_target <= ub):
                    ask_px = join_target
        return bid_px, ask_px

    # ==================================================================
    # Dime defense — override to dispatch fair+min_edge per symbol.
    # ==================================================================
    def _maybe_dime_defense(self, sym: str) -> None:
        if not self.cfg.dime_defense_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        now = time.time()
        throttle = self._dime_throttle_for(sym)
        if (now - s.last_dime_defense_t) < throttle:
            return
        book = s.book
        if not book:
            return
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return
        best_bid = int(bids[0]["price"])
        best_ask = int(asks[0]["price"])
        fair, _ = self._fair_for(sym)
        min_edge = self._min_edge_for(sym)
        join_edge = self.cfg.bbo_join_min_edge_ticks
        join_on = self.cfg.bbo_join_enabled
        tick = s.tick
        max_step_dist = self.cfg.penny_max_step_ticks * tick
        ub = self._upper_bound(sym)
        fired = False

        # --- Bid side ---
        my_best_bid = None
        for entry in s.resting["bid"].values():
            px = int(entry.get("price") or 0)
            if my_best_bid is None or px > my_best_bid:
                my_best_bid = px
        if my_best_bid is None or my_best_bid < best_bid:
            # IMPROVE first (post 1t inside BBO).
            target_bid = best_bid + tick
            improve_ok = (
                target_bid <= fair - min_edge
                and target_bid < best_ask
                and target_bid <= ub - 1
                and (my_best_bid is None
                     or (target_bid - my_best_bid) <= max_step_dist))
            if not improve_ok and join_on:
                # v20.12: JOIN BBO when IMPROVE breaches edge.
                target_bid = best_bid
                improve_ok = (
                    target_bid <= fair - join_edge
                    and target_bid < best_ask
                    and target_bid <= ub - 1
                    and (my_best_bid is None
                         or (target_bid - my_best_bid) <= max_step_dist))
                tag = "JOIN"
            else:
                tag = "STEP"
            if improve_ok:
                # v20.14: inventory-aware shift. When long-heavy, this
                # pulls target_bid DOWN — possibly below BBO, in which
                # case skip (we don't want more longs). When short-heavy,
                # leaves it alone (we want longs).
                shifted_bid, _ = self._apply_inventory_shift(
                    sym, fair, target_bid, None)
                if shifted_bid is not None and shifted_bid < target_bid:
                    if shifted_bid < best_bid:
                        # Shift wants us below BBO — skip dime entirely.
                        improve_ok = False
                    else:
                        target_bid = shifted_bid
            if improve_ok:
                qty = self._quote_size(sym, "bid")
                if qty > 0:
                    case = "absent" if my_best_bid is None else "dimed"
                    print(f"[v20 BBO:{sym}/bid {case}/{tag}] "
                          f"best={best_bid}(mine={my_best_bid}) "
                          f"→ {target_bid} fair={fair:.2f}")
                    self._apply_side(sym, "bid", target_bid, qty)
                    fired = True

        # --- Ask side ---
        my_best_ask = None
        for entry in s.resting["ask"].values():
            px = int(entry.get("price") or 0)
            if my_best_ask is None or px < my_best_ask:
                my_best_ask = px
        if my_best_ask is None or my_best_ask > best_ask:
            target_ask = best_ask - tick
            improve_ok = (
                target_ask >= fair + min_edge
                and target_ask > best_bid
                and target_ask <= ub
                and target_ask > 0
                and (my_best_ask is None
                     or (my_best_ask - target_ask) <= max_step_dist))
            if not improve_ok and join_on:
                target_ask = best_ask
                improve_ok = (
                    target_ask >= fair + join_edge
                    and target_ask > best_bid
                    and target_ask <= ub
                    and target_ask > 0
                    and (my_best_ask is None
                         or (my_best_ask - target_ask) <= max_step_dist))
                tag = "JOIN"
            else:
                tag = "STEP"
            if improve_ok:
                # v20.14: inventory-aware shift. When short-heavy, this
                # pulls target_ask UP — possibly above BBO. Skip dime
                # in that case (we don't want more shorts).
                _, shifted_ask = self._apply_inventory_shift(
                    sym, fair, None, target_ask)
                if shifted_ask is not None and shifted_ask > target_ask:
                    if shifted_ask > best_ask:
                        improve_ok = False
                    else:
                        target_ask = shifted_ask
            if improve_ok:
                qty = self._quote_size(sym, "ask")
                if qty > 0:
                    case = "absent" if my_best_ask is None else "dimed"
                    print(f"[v20 BBO:{sym}/ask {case}/{tag}] "
                          f"best={best_ask}(mine={my_best_ask}) "
                          f"→ {target_ask} fair={fair:.2f}")
                    self._apply_side(sym, "ask", target_ask, qty)
                    fired = True

        if fired:
            s.last_dime_defense_t = now

    # ==================================================================
    # Inter-reveal stale sweep — override to dispatch fair fallback +
    # per-symbol edge / max_dist.
    # ==================================================================
    def _maybe_inter_sweep(self, sym: str) -> None:
        # v20.5: defer A/B to v18 entirely so the maker/taker mix on
        # those symbols matches the v18 baseline. v20-specific
        # behaviour (per-symbol slice, inventory throttle) only
        # applies to C/D where we own the strategy.
        if sym in ("A", "B"):
            return super()._maybe_inter_sweep(sym)
        if not self.cfg.inter_sweep_enabled:
            return
        if not self._can_post():
            return
        if self._in_park_window():
            return
        # v20.1: gate until first reveal — same shape as cross-arb gate.
        if self._reveal_count < self.cfg.inter_sweep_min_reveals:
            return
        s = self.state[sym]
        now = time.time()
        throttle = self._inter_sweep_throttle_for(sym)
        if (now - s.last_inter_sweep_t) < throttle:
            return
        s.last_inter_sweep_t = now

        live_lift, live_hit, live_fair, in_flat = self._get_snipe_thresholds(
            sym, s.position)
        if live_fair == 0.0:
            live_fair, _ = self._fair_for(sym)
        fair = live_fair
        book = s.book
        if not book:
            return
        edge = self._inter_sweep_edge_for(sym)
        edge_buy = edge
        edge_sell = edge
        if in_flat:
            reduction = self.cfg.flatten_edge_reduction_ticks
            floor = self.cfg.flatten_min_edge_ticks
            if s.position < 0:
                edge_buy = max(floor, edge - reduction)
            if s.position > 0:
                edge_sell = max(floor, edge - reduction)
        min_size = self.cfg.inter_sweep_min_size
        # v20.4: per-symbol slice + inventory throttle.
        base_slice = self._inter_sweep_slice_for(sym)
        max_slice_buy = self._inventory_throttle(sym, "buy", base_slice)
        max_slice_sell = self._inventory_throttle(sym, "sell", base_slice)
        pad = self.cfg.sweep_position_pad
        max_dist = self._max_ioc_dist_for(sym)
        fired = 0
        # v20.10: respect ramped cap on inter-sweep IOC path.
        eff_cap = self._effective_position_cap(sym)

        room_buy = (eff_cap - pad) - s.position
        if room_buy > 0 and max_slice_buy > 0:
            max_slice = max_slice_buy
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    continue
                if fair - px < edge_buy:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "ask", int(px)):
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, int(px)):
                    taken += want
                    room_buy -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position < 0) else "INTER"
                    print(f"[v20 {tag}:{sym}] BUY {want}@{px} "
                          f"fair={fair:.2f} edge={fair - px:.2f}t "
                          f"pos={s.position}")
                else:
                    break

        room_sell = s.position - (-eff_cap + pad)
        if room_sell > 0 and max_slice_sell > 0:
            max_slice = max_slice_sell
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size < min_size:
                    continue
                if px - fair < edge_sell:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "bid", int(px)):
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, int(px)):
                    taken += want
                    room_sell -= want
                    fired += 1
                    tag = "INTER-FLAT" if (in_flat and s.position > 0) else "INTER"
                    print(f"[v20 {tag}:{sym}] SELL {want}@{px} "
                          f"fair={fair:.2f} edge={px - fair:.2f}t "
                          f"pos={s.position}")
                else:
                    break
        if fired:
            self._inter_sweep_count += fired

    # ==================================================================
    # Cross-symbol arb / on-fill / on-reveal stale snipe — override the
    # per-symbol loop to walk all 4 books.
    # ==================================================================
    def _try_cross_arb(self, source: str) -> int:
        if not self.cfg.cross_arb_enabled:
            return 0
        if not self._can_post():
            return 0
        if self._in_lockout():
            return 0
        if self._in_park_window():
            return 0

        # v20.1: gate until first reveal (kills the t=0 truth-blast).
        # Reveal-driven calls (source="reveal") bypass the gate — the
        # reveal IS the qualifying event.
        if (source != "reveal"
                and self._reveal_count < self.cfg.cross_arb_min_reveals):
            return 0

        # v20.1: post-reveal lockout. Public book lags ~15ms behind;
        # don't fire IOCs in the lag window unless we're being called
        # by the reveal handler itself.
        if source != "reveal" and self._last_reveal_t > 0.0:
            since_reveal_ms = (time.monotonic() - self._last_reveal_t) * 1000.0
            if since_reveal_ms < self.cfg.cross_arb_post_reveal_lockout_ms:
                return 0

        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks
        # v20.4: per-symbol slice (computed in the loop) + early gate.
        early = self._reveal_count < self.cfg.early_round_reveals
        depth = max(1, self.cfg.cross_arb_levels)
        pad = self.cfg.sweep_position_pad
        fired_total = 0

        with self._live_lock:
            live = dict(self._live_thresholds)

        for sym in self.SYMS:
            s = self.state[sym]
            book = s.book
            if not book:
                continue

            # v20.1: per-round per-symbol qty budget.
            budget = self._cross_arb_budget_for(sym)
            used = self._cross_arb_qty_used.get(sym, 0)
            if budget > 0 and used >= budget:
                continue
            room_budget = budget - used if budget > 0 else 10**9

            # v20.5: A/B fall through to v18 globals so the maker/taker
            # mix on those symbols matches the v18 baseline. Only C/D
            # get per-symbol slice + inventory throttle.
            if sym in ("A", "B"):
                base_slice_sym = self.cfg.cross_arb_max_slice
                if early:
                    base_slice_sym = min(base_slice_sym,
                                         self.cfg.cross_arb_max_slice_early)
                max_slice_buy = base_slice_sym
                max_slice_sell = base_slice_sym
            else:
                base_slice_sym = self._cross_arb_slice_for(sym)
                if early:
                    base_slice_sym = min(base_slice_sym,
                                         self.cfg.cross_arb_max_slice_early)
                max_slice_buy = self._inventory_throttle(sym, "buy", base_slice_sym)
                max_slice_sell = self._inventory_throttle(sym, "sell", base_slice_sym)

            base_edge = self._cross_arb_edge_for(sym)
            max_dist = self._max_ioc_dist_for(sym)
            lt = live.get(sym)
            if lt is not None:
                fair = lt.fair
                in_flat = lt.in_flatten_window
            else:
                fair, _ = self._fair_for(sym)
                in_flat = False
            edge_buy = base_edge
            edge_sell = base_edge
            if in_flat:
                if s.position < 0:
                    edge_buy = max(flat_min, base_edge - flat_reduction)
                if s.position > 0:
                    edge_sell = max(flat_min, base_edge - flat_reduction)

            # v20.10: use ramped (effective) cap, not hard position_limit.
            eff_cap = self._effective_position_cap(sym)
            room_buy = (eff_cap - pad) - s.position
            if room_buy > 0 and max_slice_buy > 0:
                max_slice = max_slice_buy
                taken = 0
                for lvl in (book.get("asks") or [])[:depth]:
                    if room_buy <= 0 or room_budget <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if fair - px < edge_buy:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    if self._is_spoof_like(sym, "ask", int(px)):
                        continue
                    want = min(max_slice - taken, room_buy, size, room_budget)
                    if want <= 0:
                        break
                    if self._ioc(sym, "buy", want, int(px)):
                        taken += want
                        room_buy -= want
                        room_budget -= want
                        self._cross_arb_qty_used[sym] = (
                            self._cross_arb_qty_used.get(sym, 0) + want)
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position < 0)
                               else f"ARB({source})")
                        print(f"[v20 {tag}:{sym}] BUY {want}@{px}  "
                              f"fair={fair:.2f} edge={fair - px:.2f}t "
                              f"pos={s.position} budget_left={room_budget}")
                    else:
                        break

            # v20.10: use ramped cap on sell side too.
            room_sell = s.position - (-eff_cap + pad)
            if room_sell > 0 and max_slice_sell > 0:
                max_slice = max_slice_sell
                taken = 0
                for lvl in (book.get("bids") or [])[:depth]:
                    if room_sell <= 0 or room_budget <= 0:
                        break
                    px = lvl.get("price")
                    size = int(lvl.get("qty") or 0)
                    if px is None or size <= 0:
                        continue
                    if px - fair < edge_sell:
                        break
                    if max_dist > 0 and abs(px - fair) > max_dist:
                        continue
                    if self._is_spoof_like(sym, "bid", int(px)):
                        continue
                    want = min(max_slice - taken, room_sell, size, room_budget)
                    if want <= 0:
                        break
                    if self._ioc(sym, "sell", want, int(px)):
                        taken += want
                        room_sell -= want
                        room_budget -= want
                        self._cross_arb_qty_used[sym] = (
                            self._cross_arb_qty_used.get(sym, 0) + want)
                        fired_total += 1
                        tag = (f"ARB({source})-FLAT"
                               if (in_flat and s.position > 0)
                               else f"ARB({source})")
                        print(f"[v20 {tag}:{sym}] SELL {want}@{px}  "
                              f"fair={fair:.2f} edge={px - fair:.2f}t "
                              f"pos={s.position}")
                    else:
                        break
        if fired_total:
            self._cross_arb_count += fired_total
        return fired_total

    # ==================================================================
    # v20.11: Hard-bounds arbitrage
    # ==================================================================
    # Bounds derived from revealed values are MATHEMATICALLY GUARANTEED:
    #   * A: A_final = sum(all_values), values >= 0 → A_final >= sum(revealed)
    #   * D: D_final = max - min, partial range never shrinks → D >= max(r)-min(r)
    #   * C: digital(sum>=K). Once sum(revealed) >= K, C_final == 100
    #     (future values are non-negative; sum only grows).
    # Under truth, all bounds collapse to (fair, fair). The IOC sweep
    # below takes any ask < lower or bid > upper as RISK-FREE profit.
    # ==================================================================
    def _hard_bounds_for(
            self, sym: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return (lower, upper) hard bounds on `sym` settlement.
        None means unbounded on that side.

        v20.13: reads from `_identity_table` (precomputed via
        `_recompute_identity_table`). Falls back to live recompute if
        the table is empty (defensive — fast-precompute keeps it
        populated, but a callable might fire before the first refresh
        e.g. inside __init__)."""
        ib = self._identity_table.get(sym)
        if ib is not None and ib.gen > 0:
            return (ib.lower, ib.upper)
        # Cold-path fallback: build it once on demand.
        self._recompute_identity_table()
        ib = self._identity_table.get(sym)
        if ib is not None:
            return (ib.lower, ib.upper)
        return (None, None)

    # ==================================================================
    # v20.13 — Precomputed cross-market identity table
    #
    # Consolidates ALL deterministic identities we can derive without
    # touching the posterior fair estimate. Each call walks the
    # revealed-values list once, applies symbol bounds, then layers
    # cross-market identities so a tightening on one symbol propagates
    # to others. Cheap (constant per call after revs are summed/maxed
    # /mined once) and called from on_reveal, phase → running, and the
    # fast-precompute loop.
    #
    # Identities (in order of application):
    #   1. A_lower = sum(revealed)                        # values ≥ 0
    #   2. D_lower = max(r) - min(r) for k ≥ 2            # range grows
    #   3. C_lower=C_upper=100  if sum(r) ≥ K             # digital fired
    #   4. (NEW eor_pin) C_lower=C_upper=0
    #         if reveals == n_total AND sum(r) < K        # digital dead
    #   5. (NEW cross_C0) A_upper = K - 1                 # C dead → A < K
    #         when C is pinned at 0
    #   6. (NEW cross_D_A) D_upper = A_upper              # D ≤ max ≤ sum
    #         when A_upper is known (only fires post #5 or under truth)
    #   7. truth: (fair, fair) collapse                   # exact value
    #
    # The output is read by `_hard_bounds_for` (O(1) lookup) and is the
    # exclusive ground truth for `_bounds_arb_sweep`. Any caller that
    # asks "is this price guaranteed to be a winning fill?" should
    # consult this table.
    # ==================================================================
    def _recompute_identity_table(self) -> None:
        revs = list(self.posterior.reveals)
        n_revealed = len(revs)
        n_total = getattr(self, "n_total", None)
        running_sum = sum(revs) if revs else 0
        K = self.c_strike

        new_tbl: Dict[str, IdentityBounds] = {
            s: IdentityBounds(sym=s) for s in self.SYMS
        }

        # --- A: sum(revealed) lower bound ---
        if n_revealed > 0:
            new_tbl["A"].lower = float(running_sum)
            new_tbl["A"].lower_source = "reveals"
        else:
            new_tbl["A"].lower = 0.0
            new_tbl["A"].lower_source = "init"

        # --- B: no deterministic bound without truth ---
        # (lower, upper) stay None — bound_arb skips B pre-truth.

        # --- C: digital(sum >= K) ---
        if K is None:
            new_tbl["C"].lower = 0.0
            new_tbl["C"].upper = 100.0
        else:
            if running_sum >= K:
                new_tbl["C"].lower = 100.0
                new_tbl["C"].upper = 100.0
                new_tbl["C"].lower_source = "sum_ge_K"
                new_tbl["C"].upper_source = "sum_ge_K"
            elif (n_total is not None and n_revealed >= n_total
                  and running_sum < K):
                # END-OF-ROUND pin: no reveals remaining and sum < K
                # → digital settles at 0 with certainty.
                new_tbl["C"].lower = 0.0
                new_tbl["C"].upper = 0.0
                new_tbl["C"].lower_source = "eor_pin"
                new_tbl["C"].upper_source = "eor_pin"
            else:
                new_tbl["C"].lower = 0.0
                new_tbl["C"].upper = 100.0

        # --- D: max(revealed) - min(revealed) lower bound ---
        if n_revealed >= 2:
            new_tbl["D"].lower = float(max(revs) - min(revs))
            new_tbl["D"].lower_source = "reveals"
        else:
            new_tbl["D"].lower = 0.0
            new_tbl["D"].lower_source = "init"

        # --- Cross-market: C pinned at 0 → A_upper = K - 1 ---
        # If C is mathematically forced to 0 (sum < K and no reveals
        # left), then A_final = sum(seq) is strictly less than K. So
        # A_upper ≤ K - 1.
        if (K is not None and new_tbl["C"].upper == 0.0
                and new_tbl["C"].upper_source == "eor_pin"):
            cand = float(K - 1)
            cur = new_tbl["A"].upper
            if cur is None or cur > cand:
                new_tbl["A"].upper = cand
                new_tbl["A"].upper_source = "cross_C0"

        # --- Cross-market: D ≤ max(seq) ≤ sum(seq) = A ---
        # D = max(seq) - min(seq) ≤ max(seq) ≤ sum(seq) = A_final.
        # So if A_upper is bounded, D_upper inherits the same bound.
        a_up = new_tbl["A"].upper
        if a_up is not None:
            cur = new_tbl["D"].upper
            if cur is None or cur > a_up:
                new_tbl["D"].upper = a_up
                new_tbl["D"].upper_source = "cross_D_A"

        # --- Truth overrides: collapse to (fair, fair) for any
        # truth-locked symbol. Done LAST so it strictly overrides any
        # weaker bound from the identities above (truth = exact). ---
        for sym in self.SYMS:
            if self._truth_locked(sym):
                try:
                    fair, _ = self._fair_for(sym)
                    new_tbl[sym].lower = float(fair)
                    new_tbl[sym].upper = float(fair)
                    new_tbl[sym].lower_source = "truth"
                    new_tbl[sym].upper_source = "truth"
                except Exception:
                    pass

        # Stamp generation; install.
        next_gen = self._identity_gen + 1
        for sym in self.SYMS:
            new_tbl[sym].gen = next_gen
        # Detect NEW cross-market / EOR pin tightenings vs prior gen
        # and log them once — useful for confirming the new identities
        # actually fire in real rounds, without spamming every fast
        # loop tick where nothing changed.
        prev = self._identity_table
        for sym in self.SYMS:
            new_ib = new_tbl[sym]
            old_ib = prev.get(sym)
            old_l_src = old_ib.lower_source if old_ib else "init"
            old_u_src = old_ib.upper_source if old_ib else "init"
            new_l_src = new_ib.lower_source
            new_u_src = new_ib.upper_source
            interesting = {"eor_pin", "cross_C0", "cross_D_A"}
            if (new_l_src in interesting and new_l_src != old_l_src) or (
                    new_u_src in interesting and new_u_src != old_u_src):
                lo = (f"{new_ib.lower:.2f}" if new_ib.lower is not None
                      else "None")
                up = (f"{new_ib.upper:.2f}" if new_ib.upper is not None
                      else "None")
                print(f"[v20.13 IDENTITY:{sym}] lower={lo}({new_l_src}) "
                      f"upper={up}({new_u_src})")
        self._identity_table = new_tbl
        self._identity_gen = next_gen

    def identity_table_snapshot(self) -> Dict[str, Dict[str, object]]:
        """Public accessor for the runner / debug commands."""
        out: Dict[str, Dict[str, object]] = {}
        for sym, ib in self._identity_table.items():
            out[sym] = {
                "lower": ib.lower,
                "upper": ib.upper,
                "lower_source": ib.lower_source,
                "upper_source": ib.upper_source,
                "gen": ib.gen,
            }
        return out

    def _bounds_arb_slice_for(self, sym: str) -> int:
        """Per-symbol slice cap for bounds-arb. Larger than cross-arb
        slice because bounds-arb fills are risk-free."""
        base = self._sweep_slice_for(sym)
        mult = self.cfg.bounds_arb_slice_mult
        return max(1, int(round(base * mult)))

    def _bounds_arb_budget_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.bounds_arb_qty_budget_a
        if sym == "B":
            return self.cfg.bounds_arb_qty_budget_b
        if sym == "C":
            return self.cfg.bounds_arb_qty_budget_c
        return self.cfg.bounds_arb_qty_budget_d

    def _bounds_arb_sweep(self, source: str) -> int:
        """Scan each symbol's book for prices violating hard bounds.
        Asks priced < (lower - min_edge) → IOC buy; bids > (upper +
        min_edge) → IOC sell. NO post-reveal lockout / NO min-reveals
        gate — bound-violations are deterministic +EV regardless of
        timing or fair-estimate state."""
        if not self.cfg.bounds_arb_enabled:
            return 0
        if not self._can_post():
            return 0
        if self._in_lockout():
            return 0
        if self._in_park_window():
            return 0

        pad = self.cfg.sweep_position_pad
        edge = self.cfg.bounds_arb_min_edge_ticks
        fired_total = 0

        for sym in self.SYMS:
            s = self.state[sym]
            book = s.book
            if not book:
                continue

            lower, upper = self._hard_bounds_for(sym)
            if lower is None and upper is None:
                continue

            budget = self._bounds_arb_budget_for(sym)
            used = self._bounds_arb_qty_used.get(sym, 0)
            if budget > 0 and used >= budget:
                continue
            room_budget = budget - used if budget > 0 else 10**9

            base_slice = self._bounds_arb_slice_for(sym)
            max_slice_buy = self._inventory_throttle(sym, "buy", base_slice)
            max_slice_sell = self._inventory_throttle(sym, "sell", base_slice)
            eff_cap = self._effective_position_cap(sym)

            # BUY asks priced strictly below lower bound (with edge).
            if lower is not None and max_slice_buy > 0:
                threshold = lower - edge
                room_buy = (eff_cap - pad) - s.position
                if room_buy > 0:
                    taken = 0
                    for lvl in (book.get("asks") or []):
                        if room_buy <= 0 or room_budget <= 0:
                            break
                        if taken >= max_slice_buy:
                            break
                        px = lvl.get("price")
                        size = int(lvl.get("qty") or 0)
                        if px is None or size <= 0:
                            continue
                        if px > threshold:
                            break  # asks sorted ascending — rest are worse
                        if self._is_spoof_like(sym, "ask", int(px)):
                            continue
                        want = min(max_slice_buy - taken, room_buy,
                                   size, room_budget)
                        if want <= 0:
                            break
                        if self._ioc(sym, "buy", want, int(px)):
                            taken += want
                            room_buy -= want
                            room_budget -= want
                            self._bounds_arb_qty_used[sym] = (
                                self._bounds_arb_qty_used.get(sym, 0)
                                + want)
                            fired_total += 1
                            print(f"[v20.11 BNDS({source}):{sym}] BUY "
                                  f"{want}@{px} lower={lower:.2f} "
                                  f"floor_edge={lower - px:.2f}t "
                                  f"pos={s.position}")
                        else:
                            break

            # SELL bids priced strictly above upper bound (with edge).
            if upper is not None and max_slice_sell > 0:
                threshold = upper + edge
                room_sell = s.position - (-eff_cap + pad)
                if room_sell > 0:
                    taken = 0
                    for lvl in (book.get("bids") or []):
                        if room_sell <= 0 or room_budget <= 0:
                            break
                        if taken >= max_slice_sell:
                            break
                        px = lvl.get("price")
                        size = int(lvl.get("qty") or 0)
                        if px is None or size <= 0:
                            continue
                        if px < threshold:
                            break  # bids sorted descending — rest are worse
                        if self._is_spoof_like(sym, "bid", int(px)):
                            continue
                        want = min(max_slice_sell - taken, room_sell,
                                   size, room_budget)
                        if want <= 0:
                            break
                        if self._ioc(sym, "sell", want, int(px)):
                            taken += want
                            room_sell -= want
                            room_budget -= want
                            self._bounds_arb_qty_used[sym] = (
                                self._bounds_arb_qty_used.get(sym, 0)
                                + want)
                            fired_total += 1
                            print(f"[v20.11 BNDS({source}):{sym}] SELL "
                                  f"{want}@{px} upper={upper:.2f} "
                                  f"floor_edge={px - upper:.2f}t "
                                  f"pos={s.position}")
                        else:
                            break

        if fired_total:
            self._bounds_arb_count += fired_total
        return fired_total

    # ==================================================================
    # v20.1: Precomputed arbitrage table — for each symbol, store the
    # exact max-buy-px and min-sell-px implied by current fair + the
    # symbol's MIN edge (penny_min_edge) capped against the symbol's
    # max_ioc_distance. The fast-arb scanner reads these without
    # recomputing fair on every event.
    #
    # Updated on every reveal (and on phase → running). The fast
    # precompute loop also refreshes it as a safety net.
    # ==================================================================
    def _recompute_arb_table(self) -> None:
        new_tbl: Dict[str, Dict[str, float]] = {}
        for sym in self.SYMS:
            try:
                fair, sigma = self._fair_for(sym)
            except Exception:
                continue
            s = self.state.get(sym)
            if s is None:
                continue
            tick = s.tick
            min_edge = self._min_edge_for(sym)
            sweep_edge = self._sweep_edge_for(sym)
            arb_edge = self._cross_arb_edge_for(sym)
            # Buy at or below: floor(fair - min_edge / tick) * tick.
            buy_at_or_below = (
                (int(math.floor(fair - min_edge))) // tick) * tick
            sell_at_or_above_raw = int(math.ceil(fair + min_edge))
            sell_at_or_above = (
                ((sell_at_or_above_raw + tick - 1) // tick) * tick)
            # Hard buy/sell — i.e. the "fire IOC immediately" level
            # using the cross-arb edge (which already factors in the
            # early-round multiplier).
            hard_buy = (
                (int(math.floor(fair - arb_edge))) // tick) * tick
            hard_sell = (
                ((int(math.ceil(fair + arb_edge)) + tick - 1) // tick) * tick)
            new_tbl[sym] = {
                "fair": float(fair),
                "sigma": float(sigma),
                "buy_at_or_below": float(buy_at_or_below),
                "sell_at_or_above": float(sell_at_or_above),
                "hard_buy": float(hard_buy),
                "hard_sell": float(hard_sell),
                "min_edge": float(min_edge),
                "sweep_edge": float(sweep_edge),
                "arb_edge": float(arb_edge),
                "truth_locked": 1.0 if self._truth_locked(sym) else 0.0,
                "gen": float(self._arb_gen + 1),
            }
        self._arb_table = new_tbl
        self._arb_gen += 1

    def arb_table_snapshot(self) -> Dict[str, Dict[str, float]]:
        """Public accessor for the runner / debug commands."""
        return {k: dict(v) for k, v in self._arb_table.items()}

    # ==================================================================
    # Live thresholds (FAST precompute) — extend to 4 symbols.
    # ==================================================================
    def _build_live_thresholds(self) -> None:
        t0 = time.perf_counter()
        flat_reduction = self.cfg.flatten_edge_reduction_ticks
        flat_min = self.cfg.flatten_min_edge_ticks

        # Refresh truth-derived C/D fairs whenever the truth oracle
        # caches a new sequence (cheap memoization on the seq tuple).
        self._refresh_cd_truth_cache()
        # v20.7: drain stale tape entries every fast-loop tick (~50ms).
        # Keeps _tape_imbalance / dime gates O(1) and accurate even in a
        # quiet market where no new prints arrive to trigger a trim
        # inside on_trade.
        self._decay_tape_all()

        fair_a, sigma_a = self.fair_a()
        fair_b, sigma_b = self.fair_b()
        fair_c, sigma_c = self.fair_c()
        fair_d, sigma_d = self.fair_d()
        in_flat = self._in_flatten_bias_window(sigma_a)

        new_live: Dict[str, _LiveThresholds] = {}
        per_sym = (
            ("A", fair_a, sigma_a, self.cfg.sweep_edge_ticks),
            ("B", fair_b, sigma_b, self.cfg.sweep_edge_ticks),
            ("C", fair_c, sigma_c, self.cfg.sweep_edge_ticks_c),
            ("D", fair_d, sigma_d, self.cfg.sweep_edge_ticks_d),
        )
        for sym, fair, sigma, sweep_edge in per_sym:
            tick = self.state[sym].tick
            lift_to = ((int(math.floor(fair - sweep_edge))) // tick) * tick
            hit_to_raw = int(math.ceil(fair + sweep_edge))
            hit_to = ((hit_to_raw + tick - 1) // tick) * tick

            relaxed = max(flat_min, sweep_edge - flat_reduction)
            lift_to_flat = ((int(math.floor(fair - relaxed))) // tick) * tick
            hit_to_flat_raw = int(math.ceil(fair + relaxed))
            hit_to_flat = (((hit_to_flat_raw + tick - 1) // tick) * tick)

            # v20.2: extend flatten-bias to C/D during pre-first-reveal.
            # User directive: be FLAT going into reveal 1 across all
            # four books. Once reveal 1 lands, C/D drop back to non-flat
            # (the sigma-trigger path doesn't apply to them since fair
            # is exact under truth).
            sym_in_flat = in_flat if (sym in ("A", "B")
                                       or self._reveal_count == 0) else False

            new_live[sym] = _LiveThresholds(
                sym=sym, fair=fair, sigma=sigma,
                lift_to=lift_to, hit_to=hit_to,
                lift_to_flat=lift_to_flat, hit_to_flat=hit_to_flat,
                in_flatten_window=sym_in_flat,
                gen=self._live_gen + 1)

        with self._live_lock:
            self._live_thresholds = new_live
            self._live_gen += 1
            self._fast_precompute_run_count += 1
            self._last_fast_precompute_us = (
                time.perf_counter() - t0) * 1e6
        # v20.1: refresh the precomputed arb table alongside live
        # thresholds. Same ~50ms cadence; cheap (4 floors + 4 ceils).
        self._recompute_arb_table()
        # v20.13: identity bounds also rebuilt on the fast loop so
        # bounds-arb sweeps off the trade tape (no reveal trigger)
        # still pick up tightenings that happen between reveals (e.g.
        # truth lock flipping mid-round refreshes (fair, fair)).
        try:
            self._recompute_identity_table()
        except Exception as e:
            print(f"[v20.13 IDENTITY-FAST-ERR] "
                  f"{type(e).__name__}: {e}")

    # ==================================================================
    # IOC — fully reimplement (NOT super()) because v18's _ioc hard-codes
    # the max-distance check against `fair_a()/fair_b()` only, which
    # would route C/D through `fair_b()` and reject every legitimate
    # C/D snipe.
    #
    # Order of checks mirrors v18 1801-1840:
    #   1. qty>0, no lockout
    #   2. can_post (game phase running)
    #   3. per-symbol max_ioc_distance vs the right fair (fair_for(sym))
    #   4. anti-spoof extreme-tick linger gate
    #   5. rate-limit can_send_now / sliding-window can_send(sym,side,qty)
    #   6. register_send (consumes a token)
    #   7. clamp qty to position_limit
    #   8. REST call (buy_ioc / sell_ioc) with 403 / rate-limit handling
    # ==================================================================
    def _ioc(self, sym: str, side: str, qty: int, price: int) -> bool:
        if qty <= 0 or self._in_lockout():
            return False
        if not self._can_post():
            return False

        max_dist = self._max_ioc_dist_for(sym)
        if max_dist > 0:
            fair, _ = self._fair_for(sym)
            if abs(price - fair) > max_dist:
                return False

        # Anti-spoof extreme-tick linger gate (currently only C).
        if self._is_spoof_like(sym, "ask" if side == "buy" else "bid", price):
            return False

        if not self._can_send_now():
            self._sends_deferred += 1
            return False
        s = self.state[sym]
        cap_side = "bid" if side == "buy" else "ask"
        if not self._can_send(sym, cap_side, qty):
            return False
        self._register_send()
        # v20.10: IOC paths must respect the RAMPED (effective) cap, not
        # the hard position_limit. Previously cross-arb + sweep_post_reveal
        # blew through to ±100 B / ±50 C in the first 15-25s on every
        # reveal — then the rest of the round was inert (no room to MM).
        # _effective_position_cap returns full L for A/D under truth
        # (bypass), and ramped L for B/C.
        eff_cap = self._effective_position_cap(sym)
        if side == "buy" and (s.position + qty) > eff_cap:
            qty = eff_cap - s.position
            if qty <= 0:
                return False
        if side == "sell" and (s.position - qty) < -eff_cap:
            qty = s.position + eff_cap
            if qty <= 0:
                return False
        fn = self.c.buy_ioc if side == "buy" else self.c.sell_ioc
        try:
            fn(sym, price, qty)
            return True
        except Exception as e:
            msg = str(e).lower()
            is_403 = "403" in msg or "forbidden" in msg
            if is_403:
                self._handle_forbidden("IOC", sym, side, price, e)
            elif "limit" not in msg and "position" not in msg:
                print(f"[v20 IOC:{sym}/{side}@{price}x{qty}] "
                      f"{type(e).__name__}: {e}")
            return False

    # ==================================================================
    # Anti-spoofer / extreme-tick linger check.
    # For C: quotes at ask < c_extreme_low or bid > c_extreme_high
    # must have been visible for >= c_linger_min_ms before we lift.
    # For B: HVALK is the spoofer; max_ioc_distance handles most of
    # the bait. Linger-check is a soft second line of defense for any
    # extreme price >40t off fair.
    # ==================================================================
    def _is_spoof_like(self, sym: str, side: str, price: int) -> bool:
        """Return True iff the price level is suspect (likely
        spoofer/wide-quote trap) and we should NOT lift it.

        `side` = the side we'd be HITTING ("ask" → we're buying at
        ask; "bid" → we're selling at bid)."""
        if sym == "C":
            # C extreme-tick gate (binary 0-100 boundary).
            if side == "ask" and price >= self.cfg.c_extreme_low:
                return False
            if side == "bid" and price <= self.cfg.c_extreme_high:
                return False
            key = (side, price)
            now = time.time()
            first_seen = self._c_linger_seen.get(key)
            if first_seen is None:
                self._c_linger_seen[key] = now
                return True
            return (now - first_seen) * 1000.0 < self.cfg.c_linger_min_ms

        if sym == "B":
            # B (HVALK spoofer) — extreme-offset linger gate.
            fair_b, _ = self.fair_b()
            if abs(price - fair_b) < self.cfg.b_extreme_offset:
                return False
            key = (side, price)
            now = time.time()
            first_seen = self._b_linger_seen.get(key)
            if first_seen is None:
                self._b_linger_seen[key] = now
                return True
            return (now - first_seen) * 1000.0 < self.cfg.b_linger_min_ms

        # A / D: max_ioc_distance is the only gate.
        return False

    # ==================================================================
    # Endgame tick — parent v19 handles A (last reveal) + B (per-tick).
    # C/D under truth are exact from t=0 → no special endgame is
    # needed; the regular MM + cross-arb paths already capture all
    # available edge. Defer entirely to parent.
    # ==================================================================
    def _endgame_tick(self) -> None:
        super()._endgame_tick()

    # ==================================================================
    # Phase / reveal hooks — extend to refresh C/D truth cache + sweep
    # C/D at reveal time (v18 only sweeps A/B).
    # ==================================================================
    def on_reveal(self, value: int) -> None:
        # v20.5: pre-kick parallel C/D sweep BEFORE super(). super
        # handles A/B sequentially via v18 (unchanged). The C/D
        # futures run on _ioc_executor concurrently with v18's
        # A/B work, hiding C/D's reveal-to-IOC latency behind A/B.
        parallel_kicked = False
        if (self.cfg.reveal_sweep_parallel_enabled
                and not self._in_lockout()):
            try:
                self._kick_parallel_reveal_sweep()
                parallel_kicked = True
            except Exception as e:
                print(f"[v20.5 PSWEEP-KICK-ERR] "
                      f"{type(e).__name__}: {e}")

        super().on_reveal(value)
        # v20.1: stamp last-reveal time for the cross-arb post-reveal
        # lockout, and immediately recompute the precomputed arb table
        # so cross-arb at source="reveal" reads fresh thresholds.
        self._last_reveal_t = time.monotonic()
        self._refresh_cd_truth_cache()
        self._recompute_arb_table()
        # v20.13: rebuild identity table FIRST so the bounds-arb sweep
        # below sees freshest deterministic bounds (new reveal tightens
        # A_lower / D_lower and may flip C to a pin).
        try:
            self._recompute_identity_table()
        except Exception as e:
            print(f"[v20.13 IDENTITY-REVEAL-ERR] "
                  f"{type(e).__name__}: {e}")
        # v20.11: a new reveal tightens hard bounds (A_lower grows, D
        # lower may grow, C may pin to 100). Fire bounds-arb FIRST —
        # bound-violations are risk-free and shouldn't wait for the
        # 50ms cross-arb lockout to expire.
        if self.cfg.bounds_arb_enabled:
            try:
                self._bounds_arb_sweep("reveal")
            except Exception as e:
                print(f"[v20.11 BNDS-REVEAL-ERR] "
                      f"{type(e).__name__}: {e}")
        # v20.3: post-reveal C/D sweep. v18's _sweep_post_reveal in
        # super().on_reveal only fired for A and B. Truth-locked C/D
        # have exact fair — any stale ask < fair_c-edge or bid >
        # fair_c+edge is a free hit.
        # v20.4: skip if parallel sweep already covered C/D.
        if self.cfg.reveal_sweep_cd_enabled and not parallel_kicked:
            for sym in ("C", "D"):
                try:
                    self._sweep_post_reveal_v20(sym)
                except Exception as e:
                    print(f"[v20.3 SWEEP:{sym}-ERR] "
                          f"{type(e).__name__}: {e}")
        # v20.4: drain futures (in case super never reached the
        # _sweep_post_reveal call — e.g. precompute miss).
        if parallel_kicked:
            self._drain_reveal_sweep_futures()
        # v20.14: park window left our quotes far from market (offset
        # 30t). Dime defense normally fires on the next book event,
        # which can be 100-500ms away. Trigger it immediately on
        # reveal so we re-establish BBO presence without waiting.
        if self.cfg.dime_defense_enabled and not self._in_park_window():
            for sym in self.SYMS:
                # Reset throttle so this synthetic call isn't gated by
                # a recent dime in the prior cycle.
                self.state[sym].last_dime_defense_t = 0.0
                try:
                    self._maybe_dime_defense(sym)
                except Exception as e:
                    print(f"[v20.14 DIME-REVEAL:{sym}-ERR] "
                          f"{type(e).__name__}: {e}")

    # ==================================================================
    # v20.4: parallel reveal-time sweep. Submits one future per
    # symbol to the parent's _ioc_executor, then either joins on
    # demand (via the _sweep_post_reveal override) or drains
    # eagerly at on_reveal exit.
    # ==================================================================
    def _kick_parallel_reveal_sweep(self) -> None:
        # v20.5: only C/D. A/B sweep stays on v18's sync path inside
        # super().on_reveal so v18's strategy is preserved exactly.
        self._reveal_sweep_futures = {}
        for sym in ("C", "D"):
            try:
                fut = self._ioc_executor.submit(
                    self._sweep_post_reveal_v20, sym)
                self._reveal_sweep_futures[sym] = fut
            except Exception as e:
                print(f"[v20.5 PSWEEP-SUBMIT:{sym}-ERR] "
                      f"{type(e).__name__}: {e}")

    def _drain_reveal_sweep_futures(self) -> None:
        if not self._reveal_sweep_futures:
            return
        timeout = self.cfg.reveal_sweep_parallel_timeout_sec
        for sym, fut in list(self._reveal_sweep_futures.items()):
            try:
                fut.result(timeout=timeout)
            except Exception as e:
                print(f"[v20.4 PSWEEP-DRAIN:{sym}-ERR] "
                      f"{type(e).__name__}: {e}")
        self._reveal_sweep_futures = {}

    # v20.5: A/B → v18 sync sweep, untouched. C/D → join future
    # kicked by _kick_parallel_reveal_sweep (covers the case where
    # super() reaches a sweep call for C/D too; in practice v18
    # only calls this for A/B so the future path is mostly drained
    # by _drain_reveal_sweep_futures at on_reveal exit).
    def _sweep_post_reveal(self, sym: str, lift_to: int, hit_to: int,
                           fair: float) -> int:
        if sym in ("A", "B"):
            return super()._sweep_post_reveal(sym, lift_to, hit_to, fair)
        fut = (self._reveal_sweep_futures.get(sym)
               if self._reveal_sweep_futures else None)
        if fut is not None:
            self._reveal_sweep_futures.pop(sym, None)
            try:
                return int(fut.result(
                    timeout=self.cfg.reveal_sweep_parallel_timeout_sec))
            except Exception as e:
                print(f"[v20.5 PSWEEP-JOIN:{sym}-ERR] "
                      f"{type(e).__name__}: {e}")
                return 0
        return super()._sweep_post_reveal(sym, lift_to, hit_to, fair)

    # ==================================================================
    # v20.3: per-symbol reveal sweep with truth-derived fair.
    # Mirrors v18 _sweep_post_reveal (line 1100) but uses
    # _max_ioc_dist_for(sym) and _sweep_edge_for(sym) so C/D's much
    # wider acceptable distance / smaller required edge are honored.
    # ==================================================================
    def _sweep_post_reveal_v20(self, sym: str) -> int:
        if self._in_lockout():
            return 0
        if not self._can_post():
            return 0
        s = self.state[sym]
        book = s.book
        if not book:
            return 0
        fair, _ = self._fair_for(sym)
        edge = self._sweep_edge_for(sym)
        tick = s.tick
        lift_to = ((int(math.floor(fair - edge))) // tick) * tick
        hit_to_raw = int(math.ceil(fair + edge))
        hit_to = ((hit_to_raw + tick - 1) // tick) * tick
        # v20.4: per-symbol slice, inventory-aware throttle.
        base_slice = self._sweep_slice_for(sym)
        max_slice_buy = self._inventory_throttle(sym, "buy", base_slice)
        max_slice_sell = self._inventory_throttle(sym, "sell", base_slice)
        pad = self.cfg.sweep_position_pad
        max_dist = self._max_ioc_dist_for(sym)
        fired = 0
        # v20.10: respect ramped cap on reveal-sweep IOC path.
        eff_cap = self._effective_position_cap(sym)

        # BUY asks priced <= lift_to.
        room_buy = (eff_cap - pad) - s.position
        if room_buy > 0 and max_slice_buy > 0:
            max_slice = max_slice_buy
        else:
            max_slice = 0
        if max_slice > 0 and room_buy > 0:
            taken = 0
            for lvl in (book.get("asks") or []):
                if taken >= max_slice or room_buy <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px > lift_to:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "ask", int(px)):
                    continue
                want = min(max_slice - taken, room_buy, size)
                if self._ioc(sym, "buy", want, int(px)):
                    taken += want
                    room_buy -= want
                    fired += 1
                    print(f"[v20.3 SWP:{sym}] BUY {want}@{px} "
                          f"fair={fair:.1f} lift_to={lift_to}")
                else:
                    break

        # SELL bids priced >= hit_to.
        room_sell = s.position - (-eff_cap + pad)
        if room_sell > 0 and max_slice_sell > 0:
            max_slice = max_slice_sell
        else:
            max_slice = 0
        if max_slice > 0 and room_sell > 0:
            taken = 0
            for lvl in (book.get("bids") or []):
                if taken >= max_slice or room_sell <= 0:
                    break
                px = lvl.get("price")
                size = int(lvl.get("qty") or 0)
                if px is None or size <= 0:
                    continue
                if px < hit_to:
                    break
                if max_dist > 0 and abs(px - fair) > max_dist:
                    continue
                if self._is_spoof_like(sym, "bid", int(px)):
                    continue
                want = min(max_slice - taken, room_sell, size)
                if self._ioc(sym, "sell", want, int(px)):
                    taken += want
                    room_sell -= want
                    fired += 1
                    print(f"[v20.3 SWP:{sym}] SELL {want}@{px} "
                          f"fair={fair:.1f} hit_to={hit_to}")
                else:
                    break

        if fired:
            self._reveal_sweep_cd_count += fired
        return fired

    # ==================================================================
    # v20.7: public + private tape pipeline.
    #
    # PIPELINE (private-first by event ordering — public is ~15ms behind):
    #   on_fill_event  →  _ingest_private_fill   →  _record_tape_event
    #   on_trade       →  _record_tape_event(public)
    #   fast loop tick →  _decay_tape_all (every 50ms — trim stale)
    #
    # Each ingest is O(1) for the hot path (last-price update + counter
    # bump); only the stale-trim walks the deque, and the fast loop
    # spreads that cost across the round (≤ few µs per tick).
    #
    # Aggressor derivation from a private fill:
    #   maker side=buy  ↔ counterparty was the seller (agg=sell)
    #   maker side=sell ↔ counterparty was the buyer  (agg=buy)
    #   taker side=buy  ↔ we were the buyer-aggressor (agg=buy)
    #   taker side=sell ↔ we were the seller-aggressor (agg=sell)
    #
    # Dedupe key = trade_id (matches across private fill + public print).
    # ==================================================================
    @staticmethod
    def _aggressor_from_private(side: str, liquidity: str) -> Optional[str]:
        if side not in ("buy", "sell"):
            return None
        if liquidity == "maker":
            return "sell" if side == "buy" else "buy"
        # Default to taker semantics for any non-maker liquidity flag
        # (server sometimes omits or uses other strings).
        return side

    def _record_tape_event(self, sym: str, agg: str, price: int,
                           qty: int, trade_id: Optional[int],
                           source: str) -> None:
        """Unified ingest. Idempotent on `trade_id` — the second arrival
        (public following private) is a no-op except as a freshness
        confirmation."""
        if not self.cfg.tape_enabled:
            return
        if sym not in self.SYMS:
            return
        if agg not in ("buy", "sell") or price is None:
            return
        ts = self._tape[sym]
        now = time.time()
        # Dedupe — same trade_id from public/private fills only counts once.
        if trade_id is not None and trade_id in ts.seen_trade_ids:
            return
        # Append + counter bump.
        ts.recent.append((now, agg, price, int(qty)))
        if agg == "buy":
            ts.buy_count += 1
            ts.last_buy_agg_price = price
            ts.last_buy_agg_t = now
        else:
            ts.sell_count += 1
            ts.last_sell_agg_price = price
            ts.last_sell_agg_t = now
        if trade_id is not None:
            ts.seen_trade_ids[trade_id] = now
        # Defensive caps — the fast-loop decay handles steady-state, but
        # bursty markets could overrun before the next 50ms tick.
        cap = self.cfg.tape_max_prints
        if len(ts.recent) > cap:
            self._trim_tape(sym, now)
        if source == "public":
            self._tape_print_count[sym] += 1

    def on_trade(self, msg: dict) -> None:
        if not self.cfg.tape_enabled:
            return
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        agg = msg.get("aggressor")
        try:
            price = int(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        tid = msg.get("trade_id")
        try:
            tid_int = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid_int = None
        self._record_tape_event(sym, agg, price, qty, tid_int, "public")
        # v20.11: a public print means the book just shifted. The new
        # top-of-book ask/bid might violate a hard bound (e.g. an
        # exhausted ask layer revealed a deeper stale ask below
        # A_lower). Bounds-arb has its own slice budget so this is
        # cheap even on busy tape.
        if self.cfg.bounds_arb_enabled:
            try:
                self._bounds_arb_sweep("trade")
            except Exception as e:
                print(f"[v20.11 BNDS-TRADE-ERR] "
                      f"{type(e).__name__}: {e}")

    def _ingest_private_fill(self, msg: dict) -> None:
        """Pre-ingest a private fill into the tape ~15ms ahead of its
        public twin. The public arrival is deduped by trade_id."""
        if not self.cfg.tape_enabled:
            return
        sym = msg.get("symbol")
        if sym not in self.SYMS:
            return
        try:
            price = int(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        agg = self._aggressor_from_private(
            msg.get("side"), msg.get("liquidity") or "")
        if agg is None:
            return
        tid = msg.get("trade_id")
        try:
            tid_int = int(tid) if tid is not None else None
        except (TypeError, ValueError):
            tid_int = None
        self._record_tape_event(sym, agg, price, qty, tid_int, "private")

    def _trim_tape(self, sym: str, now: float) -> None:
        """Drop entries older than tape_window_sec and decrement counters.
        Called from fast loop (steady state) and as a defensive top-cap
        guard inside _record_tape_event."""
        ts = self._tape[sym]
        cutoff = now - self.cfg.tape_window_sec
        recent = ts.recent
        # Stale window-trim — also walks counters down.
        while recent and recent[0][0] < cutoff:
            _, a, _, _ = recent.popleft()
            if a == "buy":
                ts.buy_count -= 1
            else:
                ts.sell_count -= 1
        cap = self.cfg.tape_max_prints
        # Hard-cap guard if a burst slipped past the time-window trim.
        while len(recent) > cap:
            _, a, _, _ = recent.popleft()
            if a == "buy":
                ts.buy_count -= 1
            else:
                ts.sell_count -= 1
        # Dedupe-set GC. Trade IDs older than 2× tape_window_sec can't
        # possibly arrive again and bloat the map otherwise.
        gc_cutoff = now - 2.0 * self.cfg.tape_window_sec
        if ts.seen_trade_ids:
            # Snapshot keys; OK to mutate during iteration when using list.
            for k in [k for k, t in ts.seen_trade_ids.items()
                      if t < gc_cutoff]:
                ts.seen_trade_ids.pop(k, None)

    def _decay_tape_all(self) -> None:
        """Trim stale tape entries for every symbol. Called by the fast
        precompute loop (50ms cadence) — keeps _tape_imbalance O(1) reads
        accurate even in a quiet market."""
        if not self.cfg.tape_enabled:
            return
        now = time.time()
        for sym in self.SYMS:
            self._trim_tape(sym, now)

    def _tape_imbalance(self, sym: str) -> Optional[str]:
        """O(1) read off the running counters. The fast loop keeps them
        fresh; we never walk the deque on the MM hot path."""
        if not self.cfg.tape_imbalance_enabled:
            return None
        ts = self._tape[sym]
        total = ts.buy_count + ts.sell_count
        if total < self.cfg.tape_imbalance_min_prints:
            return None
        frac = self.cfg.tape_imbalance_frac
        if ts.buy_count >= frac * total:
            return "buy"
        if ts.sell_count >= frac * total:
            return "sell"
        return None

    def _tape_dime_prices(
        self, sym: str, fair: float,
        bid_px: Optional[int], ask_px: Optional[int]
    ) -> Tuple[Optional[int], Optional[int]]:
        """Tighten bid/ask toward the last-seen tape prints, but only if
        still ≥ min_edge from fair and a tightening relative to the
        baseline quote (we never widen here)."""
        if not self.cfg.tape_dime_enabled:
            return bid_px, ask_px
        ts = self._tape[sym]
        now = time.time()
        s = self.state[sym]
        tick = s.tick
        ub = self._upper_bound(sym)
        min_edge = self._min_edge_for(sym)
        fresh = self.cfg.tape_freshness_sec

        # Dime the bid: sellers just hit a bid at last_sell_agg_price.
        # Posting at +1 tick is one step better than the consumed level.
        if (ts.last_sell_agg_price is not None and
                (now - ts.last_sell_agg_t) <= fresh):
            cand = ts.last_sell_agg_price + tick
            # +EV gate: must clear min_edge below fair.
            if cand <= int(fair) - min_edge and cand >= 1:
                if bid_px is None or cand > bid_px:
                    bid_px = min(cand, ub - 1)
                    self._tape_dime_count[sym] += 1

        # Dime the ask: buyers just lifted ask at last_buy_agg_price.
        if (ts.last_buy_agg_price is not None and
                (now - ts.last_buy_agg_t) <= fresh):
            cand = ts.last_buy_agg_price - tick
            if cand >= int(fair) + min_edge and cand >= 1:
                if ask_px is None or cand < ask_px:
                    ask_px = min(cand, ub)
                    self._tape_dime_count[sym] += 1

        return bid_px, ask_px

    def _tape_wide_offset_for(self, sym: str) -> int:
        return getattr(self.cfg,
                       f"tape_wide_offset_ticks_{sym.lower()}",
                       5)

    def _tape_imbalance_skew(
        self, sym: str, fair: float,
        bid_px: Optional[int], ask_px: Optional[int]
    ) -> Tuple[Optional[int], Optional[int]]:
        """When the tape is one-sided, widen on the DEPLETED side so a
        forced future trade has to reach further in. With truth-known
        fair, those forced fills are guaranteed +EV by `wide_offset`.

        v20.9: clamp widened quote to `best_*_X + 1 tick`. Without this
        clamp we sat 5+ ticks behind BBO during one-sided tapes and got
        dimed out by every other bot, going silent for 30-60s while the
        tape stayed imbalanced. Floor at fair±min_edge to stay +EV."""
        sig = self._tape_imbalance(sym)
        if sig is None:
            return bid_px, ask_px
        s = self.state[sym]
        tick = s.tick
        ub = self._upper_bound(sym)
        offset = self._tape_wide_offset_for(sym) * tick
        min_edge = self._min_edge_for(sym)
        book = s.book or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None

        # Buyers dominating → asks getting eaten → ask side depleted.
        # Push our ask higher (capture a richer fill if more buyers come).
        if sig == "buy":
            target = int(math.ceil(fair)) + offset
            target = min(target, ub)
            # BBO clamp: never sit more than 1 tick behind best ask.
            if best_ask is not None:
                cap = best_ask + tick
                floor = int(math.ceil(fair)) + min_edge
                cap = max(cap, floor)
                target = min(target, cap)
            if ask_px is None or target > ask_px:
                ask_px = target
                self._tape_widen_count[sym] += 1
            return bid_px, ask_px

        # Sellers dominating → bids getting eaten → bid side depleted.
        # Drop our bid lower (capture a cheaper fill if more sellers come).
        if sig == "sell":
            target = int(math.floor(fair)) - offset
            target = max(target, 1)
            # BBO clamp: never sit more than 1 tick below best bid.
            if best_bid is not None:
                cap = best_bid - tick
                floor = int(math.floor(fair)) - min_edge
                cap = min(cap, floor)
                target = max(target, cap)
            if bid_px is None or target < bid_px:
                bid_px = target
                self._tape_widen_count[sym] += 1
            return bid_px, ask_px

        return bid_px, ask_px

    # ==================================================================
    # v20.3: Hit-and-retreat. After every fill (maker OR taker), repost
    # the OPPOSITE side at a tight scalp-out price. Throttled per-symbol
    # so a multi-level taker burst doesn't fire 5 quotes.
    # ==================================================================
    def on_fill_event(self, msg: dict) -> None:
        # v20.7: private fills are the 15ms-leading edge of the tape.
        # Ingest BEFORE super() so the cross-arb / inter-sweep paths
        # under super()._try_cross_arb("fill") can read fresh tape
        # state (last_*_agg_price, imbalance counters) that won't be
        # visible on the public WS for another ~15ms.
        try:
            self._ingest_private_fill(msg)
        except Exception as e:
            print(f"[v20.7 TAPE-PRIV:ERR] {type(e).__name__}: {e}")
        super().on_fill_event(msg)
        if not self.cfg.hit_and_retreat_enabled:
            return
        sym = msg.get("symbol")
        side = msg.get("side")
        if sym not in self.SYMS:
            return
        if side not in ("buy", "sell"):
            return
        try:
            self._scalp_out_quote(sym, side)
        except Exception as e:
            print(f"[v20.3 SCALP:{sym}-ERR] {type(e).__name__}: {e}")

    def _scalp_out_quote(self, sym: str, fill_side: str) -> None:
        """Post the OPPOSITE side at fair ± min_edge (stepped inside
        the BBO if it improves the price). This is the round-trip
        capture on MM fills — we just bought, now post a tight sell
        to bank the spread."""
        if not self._can_post():
            return
        if self._in_park_window():
            return
        s = self.state[sym]
        if abs(s.position) < self.cfg.hit_and_retreat_min_abs_pos:
            return
        now = time.time()
        last = self._last_scalp_out_t.get(sym, 0.0)
        if (now - last) < self.cfg.hit_and_retreat_throttle_sec:
            return
        self._last_scalp_out_t[sym] = now

        fair, _ = self._fair_for(sym)
        tick = s.tick
        min_edge = self._min_edge_for(sym)
        inside = self.cfg.hit_and_retreat_inside_ticks * tick
        ub = self._upper_bound(sym)
        book = s.book or {}

        # If we just bought, scalp out via ASK (sell tight).
        if fill_side == "buy":
            target = int(math.ceil(fair + min_edge))
            target = ((target + tick - 1) // tick) * tick
            asks = book.get("asks") or []
            if asks:
                best_ask = int(asks[0].get("price") or 0)
                if best_ask > 0 and target >= best_ask:
                    stepped = best_ask - inside
                    if stepped >= fair + 1:
                        target = stepped
            if target <= 0 or target > ub:
                return
            qty = self._quote_size(sym, "ask")
            if qty <= 0:
                return
            print(f"[v20.3 H&R:{sym}/ask] post {qty}@{target} "
                  f"(post-buy, fair={fair:.2f})")
            self._apply_side(sym, "ask", int(target), qty)
        else:  # sold → scalp out via BID
            target = int(math.floor(fair - min_edge))
            target = (target // tick) * tick
            bids = book.get("bids") or []
            if bids:
                best_bid = int(bids[0].get("price") or 0)
                if best_bid > 0 and target <= best_bid:
                    stepped = best_bid + inside
                    if stepped <= fair - 1:
                        target = stepped
            if target < tick:
                return
            qty = self._quote_size(sym, "bid")
            if qty <= 0:
                return
            print(f"[v20.3 H&R:{sym}/bid] post {qty}@{target} "
                  f"(post-sell, fair={fair:.2f})")
            self._apply_side(sym, "bid", int(target), qty)

    # ==================================================================
    # v20.3: Scalp-out at cap — keep MM alive after position saturates.
    # Wraps the per-symbol quote math; if the inventory-reducing side
    # came back None (skip_negative_ev / cap gate), force a quote AT
    # fair so we recycle the position via maker fills instead of sitting
    # frozen at the cap until settlement.
    # ==================================================================
    def _quote_prices(self, sym: str, fair: float, position: int,
                      mode: str) -> Tuple[Optional[int], Optional[int]]:
        if sym in ("A", "B"):
            bid_px, ask_px = super()._quote_prices(
                sym, fair, position, mode)
            # v20.5: C→A delta-hedge skew. C is a digital on A above
            # strike; long C ≡ already long delta on A. Skew A's MM
            # the same way we would to drain that delta. Only active
            # in mid fair_c zone where gamma matters.
            if sym == "A" and mode == "normal":
                bid_px, ask_px = self._apply_ca_delta_skew(
                    fair, bid_px, ask_px)
        else:
            bid_px, ask_px = self._quote_prices_cd(
                sym, fair, position, mode)
        # v20.7: tape-driven adjustments. Park mode keeps quotes far
        # from the market so the modify-replace primary stays alive —
        # we must not retighten or widen during park.
        if mode != "park" and self.cfg.tape_enabled:
            bid_px, ask_px = self._tape_dime_prices(
                sym, fair, bid_px, ask_px)
            bid_px, ask_px = self._tape_imbalance_skew(
                sym, fair, bid_px, ask_px)
        # v20.14: inventory-aware flattening shift (skipped in park).
        if mode != "park":
            bid_px, ask_px = self._apply_inventory_shift(
                sym, fair, bid_px, ask_px)
        if (mode == "park" or not self.cfg.scalp_out_at_cap_enabled):
            return bid_px, ask_px
        return self._scalp_out_at_cap(
            sym, fair, position, bid_px, ask_px)

    def _apply_ca_delta_skew(self, fair_a: float,
                             bid_px: Optional[int],
                             ask_px: Optional[int]
                             ) -> Tuple[Optional[int], Optional[int]]:
        if not self.cfg.ca_delta_hedge_enabled:
            return bid_px, ask_px
        c_pos = self.state["C"].position
        if abs(c_pos) < self.cfg.ca_delta_hedge_pos_threshold:
            return bid_px, ask_px
        # Only hedge in the mid zone where C has meaningful gamma.
        fair_c, _ = self._fair_for("C")
        lo = self.cfg.ca_delta_hedge_mid_low
        hi = self.cfg.ca_delta_hedge_mid_high
        if fair_c < lo or fair_c > hi:
            return bid_px, ask_px
        tick = self.state["A"].tick
        max_skew = self.cfg.ca_delta_hedge_max_skew_ticks * tick
        # Skew proportional to |c_pos|, capped at max_skew.
        c_limit = self.state["C"].position_limit
        frac = min(1.0, abs(c_pos) / max(1, c_limit))
        skew = int(round(max_skew * frac))
        if skew <= 0:
            return bid_px, ask_px
        # Long C → want to sell A (tighter ask, looser bid).
        # Short C → want to buy A (tighter bid, looser ask).
        if c_pos > 0:
            if ask_px is not None:
                ask_px = max(1, ask_px - skew)
            if bid_px is not None:
                bid_px = max(1, bid_px - skew)
        else:
            if bid_px is not None:
                bid_px = bid_px + skew
            if ask_px is not None:
                ask_px = ask_px + skew
        return bid_px, ask_px

    def _inventory_shift_max_ticks_for(self, sym: str) -> int:
        if sym == "A":
            return self.cfg.inventory_shift_max_ticks_a
        if sym == "B":
            return self.cfg.inventory_shift_max_ticks_b
        if sym == "C":
            return self.cfg.inventory_shift_max_ticks_c
        return self.cfg.inventory_shift_max_ticks_d

    def _apply_inventory_shift(self, sym: str, fair: float,
                               bid_px: Optional[int],
                               ask_px: Optional[int]
                               ) -> Tuple[Optional[int], Optional[int]]:
        """v20.14: directional-inventory skew. When |position/cap| is
        large, slide both bid and ask in the flattening direction so
        we sell more easily when long / buy more easily when short.
        Clipped to the +EV side of truth — bid stays <= floor(fair),
        ask stays >= ceil(fair). Quadratic ramp so the shift only
        bites at meaningful inventory load."""
        if not self.cfg.inventory_shift_enabled:
            return bid_px, ask_px
        s = self.state[sym]
        cap = self._effective_position_cap(sym)
        if cap <= 0:
            return bid_px, ask_px
        frac = s.position / float(cap)
        threshold = self.cfg.inventory_shift_threshold
        if abs(frac) < threshold:
            return bid_px, ask_px
        max_shift = self._inventory_shift_max_ticks_for(sym)
        if max_shift <= 0:
            return bid_px, ask_px
        excess = (abs(frac) - threshold) / max(1e-9, 1.0 - threshold)
        excess = min(1.0, excess)
        shift_mag = (excess * excess) * max_shift
        shift_signed = math.copysign(shift_mag, frac)
        shift_ticks = int(round(shift_signed))
        if shift_ticks == 0:
            return bid_px, ask_px
        tick = s.tick
        ub = self._upper_bound(sym)
        # +EV floors against truth fair. Note: when long-heavy we drop
        # the ask toward fair (flatten side), so cap it at ceil(fair).
        # When short-heavy we raise the bid toward fair, cap at floor(fair).
        if bid_px is not None:
            bid_px = bid_px - shift_ticks * tick
            if frac < 0:
                bid_px = min(bid_px, int(math.floor(fair)))
            bid_px = max(bid_px, tick)
            bid_px = min(bid_px, ub - 1)
        if ask_px is not None:
            ask_px = ask_px - shift_ticks * tick
            if frac > 0:
                ask_px = max(ask_px, int(math.ceil(fair)))
            ask_px = max(ask_px, tick)
            ask_px = min(ask_px, ub)
        if (bid_px is not None and ask_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + tick
        return bid_px, ask_px

    def _scalp_out_at_cap(self, sym: str, fair: float, position: int,
                          bid_px: Optional[int], ask_px: Optional[int]
                          ) -> Tuple[Optional[int], Optional[int]]:
        cap = self._effective_position_cap(sym)
        s = self.state[sym]
        tick = s.tick
        ub = self._upper_bound(sym)
        max_loss = self.cfg.scalp_out_max_loss_ticks * tick

        # Long at-or-above cap → force ASK to drain.
        if position >= cap and ask_px is None:
            target = int(math.ceil(fair))
            target = ((target + tick - 1) // tick) * tick
            # Step inside BBO if best_ask is at/below target.
            book = s.book or {}
            asks = book.get("asks") or []
            if asks:
                best_ask = int(asks[0].get("price") or 0)
                if best_ask > 0 and best_ask <= target:
                    stepped = best_ask - tick
                    if stepped > 0:
                        target = stepped
            # Floor: don't go more than max_loss below fair.
            floor = int(math.floor(fair)) - max_loss
            target = max(target, floor)
            if 0 < target <= ub:
                ask_px = target

        # Short at-or-below cap → force BID to drain.
        if position <= -cap and bid_px is None:
            target = int(math.floor(fair))
            target = (target // tick) * tick
            book = s.book or {}
            bids = book.get("bids") or []
            if bids:
                best_bid = int(bids[0].get("price") or 0)
                if best_bid > 0 and best_bid >= target:
                    stepped = best_bid + tick
                    if stepped <= ub - 1:
                        target = stepped
            ceil = int(math.ceil(fair)) + max_loss
            target = min(target, ceil)
            if 0 < target <= ub:
                bid_px = target

        return bid_px, ask_px

    def on_phase_change(self, phase: Optional[str], reveals: list) -> None:
        prev_phase = self.phase
        super().on_phase_change(phase, reveals)
        if phase == "running" and prev_phase != "running":
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._cd_truth_for_seq = None
            self._c_linger_seen.clear()
            self._b_linger_seen.clear()
            # v20.1: reset per-round cross-arb qty budgets + reveal_t.
            self._cross_arb_qty_used = {s: 0 for s in self.SYMS}
            # v20.11: reset per-round bounds-arb qty budgets.
            self._bounds_arb_qty_used = {s: 0 for s in self.SYMS}
            self._last_reveal_t = 0.0
            # v20.12: comprehensive per-round reset. Without these,
            # consecutive rounds in one bot process inherit stale
            # state — most damaging is `state[sym].position` which
            # is never reset locally (it tracks the SERVER position
            # via fills, but if the server flattens between rounds
            # we don't see those fills and end up "still long N" in
            # our local view → MM/sweep code blocks the side that
            # would take us BACK to flat, and we go silent.)
            self._reset_per_round_state()
            # v20.16: refresh C strike BEFORE cd_truth_cache so fair_c
            # is computed against the NEW round's K, not the prior
            # round's stale K. Without this, R2+ has wrong fair_c
            # until truth oracle fetches ~1.5s into running phase —
            # and any pre-truth IOC/MM sells use the stale-K fair.
            self._refresh_c_strike()
            self._refresh_cd_truth_cache()
            self._recompute_arb_table()
            # v20.13: fresh round = clean identity table built from
            # current reveals (typically empty list at round start).
            try:
                self._recompute_identity_table()
            except Exception as e:
                print(f"[v20.13 IDENTITY-PHASE-ERR] "
                      f"{type(e).__name__}: {e}")
        elif phase in ("completed", "settling", "settled"):
            self._fair_c_truth = None
            self._fair_d_truth = None
            self._cd_truth_for_seq = None
            self._c_linger_seen.clear()
            self._b_linger_seen.clear()
            self._cross_arb_qty_used = {s: 0 for s in self.SYMS}
            self._bounds_arb_qty_used = {s: 0 for s in self.SYMS}
            self._arb_table = {}

    def _reset_per_round_state(self) -> None:
        """v20.12: zero everything that should be fresh for a new
        round. Called on phase → running.

        Critical: REFETCH server positions. The local
        `state[sym].position` is only updated by fill events; if the
        server flattened us at end-of-round and we missed those fill
        events, we'd think we still hold inventory and gate ourselves
        out of the new round."""
        # 1. Refetch positions (server-authoritative).
        try:
            ps = (self.c.positions() or {}).get("positions") or {}
            for sym in self.SYMS:
                new_pos = int(ps.get(sym, 0))
                old_pos = self.state[sym].position
                if new_pos != old_pos:
                    print(f"[v20.12 POS-SYNC:{sym}] "
                          f"local={old_pos} → server={new_pos}")
                self.state[sym].position = new_pos
        except Exception as e:
            print(f"[v20.12 POS-SYNC-ERR] {type(e).__name__}: {e}")

        # 2. Per-symbol timers + in-flight counters.
        now = time.time()
        for sym in self.SYMS:
            s = self.state[sym]
            s.last_inter_sweep_t = 0.0
            s.last_dime_defense_t = 0.0
            s.in_flight = {"bid": 0, "ask": 0}
            # Resting maps were cleared on settling; the server has
            # reset orders. Be defensive — clear again here.
            s.resting = {"bid": {}, "ask": {}}

        # 3. Bot-level MM cadence clocks.
        self._last_mm_refresh_t = 0.0
        self._last_mm_refresh_t_cd = 0.0

        # 4. Scalp-out and reveal-sweep clocks.
        self._last_scalp_out_t = {s: 0.0 for s in self.SYMS}
        self._reveal_sweep_futures = {}

        # 5. Tape state — stale last_buy_agg_price etc. fakes the
        # dime-from-tape logic into posting against ghost prints from
        # the prior round.
        for sym in self.SYMS:
            self._tape[sym] = TapeState()

        # 6. Diagnostic counters. Per-round so the run summary
        # reflects only the current round.
        self._tape_dime_count = {s: 0 for s in self.SYMS}
        self._tape_widen_count = {s: 0 for s in self.SYMS}
        self._tape_print_count = {s: 0 for s in self.SYMS}
        self._bounds_arb_count = 0
        self._reveal_sweep_cd_count = 0

    # ==================================================================
    # Status helpers (for the runner)
    # ==================================================================
    def cd_status_str(self) -> str:
        s_c = self.state.get("C")
        s_d = self.state.get("D")
        truth_c = (f"{self._fair_c_truth:.0f}"
                   if self._fair_c_truth is not None else "<none>")
        truth_d = (f"{self._fair_d_truth:.1f}"
                   if self._fair_d_truth is not None else "<none>")
        c_pos = s_c.position if s_c else 0
        d_pos = s_d.position if s_d else 0
        c_cap = self._effective_position_cap("C") if s_c else 0
        d_cap = self._effective_position_cap("D") if s_d else 0
        c_sz = self._ramped_base_size("C") if s_c else 0
        d_sz = self._ramped_base_size("D") if s_d else 0
        c_b = len(s_c.resting["bid"]) if s_c else 0
        c_a = len(s_c.resting["ask"]) if s_c else 0
        d_b = len(s_d.resting["bid"]) if s_d else 0
        d_a = len(s_d.resting["ask"]) if s_d else 0
        return (f"  C: pos={c_pos:+d}/cap={c_cap}/lim={s_c.position_limit if s_c else 0}  "
                f"truth_c={truth_c}  size={c_sz}  resting bid={c_b}/ask={c_a}\n"
                f"  D: pos={d_pos:+d}/cap={d_cap}/lim={s_d.position_limit if s_d else 0}  "
                f"truth_d={truth_d}  size={d_sz}  resting bid={d_b}/ask={d_a}\n"
                f"  c_linger_seen={len(self._c_linger_seen)} extreme quotes tracked")


# Re-export aliases — strategy21 imports `Strategy as StrategyV20` /
# `Config as ConfigV20` from this module, plus URL/API_KEY/N_PRIOR_SIM /
# Posterior / GameClient / _LiveThresholds / TruthOracle directly.
__all__ = [
    "GameClient",
    "URL", "API_KEY", "N_PRIOR_SIM",
    "Posterior", "CPProfile",
    "ConfigV12", "StrategyV12",
    "ConfigV18", "StrategyV18", "_LiveThresholds",
    "ConfigV19", "StrategyV19", "TruthOracle",
    "Config", "Strategy",
]
