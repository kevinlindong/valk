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


class GameClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._ws_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

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
        r = requests.post(f"{self.base_url}/api/order", json=body, headers=self._headers())
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
        r = requests.patch(
            f"{self.base_url}/api/order/{order_id}", json=body, headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    def cancel(self, order_id: int) -> dict:
        r = requests.delete(f"{self.base_url}/api/order/{order_id}", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def cancel_all(self, symbol: Optional[str] = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        r = requests.delete(f"{self.base_url}/api/orders", headers=self._headers(), params=params)
        r.raise_for_status()
        return r.json()

    def my_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        r = requests.get(f"{self.base_url}/api/orders", headers=self._headers(), params=params)
        r.raise_for_status()
        return r.json()["orders"]

    def book(self, symbol: Optional[str] = None, depth: int = 10) -> dict:
        params: dict[str, Any] = {"depth": depth}
        if symbol:
            params["symbol"] = symbol
        r = requests.get(f"{self.base_url}/api/book", params=params)
        r.raise_for_status()
        return r.json()

    def positions(self) -> dict:
        r = requests.get(f"{self.base_url}/api/positions", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def game_state(self) -> dict:
        r = requests.get(f"{self.base_url}/api/game")
        r.raise_for_status()
        return r.json()

    def instruments(self) -> dict:
        r = requests.get(f"{self.base_url}/api/instruments")
        r.raise_for_status()
        return r.json()

    def leaderboard(self) -> list:
        r = requests.get(f"{self.base_url}/api/leaderboard")
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
