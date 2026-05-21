import { useEffect, useMemo, useRef, useState } from "react";
import { api, connectWS, type WSStatus } from "./api";
import type {
  Book,
  Fill,
  GameState,
  LogLine,
  Order,
  Positions,
  Side,
} from "./types";

const DEFAULT_SYMBOLS = ["A", "B", "C", "D"];

const EMPTY_BOOK: Book = { bids: [], asks: [] };
const DEPTH = 50;
const MAX_FILLS = 500;

type RichFill = Fill & {
  notional: number;
  cashDelta: number;
  elapsedMs?: number | null;
};

export function App() {
  const [ws, setWs] = useState<WSStatus>("closed");
  const [game, setGame] = useState<GameState | null>(null);
  const [positions, setPositions] = useState<Positions>({});
  const [orders, setOrders] = useState<Record<number, Order>>({});
  const [books, setBooks] = useState<Record<string, Book>>({});
  const [fills, setFills] = useState<RichFill[]>([]);
  const [tradeCash, setTradeCash] = useState<Record<string, number>>({});
  const [tickPnl, setTickPnl] = useState<Record<string, number>>({});
  const [feesBySym, setFeesBySym] = useState<Record<string, number>>({});
  // True (settlement) prices for each market. Populated only on settlement;
  // null during the running round so per-trade True PnL renders as "—".
  const [truePrices, setTruePrices] = useState<Record<string, number> | null>(
    null
  );
  const [rlCount, setRlCount] = useState(0);
  const [lastRl, setLastRl] = useState<{ at: number; retryMs: number; op: string } | null>(null);
  const [rlFlash, setRlFlash] = useState(0);
  const [toasts, setToasts] = useState<LogLine[]>([]);

  // Anchor for round-relative time. Updated on each game_state w/ running phase.
  const gameAnchorRef = useRef<
    { duration: number; remainingAtAnchor: number; anchorMs: number } | null
  >(null);
  // Tracks the last seen phase so we can detect new-game starts.
  const prevPhaseRef = useRef<string | null>(null);
  // Bumped on every WS event that mutates positions/orders/fills. Used to
  // detect races between in-flight REST refreshes and WS events: if the seq
  // changes during a refresh, we skip overwriting positions/orders to avoid
  // clobbering newer WS state with an older REST snapshot.
  const wsSeqRef = useRef(0);
  const bumpWsSeq = () => {
    wsSeqRef.current += 1;
  };
  const computeElapsedMs = (): number | null => {
    const a = gameAnchorRef.current;
    if (a == null) return null;
    return Math.max(
      0,
      (a.duration - a.remainingAtAnchor) * 1000 + (Date.now() - a.anchorMs)
    );
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

  const toast = (level: LogLine["level"], text: string) => {
    const t = Date.now() + Math.random();
    setToasts((tt) => [...tt, { t, level, text }]);
    setTimeout(
      () => setToasts((tt) => tt.filter((x) => x.t !== t)),
      level === "error" || level === "warn" ? 6000 : 4000
    );
  };

  const refreshAll = async () => {
    try {
      // gameState first so we know which symbols exist; everything else can
      // then fan out in parallel.
      const g = await api.gameState();
      setGame(g);
      const syms =
        g?.instruments && Object.keys(g.instruments).length > 0
          ? Object.keys(g.instruments).sort()
          : DEFAULT_SYMBOLS;
      const seqAtStart = wsSeqRef.current;
      const [p, oAll, ...bookRes] = await Promise.all([
        api.positions(),
        api.myOrders(),
        ...syms.map((s) => api.book(s, DEPTH)),
      ]);
      // Books are pure server state — always safe to overwrite.
      const bMap: Record<string, Book> = {};
      for (let i = 0; i < syms.length; i++) {
        const b = bookRes[i] as any;
        bMap[syms[i]] = { bids: b.bids || [], asks: b.asks || [] };
      }
      setBooks(bMap);
      // Positions & orders are mutated by WS events too — only overwrite if
      // no WS event fired during the fetch, otherwise we'd clobber a newer
      // fill/ack with an older REST snapshot.
      if (wsSeqRef.current === seqAtStart) {
        setPositions(p.positions || {});
        const oMap: Record<number, Order> = {};
        for (const o of oAll.orders || []) oMap[o.order_id] = o;
        setOrders(oMap);
      }
    } catch (e: any) {
      toast("error", `refresh: ${e.message}`);
    }
  };

  useEffect(() => {
    refreshAll();
    const conn = connectWS(onMessage, setWs);
    // Safety-net refresh: WS can miss messages on reconnect, so periodically
    // re-sync books (always) and positions/orders (race-guarded inside
    // refreshAll). Keeps C/D books, my orders and PnL fresh during a round.
    const id = setInterval(refreshAll, 2000);
    return () => {
      clearInterval(id);
      conn.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onMessage = (m: any) => {
    const t = m.type;
    if (t === "book") {
      const sym = m.symbol;
      setBooks((b) => ({
        ...b,
        [sym]: { bids: m.bids || [], asks: m.asks || [] },
      }));
      return;
    }
    if (t === "fill") {
      bumpWsSeq();
      const notional = m.price * m.qty;
      const sign = m.side === "sell" ? 1 : -1;
      // Server cash is signed notional only (NO fee subtraction). Fees are
      // tracked separately and are NOT part of competition PnL.
      const cashDelta = sign * notional;
      const fee = m.fee || 0;
      const elapsedMs = computeElapsedMs();
      const rich: RichFill = {
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
        [m.symbol]:
          (p[m.symbol] || 0) + (m.side === "buy" ? m.qty : -m.qty),
      }));
      setOrders((o) => {
        const next = { ...o };
        const cur = next[m.order_id];
        if (cur) {
          const remaining = Math.max(0, (cur.remaining ?? cur.qty) - m.qty);
          if (remaining === 0) delete next[m.order_id];
          else next[m.order_id] = { ...cur, remaining };
        }
        return next;
      });
      return;
    }
    if (t === "order_ack") {
      bumpWsSeq();
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
      bumpWsSeq();
      setOrders((o) => {
        const n = { ...o };
        delete n[m.order_id];
        return n;
      });
      return;
    }
    if (t === "modify_ack") {
      bumpWsSeq();
      setOrders((o) => {
        const cur = o[m.order_id];
        if (!cur) return o;
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
        toast(
          "warn",
          `RATE LIMITED · ${m.op ?? "?"} · retry in ${m.retry_after_ms ?? "?"}ms`
        );
      } else {
        toast(
          "error",
          `reject ${m.op || ""}${m.order_id ? ` #${m.order_id}` : ""}: ${m.reason}`
        );
      }
      return;
    }
    if (t === "tick_settlement") {
      bumpWsSeq();
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
      toast(
        "info",
        `tick ${m.symbol} val=${m.value}${m.credit != null ? ` credit=${fmtSigned(m.credit)}` : ""}`
      );
      return;
    }
    if (t === "settlement") {
      bumpWsSeq();
      setPositions(m.positions || {});
      // Capture true prices so per-trade True PnL can be revealed in the
      // Executed Orders table after the round ends.
      if (m.prices) setTruePrices(m.prices);
      // Reset per-round PnL state so the UI tracks the NEXT round, not cumulative.
      setTradeCash({});
      setTickPnl({});
      setFeesBySym({});
      gameAnchorRef.current = null;
      toast(
        "info",
        `settlement pnl=${m.pnl} A=${m.prices?.A} B=${m.prices?.B}`
      );
      refreshAll();
      return;
    }
    if (t === "reveal") {
      setGame((g) => {
        if (!g) return g;
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

      if (
        next === "running" &&
        m.duration != null &&
        m.remaining_seconds != null
      ) {
        gameAnchorRef.current = {
          duration: m.duration,
          remainingAtAnchor: m.remaining_seconds,
          anchorMs: Date.now(),
        };
      } else if (next !== "running") {
        gameAnchorRef.current = null;
      }

      // Refresh whenever phase changes — covers new game, settlement→next,
      // or any other transition.
      if (next !== prev) refreshAll();
      return;
    }
  };

  // PnL matches the exchange site (NET of fees):
  //   pnl[sym] = (trade_cash[sym] + pos[sym]*mark) * mult + tick_pnl - fees
  //   - trade_cash is signed notional only (no fees).
  //   - tick_pnl already includes the multiplier (server `credit` field).
  //   - fees ARE subtracted from competition PnL (matches the exchange site).
  const pnl = useMemo(() => {
    const out: Record<string, number> = {};
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
  const totalFees = symbols.reduce(
    (s, sym) => s + (feesBySym[sym] ?? 0),
    0
  );

  // Per-symbol fill aggregates for the market head.
  const symStats = useMemo(() => {
    const init = (): { count: number; vol: number; fees: number; last?: RichFill } => ({
      count: 0,
      vol: 0,
      fees: 0,
    });
    const out: Record<string, ReturnType<typeof init>> = {};
    for (const sym of symbols) out[sym] = init();
    for (const f of fills) {
      const s = out[f.symbol];
      if (!s) continue;
      s.count += 1;
      s.vol += f.qty;
      s.fees += f.fee ?? 0;
      if (!s.last) s.last = f;
    }
    return out;
  }, [fills, symbols]);

  const doBBO = async (sym: string, side: Side, qty: number) => {
    const book = books[sym] ?? EMPTY_BOOK;
    const px = side === "buy" ? book.asks[0]?.price : book.bids[0]?.price;
    if (px == null) return toast("warn", `no ${side === "buy" ? "ask" : "bid"} on ${sym}`);
    if (qty <= 0) return toast("warn", "qty must be > 0");
    try {
      await api.place(sym, side, qty, px, "ioc", "limit");
      toast("info", `${side} ${qty} ${sym} @ ${px} IOC`);
    } catch (e: any) {
      toast("error", e.message);
    }
  };

  const cancelMineAt = async (sym: string, side: Side, price: number) => {
    const mine = Object.values(orders).filter(
      (o) => o.symbol === sym && o.side === side && o.price === price
    );
    if (mine.length === 0) return;
    try {
      await Promise.all(mine.map((o) => api.cancel(o.order_id)));
      toast("info", `cancel ${mine.length} ${sym} ${side} @ ${price}`);
    } catch (e: any) {
      toast("error", `cancel: ${e.message}`);
    }
  };

  const doCancelAll = async (sym: string) => {
    try {
      const r = await api.cancelAll(sym);
      toast("info", `cancelled ${r.cancelled ?? "?"} ${sym}`);
    } catch (e: any) {
      toast("error", `cancel all: ${e.message}`);
    }
  };

  // Grid layout: 1–2 markets → row; 3+ markets → 2-col grid (wraps to rows).
  const cols = symbols.length >= 3 ? 2 : Math.max(1, symbols.length);

  return (
    <div className="app">
      <Header
        ws={ws}
        game={game}
        total={totalPnl}
        fees={totalFees}
        rlCount={rlCount}
        lastRl={lastRl}
        elapsedMs={computeElapsedMs()}
      />
      <div
        className="main"
        style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}
      >
        {symbols.map((sym) => (
          <Market
            key={sym}
            sym={sym}
            book={books[sym] ?? EMPTY_BOOK}
            pos={positions[sym] ?? 0}
            limit={game?.instruments?.[sym]?.position_limit}
            mult={game?.instruments?.[sym]?.multiplier}
            pnl={pnl[sym] ?? 0}
            orders={Object.values(orders).filter((o) => o.symbol === sym)}
            stats={symStats[sym] ?? { count: 0, vol: 0, fees: 0 }}
            onBBO={doBBO}
            onCancelMineAt={cancelMineAt}
            onCancelAll={() => doCancelAll(sym)}
          />
        ))}
      </div>
      <Executed
        fills={fills}
        symbols={symbols}
        game={game}
        truePrices={truePrices}
      />
      <Toasts toasts={toasts} />
      {/* Re-mount the flash element on each rate-limit event by key */}
      {rlFlash > 0 ? <div key={rlFlash} className="rl-flash" /> : null}
    </div>
  );
}

// ============================================================================

function Header({
  ws,
  game,
  total,
  fees,
  rlCount,
  lastRl,
  elapsedMs,
}: {
  ws: WSStatus;
  game: GameState | null;
  total: number;
  fees: number;
  rlCount: number;
  lastRl: { at: number; retryMs: number; op: string } | null;
  elapsedMs: number | null;
}) {
  const reveals = game?.reveals ?? [];
  const totalReveals =
    game?.duration && game.reveal_interval
      ? Math.round(game.duration / game.reveal_interval)
      : null;

  // Show recent RL freshness
  const rlAge = lastRl ? Math.round((Date.now() - lastRl.at) / 1000) : null;

  return (
    <div className="header">
      <span className="title">
        VALK · <span className="accent">TRADER</span>
      </span>
      <span className="stat">
        <span className={`dot ${ws}`} />
        <span className="val dim">{ws}</span>
      </span>
      <span className="stat">
        <span className="lbl">Phase</span>
        <span className="val">
          <span className="pill phase">{game?.phase ?? "—"}</span>
        </span>
      </span>
      <span className="stat">
        <span className="lbl">Reveals</span>
        <span className="val tabnum">
          {reveals.length > 0 ? reveals.join(",") : "—"}
          {totalReveals ? (
            <span className="dim"> ({reveals.length}/{totalReveals})</span>
          ) : null}
        </span>
      </span>
      <span className="spacer" />
      <span className="stat">
        <span className="lbl">RL</span>
        <span className={`val tabnum ${rlCount > 0 ? "warn" : "dim"}`}>
          {rlCount}
          {rlCount > 0 && rlAge !== null ? (
            <span className="dim"> ·{rlAge}s</span>
          ) : null}
        </span>
      </span>
      <span className="stat">
        <span className="lbl">Total PnL</span>
        <span className={`val big tabnum ${total >= 0 ? "pos" : "neg"}`}>
          {fmtSigned(Math.round(total))}
        </span>
      </span>
      <span className="stat">
        <span className="lbl">Fees</span>
        <span className="val tabnum dim">{fees.toFixed(1)}</span>
      </span>
      <span
        className="stat"
        title="time elapsed since the current round started"
      >
        <span className="lbl">Elapsed</span>
        <span className="val tabnum">
          {elapsedMs != null && game?.phase === "running"
            ? fmtElapsed(elapsedMs)
            : "—"}
          {game?.duration != null ? (
            <span className="dim"> / {fmtElapsed(game.duration * 1000)}</span>
          ) : null}
        </span>
      </span>
      <span className="stat">
        <span className="lbl">Time</span>
        <span className="val tabnum">
          {game?.remaining_seconds != null
            ? `${Math.max(0, Math.round(game.remaining_seconds))}s`
            : "—"}
        </span>
      </span>
    </div>
  );
}

function fmtSigned(n: number) {
  return n > 0 ? `+${n}` : `${n}`;
}

// ============================================================================

type SymStats = {
  count: number;
  vol: number;
  fees: number;
  last?: RichFill;
};

function Market({
  sym,
  book,
  pos,
  limit,
  mult,
  pnl,
  orders,
  stats,
  onBBO,
  onCancelMineAt,
  onCancelAll,
}: {
  sym: string;
  book: Book;
  pos: number;
  limit?: number;
  mult?: number;
  pnl: number;
  orders: Order[];
  stats: SymStats;
  onBBO: (sym: string, side: Side, qty: number) => void;
  onCancelMineAt: (sym: string, side: Side, price: number) => void;
  onCancelAll: () => void;
}) {
  const [qty, setQty] = useState(1);
  const bestBid = book.bids[0];
  const bestAsk = book.asks[0];
  const mid =
    bestBid && bestAsk ? (bestBid.price + bestAsk.price) / 2 : null;
  const spread = bestBid && bestAsk ? bestAsk.price - bestBid.price : null;
  const bidTot = book.bids.reduce((s, l) => s + l.qty, 0);
  const askTot = book.asks.reduce((s, l) => s + l.qty, 0);

  const myBidQtyAt = useMemo(() => {
    const m: Record<number, number> = {};
    for (const o of orders) {
      if (o.side === "buy") m[o.price] = (m[o.price] || 0) + o.remaining;
    }
    return m;
  }, [orders]);
  const myAskQtyAt = useMemo(() => {
    const m: Record<number, number> = {};
    for (const o of orders) {
      if (o.side === "sell") m[o.price] = (m[o.price] || 0) + o.remaining;
    }
    return m;
  }, [orders]);
  const myBidQtyTot = Object.values(myBidQtyAt).reduce((s, v) => s + v, 0);
  const myAskQtyTot = Object.values(myAskQtyAt).reduce((s, v) => s + v, 0);

  return (
    <div className="market">
      <div className="market-head">
        <div className="name">
          {sym}
          <span className="badge">
            {mult ? `×${mult}` : ""}
            {limit ? `  ±${limit}` : ""}
          </span>
        </div>
        <div className="info">
          <span>
            <span className="lbl">Mid</span>
            <span className="val">{mid !== null ? mid.toFixed(1) : "—"}</span>
          </span>
          <span>
            <span className="lbl">Sprd</span>
            <span className="val">{spread !== null ? spread : "—"}</span>
          </span>
          <span>
            <span className="lbl">Pos</span>
            <span className={`val ${pos > 0 ? "pos" : pos < 0 ? "neg" : ""}`}>
              {fmtSigned(pos)}
            </span>
          </span>
          <span>
            <span className="lbl">PnL</span>
            <span className={`val ${pnl >= 0 ? "pos" : "neg"}`}>
              {fmtSigned(Math.round(pnl))}
            </span>
          </span>
          <span>
            <span className="lbl">Vol</span>
            <span className="val">
              {stats.vol}
              {stats.fees > 0 ? (
                <span className="dim"> · {stats.fees.toFixed(1)} fee</span>
              ) : null}
            </span>
          </span>
        </div>
      </div>

      <div className="market-controls">
        <div className="qty-input">
          <span className="lbl">Qty</span>
          <input
            type="number"
            min={1}
            value={qty}
            onChange={(e) =>
              setQty(Math.max(1, parseInt(e.target.value, 10) || 1))
            }
          />
        </div>
        <button
          className="bid bbo"
          disabled={!bestAsk}
          onClick={() => onBBO(sym, "buy", qty)}
          title={bestAsk ? `buy ${qty} ${sym} @ ${bestAsk.price} IOC` : "no ask"}
        >
          <span className="big">BUY</span>
          <span className="small">
            {bestAsk ? `@ ${bestAsk.price} × ${bestAsk.qty}` : "—"}
          </span>
        </button>
        <button
          className="ask bbo"
          disabled={!bestBid}
          onClick={() => onBBO(sym, "sell", qty)}
          title={bestBid ? `sell ${qty} ${sym} @ ${bestBid.price} IOC` : "no bid"}
        >
          <span className="big">SELL</span>
          <span className="small">
            {bestBid ? `@ ${bestBid.price} × ${bestBid.qty}` : "—"}
          </span>
        </button>
        <button
          className="ghost"
          onClick={onCancelAll}
          disabled={!orders.length}
          title="cancel all of my orders on this symbol"
        >
          cancel all ({orders.length})
        </button>
      </div>

      <BookLadder
        sym={sym}
        book={book}
        bestBid={bestBid?.price}
        bestAsk={bestAsk?.price}
        bidTot={bidTot}
        askTot={askTot}
        myBidQtyTot={myBidQtyTot}
        myAskQtyTot={myAskQtyTot}
        myBidQtyAt={myBidQtyAt}
        myAskQtyAt={myAskQtyAt}
        mid={mid}
        onCancelMineAt={onCancelMineAt}
      />
    </div>
  );
}

// ============================================================================

function BookLadder({
  sym,
  book,
  bestBid,
  bestAsk,
  bidTot,
  askTot,
  myBidQtyTot,
  myAskQtyTot,
  myBidQtyAt,
  myAskQtyAt,
  mid,
  onCancelMineAt,
}: {
  sym: string;
  book: Book;
  bestBid?: number;
  bestAsk?: number;
  bidTot: number;
  askTot: number;
  myBidQtyTot: number;
  myAskQtyTot: number;
  myBidQtyAt: Record<number, number>;
  myAskQtyAt: Record<number, number>;
  mid: number | null;
  onCancelMineAt: (sym: string, side: Side, price: number) => void;
}) {
  // Render asks best-first; CSS uses column-reverse so the best ask sits at
  // the bottom of the upper half, right next to the centered mid divider.
  const asks = book.asks;
  const bids = book.bids;

  const maxQty = Math.max(
    1,
    ...book.asks.map((l) => l.qty),
    ...book.bids.map((l) => l.qty)
  );

  if (asks.length === 0 && bids.length === 0) {
    return (
      <div className="book">
        <div className="empty">no book</div>
      </div>
    );
  }

  return (
    <div className="book">
      <div className="asks-section">
        {asks.map((l) => {
          const isBest = l.price === bestAsk;
          const myQty = myAskQtyAt[l.price];
          const isMine = !!myQty;
          const w = `${Math.max(4, (l.qty / maxQty) * 70)}%`;
          return (
            <div
              key={`a-${l.price}`}
              className={`row ask ${isBest ? "best" : ""} ${isMine ? "mine" : ""}`}
            >
              <span className="bar" style={{ width: w }} />
              <span className="px">
                {l.price}
                {isMine ? <span className="mine-dot" /> : null}
              </span>
              <span />
              <span className="qty">
                {l.qty}
                {isMine ? (
                  <span className="mine-tag">MINE {myQty}</span>
                ) : null}
              </span>
              {isMine ? (
                <span
                  className="cancel"
                  title={`cancel my ${myQty} @ ${l.price}`}
                  onClick={() => onCancelMineAt(sym, "sell", l.price)}
                >
                  ×
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
      <div className="mid">
        <span className="l">
          <span className="dim">bid </span>
          <span className="bidc">{bidTot}</span>
          {myBidQtyTot > 0 ? (
            <span className="mine-mini"> · mine {myBidQtyTot}</span>
          ) : null}
        </span>
        <span className="price">{mid !== null ? mid.toFixed(1) : "—"}</span>
        <span>
          <span className="askc">{askTot}</span>
          <span className="dim"> ask</span>
          {myAskQtyTot > 0 ? (
            <span className="mine-mini"> · mine {myAskQtyTot}</span>
          ) : null}
        </span>
      </div>
      <div className="bids-section">
        {bids.map((l) => {
          const isBest = l.price === bestBid;
          const myQty = myBidQtyAt[l.price];
          const isMine = !!myQty;
          const w = `${Math.max(4, (l.qty / maxQty) * 70)}%`;
          return (
            <div
              key={`b-${l.price}`}
              className={`row bid ${isBest ? "best" : ""} ${isMine ? "mine" : ""}`}
            >
              <span className="bar" style={{ width: w }} />
              <span className="px">
                {l.price}
                {isMine ? <span className="mine-dot" /> : null}
              </span>
              <span />
              <span className="qty">
                {l.qty}
                {isMine ? (
                  <span className="mine-tag">MINE {myQty}</span>
                ) : null}
              </span>
              {isMine ? (
                <span
                  className="cancel"
                  title={`cancel my ${myQty} @ ${l.price}`}
                  onClick={() => onCancelMineAt(sym, "buy", l.price)}
                >
                  ×
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ============================================================================

function Executed({
  fills,
  symbols,
  game,
  truePrices,
}: {
  fills: RichFill[];
  symbols: string[];
  game: GameState | null;
  truePrices: Record<string, number> | null;
}) {
  // Per-trade True PnL against the final settlement value, NET of fees.
  //   (cashDelta + signed_position_change * true_value) * multiplier - fee
  //   - buy adds +qty (long), sell adds -qty (short).
  //   - cashDelta is signed notional (no fees).
  //   - fee is subtracted to match the exchange site's net-of-fees PnL.
  //   - returns null while truePrices is not yet known.
  const truePnlFor = (f: RichFill): number | null => {
    if (!truePrices) return null;
    const v = truePrices[f.symbol];
    if (v == null) return null;
    const mult = game?.instruments?.[f.symbol]?.multiplier ?? 1;
    const posChange = f.side === "buy" ? f.qty : -f.qty;
    return (f.cashDelta + posChange * v) * mult - (f.fee ?? 0);
  };
  const [symFilter, setSymFilter] = useState<Record<string, boolean>>({});
  const [sideFilter, setSideFilter] = useState<{ buy: boolean; sell: boolean }>(
    { buy: true, sell: true }
  );
  const [liqFilter, setLiqFilter] = useState<{
    maker: boolean;
    taker: boolean;
    none: boolean;
  }>({ maker: true, taker: true, none: true });

  // Lookup helper: treat unset (undefined) symFilter entries as ON, so newly
  // discovered symbols start visible without an explicit opt-in.
  const symOn = (sym: string) => symFilter[sym] !== false;

  const filtered = useMemo(
    () =>
      fills.filter((f) => {
        if (!symOn(f.symbol)) return false;
        if (f.side === "buy" && !sideFilter.buy) return false;
        if (f.side === "sell" && !sideFilter.sell) return false;
        const liq = f.liquidity;
        if (liq === "maker" && !liqFilter.maker) return false;
        if (liq === "taker" && !liqFilter.taker) return false;
        if (liq == null && !liqFilter.none) return false;
        return true;
      }),
    [fills, symFilter, sideFilter, liqFilter]
  );

  const checkAll = () => {
    setSymFilter({});
    setSideFilter({ buy: true, sell: true });
    setLiqFilter({ maker: true, taker: true, none: true });
  };
  const allOn =
    symbols.every((s) => symOn(s)) &&
    sideFilter.buy &&
    sideFilter.sell &&
    liqFilter.maker &&
    liqFilter.taker &&
    liqFilter.none;

  // Click semantics: clicking a chip ISOLATES that chip in its group (turns
  // every other chip in the group off). Clicking the same chip again — when
  // it's already the only one ON — restores the whole group to ON. This
  // matches "click A to show only A; click A again to see everything".
  const isolateSym = (s: string) => {
    const onSyms = symbols.filter((sy) => symOn(sy));
    const isOnlyOn = symOn(s) && onSyms.length === 1;
    if (isOnlyOn) {
      setSymFilter({});
      return;
    }
    const next: Record<string, boolean> = {};
    for (const sy of symbols) next[sy] = sy === s;
    setSymFilter(next);
  };
  const isolateSide = (side: "buy" | "sell") => {
    const other = side === "buy" ? "sell" : "buy";
    const isOnlyOn = sideFilter[side] && !sideFilter[other];
    if (isOnlyOn) {
      setSideFilter({ buy: true, sell: true });
    } else {
      setSideFilter({ buy: side === "buy", sell: side === "sell" });
    }
  };
  const isolateLiq = (key: "maker" | "taker" | "none") => {
    const onCount =
      (liqFilter.maker ? 1 : 0) +
      (liqFilter.taker ? 1 : 0) +
      (liqFilter.none ? 1 : 0);
    const isOnlyOn = liqFilter[key] && onCount === 1;
    if (isOnlyOn) {
      setLiqFilter({ maker: true, taker: true, none: true });
    } else {
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
      if (f.liquidity === "maker") maker += 1;
      else if (f.liquidity === "taker") taker += 1;
      if (f.side === "buy") buys += f.qty;
      else sells += f.qty;
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

  return (
    <div className="bottom">
      <div className="bottom-head">
        <span className="title">Executed Orders</span>
        <div className="filters">
          <div className="filter-group">
            <span className="lbl">Sym</span>
            {symbols.map((s) => (
              <button
                key={s}
                className={`chip ${symOn(s) ? "on" : ""}`}
                title={`show only ${s} (click again to show all)`}
                onClick={() => isolateSym(s)}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="filter-group">
            <span className="lbl">Side</span>
            <button
              className={`chip ${sideFilter.buy ? "on bid" : ""}`}
              title="show only buys (click again to show all)"
              onClick={() => isolateSide("buy")}
            >
              buy
            </button>
            <button
              className={`chip ${sideFilter.sell ? "on ask" : ""}`}
              title="show only sells (click again to show all)"
              onClick={() => isolateSide("sell")}
            >
              sell
            </button>
          </div>
          <div className="filter-group">
            <span className="lbl">Liq</span>
            <button
              className={`chip ${liqFilter.maker ? "on" : ""}`}
              title="show only maker fills (click again to show all)"
              onClick={() => isolateLiq("maker")}
            >
              maker
            </button>
            <button
              className={`chip ${liqFilter.taker ? "on" : ""}`}
              title="show only taker fills (click again to show all)"
              onClick={() => isolateLiq("taker")}
            >
              taker
            </button>
            <button
              className={`chip ${liqFilter.none ? "on" : ""}`}
              title="show only fills with no liq tag (click again to show all)"
              onClick={() => isolateLiq("none")}
            >
              n/a
            </button>
          </div>
          <button
            className="ghost"
            onClick={checkAll}
            disabled={allOn}
            title="re-enable every filter (show all)"
          >
            check all
          </button>
        </div>
        <div className="agg">
          <span>
            <span className="lbl">Fills</span>
            <span className="val">
              {agg.count}
              {agg.count !== fills.length ? (
                <span className="dim"> / {fills.length}</span>
              ) : null}
            </span>
          </span>
          <span>
            <span className="lbl">Vol</span>
            <span className="val">{agg.vol}</span>
          </span>
          <span>
            <span className="lbl">Buys / Sells</span>
            <span className="val">
              <span className="bidc">{agg.buys}</span>
              <span className="dim"> / </span>
              <span className="askc">{agg.sells}</span>
            </span>
          </span>
          <span>
            <span className="lbl">Notional</span>
            <span className="val tabnum">{agg.notional.toFixed(0)}</span>
          </span>
          <span>
            <span className="lbl">Fees</span>
            <span className="val tabnum">{agg.fees.toFixed(1)}</span>
          </span>
          <span>
            <span className="lbl">Maker / Taker</span>
            <span className="val">
              <span className="dim">{agg.maker}</span>
              <span className="dim"> / </span>
              <span className="warn">{agg.taker}</span>
            </span>
          </span>
          <span>
            <span className="lbl">Realized*</span>
            <span className={`val ${agg.realized >= 0 ? "pos" : "neg"}`}>
              {fmtSigned(Math.round(agg.realized))}
            </span>
          </span>
          <span title="per-trade PnL against the true (settlement) value; shown after the round ends">
            <span className="lbl">True PnL</span>
            {agg.truePnlAvailable ? (
              <span
                className={`val ${agg.truePnl >= 0 ? "pos" : "neg"}`}
              >
                {fmtSigned(Math.round(agg.truePnl))}
              </span>
            ) : (
              <span className="val dim">—</span>
            )}
          </span>
        </div>
      </div>
      <div className="bottom-body">
        {filtered.length === 0 ? (
          <div className="empty">
            {fills.length === 0
              ? "no executed orders yet"
              : "no fills match the current filters"}
          </div>
        ) : (
          <table className="fills-table">
            <thead>
              <tr>
                <th className="l" title="time since the round started">
                  Round T
                </th>
                <th className="l">Sym</th>
                <th className="l">Side</th>
                <th>Px</th>
                <th>Qty</th>
                <th>Notional</th>
                <th>Fee</th>
                <th title="PnL contribution = signed notional × multiplier − fee">
                  Δ PnL
                </th>
                <th title="per-trade PnL against the true (settlement) value, net of fee; shown after the round ends">
                  True PnL
                </th>
                <th className="l">Liq</th>
                <th className="l">CP</th>
                <th>Order ID</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((f, i) => {
                const mult =
                  game?.instruments?.[f.symbol]?.multiplier ?? 1;
                const pnlImpact = f.cashDelta * mult - (f.fee ?? 0);
                return (
                  <tr key={`${f.trade_id}-${i}`}>
                    <td
                      className="l dim tabnum"
                      title={fmtTime(f.t)}
                    >
                      {fmtElapsed(f.elapsedMs)}
                    </td>
                    <td className="l">{f.symbol}</td>
                    <td className="l">
                      <span
                        className={`pill ${f.side === "buy" ? "bid" : "ask"}`}
                      >
                        {f.side}
                      </span>
                    </td>
                    <td className={f.side === "buy" ? "bidc" : "askc"}>
                      {f.price}
                    </td>
                    <td>{f.qty}</td>
                    <td className="tabnum">{f.notional.toFixed(0)}</td>
                    <td className="tabnum dim">
                      {f.fee != null ? f.fee.toFixed(1) : ""}
                    </td>
                    <td
                      className={`tabnum ${
                        pnlImpact > 0 ? "pos" : pnlImpact < 0 ? "neg" : ""
                      }`}
                    >
                      {fmtSigned(Math.round(pnlImpact))}
                    </td>
                    {(() => {
                      const tp = truePnlFor(f);
                      return (
                        <td
                          className={`tabnum ${
                            tp == null
                              ? "dim"
                              : tp > 0
                              ? "pos"
                              : tp < 0
                              ? "neg"
                              : ""
                          }`}
                        >
                          {tp == null ? "—" : fmtSigned(Math.round(tp))}
                        </td>
                      );
                    })()}
                    <td
                      className={`l ${
                        f.liquidity === "maker"
                          ? "liq-maker"
                          : f.liquidity === "taker"
                          ? "liq-taker"
                          : "dim"
                      }`}
                    >
                      {f.liquidity ?? "—"}
                    </td>
                    <td className="l dim">{f.counterparty ?? "—"}</td>
                    <td className="dim tabnum">{f.order_id ?? ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function fmtTime(t_ns: number) {
  const ms = Math.floor(t_ns / 1e6);
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

function fmtElapsed(ms?: number | null) {
  if (ms == null) return "—";
  const total = Math.max(0, Math.round(ms));
  const mm = Math.floor(total / 60000);
  const ss = Math.floor((total % 60000) / 1000);
  const mmm = total % 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(mm)}:${pad(ss)}.${pad(mmm, 3)}`;
}

// ============================================================================

function Toasts({ toasts }: { toasts: LogLine[] }) {
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.t} className={`toast ${t.level}`}>
          {t.text}
        </div>
      ))}
    </div>
  );
}
