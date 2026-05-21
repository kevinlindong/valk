// Default matches strategy12.py; override with VITE_API_KEY in .env.local.
export const API_KEY = import.meta.env.VITE_API_KEY ?? "intern2-KEVD";
async function req(path, init) {
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
    return (await r.json());
}
export const api = {
    gameState: () => req("/game"),
    positions: () => req("/positions"),
    myOrders: (symbol) => req(`/orders${symbol ? `?symbol=${symbol}` : ""}`),
    book: (symbol, depth = 10) => req(`/book?symbol=${symbol}&depth=${depth}`),
    place: (symbol, side, qty, price, tif = "gtc", type = "limit") => req("/order", {
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
    cancel: (id) => req(`/order/${id}`, { method: "DELETE" }),
    cancelAll: (symbol) => req(`/orders${symbol ? `?symbol=${symbol}` : ""}`, { method: "DELETE" }),
};
export function connectWS(onMsg, onStatus) {
    let ws = null;
    let stopped = false;
    let backoff = 500;
    const open = () => {
        if (stopped)
            return;
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
            }
            catch { }
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
            }
            catch { }
        });
    };
    open();
    return {
        close: () => {
            stopped = true;
            try {
                ws?.close();
            }
            catch { }
        },
    };
}
