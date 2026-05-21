export type Side = "buy" | "sell";

export type Level = { price: number; qty: number };
export type Book = { bids: Level[]; asks: Level[] };

export type Order = {
  order_id: number;
  symbol: string;
  side: Side;
  price: number;
  qty: number;
  remaining: number;
  tif?: string;
  status?: string;
};

export type Fill = {
  t: number;
  trade_id: number;
  order_id?: number;
  symbol: string;
  side: Side;
  price: number;
  qty: number;
  liquidity?: string;
  counterparty?: string;
  fee?: number;
};

export type Positions = Record<string, number>;

export type Instrument = {
  position_limit: number;
  tick_size: number;
  multiplier?: number;
};

export type GameState = {
  phase: string;
  reveals?: number[];
  running_sum?: number;
  remaining_seconds?: number | null;
  duration?: number;
  reveal_interval?: number;
  instruments?: Record<string, Instrument>;
};

export type Tif = "gtc" | "ioc" | "fok";

export type LogLine = {
  t: number;
  level: "info" | "warn" | "error";
  text: string;
};
