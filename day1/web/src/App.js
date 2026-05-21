import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, connectWS } from "./api";
const DEFAULT_SYMBOLS = ["A", "B", "C", "D"];
const EMPTY_BOOK = { bids: [], asks: [] };
const DEPTH = 50;
const MAX_FILLS = 500;
export function App() {
    const [ws, setWs] = useState("closed");
    const [game, setGame] = useState(null);
    const [positions, setPositions] = useState({});
    const [orders, setOrders] = useState({});
    const [books, setBooks] = useState({});
    const [fills, setFills] = useState([]);
    const [tradeCash, setTradeCash] = useState({});
    const [tickPnl, setTickPnl] = useState({});
    const [feesBySym, setFeesBySym] = useState({});
    // True (settlement) prices for each market. Populated only on settlement;
    // null during the running round so per-trade True PnL renders as "—".
    const [truePrices, setTruePrices] = useState(null);
    const [rlCount, setRlCount] = useState(0);
    const [lastRl, setLastRl] = useState(null);
    const [rlFlash, setRlFlash] = useState(0);
    const [toasts, setToasts] = useState([]);
    // Anchor for round-relative time. Updated on each game_state w/ running phase.
    const gameAnchorRef = useRef(null);
    // Tracks the last seen phase so we can detect new-game starts.
    const prevPhaseRef = useRef(null);
    const computeElapsedMs = () => {
        const a = gameAnchorRef.current;
        if (a == null)
            return null;
        return Math.max(0, (a.duration - a.remainingAtAnchor) * 1000 + (Date.now() - a.anchorMs));
    };
    // Drive a re-render every ~250ms so the live game clock in the header
    // ticks smoothly without depending on inbound server messages.
    const [, setNowTick] = useState(0);
    useEffect(() => {
        const id = setInterval(() => setNowTick((n) => n + 1), 250);
        return () => clearInterval(id);
    }, []);
    // Dynamic symbol list — derived from server instruments; falls back to A-D.
    const symbols = useMemo(() => {
        const inst = game?.instruments;
        if (inst && Object.keys(inst).length > 0) {
            return Object.keys(inst).sort();
        }
        return DEFAULT_SYMBOLS;
    }, [game]);
    const toast = (level, text) => {
        const t = Date.now() + Math.random();
        setToasts((tt) => [...tt, { t, level, text }]);
        setTimeout(() => setToasts((tt) => tt.filter((x) => x.t !== t)), level === "error" || level === "warn" ? 6000 : 4000);
    };
    const refreshAll = async () => {
        try {
            // gameState first so we know which symbols exist; everything else can
            // then fan out in parallel.
            const g = await api.gameState();
            setGame(g);
            const syms = g?.instruments && Object.keys(g.instruments).length > 0
                ? Object.keys(g.instruments).sort()
                : DEFAULT_SYMBOLS;
            const [p, oAll, ...bookRes] = await Promise.all([
                api.positions(),
                api.myOrders(),
                ...syms.map((s) => api.book(s, DEPTH)),
            ]);
            setPositions(p.positions || {});
            const oMap = {};
            for (const o of oAll.orders || [])
                oMap[o.order_id] = o;
            setOrders(oMap);
            const bMap = {};
            for (let i = 0; i < syms.length; i++) {
                const b = bookRes[i];
                bMap[syms[i]] = { bids: b.bids || [], asks: b.asks || [] };
            }
            setBooks(bMap);
        }
        catch (e) {
            toast("error", `refresh: ${e.message}`);
        }
    };
    useEffect(() => {
        refreshAll();
        const conn = connectWS(onMessage, setWs);
        return () => conn.close();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    const onMessage = (m) => {
        const t = m.type;
        if (t === "book") {
            const sym = m.symbol;
            if (sym === "A" || sym === "B") {
                setBooks((b) => ({
                    ...b,
                    [sym]: { bids: m.bids || [], asks: m.asks || [] },
                }));
            }
            return;
        }
        if (t === "fill") {
            const notional = m.price * m.qty;
            const sign = m.side === "sell" ? 1 : -1;
            // Server cash is signed notional only (NO fee subtraction). Fees are
            // tracked separately and are NOT part of competition PnL.
            const cashDelta = sign * notional;
            const fee = m.fee || 0;
            const elapsedMs = computeElapsedMs();
            const rich = {
                t: m.t_ns || m.ts_ns || Date.now() * 1e6,
                trade_id: m.trade_id,
                order_id: m.order_id,
                symbol: m.symbol,
                side: m.side,
                price: m.price,
                qty: m.qty,
                liquidity: m.liquidity,
                counterparty: m.counterparty,
                fee: m.fee,
                notional,
                cashDelta,
                elapsedMs,
            };
            setFills((f) => [rich, ...f].slice(0, MAX_FILLS));
            setTradeCash((c) => ({ ...c, [m.symbol]: (c[m.symbol] || 0) + cashDelta }));
            if (fee) {
                setFeesBySym((c) => ({ ...c, [m.symbol]: (c[m.symbol] || 0) + fee }));
            }
            setPositions((p) => ({
                ...p,
                [m.symbol]: (p[m.symbol] || 0) + (m.side === "buy" ? m.qty : -m.qty),
            }));
            setOrders((o) => {
                const next = { ...o };
                const cur = next[m.order_id];
                if (cur) {
                    const remaining = Math.max(0, (cur.remaining ?? cur.qty) - m.qty);
                    if (remaining === 0)
                        delete next[m.order_id];
                    else
                        next[m.order_id] = { ...cur, remaining };
                }
                return next;
            });
            return;
        }
        if (t === "order_ack") {
            setOrders((o) => ({
                ...o,
                [m.order_id]: {
                    order_id: m.order_id,
                    symbol: m.symbol,
                    side: m.side,
                    price: m.price,
                    qty: m.qty,
                    remaining: m.remaining ?? m.qty,
                    tif: m.tif,
                    status: m.status,
                },
            }));
            return;
        }
        if (t === "cancel_ack") {
            setOrders((o) => {
                const n = { ...o };
                delete n[m.order_id];
                return n;
            });
            return;
        }
        if (t === "modify_ack") {
            setOrders((o) => {
                const cur = o[m.order_id];
                if (!cur)
                    return o;
                return {
                    ...o,
                    [m.order_id]: {
                        ...cur,
                        price: m.price ?? cur.price,
                        qty: m.qty ?? cur.qty,
                        remaining: m.remaining ?? cur.remaining,
                    },
                };
            });
            return;
        }
        if (t === "reject") {
            if (m.reason === "rate_limited") {
                setRlCount((c) => c + 1);
                setLastRl({ at: Date.now(), retryMs: m.retry_after_ms ?? 0, op: m.op ?? "?" });
                setRlFlash((n) => n + 1);
                toast("warn", `RATE LIMITED · ${m.op ?? "?"} · retry in ${m.retry_after_ms ?? "?"}ms`);
            }
            else {
                toast("error", `reject ${m.op || ""}${m.order_id ? ` #${m.order_id}` : ""}: ${m.reason}`);
            }
            return;
        }
        if (t === "tick_settlement") {
            // m.credit is already multiplier-applied (= pos_before * value * mult).
            // Store it as raw PnL contribution so the final formula does NOT
            // re-multiply by `mult`.
            if (m.credit != null) {
                setTickPnl((c) => ({
                    ...c,
                    [m.symbol]: (c[m.symbol] || 0) + m.credit,
                }));
            }
            if (m.symbol === "B") {
                setPositions((p) => ({ ...p, B: 0 }));
            }
            toast("info", `tick ${m.symbol} val=${m.value}${m.credit != null ? ` credit=${fmtSigned(m.credit)}` : ""}`);
            return;
        }
        if (t === "settlement") {
            setPositions(m.positions || {});
            // Capture true prices so per-trade True PnL can be revealed in the
            // Executed Orders table after the round ends.
            if (m.prices)
                setTruePrices(m.prices);
            // Reset per-round PnL state so the UI tracks the NEXT round, not cumulative.
            setTradeCash({ A: 0, B: 0 });
            setTickPnl({ A: 0, B: 0 });
            setFeesBySym({ A: 0, B: 0 });
            gameAnchorRef.current = null;
            toast("info", `settlement pnl=${m.pnl} A=${m.prices?.A} B=${m.prices?.B}`);
            refreshAll();
            return;
        }
        if (t === "reveal") {
            setGame((g) => {
                if (!g)
                    return g;
                const reveals = [...(g.reveals || []), m.value];
                return { ...g, reveals, running_sum: (g.running_sum || 0) + m.value };
            });
            return;
        }
        if (t === "game_state") {
            const prev = prevPhaseRef.current;
            const next = m.phase;
            prevPhaseRef.current = next;
            setGame(m);
            // Detect a new game starting: phase transitions INTO "running" from
            // anything else (or from null on first connect after game start).
            const newGameStarted = next === "running" && prev !== "running";
            if (newGameStarted) {
                setFills([]);
                setTradeCash({});
                setTickPnl({});
                setFeesBySym({});
                setOrders({});
                setTruePrices(null);
                setRlCount(0);
                setLastRl(null);
                toast("info", `new game started${m.duration ? ` · ${m.duration}s` : ""}`);
            }
            if (next === "running" &&
                m.duration != null &&
                m.remaining_seconds != null) {
                gameAnchorRef.current = {
                    duration: m.duration,
                    remainingAtAnchor: m.remaining_seconds,
                    anchorMs: Date.now(),
                };
            }
            else if (next !== "running") {
                gameAnchorRef.current = null;
            }
            // Refresh whenever phase changes — covers new game, settlement→next,
            // or any other transition.
            if (next !== prev)
                refreshAll();
            return;
        }
    };
    // PnL matches the exchange site (NET of fees):
    //   pnl[sym] = (trade_cash[sym] + pos[sym]*mark) * mult + tick_pnl - fees
    //   - trade_cash is signed notional only (no fees).
    //   - tick_pnl already includes the multiplier (server `credit` field).
    //   - fees ARE subtracted from competition PnL (matches the exchange site).
    const pnl = useMemo(() => {
        const out = {};
        for (const sym of symbols) {
            const inst = game?.instruments?.[sym];
            const mult = inst?.multiplier ?? 1;
            const book = books[sym] ?? EMPTY_BOOK;
            const bb = book.bids[0]?.price;
            const ba = book.asks[0]?.price;
            const mark = bb != null && ba != null ? (bb + ba) / 2 : 0;
            const pos = positions[sym] ?? 0;
            const cash = tradeCash[sym] ?? 0;
            const fees = feesBySym[sym] ?? 0;
            out[sym] =
                (cash + pos * mark) * mult + (tickPnl[sym] ?? 0) - fees;
        }
        return out;
    }, [symbols, game, books, positions, tradeCash, tickPnl, feesBySym]);
    const totalPnl = symbols.reduce((s, sym) => s + (pnl[sym] ?? 0), 0);
    const totalFees = symbols.reduce((s, sym) => s + (feesBySym[sym] ?? 0), 0);
    // Per-symbol fill aggregates for the market head.
    const symStats = useMemo(() => {
        const init = () => ({
            count: 0,
            vol: 0,
            fees: 0,
        });
        const out = {};
        for (const sym of symbols)
            out[sym] = init();
        for (const f of fills) {
            const s = out[f.symbol];
            if (!s)
                continue;
            s.count += 1;
            s.vol += f.qty;
            s.fees += f.fee ?? 0;
            if (!s.last)
                s.last = f;
        }
        return out;
    }, [fills, symbols]);
    const doBBO = async (sym, side, qty) => {
        const book = books[sym] ?? EMPTY_BOOK;
        const px = side === "buy" ? book.asks[0]?.price : book.bids[0]?.price;
        if (px == null)
            return toast("warn", `no ${side === "buy" ? "ask" : "bid"} on ${sym}`);
        if (qty <= 0)
            return toast("warn", "qty must be > 0");
        try {
            await api.place(sym, side, qty, px, "ioc", "limit");
            toast("info", `${side} ${qty} ${sym} @ ${px} IOC`);
        }
        catch (e) {
            toast("error", e.message);
        }
    };
    const cancelMineAt = async (sym, side, price) => {
        const mine = Object.values(orders).filter((o) => o.symbol === sym && o.side === side && o.price === price);
        if (mine.length === 0)
            return;
        try {
            await Promise.all(mine.map((o) => api.cancel(o.order_id)));
            toast("info", `cancel ${mine.length} ${sym} ${side} @ ${price}`);
        }
        catch (e) {
            toast("error", `cancel: ${e.message}`);
        }
    };
    const doCancelAll = async (sym) => {
        try {
            const r = await api.cancelAll(sym);
            toast("info", `cancelled ${r.cancelled ?? "?"} ${sym}`);
        }
        catch (e) {
            toast("error", `cancel all: ${e.message}`);
        }
    };
    // Grid layout: 1–2 markets → row; 3+ markets → 2-col grid (wraps to rows).
    const cols = symbols.length >= 3 ? 2 : Math.max(1, symbols.length);
    return (_jsxs("div", { className: "app", children: [_jsx(Header, { ws: ws, game: game, total: totalPnl, fees: totalFees, rlCount: rlCount, lastRl: lastRl, elapsedMs: computeElapsedMs() }), _jsx("div", { className: "main", style: { gridTemplateColumns: `repeat(${cols}, 1fr)` }, children: symbols.map((sym) => (_jsx(Market, { sym: sym, book: books[sym] ?? EMPTY_BOOK, pos: positions[sym] ?? 0, limit: game?.instruments?.[sym]?.position_limit, mult: game?.instruments?.[sym]?.multiplier, pnl: pnl[sym] ?? 0, orders: Object.values(orders).filter((o) => o.symbol === sym), stats: symStats[sym] ?? { count: 0, vol: 0, fees: 0 }, onBBO: doBBO, onCancelMineAt: cancelMineAt, onCancelAll: () => doCancelAll(sym) }, sym))) }), _jsx(Executed, { fills: fills, symbols: symbols, game: game, truePrices: truePrices }), _jsx(Toasts, { toasts: toasts }), rlFlash > 0 ? _jsx("div", { className: "rl-flash" }, rlFlash) : null] }));
}
// ============================================================================
function Header({ ws, game, total, fees, rlCount, lastRl, elapsedMs, }) {
    const reveals = game?.reveals ?? [];
    const totalReveals = game?.duration && game.reveal_interval
        ? Math.round(game.duration / game.reveal_interval)
        : null;
    // Show recent RL freshness
    const rlAge = lastRl ? Math.round((Date.now() - lastRl.at) / 1000) : null;
    return (_jsxs("div", { className: "header", children: [_jsxs("span", { className: "title", children: ["VALK \u00B7 ", _jsx("span", { className: "accent", children: "TRADER" })] }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: `dot ${ws}` }), _jsx("span", { className: "val dim", children: ws })] }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: "lbl", children: "Phase" }), _jsx("span", { className: "val", children: _jsx("span", { className: "pill phase", children: game?.phase ?? "—" }) })] }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: "lbl", children: "Reveals" }), _jsxs("span", { className: "val tabnum", children: [reveals.length > 0 ? reveals.join(",") : "—", totalReveals ? (_jsxs("span", { className: "dim", children: [" (", reveals.length, "/", totalReveals, ")"] })) : null] })] }), _jsx("span", { className: "spacer" }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: "lbl", children: "RL" }), _jsxs("span", { className: `val tabnum ${rlCount > 0 ? "warn" : "dim"}`, children: [rlCount, rlCount > 0 && rlAge !== null ? (_jsxs("span", { className: "dim", children: [" \u00B7", rlAge, "s"] })) : null] })] }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: "lbl", children: "Total PnL" }), _jsx("span", { className: `val big tabnum ${total >= 0 ? "pos" : "neg"}`, children: fmtSigned(Math.round(total)) })] }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: "lbl", children: "Fees" }), _jsx("span", { className: "val tabnum dim", children: fees.toFixed(1) })] }), _jsxs("span", { className: "stat", title: "time elapsed since the current round started", children: [_jsx("span", { className: "lbl", children: "Elapsed" }), _jsxs("span", { className: "val tabnum", children: [elapsedMs != null && game?.phase === "running"
                                ? fmtElapsed(elapsedMs)
                                : "—", game?.duration != null ? (_jsxs("span", { className: "dim", children: [" / ", fmtElapsed(game.duration * 1000)] })) : null] })] }), _jsxs("span", { className: "stat", children: [_jsx("span", { className: "lbl", children: "Time" }), _jsx("span", { className: "val tabnum", children: game?.remaining_seconds != null
                            ? `${Math.max(0, Math.round(game.remaining_seconds))}s`
                            : "—" })] })] }));
}
function fmtSigned(n) {
    return n > 0 ? `+${n}` : `${n}`;
}
function Market({ sym, book, pos, limit, mult, pnl, orders, stats, onBBO, onCancelMineAt, onCancelAll, }) {
    const [qty, setQty] = useState(1);
    const bestBid = book.bids[0];
    const bestAsk = book.asks[0];
    const mid = bestBid && bestAsk ? (bestBid.price + bestAsk.price) / 2 : null;
    const spread = bestBid && bestAsk ? bestAsk.price - bestBid.price : null;
    const bidTot = book.bids.reduce((s, l) => s + l.qty, 0);
    const askTot = book.asks.reduce((s, l) => s + l.qty, 0);
    const myBidQtyAt = useMemo(() => {
        const m = {};
        for (const o of orders) {
            if (o.side === "buy")
                m[o.price] = (m[o.price] || 0) + o.remaining;
        }
        return m;
    }, [orders]);
    const myAskQtyAt = useMemo(() => {
        const m = {};
        for (const o of orders) {
            if (o.side === "sell")
                m[o.price] = (m[o.price] || 0) + o.remaining;
        }
        return m;
    }, [orders]);
    const myBidQtyTot = Object.values(myBidQtyAt).reduce((s, v) => s + v, 0);
    const myAskQtyTot = Object.values(myAskQtyAt).reduce((s, v) => s + v, 0);
    return (_jsxs("div", { className: "market", children: [_jsxs("div", { className: "market-head", children: [_jsxs("div", { className: "name", children: [sym, _jsxs("span", { className: "badge", children: [mult ? `×${mult}` : "", limit ? `  ±${limit}` : ""] })] }), _jsxs("div", { className: "info", children: [_jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Mid" }), _jsx("span", { className: "val", children: mid !== null ? mid.toFixed(1) : "—" })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Sprd" }), _jsx("span", { className: "val", children: spread !== null ? spread : "—" })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Pos" }), _jsx("span", { className: `val ${pos > 0 ? "pos" : pos < 0 ? "neg" : ""}`, children: fmtSigned(pos) })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "PnL" }), _jsx("span", { className: `val ${pnl >= 0 ? "pos" : "neg"}`, children: fmtSigned(Math.round(pnl)) })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Vol" }), _jsxs("span", { className: "val", children: [stats.vol, stats.fees > 0 ? (_jsxs("span", { className: "dim", children: [" \u00B7 ", stats.fees.toFixed(1), " fee"] })) : null] })] })] })] }), _jsxs("div", { className: "market-controls", children: [_jsxs("div", { className: "qty-input", children: [_jsx("span", { className: "lbl", children: "Qty" }), _jsx("input", { type: "number", min: 1, value: qty, onChange: (e) => setQty(Math.max(1, parseInt(e.target.value, 10) || 1)) })] }), _jsxs("button", { className: "bid bbo", disabled: !bestAsk, onClick: () => onBBO(sym, "buy", qty), title: bestAsk ? `buy ${qty} ${sym} @ ${bestAsk.price} IOC` : "no ask", children: [_jsx("span", { className: "big", children: "BUY" }), _jsx("span", { className: "small", children: bestAsk ? `@ ${bestAsk.price} × ${bestAsk.qty}` : "—" })] }), _jsxs("button", { className: "ask bbo", disabled: !bestBid, onClick: () => onBBO(sym, "sell", qty), title: bestBid ? `sell ${qty} ${sym} @ ${bestBid.price} IOC` : "no bid", children: [_jsx("span", { className: "big", children: "SELL" }), _jsx("span", { className: "small", children: bestBid ? `@ ${bestBid.price} × ${bestBid.qty}` : "—" })] }), _jsxs("button", { className: "ghost", onClick: onCancelAll, disabled: !orders.length, title: "cancel all of my orders on this symbol", children: ["cancel all (", orders.length, ")"] })] }), _jsx(BookLadder, { sym: sym, book: book, bestBid: bestBid?.price, bestAsk: bestAsk?.price, bidTot: bidTot, askTot: askTot, myBidQtyTot: myBidQtyTot, myAskQtyTot: myAskQtyTot, myBidQtyAt: myBidQtyAt, myAskQtyAt: myAskQtyAt, mid: mid, onCancelMineAt: onCancelMineAt })] }));
}
// ============================================================================
function BookLadder({ sym, book, bestBid, bestAsk, bidTot, askTot, myBidQtyTot, myAskQtyTot, myBidQtyAt, myAskQtyAt, mid, onCancelMineAt, }) {
    // Render asks best-first; CSS uses column-reverse so the best ask sits at
    // the bottom of the upper half, right next to the centered mid divider.
    const asks = book.asks;
    const bids = book.bids;
    const maxQty = Math.max(1, ...book.asks.map((l) => l.qty), ...book.bids.map((l) => l.qty));
    if (asks.length === 0 && bids.length === 0) {
        return (_jsx("div", { className: "book", children: _jsx("div", { className: "empty", children: "no book" }) }));
    }
    return (_jsxs("div", { className: "book", children: [_jsx("div", { className: "asks-section", children: asks.map((l) => {
                    const isBest = l.price === bestAsk;
                    const myQty = myAskQtyAt[l.price];
                    const isMine = !!myQty;
                    const w = `${Math.max(4, (l.qty / maxQty) * 70)}%`;
                    return (_jsxs("div", { className: `row ask ${isBest ? "best" : ""} ${isMine ? "mine" : ""}`, children: [_jsx("span", { className: "bar", style: { width: w } }), _jsxs("span", { className: "px", children: [l.price, isMine ? _jsx("span", { className: "mine-dot" }) : null] }), _jsx("span", {}), _jsxs("span", { className: "qty", children: [l.qty, isMine ? (_jsxs("span", { className: "mine-tag", children: ["MINE ", myQty] })) : null] }), isMine ? (_jsx("span", { className: "cancel", title: `cancel my ${myQty} @ ${l.price}`, onClick: () => onCancelMineAt(sym, "sell", l.price), children: "\u00D7" })) : null] }, `a-${l.price}`));
                }) }), _jsxs("div", { className: "mid", children: [_jsxs("span", { className: "l", children: [_jsx("span", { className: "dim", children: "bid " }), _jsx("span", { className: "bidc", children: bidTot }), myBidQtyTot > 0 ? (_jsxs("span", { className: "mine-mini", children: [" \u00B7 mine ", myBidQtyTot] })) : null] }), _jsx("span", { className: "price", children: mid !== null ? mid.toFixed(1) : "—" }), _jsxs("span", { children: [_jsx("span", { className: "askc", children: askTot }), _jsx("span", { className: "dim", children: " ask" }), myAskQtyTot > 0 ? (_jsxs("span", { className: "mine-mini", children: [" \u00B7 mine ", myAskQtyTot] })) : null] })] }), _jsx("div", { className: "bids-section", children: bids.map((l) => {
                    const isBest = l.price === bestBid;
                    const myQty = myBidQtyAt[l.price];
                    const isMine = !!myQty;
                    const w = `${Math.max(4, (l.qty / maxQty) * 70)}%`;
                    return (_jsxs("div", { className: `row bid ${isBest ? "best" : ""} ${isMine ? "mine" : ""}`, children: [_jsx("span", { className: "bar", style: { width: w } }), _jsxs("span", { className: "px", children: [l.price, isMine ? _jsx("span", { className: "mine-dot" }) : null] }), _jsx("span", {}), _jsxs("span", { className: "qty", children: [l.qty, isMine ? (_jsxs("span", { className: "mine-tag", children: ["MINE ", myQty] })) : null] }), isMine ? (_jsx("span", { className: "cancel", title: `cancel my ${myQty} @ ${l.price}`, onClick: () => onCancelMineAt(sym, "buy", l.price), children: "\u00D7" })) : null] }, `b-${l.price}`));
                }) })] }));
}
// ============================================================================
function Executed({ fills, symbols, game, truePrices, }) {
    // Per-trade True PnL against the final settlement value, NET of fees.
    //   (cashDelta + signed_position_change * true_value) * multiplier - fee
    //   - buy adds +qty (long), sell adds -qty (short).
    //   - cashDelta is signed notional (no fees).
    //   - fee is subtracted to match the exchange site's net-of-fees PnL.
    //   - returns null while truePrices is not yet known.
    const truePnlFor = (f) => {
        if (!truePrices)
            return null;
        const v = truePrices[f.symbol];
        if (v == null)
            return null;
        const mult = game?.instruments?.[f.symbol]?.multiplier ?? 1;
        const posChange = f.side === "buy" ? f.qty : -f.qty;
        return (f.cashDelta + posChange * v) * mult - (f.fee ?? 0);
    };
    const [symFilter, setSymFilter] = useState({});
    const [sideFilter, setSideFilter] = useState({ buy: true, sell: true });
    const [liqFilter, setLiqFilter] = useState({ maker: true, taker: true, none: true });
    // Lookup helper: treat unset (undefined) symFilter entries as ON, so newly
    // discovered symbols start visible without an explicit opt-in.
    const symOn = (sym) => symFilter[sym] !== false;
    const filtered = useMemo(() => fills.filter((f) => {
        if (!symOn(f.symbol))
            return false;
        if (f.side === "buy" && !sideFilter.buy)
            return false;
        if (f.side === "sell" && !sideFilter.sell)
            return false;
        const liq = f.liquidity;
        if (liq === "maker" && !liqFilter.maker)
            return false;
        if (liq === "taker" && !liqFilter.taker)
            return false;
        if (liq == null && !liqFilter.none)
            return false;
        return true;
    }), [fills, symFilter, sideFilter, liqFilter]);
    const checkAll = () => {
        setSymFilter({});
        setSideFilter({ buy: true, sell: true });
        setLiqFilter({ maker: true, taker: true, none: true });
    };
    const allOn = symbols.every((s) => symOn(s)) &&
        sideFilter.buy &&
        sideFilter.sell &&
        liqFilter.maker &&
        liqFilter.taker &&
        liqFilter.none;
    // Click semantics: clicking a chip ISOLATES that chip in its group (turns
    // every other chip in the group off). Clicking the same chip again — when
    // it's already the only one ON — restores the whole group to ON. This
    // matches "click A to show only A; click A again to see everything".
    const isolateSym = (s) => {
        const onSyms = symbols.filter((sy) => symOn(sy));
        const isOnlyOn = symOn(s) && onSyms.length === 1;
        if (isOnlyOn) {
            setSymFilter({});
            return;
        }
        const next = {};
        for (const sy of symbols)
            next[sy] = sy === s;
        setSymFilter(next);
    };
    const isolateSide = (side) => {
        const other = side === "buy" ? "sell" : "buy";
        const isOnlyOn = sideFilter[side] && !sideFilter[other];
        if (isOnlyOn) {
            setSideFilter({ buy: true, sell: true });
        }
        else {
            setSideFilter({ buy: side === "buy", sell: side === "sell" });
        }
    };
    const isolateLiq = (key) => {
        const onCount = (liqFilter.maker ? 1 : 0) +
            (liqFilter.taker ? 1 : 0) +
            (liqFilter.none ? 1 : 0);
        const isOnlyOn = liqFilter[key] && onCount === 1;
        if (isOnlyOn) {
            setLiqFilter({ maker: true, taker: true, none: true });
        }
        else {
            setLiqFilter({
                maker: key === "maker",
                taker: key === "taker",
                none: key === "none",
            });
        }
    };
    const agg = useMemo(() => {
        let count = 0;
        let vol = 0;
        let notional = 0;
        let fees = 0;
        let maker = 0;
        let taker = 0;
        let buys = 0;
        let sells = 0;
        let realized = 0;
        let truePnl = 0;
        let truePnlAvailable = false;
        for (const f of filtered) {
            count += 1;
            vol += f.qty;
            notional += f.notional;
            fees += f.fee ?? 0;
            if (f.liquidity === "maker")
                maker += 1;
            else if (f.liquidity === "taker")
                taker += 1;
            if (f.side === "buy")
                buys += f.qty;
            else
                sells += f.qty;
            const mult = game?.instruments?.[f.symbol]?.multiplier ?? 1;
            realized += f.cashDelta * mult - (f.fee ?? 0);
            const tp = truePnlFor(f);
            if (tp != null) {
                truePnl += tp;
                truePnlAvailable = true;
            }
        }
        return {
            count,
            vol,
            notional,
            fees,
            maker,
            taker,
            buys,
            sells,
            realized,
            truePnl,
            truePnlAvailable,
        };
    }, [filtered, game, truePrices]);
    return (_jsxs("div", { className: "bottom", children: [_jsxs("div", { className: "bottom-head", children: [_jsx("span", { className: "title", children: "Executed Orders" }), _jsxs("div", { className: "filters", children: [_jsxs("div", { className: "filter-group", children: [_jsx("span", { className: "lbl", children: "Sym" }), symbols.map((s) => (_jsx("button", { className: `chip ${symOn(s) ? "on" : ""}`, title: `show only ${s} (click again to show all)`, onClick: () => isolateSym(s), children: s }, s)))] }), _jsxs("div", { className: "filter-group", children: [_jsx("span", { className: "lbl", children: "Side" }), _jsx("button", { className: `chip ${sideFilter.buy ? "on bid" : ""}`, title: "show only buys (click again to show all)", onClick: () => isolateSide("buy"), children: "buy" }), _jsx("button", { className: `chip ${sideFilter.sell ? "on ask" : ""}`, title: "show only sells (click again to show all)", onClick: () => isolateSide("sell"), children: "sell" })] }), _jsxs("div", { className: "filter-group", children: [_jsx("span", { className: "lbl", children: "Liq" }), _jsx("button", { className: `chip ${liqFilter.maker ? "on" : ""}`, title: "show only maker fills (click again to show all)", onClick: () => isolateLiq("maker"), children: "maker" }), _jsx("button", { className: `chip ${liqFilter.taker ? "on" : ""}`, title: "show only taker fills (click again to show all)", onClick: () => isolateLiq("taker"), children: "taker" }), _jsx("button", { className: `chip ${liqFilter.none ? "on" : ""}`, title: "show only fills with no liq tag (click again to show all)", onClick: () => isolateLiq("none"), children: "n/a" })] }), _jsx("button", { className: "ghost", onClick: checkAll, disabled: allOn, title: "re-enable every filter (show all)", children: "check all" })] }), _jsxs("div", { className: "agg", children: [_jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Fills" }), _jsxs("span", { className: "val", children: [agg.count, agg.count !== fills.length ? (_jsxs("span", { className: "dim", children: [" / ", fills.length] })) : null] })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Vol" }), _jsx("span", { className: "val", children: agg.vol })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Buys / Sells" }), _jsxs("span", { className: "val", children: [_jsx("span", { className: "bidc", children: agg.buys }), _jsx("span", { className: "dim", children: " / " }), _jsx("span", { className: "askc", children: agg.sells })] })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Notional" }), _jsx("span", { className: "val tabnum", children: agg.notional.toFixed(0) })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Fees" }), _jsx("span", { className: "val tabnum", children: agg.fees.toFixed(1) })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Maker / Taker" }), _jsxs("span", { className: "val", children: [_jsx("span", { className: "dim", children: agg.maker }), _jsx("span", { className: "dim", children: " / " }), _jsx("span", { className: "warn", children: agg.taker })] })] }), _jsxs("span", { children: [_jsx("span", { className: "lbl", children: "Realized*" }), _jsx("span", { className: `val ${agg.realized >= 0 ? "pos" : "neg"}`, children: fmtSigned(Math.round(agg.realized)) })] }), _jsxs("span", { title: "per-trade PnL against the true (settlement) value; shown after the round ends", children: [_jsx("span", { className: "lbl", children: "True PnL" }), agg.truePnlAvailable ? (_jsx("span", { className: `val ${agg.truePnl >= 0 ? "pos" : "neg"}`, children: fmtSigned(Math.round(agg.truePnl)) })) : (_jsx("span", { className: "val dim", children: "\u2014" }))] })] })] }), _jsx("div", { className: "bottom-body", children: filtered.length === 0 ? (_jsx("div", { className: "empty", children: fills.length === 0
                        ? "no executed orders yet"
                        : "no fills match the current filters" })) : (_jsxs("table", { className: "fills-table", children: [_jsx("thead", { children: _jsxs("tr", { children: [_jsx("th", { className: "l", title: "time since the round started", children: "Round T" }), _jsx("th", { className: "l", children: "Sym" }), _jsx("th", { className: "l", children: "Side" }), _jsx("th", { children: "Px" }), _jsx("th", { children: "Qty" }), _jsx("th", { children: "Notional" }), _jsx("th", { children: "Fee" }), _jsx("th", { title: "PnL contribution = signed notional \u00D7 multiplier \u2212 fee", children: "\u0394 PnL" }), _jsx("th", { title: "per-trade PnL against the true (settlement) value, net of fee; shown after the round ends", children: "True PnL" }), _jsx("th", { className: "l", children: "Liq" }), _jsx("th", { className: "l", children: "CP" }), _jsx("th", { children: "Order ID" })] }) }), _jsx("tbody", { children: filtered.map((f, i) => {
                                const mult = game?.instruments?.[f.symbol]?.multiplier ?? 1;
                                const pnlImpact = f.cashDelta * mult - (f.fee ?? 0);
                                return (_jsxs("tr", { children: [_jsx("td", { className: "l dim tabnum", title: fmtTime(f.t), children: fmtElapsed(f.elapsedMs) }), _jsx("td", { className: "l", children: f.symbol }), _jsx("td", { className: "l", children: _jsx("span", { className: `pill ${f.side === "buy" ? "bid" : "ask"}`, children: f.side }) }), _jsx("td", { className: f.side === "buy" ? "bidc" : "askc", children: f.price }), _jsx("td", { children: f.qty }), _jsx("td", { className: "tabnum", children: f.notional.toFixed(0) }), _jsx("td", { className: "tabnum dim", children: f.fee != null ? f.fee.toFixed(1) : "" }), _jsx("td", { className: `tabnum ${pnlImpact > 0 ? "pos" : pnlImpact < 0 ? "neg" : ""}`, children: fmtSigned(Math.round(pnlImpact)) }), (() => {
                                            const tp = truePnlFor(f);
                                            return (_jsx("td", { className: `tabnum ${tp == null
                                                    ? "dim"
                                                    : tp > 0
                                                        ? "pos"
                                                        : tp < 0
                                                            ? "neg"
                                                            : ""}`, children: tp == null ? "—" : fmtSigned(Math.round(tp)) }));
                                        })(), _jsx("td", { className: `l ${f.liquidity === "maker"
                                                ? "liq-maker"
                                                : f.liquidity === "taker"
                                                    ? "liq-taker"
                                                    : "dim"}`, children: f.liquidity ?? "—" }), _jsx("td", { className: "l dim", children: f.counterparty ?? "—" }), _jsx("td", { className: "dim tabnum", children: f.order_id ?? "" })] }, `${f.trade_id}-${i}`));
                            }) })] })) })] }));
}
function fmtTime(t_ns) {
    const ms = Math.floor(t_ns / 1e6);
    const d = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}
function fmtElapsed(ms) {
    if (ms == null)
        return "—";
    const total = Math.max(0, Math.round(ms));
    const mm = Math.floor(total / 60000);
    const ss = Math.floor((total % 60000) / 1000);
    const mmm = total % 1000;
    const pad = (n, w = 2) => String(n).padStart(w, "0");
    return `${pad(mm)}:${pad(ss)}.${pad(mmm, 3)}`;
}
// ============================================================================
function Toasts({ toasts }) {
    return (_jsx("div", { className: "toasts", children: toasts.map((t) => (_jsx("div", { className: `toast ${t.level}`, children: t.text }, t.t))) }));
}
