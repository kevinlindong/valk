import type { Book, Order, Positions, GameState, Tif, Side } from "./types";

// Default matches strategy12.py; override with VITE_API_KEY in .env.local.
export const API_KEY =
  (import.meta.env.VITE_API_KEY as string | undefined) ?? "intern2-KEVD";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "X-API-Key": API_KEY,
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => "");
    throw new Error(`${r.status} ${r.statusText}${txt ? `: ${txt}` : ""}`);
  }
  return (await r.json()) as T;
}

export const api = {
  gameState: () => req<GameState>("/game"),
  positions: () => req<{ positions: Positions }>("/positions"),
  myOrders: (symbol?: string) =>
    req<{ orders: Order[] }>(`/orders${symbol ? `?symbol=${symbol}` : ""}`),
  book: (symbol: string, depth = 10) =>
    req<Book & { symbol: string }>(`/book?symbol=${symbol}&depth=${depth}`),
  place: (
    symbol: string,
    side: Side,
    qty: number,
    price: number | null,
    tif: Tif = "gtc",
    type: "limit" | "market" = "limit"
  ) =>
    req<{ order: Order; trades: any[] }>("/order", {
      method: "POST",
      body: JSON.stringify({
        symbol,
        side,
        type,
        qty,
        tif,
        ...(price !== null ? { price } : {}),
      }),
    }),
  cancel: (id: number) =>
    req<{ ok: boolean }>(`/order/${id}`, { method: "DELETE" }),
  cancelAll: (symbol?: string) =>
    req<{ cancelled: number }>(
      `/orders${symbol ? `?symbol=${symbol}` : ""}`,
      { method: "DELETE" }
    ),
};

export type WSStatus = "connecting" | "open" | "closed";

export function connectWS(
  onMsg: (m: any) => void,
  onStatus: (s: WSStatus) => void
): { close: () => void } {
  let ws: WebSocket | null = null;
  let stopped = false;
  let backoff = 500;

  const open = () => {
    if (stopped) return;
    onStatus("connecting");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/private?api_key=${encodeURIComponent(API_KEY)}`;
    ws = new WebSocket(url);
    ws.addEventListener("open", () => {
      backoff = 500;
      onStatus("open");
    });
    ws.addEventListener("message", (e) => {
      try {
        onMsg(JSON.parse(e.data));
      } catch {}
    });
    ws.addEventListener("close", () => {
      onStatus("closed");
      if (!stopped) {
        setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, 5000);
      }
    });
    ws.addEventListener("error", () => {
      try {
        ws?.close();
      } catch {}
    });
  };
  open();
  return {
    close: () => {
      stopped = true;
      try {
        ws?.close();
      } catch {}
    },
  };
}
