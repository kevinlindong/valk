# Day 1 — Algo Trading Game

Working notes for the Day 1 algo. Captures the game model, the strategy,
findings from probing the live market, and the resulting changes — written so
you can pick this up cold and keep iterating.

## Game model

- Each round, `(a, w)` are drawn once from priors and fixed:
  - `a ~ floor(LogN(1.0, 0.8))`
  - `w ~ 1 + floor(LogN(0.5, 0.7))`,   with `b = a + w`
- Then `N` reveals `X_i ~ Uniform{a, ..., b}` are drawn over T seconds (one
  every `reveal_interval` seconds).
- Settlement price = `sum(X_i)`.
- Position limit ±100. Fees: maker 0.5 + taker 0.5 per lot.
- Multiple participants: bots **and** other human teams.

Connection (hardcoded at top of `strategy.py` and `probe.py`):

```
URL     = http://192.168.50.167:8000
API_KEY = intern2-KEVD
TRADER  = KEVD  (echoed back via the WS 'hello' message)
SYMBOL  = A
```

## Strategy in one sentence

Maintain a Bayesian posterior over `(a, w)`, quote a passive bid/ask around
the posterior expectation of the final settlement with a σ-scaled half-spread
plus inventory skew, and snipe any book level mispriced by more than
`taker_fee + buffer·σ`.

### Pieces (in `strategy.py`)

1. **`Posterior`** — discrete grid over `(a, w)` built by MC from the prior.
   Each reveal applies Bayes (support constraint + uniform likelihood `1/w`).
   `predict_settle(running_sum, n_remaining)` returns `(mean, std)` of final
   settlement via the law of total variance:
   `Var = E[Var | a,w] + Var[E | a,w]`.

2. **`Strategy.desired_quotes`** — `bid = fair - edge + skew`,
   `ask = fair + edge + skew`. `edge = max(min_edge, edge_per_sigma·σ)`,
   `skew = -position·skew_per_unit`.

3. **`Strategy.maybe_snipe`** — taker IOC against any level with
   `mispricing > taker_fee + max(snipe_min_edge, snipe_buffer_sigma·σ)`.
   Filters own resting prices client-side.

4. **`Strategy.on_quote_event`** — fires on every `quote_add` / `quote_cancel`
   delta (see "exploit" below) and tries `maybe_snipe` immediately.

## Files in `day1/`

| File | Purpose |
|---|---|
| `strategy.py` | Live trading bot, standalone. Run with `python day1/strategy.py`. |
| `probe.py` | Passive WS logger + optional active probes, standalone. Writes JSONL. |
| `run_combined.py` | **Recommended** — run Strategy + Probe over ONE WS connection. Trades AND logs simultaneously (the exchange allows only one WS per API key). |
| `analyze.py` | Reads a JSONL log and prints market-structure findings. |
| `simulation.py` | Offline simulation (single MM) of OurBot vs ImprovedBot. |
| `simulation_mc.py` | Full Monte Carlo (NoiseMM + TightMM + ValueSniper, bootstrap CIs, 6-panel plot). |
| `logs/` | Per-run JSONL log files (created on first run). All sessions write here; old logs are never overwritten. |
| `logs/combined_log_<timestamp>.jsonl` | One file per `run_combined.py` session (append-only). |
| `logs/probe_log_<timestamp>.jsonl` | One file per `probe.py` session (append-only). |
| `NOTES.md` | This file. |

## Findings from probing the live market

Run on a probe log of 11,709 events across 2 rounds (`python day1/analyze.py`).

### Undocumented WS message types

The exchange pushes extra event types beyond what the SDK's typed handlers
dispatch. Capture them via `c.on_message` (catch-all):

| `type` | Payload | What it tells you |
|---|---|---|
| `quote_add` | `order_id, symbol, side, price, qty, ts_ns` | Every new order on the book (any participant) |
| `quote_cancel` | `order_id, side, price, qty, reason, ts_ns` | Every cancel. `reason ∈ {user, modify_replace, self_match_prevention, game_end}` |
| `quote_fill` | `order_id, side, price, qty, remaining, trade_id, ts_ns` | Every fill, including residual qty left on the order |
| `hello` | `trader` | One-time handshake; confirms your trader name |

Public `trade` events expose `aggressor` SIDE (`buy`/`sell`) only — **no
trader names**. The only place you learn a counterparty's name is your own
`on_fill` messages (the `counterparty` field).

### Market structure (numbers)

The spread has tightened markedly session-over-session — competition is
sharpening. Knobs have been re-tuned each time.

| Metric | Session 1 | Session 2 | Session 3 (latest) |
|---|---:|---:|---:|
| Median quote size (`quote_add.qty`) | 5 | 5 | 5 |
| Median trade size | 2 | 3 | 2 |
| Median quote lifetime | ~1.0s | ~0.9s | ~0.9s |
| Median spread | **6 ticks** | **3 ticks** | **2 ticks** |
| Add/cancel ratio | 1.05 | 1.05 | 1.08 |
| Aggressor balance | 58% sell | ~even | ~even |
| `self_match_prevention` cancels | 46 | 69 | 45 |

## Strategy changes (already applied)

### Knob updates in `Strategy.__init__`

Knobs have been re-tuned each session as the market evolved. Current values
are in the rightmost column. Older columns are kept so the reasoning is visible.

| Knob | Initial | After 1st probe (spread 6) | After +629 session (spread 3) | **Current** (spread 2) |
|---|---:|---:|---:|---:|
| `quote_qty` | 10 | 5 | 5 | **5** |
| `min_edge` | 1.5 | 2.5 | 1.5 | **1.0** |
| `snipe_min_edge` | 0.5 | 1.5 | 1.0 | **0.5** |
| `snipe_buffer_sigma` | 0.30 | 0.50 | 0.40 | **0.30** |
| `skew_per_unit` | 0.10 | 0.15 | 0.20 | **0.20** |
| `edge_per_sigma` | 0.25 | 0.30 | 0.25 | **0.22** |
| `snipe_full_size_sigma` | — | — | 1.5 | **1.5** |
| `snipe_book_depth` | — | — | 10 | **10** |

**Why the current values:** median market spread is 2 ticks. `min_edge=1.0`
keeps us competitive at top-of-book on each side without giving away more
than `min_edge - maker_fee = 0.5` per lot. `snipe_min_edge=0.5` means
`edge_required = taker_fee + 0.5 = 1.0` so 1-tick mispricings in a 2-tick
market actually trigger (the previous 1.0 floor meant snipes almost never
fired — only 4 of 78 fills in the +366 session were taker).

**Fee guardrail:** `quote_qty` stays at 5 (market median trade is 2-3 — going
above 5 makes us the disproportionate target). Tighter spread does grow fill
count, but each maker fill still earns roughly `min_edge - maker_fee = 0.5`
net of fees, so volume is profitable.

### Structural changes in `Strategy`

1. **`maybe_snipe` skips own resting prices client-side** — looks at
   `self.resting["bid"]["price"]` and `self.resting["ask"]["price"]` before
   sending IOC. Eliminates the 46 `self_match_prevention` round-trips/session.

2. **`on_quote_event` handler subscribed via `c.on_message`** — fires on every
   `quote_add` / `quote_cancel`. Calls `maybe_snipe` immediately. This is the
   **highest-impact change**: median quote lifetime is ~1 second, so reacting
   only on reveals (every 30s) missed the entire stale-quote window. Now we
   snipe within milliseconds of a mispriced quote appearing.

### Safety guards (already in place — do not remove)

- **`cancel_all` on `phase -> running`**: prevents stale orders from the
  previous round being lifted instantly when a new round starts (this was the
  root cause of "instant -100" at game start).
- **`min_reveals_to_quote = 1` and `min_reveals_to_snipe = 1`**: no quoting or
  sniping from prior-only fair (the unconditional prior mean is biased
  relative to the realized `(a, w)` and has wide uncertainty).
- **`quote_after_final_reveal = False`**: once all `N` reveals are in,
  `n_remaining = 0` and fair is the exact settlement (not theoretical).
  Quoting passive spread on a known value just gives away edge with no
  informational advantage, so passive bid/ask are cancelled immediately on
  the final reveal. `Strategy.on_reveal` prints `FINAL  all k/N reveals in...
  passive quotes -> cancel; sweeping book.` when this triggers.
- **`snipe_after_final_reveal = True`** (kept): visible mispricings after
  the final reveal are someone else's mistake on a value we both know — pure
  profit.
- **`_sweep_at_settle`** (new): the moment the final reveal lands, we cancel
  passive quotes AND fire `maybe_snipe(settle, sigma=0)` in a short loop
  (up to 6 passes, 50ms apart). With `sigma=0` and `n_remaining==0`,
  `maybe_snipe` uses `edge_required = taker_fee` (no buffer / no min_edge)
  and `snipe_cap = position_limit` — so it walks the book and takes any
  level whose mispricing exceeds the taker fee, up to the position limit.
  This is the "take a profitable position immediately" rule: when settlement
  is known, every cent of mispricing on the visible book is risk-free profit.
- **`Strategy.flatten`**: cancel-all then market-flatten. Wired to `q` /
  Ctrl-C exit and to the `f` interactive command.

## How to reproduce / continue

### Run the live bot

Recommended (trades AND logs market data via one WS connection):

```
python day1/run_combined.py
```

Standalone bot (no logging):

```
python day1/strategy.py
```

Interactive commands while running: `s` = status, `c` = counterparty stats
(combined only), `f` = flatten now, `q` = quit (auto-flattens). Ctrl-C also
flattens.

**Do NOT run `strategy.py` and `probe.py` in two terminals at the same time** —
the exchange allows only one private WS per API key. Use `run_combined.py`.

### Collect more data

```
python day1/probe.py                                # passive logging only
python day1/probe.py --mode all --probe-interval 60 # gentle active probes
python day1/probe.py --exclude-counterparty intern3-XXXX  # skip a human
```

Interactive in probe: `s` status, `c` counterparty stats, `f` flatten, `q` quit.

### Re-run the analysis

```
python day1/analyze.py                                            # newest in day1/logs/
python day1/analyze.py --log day1/logs/combined_log_20260518_220803.jsonl
```

`analyze.py` looks in `day1/logs/` first; legacy `day1/*.jsonl` paths are
still searched as a fallback for older sessions.

The "Actionable findings" section auto-generates parameter recommendations
from the data, so re-run it after the market changes.

### Offline backtest

```
python day1/simulation.py --compare --rounds 100
python day1/simulation_mc.py                        # 200 rounds × 10 seeds
python day1/simulation_mc.py --quick                # 50 × 3, ~few seconds
```

## Known limitations / open questions

- **No counterparty names on the public tape.** We can only fingerprint
  counterparties we've personally filled against. To build a fuller picture,
  the bot would need to trade more (which costs fees) — there's no free way
  to identify the other participants.
- **Effect of the latest changes isn't measured yet.** Need a few rounds with
  the new `strategy.py` to confirm reduced fees and improved PnL. Re-run
  `analyze.py` on a fresh log after a session.
- **The `on_quote_event` snipe path may be chatty.** Every quote_add triggers
  a check. If REST volume becomes a problem, debounce or cache the last fair
  for ~50ms inside `maybe_snipe`.
- **Prior parameters are hardcoded.** The handout warns parameters may change.
  All four `*_LOGN_MU/SIGMA` constants live at the top of `strategy.py`;
  rebuild the `Posterior` if they change.
- **`Posterior` likelihood uses `1/w` (continuous-uniform).** The reveals are
  integers, so the strict likelihood is `1/(w+1)` (discrete-uniform). The
  `ImprovedPosterior` in `simulation.py` uses the corrected version. Worth
  porting that to live `strategy.py` if PnL still lags.
