# valk trader

Minimal dark-mode web UI for manually trading alongside `strategy16.py`. Connects to
the same game server with the same API key, so positions/fills/resting orders
include anything the strategy posts.

## Run

```bash
bun install
bun run dev
```

Open <http://localhost:5173>.

To point at a different game server:

```bash
GAME_URL=http://192.168.50.167:8000 bun run dev
```

Override the API key with `.env.local`:

```
VITE_API_KEY=intern2-KEVD
```

## What it shows

- Phase, running sum, reveals, live position per symbol
- Top-of-book + 8-level depth, click a price to preload it into the form
- Quick action: `HIT BID` (IOC sell at best bid) / `LIFT ASK` (IOC buy at best ask)
- My resting orders with one-click cancel + `cancel all`
- Manual order form (side / qty / price / TIF: gtc/ioc/fok)
- Live fills tape with counterparty, liquidity, fee

REST and WebSocket are proxied through Vite at `/api/*` and `/ws/private`.
