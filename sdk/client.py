"""Python client for the trading-game exchange.

Multi-instrument: every order/cancel/book call takes a `symbol`. Incoming WS
messages are tagged with `symbol` where applicable.

Usage:
    from sdk.client import GameClient
    c = GameClient(base_url="http://localhost:8000", api_key="intern1-key-aaaa")
    c.on_fill   = lambda m: print("FILL  ", m["symbol"], m)
    c.on_trade  = lambda m: print("TRADE ", m["symbol"], m)
    c.on_book   = lambda m: print("BOOK  ", m["symbol"], m["bids"][:1], m["asks"][:1])
    c.on_reveal = lambda m: print("REVEAL", m)
    c.on_ack    = lambda m: print("ACK   ", m)
    c.start()

    order = c.buy("A", price=50, qty=5)              # GTC limit (default)
    c.buy_ioc("A", price=50, qty=5)                   # limit IOC — kill residual
    c.buy_fok("A", price=50, qty=5)                   # all-or-nothing
    c.modify(order["order"]["order_id"], qty=3)       # qty-down keeps priority
    c.modify(order["order"]["order_id"], price=51)    # price change loses priority
    c.cancel(order["order"]["order_id"])
    c.sell_market("B", qty=2)
    print(c.book("A"))
    print(c.book())              # all instruments
    print(c.positions())         # per-symbol map

    c.wait_forever()

Every WS message includes `sent_ns` (server send time, time.time_ns), so
clients can measure their own end-to-end latency.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable, Optional

import requests
import websockets


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
