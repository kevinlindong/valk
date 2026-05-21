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
plus inventory skew, opportunistically penny/dime inside the market's best
when the per-fill margin stays positive, and snipe any book level mispriced
by more than `taker_fee + buffer·σ`.

### Pieces (in `strategy.py`)

1. **`Posterior`** — discrete grid over `(a, w)` built by MC from the prior.
   Each reveal applies Bayes (support constraint + **discrete-uniform**
   likelihood `1/(w+1)` over integers `{a, ..., a+w}`; the `Var` term
   correspondingly uses `w(w+2)/12`). `predict_settle(running_sum, n_remaining)`
   returns `(mean, std)` of final settlement via the law of total variance:
   `Var = E[Var | a,w] + Var[E | a,w]`.

2. **`Strategy.desired_quotes`** — `bid = fair - edge + skew`,
   `ask = fair + edge + skew`. `edge = max(min_edge, edge_per_sigma·σ)`,
   `skew = -position·skew_per_unit`. Then `_apply_penny` may step
   `tight_step_inside_ticks` inside the market's best — only when (a) the
   book cache is fresh, (b) we're not already at/inside the market's best,
   (c) the stepped-in price is strictly tighter than our default, and
   (d) it preserves `tight_floor_edge` against `fair + skew`. Skipping
   the step is always safe (default quote is still a valid post).

3. **`Strategy.maybe_snipe`** — taker IOC against any level with
   `mispricing > taker_fee + max(snipe_min_edge, snipe_buffer_sigma·σ)`.
   Filters own resting prices client-side. Refreshes the shared book cache
   used by `_apply_penny`. Mid-round snipes count against
   `max_snipes_per_round` (hard fee-runaway cap); the post-final-reveal
   sweep is exempt.

4. **`Strategy._try_direct_snipe`** — fast-path IOC fired directly off a
   `quote_add` WS event. No book fetch (~5-15ms vs ~50-100ms for the
   book-scan path), so we beat competing snipers to fresh mispriced
   quotes. Requires a FRESH `_book_cache` for the disagreement gate
   (otherwise defers to the throttled path). Same other safety gates as
   `maybe_snipe` (phase, min_reveals, rate cap, own-quote skip, position
   cap, variance cap).

5. **`Strategy.on_quote_event`** — fires on every `quote_add` /
   `quote_cancel` delta. Two paths: (a) un-throttled direct snipe on
   quote_add (naturally rate-limited by the mispricing gate); (b)
   throttled (~10/sec) `maybe_snipe` plus reprice of maker quotes
   (penny path) so a new top-of-book triggers a reprice within ~100ms.
   `_reprice` is idempotent — unchanged prices don't churn the queue.

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

Knobs are re-derived after each session, but the **current** values come from
fee economics — not from session-specific tuning — to avoid overfitting.

| Knob | Initial | After 1st probe (spread 6) | After +629 session (spread 3) | After disaster session | After over-correction | After top-of-book rebalance | **Current** |
|---|---:|---:|---:|---:|---:|---:|---:|
| `quote_qty` | 10 | 5 | 5 | 5 | 5 | 2 | **2** |
| `min_edge` | 1.5 | 2.5 | 1.5 | 1.5 | 1.0 | 1.0 | **1.0** |
| `snipe_min_edge` | 0.5 | 1.5 | 1.0 | 1.5 | 1.0 | 1.0 | **1.0** |
| `snipe_buffer_sigma` | 0.30 | 0.50 | 0.40 | 0.60 | 0.60 | 0.40 | **0.40** |
| `skew_per_unit` | 0.10 | 0.15 | 0.20 | 0.20 | 0.20 | 0.20 | **0.20** |
| `edge_per_sigma` | 0.25 | 0.30 | 0.25 | 0.25 | 0.25 | 0.25 | **0.25** |
| `snipe_book_depth` | — | — | 10 | 10 | 10 | 10 | **10** |
| `tight_floor_edge` | — | — | — | 1.0 | 1.0 | 1.0 | **1.0** |
| `tight_step_inside_ticks` | — | — | — | 1 | 1 | 1 | **1** |
| `max_snipes_per_round` | — | — | — | 30 | 30 | 60 | **120** |
| `snipe_max_qty_per_level` | — | — | — | — | — | — | **10** |
| `min_reveals_to_quote` | 0 | 0 | 0 | 0 | 1 | 1 | **1** |
| `pre_reveal_min_edge` | — | — | — | — | — | 10.0 | **4.0** |
| `pre_reveal_pull_disagreement` | — | — | — | — | — | 15.0 | **10.0** |
| `empty_side_edge` | — | — | — | — | — | — | **15.0** |
| `quote_event_throttle_sec` | — | — | — | — | — | 0.10 | **0.05** |
| `max_pos_target_sigma` | — | — | — | — | 2.0 | 2.0 | **2.0** |
| `max_pos_floor` | — | — | — | — | 10 | 10 | **10** |
| `max_pos_target_sigma_normal` | — | — | — | — | — | — | **3.0** |
| `max_pos_floor_normal` | — | — | — | — | — | — | **15** |
| `market_anchor_disagreement_min` | — | — | — | — | 5.0 | 5.0 | **5.0** |
| `snipe_max_disagreement` | — | — | — | — | 10.0 | 10.0 | **10.0** |
| `full_qty_sigma_max` | — | — | — | — | 3.0 | 3.0 | **3.0** |
| `mid_round_quote_qty` | — | — | — | — | 2 | 1 | **1** |
| `mid_round_quote_qty_normal` | — | — | — | — | — | — | **2** |
| `quote_qty_normal` | — | — | — | — | — | — | **5** |
| `tight_penny_min_reveals` | — | — | — | — | 2 | 1 | **0** |
| `tight_penny_sigma_max` | — | — | — | — | 4.0 | 10.0 | **10.0** |
| `inv_dampen_threshold_frac` | — | — | — | — | — | — | **0.40** |
| `inv_dampen_factor` | — | — | — | — | — | — | **0.0** |
| `inv_hard_kill_frac` | — | — | — | — | — | — | **0.55** |
| `snipe_adverse_extra_edge` | — | — | — | — | — | — | **1.5** |

Removed (folded into `snipe_max_qty_per_level`): `snipe_full_size_sigma`.

**Why the current values (derived, not tuned):**

- **`min_edge = 1.0`** = `maker_fee + 0.5` (positive expected per-fill margin
  with a small adverse-selection buffer). At maker_fee=0.5 this means each
  maker fill earns net ≥ 0.5 per lot.
- **`tight_floor_edge = 1.0`** = the absolute minimum gross per-fill margin
  we will ever quote — same derivation. The penny logic will not step
  tighter than this even if the market does.
- **`snipe_min_edge = 1.5` + `snipe_buffer_sigma = 0.60`** ⇒
  `edge_required ≥ taker_fee + 1.5 = 2.0` ticks. The disaster session showed
  that requiring only +0.5 over taker fee fires on 1-tick noise in a 2-tick
  market and burns fees faster than the edge pays. 2.0 means we only snipe
  genuine mispricings, not stochastic fluctuations.
- **`max_snipes_per_round = 30`** is a hard ceiling against fee runaway if
  the edge calc is wrong. The +366 session used 4 snipes; the disaster
  session fired 100+. 30 is generous middle ground that still pages us before
  we self-destruct. Reset on every `phase -> running` so it doesn't leak
  across rounds.

**Fee guardrail:** `quote_qty` stays at 5 (market median trade is 2-3 — going
above 5 makes us the disproportionate target). Tighter spread does grow fill
count, but each maker fill still earns roughly `min_edge - maker_fee = 0.5`
net of fees, so volume is profitable.

### Penny/dime (`_apply_penny`)

When another trader is between our default quote and our `tight_floor_edge`,
step `tight_step_inside_ticks` inside their best on that side. Designed to
**only** activate when:

- The book cache (refreshed by `maybe_snipe`) is < 1 second old.
- We are not already at-or-better-than the market's best on that side
  (preserves queue priority — don't undercut yourself).
- The stepped-in price is strictly tighter than our default.
- The stepped-in price still preserves `tight_floor_edge` against
  `fair + skew` (so per-fill expected margin stays ≥ 0.5 net).

This is reactive to live book state, not session-tuned. In a 2-tick market
the penny condition often won't fire (we're already inside the market's best
at our default `min_edge=1.0`), which is correct — there's no tighter price
that still earns. In wider markets or when sigma temporarily widens our
default, penny grabs the inside without sacrificing the floor.

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
- **`min_reveals_to_quote = 0`** (RE-ENABLED with explicit tail defense
  and tightened to ride near the actual market spread). Pre-reveal MM
  was previously disabled after a tail-prior round (reveals
  14,14,14,12,14,13,12,12,14,12 → settle=131) caused **−5343** loss in
  30s: our prior fair (~45) was far below market consensus (~60), so our
  ask at 55 (= prior + edge=10) landed INSIDE the market bid of 58 →
  marketable post → taker-filled at 58 → −73 per lot. Now re-enabled with
  three layered protections that didn't exist before:
  1. **Pre-reveal market_mid anchor** (extends existing K≥1 logic):
     `desired_quotes` returns `(None, None)` if BOTH sides of the
     pre-reveal book are empty (or the cache is stale). When at least
     one side has a quote, the same `biased` clamp that protects K≥1
     quotes runs pre-reveal too: ask = max(default_ask,
     ceil(market_mid + edge)), bid = min(default_bid, floor(market_mid −
     edge)). Widens quotes OUTSIDE the market spread — no more
     inside-spread posts.
  2. **`pre_reveal_pull_disagreement = 10.0`** (was 15.0): when
     `market_mid > prior_fair + 10` (tail-UP suspected) the ASK is
     suppressed entirely, not just widened. Symmetric for tail-DOWN.
     Tightened from 15 because the `pre_reveal_min_edge` floor was
     lowered from 10 to 4 — the safety margin shrunk, so we must catch
     borderline disagreement sooner. The original disaster had
     `|diff|=15` exactly; 10 catches it earlier.
  3. **`empty_side_edge = 15.0`** (new): when ONE side of the pre-reveal
     book is empty, quote the missing side `prior_fair ± 15` (extra
     wide vs the default `prior_fair ± 4`). Log analysis showed empty-
     side cases get filled within ~100ms by a walker with a wide-
     marketable order, who pays the fat premium. The surviving side
     still acts as a single-sided tail signal: high lone bid →
     ASK pulled, low lone ask → BID pulled (same `pre_reveal_pull_
     disagreement = 10` threshold).
  Knob shift to ride inside the actual pre-reveal spread:
  **`pre_reveal_min_edge = 4.0`** (was 10.0). Older-log analysis
  across 6 rounds: other traders made 36-103 pre-reveal trades each
  while we made 9 TOTAL (loss −26). The old `edge=10` parked us at
  `prior ± 10`, way outside the typical 3-tick pre-reveal spread that
  EVERYONE else lives inside. At `edge=4` we're at `prior ± 4`, inside
  the median spread but with the three layered defenses above blocking
  the disaster mode.
- **Sigma-scaled position cap** (`max_pos_target_sigma=2.0`,
  `max_pos_floor=10`, new): `_max_position_for_sigma(sigma)` returns
  `position_limit` when `sigma ≤ 2.0`, else `max(floor, limit · 2.0 / sigma)`.
  Applied in BOTH `desired_quotes` (suppress bid/ask when over cap) and
  `maybe_snipe` (cap headroom for both ask-snipes and bid-snipes). The
  intuition: each adverse-pickoff fill costs ~sigma ticks, so cap
  inventory linearly in 1/sigma to bound worst-case loss to
  `cap · sigma`. With `sigma=10`, cap=20; with `sigma=4`, cap=50. At
  low sigma (well-formed posterior), full `position_limit` is available.
- **Bias-aware cap relaxation** (`max_pos_target_sigma_normal=3.0`,
  `max_pos_floor_normal=15`, new): when `market_mid` is fresh AND
  `|fair − market_mid| < 5` (consensus confirms our fair, "relaxed"
  regime), `_max_position_for_sigma(sigma, relaxed=True)` is used
  instead, raising the cap. K=1 (sigma~8) goes from 25 → ~38; K=2
  (sigma~6) from 33 → 50; pre-reveal floor from 10 → 15. Log
  analysis of 3 recent rounds: max|pos| was 15 / 35 / 100 vs the
  conservative cap of 25 (K=1) — the cap was BARELY binding in the
  two settled rounds, leaving headroom unused, AND max-pos was NOT
  correlated with worst-PnL rounds (tail-round had highest max-pos
  *and* highest PnL). The relaxation only activates when we have
  consensus-confirmation; in the biased / no-signal regime we fall
  back to the conservative cap above. Passed through to `maybe_snipe`,
  `_try_direct_snipe`, and `desired_quotes`.
- **Bias-aware quote sizing** (`quote_qty_normal=5`,
  `mid_round_quote_qty_normal=2`, new): per-quote size is the actual
  volume bottleneck — log analysis showed 49 fills in a settled round
  at avg|pos|=5, so we're getting fills but each is tiny. In the
  same "relaxed" regime as above (market_mid confirms fair), upsize
  to quote_qty=5 / mid_round=2 (from 2 / 1). Worst-case per-fill
  adverse loss at qty=2, sigma=8 is 16 ticks vs 8 at qty=1 — still
  small in absolute terms, and the bias detector blocks the regime
  where this matters. `_current_quote_qty` consults market_mid itself
  so `_post` / `_reprice` agree with the headroom check in
  `desired_quotes`.
- **Market-mid anchor** (`market_anchor_disagreement_min=5.0`, new):
  when `|fair - market_mid| ≥ 5.0`, the posterior is likely biased (we
  trust other traders' aggregate consensus over our prior in tail rounds).
  In `desired_quotes`, ANCHOR widens quotes toward the market — bid
  clamped down to `floor(market_mid - edge)`, ask clamped up to
  `ceil(market_mid + edge)`. Penny is skipped in the biased regime
  (its floors use our biased fair, so pennying near the market is
  unsafe). Never tightens vs default — purely one-sided safety.
- **Snipe disagreement gate** (`snipe_max_disagreement=10.0`, new):
  when `|fair - market_mid| > 10.0` mid-round, `maybe_snipe` returns
  False immediately. Our snipe edge calc uses our fair; in a biased
  regime that fair is wrong, so a snipe would buy a stack of contracts
  at "low" prices that are actually near truth (or sell at "high" prices
  that are actually below truth). Post-final-reveal sweep is EXEMPT
  (fair == settle exactly there, so disagreement IS profit).
- **`min_reveals_to_snipe = 1`** (kept): sniping pre-reveal would burn fees
  on prior-noise mispricings. Quoting is fine because the edge is large;
  sniping requires confidence that a level is genuinely off.
- **Asymmetric inventory dampener** (`inv_dampen_threshold_frac=0.40`,
  `inv_dampen_factor=0.0`, new): when `|position| > 0.40 * max_pos`, the
  quote on the side that would ADD to the position is shrunk to 1 lot.
  `_current_quote_qty(side)` consults `position` and the side, so the
  bid and ask can carry different sizes. Sim round 2 (-950 baseline) and
  round 23 (-470) showed the strategy filling 3-5 lots on the same side
  for 4-5 reveals while a wrong-confident posterior held; this knob
  caps that same-direction bleed without abandoning the profitable
  market-make on the OPPOSITE side.
- **Hard kill on adverse side** (`inv_hard_kill_frac=0.55`, new): a 1-lot
  adverse quote still bled (many fills × 1 lot adds up). When
  `|position| > 0.55 * max_pos`, the adverse-side quote is dropped to
  `None` entirely. Skew + max_pos cap are softer defenses; this is the
  hard backstop.
- **Inventory-aware snipe edge** (`snipe_adverse_extra_edge=1.5`, new):
  in both `maybe_snipe` and `_try_direct_snipe`, when a take would ADD
  to existing position past `inv_dampen_threshold_frac`, require
  `+1.5` ticks of additional edge. Defends against value_sniper-style
  counterparties that pass us bad fills when we're already exposed.
- **`min_reveals_to_quote = 1`** (changed from 0): pre-reveal MM was a
  consistent net-negative in the sim — per-fill PnL of -1.46 (seed 1) to
  -3.54 (seed 7) on the 195-245 fills we collected pre-reveal. With it
  off, seed 7 mean PnL rose from +163 → +189 and worst-round from
  -406 → -274. Pre-reveal sniping is still gated by `min_reveals_to_snipe=1`.
- **`_apply_target_quotes` cross-aware ordering** (new): when the fair
  jumps after a reveal and the new bid would cross the OLD resting ask
  (or vice versa), cancel the would-be-crossed side first. Previously
  the bid-first reprice fired `c.modify("bid", new_px)` which crossed
  our own ask in the sim, eating 0.5/lot in fees; in production the
  server rejects via SMP but at the cost of round-trips.
- **Cancel-all before each reveal**: `on_reveal` issues `cancel_all` BEFORE
  calling `posterior.update(value)` (single REST round-trip vs two
  individual cancels, sweeps any untracked stragglers). Other bots see the
  same reveal we do; the window between posterior update and step()-driven
  reprice is exactly when a faster taker can lift our order at the OLD
  fair. Cost: queue priority on each reveal. Gain: immunity to fair-shift
  pickoff (matters most on the first reveal where fair can shift ~10+
  ticks as the K=1 posterior collapses out of the prior).
- **`tight_penny_sigma_max = 4.0`**: penny logic disabled when uncertainty
  is high. Pennying inside the market preserves only `tight_floor_edge=1.0`
  of margin; with sigma=25 the next reveal can shift fair by ~10+ ticks,
  far past that floor. Penny only fires once posterior has tightened.
- **`tight_penny_min_reveals = 0`** (was 1): compete for top-of-book
  even pre-reveal. Older-log analysis: inside-maker fills pay ~42%
  more per fill than deeper fills, and other traders are making 36-103
  pre-reveal trades / round while we sit deep (only 9 pre-reveal fills
  total across 6 rounds). The `biased` gate (set when |fair −
  market_mid| ≥ 5) still blocks penny in tail-prior rounds where our
  anchor is wrong, and the pre-reveal pull-side defense additionally
  suppresses the exposed side at |diff| ≥ 10. So penny pre-reveal only
  fires when our prior is roughly in line with market consensus —
  exactly when stepping inside is safe.
- **`tight_penny_sigma_max = 10.0`** (rebalanced from 4.0): the old 4.0
  threshold gated penny off at K=1 sigma (~8-9). Re-enabling penny at
  K=1 unlocks fills in the highest-edge window of the round. The
  `tight_floor_edge=1.0` floor still protects per-fill economics; the
  sigma-scaled position cap still bounds cumulative exposure.
- **Variance-scaled quote size** (`full_qty_sigma_max=3.0`,
  `mid_round_quote_qty=1`): now qty=1 at K=1-2 (was 2). Combined with
  `quote_qty=2` for low-sigma, the size profile is: K=0 → 1 (disabled),
  K=1-2 → 1, K>=3 with tight posterior → 2. Each fill is small enough
  that a wrong-direction sigma shift can't blow up the round; capture
  comes from VOLUME (top-of-book + fast snipes), not per-fill size.
- **"Join inside" fallback in `_apply_penny`**: when stepping inside
  market best would violate `tight_floor_edge` but the market best
  ITSELF is at-or-above our safe-quote floor, we JOIN the queue at the
  inside instead of staying at the deeper default. Log analysis showed
  recent sessions at top-of-ask only 7% of book snapshots — purely
  because penny.step was rejected and we sat 1-2 ticks behind. Joining
  trades queue-priority-at-deep-price for queue-tail-at-inside-price:
  fewer-priority fills, but at the better price when sweeps walk
  through.
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
- **Per-snipe size cap** (`snipe_max_qty_per_level=10`, new): every snipe
  takes ≤ 10 lots regardless of sigma. Replaces the old
  `snipe_full_size_sigma=1.5` branch that used `position_limit=100` at low
  sigma — that branch could slam our entire trader headroom into ONE bad
  fair calc. Log analysis (3 sessions, 50% miss rate on bad trades) showed
  bots repost the same mispriced quote multiple times (median rest >2s),
  so coming back to a level is cheap. Each round we can fire up to 100
  snipes (= 10 lots × 10 levels) and bots don't run out — they have no
  position limit. The sigma-scaled `max_pos_for_sigma` still binds total
  accumulation.
- **`max_snipes_per_round=120`** (was 60): the disagreement gate plus
  per-level qty cap together prevent fee-runaway, so the count cap can be
  relaxed to let pattern-exploit volume through. Combined safety:
  120 snipes × 10 qty = 1200 lots max, but `position_limit=100` binds
  first — count cap is just a guard against runaway loops.
- **Fair/sigma cache** (new): `fair_and_sigma` now caches its result by
  generation; invalidated on `posterior.update` (in `on_reveal`) and
  `posterior.reset` (in `on_phase_change`). Underlying `predict_settle`
  iterates ~200-500 (a,w) posterior cells doing law-of-total-variance
  math; cache hit replaces that with a single tuple lookup. Removes
  4-6× redundant compute per WS event in the hot path
  (desired_quotes → maybe_snipe → _try_direct_snipe → _current_quote_qty
  → ...). Matters in a FIFO market where each spare millisecond is a
  better queue position.

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

### Live-strategy simulation (`sim_strategy.py`)

Unlike `simulation.py` / `simulation_mc.py` (which use parallel
implementations), `sim_strategy.py` exercises the **actual** `Strategy`
class through a mock `GameClient`. The matcher implements FIFO + SMP, the
mock client returns the same shapes as the live SDK, and `strategy_mod.time`
is monkey-patched to a deterministic clock so book-cache TTLs and warmup
gates behave exactly as in production.

```
.venv/bin/python day1/sim_strategy.py --quick           # 10 rounds smoke
.venv/bin/python day1/sim_strategy.py --rounds 50 --seed 1
```

Counterparties (`NoiseMM`, `TightMM`, `ValueSniper`) have *oracle* knowledge
of the round's true (a, w) — they are an upper-bound stress test, the real
field is partially uninformed. Logs are JSONL at
`day1/logs/sim_log_<ts>.jsonl`, same schema as the live combined log.

**Latest tuning gains** (50 rounds × 2 seeds, after self-cross fix,
asymmetric inventory dampener, hard kill, snipe adverse-edge, and
disabling pre-reveal MM):

| Seed | Mean PnL | Median | Win % | Worst | VaR_5 |
|------|---------:|-------:|------:|------:|------:|
| baseline / 1 | +71 | +128 | 72% | -576 | -332 |
| current / 1 | **+152** | **+229** | 74% | -754 | -258 |
| baseline / 7 | +38 | +87 | 74% | -868 | -399 |
| current / 7 | **+189** | **+244** | **88%** | **-274** | **-78** |

### Directional snipe disagreement gate (2026-05-19)

`combined_log_20260518_222323.jsonl` showed the round won +$239 but **210 fat
bids (821 lots) at avg +7.9 ticks above settle went un-sniped** because the
symmetric `snipe_max_disagreement = 10` gate blocked snipes in BOTH
directions when our prior fair (~45) diverged from market_mid (~65-75). The
market was wrong about truth (settle was 53), not us.

Replaced the symmetric block with an asymmetric, conviction-aligned one:

- `fair < market_mid - snipe_max_disagreement`: we trust the lower fair, so
  BUY snipes are the wrong-direction bet (would be paying market-mid
  prices) — block BUYS, allow SELLS into fat bids.
- `fair > market_mid + snipe_max_disagreement`: symmetric — allow BUYS,
  block SELLS.
- New knob: `snipe_disagreement_directional = True` (set False to restore
  the symmetric block as a quick rollback).

Sim comparison (50 rounds × 3 seeds, gate off vs on, same code state):

| Seed | Mode | Mean | Median | Win% | Worst | VaR_5 | Sum |
|------|------|-----:|-------:|-----:|------:|------:|----:|
| 0 | off | +99.6 | +139.2 | 70% | -602 | -278 | +4980 |
| 0 | **on** | **+108.2** | **+145.0** | **72%** | **-598** | **-266** | **+5409** |
| 1 | off | +74.0 | +117.8 | 68% | -763 | -335 | +3698 |
| 1 | **on** | **+101.2** | **+132.8** | **72%** | -854 | **-287** | **+5059** |
| 7 | off | +115.8 | +158.0 | 72% | -446 | -189 | +5791 |
| 7 | **on** | **+144.3** | **+161.5** | **86%** | -521 | **-173** | **+7217** |

Mean, median, win%, VaR_5, and total all improve across every seed; worst-
case on seeds 1/7 widens by 75-90 ticks — acceptable given the systematic
gains and that the inventory dampener + hard kill still bound exposure.

## Latency / FIFO hardening (REST hot-path audit)

The strategy ran several REST calls on the hot path that hurt FIFO standing
against bot/human snipers. Each round-trip we save is one fewer ms a
competitor's IOC reaches the server before ours. Changes:

1. **`sdk/client.py` now uses `requests.Session()`** instead of bare
   `requests.{get,post,patch,delete}` calls. Stock `requests` opens a fresh
   TCP connection per request (no keep-alive); a Session reuses the same
   connection, eliminating the TCP/handshake cost per call (~1-3ms on LAN).
   At ~20-50 REST/sec on the hot path this is the single biggest FIFO win.
   The API surface is unchanged.

2. **`step()` no longer calls `game_state()` REST.** It now reads `self.phase`
   directly. `on_phase_change` already keeps `self.phase` in sync from WS, so
   the REST was pure overhead — paid on every fill, every reveal, every
   status command.

3. **`on_fill_event` no longer triggers `positions()` REST when the fill
   matches one of our resting orders.** Position is updated locally from the
   fill msg's side+qty. `step(reconcile=True)` is still used as a safety net
   when the fill order_id doesn't match any local resting (i.e., IOC fills,
   which already update position synchronously from the IOC response, or
   races where `self.resting` hasn't caught up yet).

Considered but not applied:

- **Releasing `self.lock` around REST in `maybe_snipe` / `_try_direct_snipe`.**
  Would let WS quote_add events land while a REST is in flight, but creates
  position-cap-overshoot risk (we could fire a buy IOC for X lots while
  another quote_add concurrently lifts the same level for Y, and both fills
  arrive together). Session keep-alive already cut REST RTT to ~1-3ms; the
  marginal benefit didn't justify the reentrance complexity. Revisit if
  live logs show WS event queue backup symptoms (e.g., desired_quotes
  reacting to old market_mid).

- **In-memory book mirror from WS events.** `maybe_snipe` still calls
  `c.book()` per throttled fire; a mirror updated by quote_add /
  quote_cancel / trade WS events would eliminate it entirely. Bigger
  change (~80 LOC), defer to a session where we can A/B vs the live
  `c.book()` path.

## Rate-limit suffocation fix (2026-05-19, evening)

Analysis of the first 4 live sessions on the new probe (combined_log_20260519_*)
turned up a single dominant bottleneck.

**Findings (PnL over 5 settled rounds = +$2,069):**

| Metric | Value |
|---|---|
| Rejects, all `rate_limited` | 3,246 across sessions |
| Reject breakdown | **91.7% on new orders**, 4.1% modify, 4.1% cancel |
| Server cap (from `game_state.rate_limit.max_per_second`) | 20 req/s, 3-sec cool-off on overshoot |
| Median req-rate in the 1s before each lockout | 20.0 (we hammer right at the cap) |
| Modify churn: pct firing <200 ms after prior modify on same order | 87.9% |
| Snipe (taker) profit | 78/79 lots profitable, mean edge +9.2, +$688 |
| Maker lots vs snipe lots | 1,219 vs 79 (snipes are 6.1% of volume, ~100% of edge) |
| Missed-snipe opportunities (edge ≥10, no follow-up fill) | ~2,023 orders, ~10,732 lots |
| Adverse counterparties | VALKE (−$267), VALKC (−$265), VARU (−$150) |
| Favorable counterparties | VALKG (+$455), CHAR (+$393), VALKB (+$174) |

All six symptoms collapse to the same root cause: we burn the 20 req/s budget
on redundant maker requotes (87.9% of modifies fire within 200 ms of the
previous one), so when a mispriced quote_add lands we're locked out and miss
it. Fixes in this commit:

1. **`sdk/client.py`: client-side `_TokenBucket(rate=18.0, capacity=20.0)`.**
   Every REST call now `acquire()`s one token before hitting the wire. Sits
   slightly under the server cap (18 vs 20) so we never trip the 3-second
   server lockout — instead we self-throttle in 55-ms slices that are
   recoverable. `tokens_available()` is exposed for caller-side prioritization.

2. **`day1/strategy.py` `_reprice` per-order debounce.**
   `min_modify_interval_sec = 0.25`. If we tried to modify the same
   `order_id` within 250 ms of the previous successful modify, skip the
   call. Reveal-driven repricing goes through `cancel_all` + fresh `_post`,
   so it bypasses this debounce (the fair-shift path must not be throttled).

3. **`_reprice` token-budget guard: skip when `tokens_available() < 3`.**
   Reserves the last few tokens for an IOC snipe in case one shows up
   immediately after. Snipes are 98.7% profitable in the analyzed logs;
   maker requotes are not.

Expected impact (back-of-envelope from the log numbers):
- Modify traffic falls ~6× (median inter-modify gap was 62 ms vs 250 ms gate).
- Frees ~12-15 tokens/sec for new orders + snipes.
- If even 10% of the 2,023 missed-snipe opportunities convert at the median
  edge, that's ~+$1k/session on top of the current ~+$400/round baseline.

Outstanding (not in this commit): counterparty-aware quoting (widen vs
VALKE/VALKC, tighten vs VALKG/CHAR). Requires online counterparty
classification — defer until we have more fill data.

## FIFO-priority retention fix (2026-05-19, late evening)

Two fresh sessions (`combined_log_20260519_124658.jsonl`,
`combined_log_20260519_125314.jsonl`) showed `kept_priority=False` on
**99.6-99.8% of all modifies** (1853 modifies across the two sessions).
Of those, **71.6-79.6% had identical `(price, qty)` to the prior known
state** — yet they still made it past the existing guard
`if rest["price"] == want_px and rest["qty"] == qty: return`.

**Root cause:** the guard compares `rest["qty"]` (which we set to
`o["remaining"]` after every post/modify) to `_current_quote_qty(side)`
(the target post-size). After any partial fill `remaining < target`, so
the guard never fires; we issue a "refill" modify each tick that resets
qty back to target. Every refill loses queue priority. Sample trace
(oid 313789, 5-lot bid @ 41, partial fill -1):

```
fill         px=41 qty=1
modify_ack   px=41 qty=5 rem=4 kept=False    # refill: rest_qty (4) < target (5)
modify_ack   px=41 qty=5 rem=4 kept=False    # 13+ more identical refills
modify_ack   px=41 qty=5 rem=4 kept=False
```

**Fix:** widen the early-return in `_reprice` from `== qty` to `<= qty`:

```python
if rest["price"] == want_px and rest["qty"] <= qty:
    return
```

At the same price, only allow modifies that *increase* priority retention
— i.e. pure qty-DOWN (rest > target), which the docstring guarantees
keeps the FIFO spot. Same-price refills are now skipped: the residual
fills at preserved priority, and we only post a fresh quote when it
fully fills. Price changes still flow through unchanged.

Expected lift: 700-800 same-price modifies/session → ~0. Holding
priority through partial fills means more fills per posted quote and
fewer requote round-trips. Conservative est: +$36-75/round on top of
the current +$1,053/round baseline; will be re-measured after fresh
logs.

## Probe / live-game logging fixes (2026-05-19)

The probe's `on_message` catch-all was bucketing everything that the SDK
doesn't typed-dispatch as `kind=raw_unknown_msg`. Counting `kind` values in
`combined_log_20260518_222323.jsonl` (a representative live session):

| kind                 | count   |
|----------------------|---------|
| **raw_unknown_msg**  | **13,233** |
| trade                | 527     |
| book_periodic        | 283     |
| modify_ack           | 63      |
| fill                 | 49      |
| reveal               | 10      |
| book_after_reveal    | 10      |
| ...                  | ...     |

Breaking down the `raw_unknown_msg` bucket by inner `type`:

| type           | count |
|----------------|-------|
| quote_add      | 6,462 |
| quote_cancel   | 6,235 |
| quote_fill     | 527   |
| quote_modify   | 8     |
| hello          | 1     |

So the exchange streams five message types that don't appear in the handout
notebook (`day1_instructions.pdf` warns: *"not all exchange messages provided
by the exchange are given in the notebook"*) — `quote_*` are per-order book
deltas and `hello` is a one-shot greeting at WS connect. None had a typed
SDK handler, so all 13k events landed in `raw_unknown_msg` with no usable
`kind` for jq analysis.

Fixes in `probe.py`:

1. **`on_message`** now logs each documented-but-untyped event under its
   actual type as the `kind` (so `kind=quote_add`, etc.) and only prints to
   the terminal for *truly* novel types. `kind=raw_unknown_msg` is reserved
   for genuinely unknown events — that's what should trigger investigation.
   The five known untyped events are listed inline in code so future
   discoveries get added there.

2. **`on_reveal` book snapshot moved to a daemon thread.** The probe's
   `book_after_reveal` REST fetch was synchronous, running BEFORE
   `strat.on_reveal` in `run_combined`'s chained handler. Strategy's
   `on_reveal` starts with `cancel_all()` — the longer that's delayed, the
   wider the adverse-pickoff window where a faster taker can lift our
   stale-priced quotes at the OLD fair. The snapshot still lands in the log
   ~10ms later, which is fine for offline analysis but no longer blocks the
   strategy.

`run_combined.py` itself is already correctly wired: one shared WS
connection (the exchange allows only one private WS per API key), chained
handlers so every event reaches both probe (log) and strategy (trade),
default log path `day1/logs/combined_log_<YYYYMMDD_HHMMSS>.jsonl`, and the
probe's `_safety_check` is no-op'd because strategy owns position
management.

Analyze a fresh session:

```
jq -c 'select(.kind=="quote_add")'    day1/logs/combined_log_*.jsonl | head
jq -c 'select(.kind=="quote_fill")'   day1/logs/combined_log_*.jsonl | head
jq -c 'select(.kind=="raw_unknown_msg")' day1/logs/combined_log_*.jsonl
```

The third query should be empty in a healthy log; non-empty means a new
message type appeared and the documented-untyped set in `probe.on_message`
should be updated.

Open question (not changed yet): `strategy.on_quote_event` reacts to
`quote_add`/`quote_cancel` only. `quote_modify` (only 8 of them in the
sample log, but each represents a price/qty change of a resting order)
could also indicate a sniping opportunity if the new price is mispriced.
Worth measuring before adding.

## Known limitations / open questions

- **No counterparty names on the public tape.** We can only fingerprint
  counterparties we've personally filled against. To build a fuller picture,
  the bot would need to trade more (which costs fees) — there's no free way
  to identify the other participants.
- **Effect of the latest changes isn't measured yet.** Need a few rounds with
  the new `strategy.py` to confirm reduced fees and improved PnL. Re-run
  `analyze.py` on a fresh log after a session.
- **The `on_quote_event` snipe path may be chatty.** Every quote_add triggers
  a check (throttled to 10/sec via `quote_event_throttle_sec=0.1`). The
  handler now also reprices passive quotes via `_apply_target_quotes` (penny
  path), but `_reprice` is idempotent so unchanged prices don't churn.
- **Prior parameters are hardcoded.** The handout warns parameters may change.
  All four `*_LOGN_MU/SIGMA` constants live at the top of `strategy.py`;
  rebuild the `Posterior` if they change.
- **Simulation does NOT exercise the live `Strategy` class.** `OurBot` and
  `ImprovedBot` in `simulation.py` are parallel implementations, so
  `simulation_mc.py` cannot validate changes to `strategy.py` directly. To
  test new live-strategy logic, either port it to `ImprovedBot` or run a
  short live session and inspect the log.

## v7 cross-symbol VWAP contamination + maker-cross (2026-05-19)

Audit of 6 live sessions (`logs/strat7_sean_bot{,2..6}.jsonl`, 7 rounds total):

| session | settle | reveals | PnL | buys | sells | maker | taker |
|---|---:|---|---:|---:|---:|---:|---:|
| sean_bot   | 22 | [5,5,5,2,2,3] | **+234** | 6 | 40 | 7 | 39 |
| sean_bot2  | 53 | [7,9,10,9,10,8] | **−427.5** | 0 | 35 | 0 | 35 |
| sean_bot3  | 11 | [2,1,3,1,3,1] | **+161.5** | 65 | 76 | 34 | 107 |
| sean_bot4 r1 | 22 | [3,5,5,3,4,2] | **+167.5** | 24 | 69 | 23 | 70 |
| sean_bot4 r2 | 31 | [6,6,5,5,5,4] | **+66.5** | 0 | 45 | 1 | 44 |
| sean_bot5  | 33 | [6,6,5,6,5,5] | **+40** | 0 | 44 | 3 | 41 |
| sean_bot6  | 34 | [5,5,6,6,6,6] | **−92.5** | 2 | 47 | 6 | 43 |
| **total** | | | **+149.5** | | | | |

Session 2 alone took −427.5 (settle=53, a right-tail round). Without it the
strategy is +577 across 6 rounds.

**Root cause — cross-symbol VWAP contamination.** `Strategy.on_trade`
ingested *every* public `trade` event into the v6/v7 VWAP buffer regardless
of symbol. Symbol B settles in the 1-8 range vs symbol A's ~30, so mixing
the two systematically biases VWAP downward.

Trace from sean_bot2 at t+2.61s (right before our first K=0 fill):
- Symbol-A-only trades: 40×2, 39×1, 40×1 → true VWAP = **39.75**
- All-symbol buffer fed to strategy: A trades + B@10, B@4 → VWAP = **22.6**
- Side guard's `ask_floor = ceil(VWAP)+1` becomes 24 instead of 41
- Our maker SELL post @ 38 (= fair_eff + edge, with fair pulled toward 22.6)
  crossed the existing market bid @ 40 → filled as **TAKER @ 40** three
  times in 0.1s. Reveal=7 followed at K=1; the still-corrupt VWAP let
  the snipe path IOC into bids at 38/37/37 (10 lots vs SEAN/VALKL/VALKD).
  All 35 fills in session 2 were SELLS, all TAKER, at avg 37-43 vs settle 53.

Sessions 5/6 show the same short-bias pattern, masked because settles
happened to be close to the biased fair.

**Fixes applied to `strategy7.py`:**

1. **Symbol filter in `on_trade`**: early-return if `msg["symbol"] != self.symbol`.
   Defensive — survives any runner-side dispatch mistake. One-line change at
   the top of the method.

2. **`min_trade_vol_pre_reveal: 8 → 3`** and **`vwap_clamp_min_volume: 8 → 3`**.
   With the clean (A-only) tape, the K=0 v6 blend and the K∈{1,2,3} VWAP
   clamp / side guard need only a handful of A-lots to engage. The old
   floor of 8 was conservative against a noisy mixed buffer — no longer
   needed and was preventing the defenses from firing in the first 1-2s
   of a new round.

3. **No-cross guard in `desired_quotes`** (final block before `return`):
   if the cached inside book shows `ask_px <= best_bid` or `bid_px >= best_ask`,
   drop the post. Pure invariant — a maker order should never be marketable.
   Backstops the VWAP-side-guard soft defense for the cases where VWAP
   itself lags a fast-moving consensus.

Smoke test (`unittest.mock` client): B trades ignored, A-only VWAP correct,
side guard returns valid VWAP at K=1 with only 4 lots, no-cross guard drops
both `ask_px <= best_bid` and `bid_px >= best_ask`. To validate live:
run `python day1/run_combined7.py` and confirm K=0 fills are no longer
TAKER fills crossing the inside book.

Open: the 6-session evidence suggests our prior `E[settle]≈27.3` understates
the right tail (observed mean 29.4, but P(settle≥50)≈14% empirically vs
near-zero under the LogN priors). Heavier-tailed prior is a candidate
next change — but contaminated VWAP was the dominant bug; re-measure after
the symbol filter before tuning the prior.

## Strategy v8 — SEAN-window passive + reveal-math snipe guard (2026-05-19)

**Motivation:** strat7.5 ran the `sean_bot{1,2,3}` sessions. SEAN consistently
wins by posting `qty=5` maker quotes ~1s before each scheduled reveal and
never aggressing. v7 still leaked $384 on one IOC BUY at 102 against true
fair 86 — a reveal-math projection (sample-mean over revealed values)
disagreed with the posterior at the moment of that trade and would have
blocked it.

Position limit on symbol A is **50** (was 100). Symbol B exists with
multiplier 5x and settles to next draw, but v8 trades **A only** for now.

**Files:** `day1/strategy8.py` (subclass of Strategy7, ~460 lines),
`day1/run_combined8.py` (runner). All round params (`duration`,
`reveal_interval`, `position_limit`, `tick_size`, `multiplier`) come from
`game_state` — nothing hardcoded.

**v8 knob table (deltas vs v7):**

| knob                          | v7    | v8    | rationale                                                |
|-------------------------------|-------|-------|----------------------------------------------------------|
| `snipe_min_edge`              | 1.0   | 2.0   | the 102 vs 86 leak crossed the 1.0 gate; double it       |
| `snipe_max_disagreement`      | 6.0   | 3.0   | tighten so a noisy market-mid can veto bad snipes        |
| `max_snipes_per_round`        | 80    | 40    | post-VWAP-fix v7 already snipes less; cap further        |
| `snipe_position_buffer`       | 4     | 8     | leave more headroom against the 50 cap                   |
| `sean_window_sec`             | —     | 1.5   | post upsized maker quotes 1.5s before each reveal        |
| `sean_pre_reveal_cancel_sec`  | —     | 0.3   | cancel ALL orders 0.3s before the reveal (avoid late MM) |
| `sean_window_qty`             | —     | 5     | matches SEAN's observed size                             |
| `reveal_math_min_k`           | —     | 2     | need ≥2 reveals before projection is informative         |
| `reveal_math_guard_ticks`     | —     | 4.0   | block snipe when posterior fair drifts >4 ticks from RM  |

**Background scheduler:** `_sean_scheduler_loop` (daemon thread) ticks every
0.1s, keyed off `_last_reveal_t + self.reveal_interval`. At
`reveal_t - 0.3s` it cancels all live quotes; at `reveal_t - 1.5s` it
triggers `step()` which re-posts at `sean_window_qty` *if* the consensus
gate agrees (`|fair - market_signal| < market_anchor_disagreement_min`).

**Reveal-math guard:** `_reveal_math_fair()` projects
`running_sum + (N-K) * mean(reveals)`. `_reveal_math_blocks_buy(fair_eff)`
returns True when `fair_eff > rm_fair + guard_ticks` (we'd be paying more
than the sample-mean projection); symmetric for sell. Applied inside
`maybe_snipe` and `_try_direct_snipe` to veto direction-of-bad-edge IOCs.

**Smoke test (`.venv/bin/python`, mocked client):** all v8 knobs visible,
`_reveal_math_fair`/`_in_sean_window`/`_next_reveal_at` compute correctly,
scheduler thread starts/stops cleanly, guard blocks the 90→492 sell case
and the 600→492 buy case (the two leak directions). Run live with:
`python day1/run_combined8.py` (writes `combined_log_v8_<TS>.jsonl`).

## Strategy v9 — reveal-zone IOC block + K=1 reveal-math + drift cancel (2026-05-19)

**Motivation:** audit of `combined_log_v8_20260519_{204107,204402}.jsonl`
showed s1 PnL +63, s2 PnL **-292**. The s2 loss is one event: at
t+16.9 to +17.5s (≈2s AFTER reveal_1=1, K=1, posterior~12, public mid=15)
we **IOC-bought 21 lots from SEAN at 19→21** for -290 PnL. Counterparty
blacklisting isn't implementable from the public feed (`quote_add` does
NOT carry trader id), so the fix has to be price/time-based.

**Files:** `day1/strategy9.py` (subclass of Strategy8, ~370 lines),
`day1/run_combined9.py` (runner). All round params still from
`game_state` — nothing hardcoded.

**v9 knob table (deltas vs v8):**

| knob                              | v8    | v9    | rationale                                                    |
|-----------------------------------|-------|-------|--------------------------------------------------------------|
| `reveal_math_min_k`               | 2     | **1** | the s2 leak fired at K=1 when v8 had no guard                |
| `reveal_math_k1_widening`         | —     | 2.0   | single-sample sample-mean noise — widen guard 4→8 at K=1     |
| `reveal_danger_zone_sec`          | —     | 2.0   | block IOCs in [last_reveal, +2s] AND [next_reveal-2s, …]    |
| `drift_cancel_ticks`              | —     | 3.0   | cancel-all + restep when fair_eff drifts >3 from quote anchor|
| `drift_cancel_min_interval`       | —     | 0.5s  | rate-limit drift-cancel to avoid flap                        |
| `inv_hard_kill_frac`              | 0.85  | **0.4** | s1/s2 drifted long to +13/+15 well below 0.85 threshold    |
| `opening_clamp_ticks`             | —     | 1     | K=0 only: clamp bid ≤ prior_E_settle+1, ask ≥ prior_E−1      |

**Key v9 helpers:**
- `_in_reveal_danger_zone()`: True in 2s windows around each reveal.
  Both `maybe_snipe` and `_try_direct_snipe` short-circuit when True.
- `_reveal_math_guard_width()`: 4 ticks at K≥2, 8 ticks at K=1.
- `_maybe_drift_cancel()`: hooked into `on_quote_event` and `on_trade`.
  Compares current `fair_eff` to `_last_quote_fair_eff` recorded in
  `desired_quotes()`. >3 tick drift triggers cancel_all + step.
- Scheduler now emits `[SCHED #N]` tags on every action and uses
  `cancel_all()` unconditionally even if local resting state says
  empty. `on_reveal` logs `actual_vs_predicted` lag for forensics.

**Smoke test (`.venv/bin/python`, mocked client):**
- All knobs visible
- Danger zone correctly fires 1s after reveal AND 1.5s before next reveal
- K=1 reveal-math projects rm=6 for reveals=[1] with N=6
- `_reveal_math_blocks_buy(21.0)` = **True** (the exact s2 leak case
  would have been blocked)
- Scheduler thread starts/stops cleanly

Run live: `python day1/run_combined9.py` (writes
`combined_log_v9_<TS>.jsonl`).

## Strategy v10 — precompute scenarios + fast-path reveal (2026-05-19 late)

**Motivation:** user microstructure directive — pre-calculate, for every
plausible reveal value, what we'd lift/hit and what bid/ask to quote.
On reveal, look up the answer — no Bayesian update in the hot path.
Between reveals, penny BBO when there's edge; pull quotes when there
isn't. The goal is to be *fastest* around reveals.

**Files:** `day1/strategy10.py` (~510 lines, subclass of Strategy9),
`day1/run_combined10.py` (runner).

**Architecture:**
- `PrecomputedScenario` dataclass: snapshot of `(posterior_after, fair,
  sigma, bid_px, ask_px, lift_to_px, hit_to_px)` for one hypothesized
  reveal value v.
- `_precompute_worker` daemon thread: woken via Event. For each value
  v with marginal `P(X=v) >= precompute_min_prob` (0.5%), computes the
  scenario. Caps at `precompute_max_values=40`.
- `on_reveal` fast-path: dict lookup → swap posterior → post precomputed
  quotes → walk top of book for IOCs inside precomputed bounds. No
  Bayesian math in hot path. Falls back to v7 path on cache miss.
- Scheduler tick (inherited from v9) also fires a precompute prefetch
  `precompute_lead_sec=4` seconds before each predicted reveal.
- Between-reveal MM: `desired_quotes` pulls both sides when expected
  edge < `no_edge_pull_ticks=0.5`. Adverse-fill skew widens the side
  that just got a maker fill (linear decay over 8s).
- v9 danger zone DISABLED — precompute eliminates the slow-recompute
  problem it was guarding against. Aggressive post-reveal sweeps are
  now safe because fair is instantly accurate.

**v10 knob table (new):**

| knob                          | value | rationale                                          |
|-------------------------------|-------|----------------------------------------------------|
| `precompute_min_prob`         | 0.005 | covers ~99.5% of mass, O(20-40) scenarios          |
| `precompute_lead_sec`         | 4.0   | prefetch 4s before reveal — breathing room         |
| `precompute_max_values`       | 40    | hard cap so high-w branches don't explode work     |
| `no_edge_pull_ticks`          | 0.5   | half-tick fee + adverse variance dominates EV      |
| `penny_aggressive`            | True  | step inside BBO whenever per-fill edge stays >=1   |
| `tight_penny_min_reveals`     | 1     | (was 2) — precomputed fair is trustworthy from K=1 |
| `adverse_fill_skew_ticks`     | 1.5   | widen hit side after maker fill                    |
| `adverse_fill_decay_sec`      | 8.0   | linear decay window                                |
| `reveal_danger_zone_enabled`  | False | (v9 default True) — fast path replaces the guard   |

**Smoke test (`.venv/bin/python`, mocked client):**
- Initial precompute: 15 scenarios for K=0 in **1.9ms**
- Top-mass scenarios match prior intuition: v=3 (P=16.8%) → fair=18.2,
  bid=16, ask=20, lift≤15, hit≥21
- Fast-path `on_reveal(0)` end-to-end: **5.4ms** (most is book fetch +
  cancel_all)
- Worker + scheduler threads start/stop cleanly

Run: `python day1/run_combined10.py` (writes `combined_log_v10_<TS>.jsonl`).
The 's' command shows precompute count + cycle + last duration; 'p'
dumps the full precompute table.

## Strategy v10 — low-latency reveal-race hardening (2026-05-19, night)

A series of log-driven infrastructure fixes targeting two concrete pathologies
visible in `combined_log_v10_2026051922{2449,2654,4229,4419,4557}.jsonl`:

1. **We win 0/20 reveal races.** Competitors' first IOC lands at T+46-78ms;
   ours at p50 T+94-117ms. We're 30-40ms behind.
2. **We are the hit-bait.** 92% of reveals saw us *get hit as maker* before
   our IOC fired. Our pre-reveal MM quotes at T-62ms became free liquidity
   for competitors racing the reveal.

### Slow / fast / precompute loop architecture (already in v10, confirmed working)

The hot path is now a pure dict lookup → parallel IOC fire:

- **Slow housekeeping loop** (2s tick): periodic position reconcile + sanity
  checks. No timing-critical work. `_slow_housekeeping_loop` /
  `_slow_housekeeping_tick`.
- **Fast scheduler loop** (20ms tick): pre-reveal cancel slot, JIT theo
  refresh, book pre-warm, precompute prefetch trigger. `_fast_scheduler_loop` /
  `_fast_scheduler_tick`.
- **Precompute worker** (event-driven daemon): computes
  `PrecomputedScenario` (fair, sigma, bid_px, ask_px, lift_to_px, hit_to_px,
  posterior_snapshot) for every plausible reveal value v.
- **`on_reveal` fast path** (`strategy10.py:1584-1689`):
  1. **Dict lookup** — `scenario = self._precomputed.get(int_value)`
  2. **Local posterior swap** (`with self.lock`, no I/O) — ~50µs
  3. **Parallel IOC fire** — `_fast_post_reveal_sweep` builds the full
     buy/sell IOC plan upfront (capping cumulative qty by full headroom so
     concurrent fires can't exceed position limit), submits all to
     `self._ioc_executor` (6-worker `ThreadPoolExecutor`), then collects
     results. 3 sequential IOCs at ~80ms each → ~80ms wall.
  4. **Post new MM quotes** (after race is won)

Smoke-tested: 6 parallel IOCs across 6 worker threads complete in 82ms wall
vs ~480ms sequential — clean 6× speedup. `_ioc_executor.shutdown(wait=False)`
added to `flatten()` for clean teardown.

### Quote blackout (round 1)

**Knob:** `pre_reveal_quote_blackout_sec` — refuse all new MM quote posts /
modifies inside `[next_reveal − blackout_sec, next_reveal]`. Cancels of
existing quotes still flow (we want to be flat at reveal).

**Gates added:**
- `_in_quote_blackout()` helper (`strategy10.py:807`).
- `_tokens_reserved_for_reveal()` helper (`strategy10.py:819`) — True when
  in blackout AND `tokens_available() < pre_reveal_token_reserve` (default
  7 of 20). Available for future use; not currently wired.
- `_apply_target_quotes` first checks blackout — if active, cancels existing
  side(s) and returns without posting (`strategy10.py:1136-1144`).
- `step()` first checks blackout — if active, returns immediately, saving
  the full reconcile + desired_quotes + REST chain (`strategy10.py:1577-1582`).

**Initial values:** `pre_reveal_cancel_sec=0.6`, `pre_reveal_quote_blackout_sec=0.65`.
Result (3 new logs): hit-bait dropped 45% (10 → 5 lots/reveal), but **96% of
surviving hits landed in T-650ms..T-100ms** — the gap between cancel-issue
(T-600ms) and cancel-ack (~T-450ms). Cancel ack lag is 150-200ms, so quotes
were on the book during the blackout *because the cancel hadn't acked yet*.

### Quote blackout (round 2 — extend the cancel lead)

To eliminate the surviving 5 lots/reveal:

| knob                              | before | after | rationale                                                  |
|-----------------------------------|--------|-------|------------------------------------------------------------|
| `pre_reveal_cancel_sec`           | 0.6    | **0.9** | cancel-ack RTT 150-200ms; T-0.9 cancel acks by T-0.7      |
| `pre_reveal_quote_blackout_sec`   | 0.65   | **0.95** | stays slightly wider than cancel — gate opens first      |
| `book_prewarm_lead_sec`           | 1.0    | **0.7** | focus pre-warm on critical window, save tokens earlier    |
| `book_prewarm_max_age_sec`        | 0.2    | **0.1** | cache ≤100ms stale at T+0 — no synchronous REST fallback  |

With cancel at T-0.9s and ack lag ~200ms, quotes are off the book by T-0.7s.
Blackout from T-0.95s prevents any new posts. The 700ms window T-0.7..T+0 is
therefore clean. Cost: ~6% additional MM downtime per 5s cycle (acceptable).

### Other infrastructure shaves

- **Strategy-before-probe ordering in `run_combined10.py:113-122`** — probe's
  on_reveal does a JSON write + stdout print (~500µs). Originally fired
  before `strat.on_reveal`, pushing our IOC submit later in the reveal-race
  window. Now strategy first, probe second.
- **Book pre-warm hot path** (already in v10): fast scheduler refreshes the
  book cache from T-0.7s onward; `_fast_post_reveal_sweep` reads the cache
  if it's ≤100ms old, avoiding a 30-50ms synchronous REST in the IOC sweep.
- **Parallel IOC executor pre-allocated** at strategy init —
  `ThreadPoolExecutor(max_workers=6, thread_name_prefix="ioc-fire")`. No
  thread-spawn cost on the hot path.

### Measurement bottom line

The 30-40ms head-to-head IOC latency gap vs competitors is **not closable**
by these changes — it's dominated by the requests/urllib3 stack plus network
RTT. Closing it would require a low-level rewrite (raw sockets or async I/O).
What these changes DO fix is the *adverse-selection* leak: we stop bleeding
maker-side fills to competitors who arrive at the reveal moment, even though
we still don't beat them to the take.

### Knobs reference (v10 latency-hardening)

| knob                                | value | location                |
|-------------------------------------|-------|-------------------------|
| `pre_reveal_cancel_sec`             | 0.9   | `strategy10.py:391`     |
| `pre_reveal_quote_blackout_sec`     | 0.95  | `strategy10.py:408`     |
| `pre_reveal_token_reserve`          | 7.0   | `strategy10.py:415`     |
| `book_prewarm_lead_sec`             | 0.7   | `strategy10.py:453`     |
| `book_prewarm_max_age_sec`          | 0.1   | `strategy10.py:454`     |
| `ioc_parallel_workers`              | 6     | `strategy10.py:447`     |
| `_scheduler_tick_sec` (fast loop)   | 0.02  | `strategy10.py:465`     |
| `slow_housekeeping_interval_sec`    | 2.0   | `strategy10.py` (slow)  |
| `jit_theo_refresh_sec`              | 0.5   | `strategy10.py` (JIT)   |

### What was tried and didn't help

- **Pre-built `requests.PreparedRequest` payloads** — JSON serialization
  is ~5µs per call; pre-building saves <1ms total. Not the bottleneck.
- **Separate `requests.Session` for IOCs** — connection pool already
  parallelizes 3 calls. Server-side rate limit is per-API-key, so a 2nd
  session doesn't increase throughput.
- **Token-bucket reservation gating non-essential REST** — bucket is at
  capacity (20) entering blackout because no work happened in the prior
  950ms. IOC burst uses 3-6 tokens; plenty of headroom.

### What's outstanding

- Verify the blackout-round-2 fix on fresh logs (extend cancel + tighter
  pre-warm). Expected hit-bait drop from 5 lots/reveal toward 0.
- Recover MM volume lost to wider blackout (currently 19% downtime per
  5s cycle vs 13% before round 1, vs ~8% before any blackout).
- Investigate the rare reveal where straggler-count went to 0 (224557, 6
  reveals, 0 taker fills) — possibly stale precompute or sparse book.

## Strategy v17 — execution polish + orphan drain (2026-05-20)

Dual-symbol (A + B) live game showed three classes of issue in
`combined_log_v17_*.jsonl` and stdout: (1) zero reveal-IOC taker fills
despite precompute infrastructure, (2) `HTTPError: 400`/`403` repeating on
the same orders, and (3) orphan accumulation outrunning the reconcile
drain rate. Fix bundle landed in `strategy17.py` + `probe.py`.

### Reveal-IOC latency exploit (post path → IOC-first path)

Logs showed `precompute` table built fine but no `IOC` events on reveals.
`on_reveal(v)` was only POSTing fresh passive at the new fair — never
*lifting* laggard book orders. Added `_ioc_sweep_on_reveal(sym, fair)`
that runs BEFORE `_apply_quote`:

- Snaps `state[sym].book` and iterates levels.
- Asks below `fair - ioc_on_reveal_edge_ticks` get a buy IOC.
- Bids above `fair + ioc_on_reveal_edge_ticks` get a sell IOC.
- Bounded by `ioc_on_reveal_max_slice=10` per side, padded by
  `ioc_on_reveal_position_pad=5` so a sweep doesn't push past the
  position limit.
- All math runs at precompute time, hot path is dict lookup + book
  comparison only.

### Wide-quoter trap defense (`max_ioc_distance_ticks=50`)

Several adversaries quote at extreme prices (ask@5 with fair=50,
bid@5000 with fair=50) to bait IOC sweepers. Added a distance filter
in `_ioc` AND `_ioc_sweep_on_reveal`: refuse any IOC where
`abs(price - fair) > max_ioc_distance_ticks`. Defends arb path,
stale-sweep, and reveal-IOC alike. Logged once per block when
`log_every_modify=True`.

### Park-not-cancel during pre-reveal pull (modify-replace recovery)

`combined_log_v17_20260520_145320.jsonl`: 785 posts, 692 cancels,
**0 modifies**. Median order lifetime 2.8s ≈ one MM cycle. Root cause:
`_passive_quote` returned `(None, None)` during the 2.5s pre-reveal
pull window → `_apply_side` cancelled primary → next post-reveal MM
cycle posted FRESH (no primary to modify) → modify path was dead code.

Fix: `pre_reveal_use_park=True` returns `(fair - 30t, fair + 30t)`
during the pull window. Order parks at extreme price (no fills there
under normal markets), survives the pull window, gets *modified* back
into the market on the next MM cycle = 1 REST call instead of 2.

Knob: `pre_reveal_park_offset_ticks=30`. Inventory pad respected
(park bid pulled when `position >= +position_limit - pad`, ask pulled
symmetrically).

### Negative-price floor (B fair ~10 + park offset 30t = -20)

`HTTPError: 400` storm on B bid posts at negative prices. Two paths:
- `_passive_quote`: park branch floors `park_bid < tick → None`.
- `_apply_side`: defense-in-depth guard `if target_px < state[sym].tick: return`.

### 400 / 403 terminal-error categorization

Previously a 400 from `modify` or `cancel` was treated as transient —
strategy kept retrying the same oid each cycle → log spam + wasted
tokens. Three classes now treated as terminal (drop tracking, don't
retry):
- `404 / not found / no such / gone` (server doesn't have it)
- `400 / bad request` (terminal-state order, e.g. already cancelled/filled)
- `403 / forbidden` (account-state / cash-margin issue)

### Per-(sym,side) post backoff (`post_backoff_sec=5.0`)

When a fresh POST returns 400 or 403, arm a 5s backoff for that
`(sym, side)` pair so the next MM cycle doesn't burn a token on a
guaranteed-reject. Log ONCE per backoff window. Cleared on the next
successful post on that side.

### Nested-oid extraction on post

Server's POST response shape is inconsistent: sometimes
`{"order_id": N, ...}` (flat, like the dashboard), sometimes
`{"order": {"order_id": N}, "trades": [...]}` (nested, same shape as
modify). Old code only checked top-level — if the response was nested,
`oid` was None, the order was NEVER tracked locally, and reconcile saw
it as an orphan on the next tick. Fixed:

```python
res_obj = res or {}
nested = res_obj.get("order") or {}
oid = (res_obj.get("order_id") or res_obj.get("id")
       or nested.get("order_id") or nested.get("id"))
```

If still None, log under `log_every_modify=True` with the full `res_obj`
for inspection.

### Reconcile drain rate + bulk cancel_all fallback

`combined_log_v17_20260520_200715.jsonl` log showed orphan accumulation:
`(capped at 5/11) → 5/13 → 5/14 → 5/15`. Drain rate (1 orphan/sec at
cap=5/tick × 1/5s) was losing to creation rate (~1.3 orphans/sec).

Tuned:
- `reconcile_check_sec`: 5.0 → 3.0
- `reconcile_max_cancels_per_tick`: 5 → 8 (drain ≈ 2.7 orphans/sec)
- New `reconcile_bulk_cancel_threshold=20`: when orphans on a single
  symbol exceed threshold, fire `client.cancel_all(symbol=sym)` (1 REST
  call) and wipe local tracker — MM cycle re-quotes from clean slate.

### MM throttle (`mm_min_interval_sec=0.5`)

Fill bursts on B (which fills frequently) wake the MM thread many times
per second, each refresh costing ≥ 1 REST call. Added a 500ms minimum
between `_refresh_mm_quotes` invocations. Reduces wasted modifies when
the book hasn't actually moved.

### Reconcile-log collapse + tick_settlement / strikes silence

`probe.on_message` previously printed `RAW unknown type='tick_settlement'`
and `'strikes'` per event (multiple per reveal). Added both to
`documented_untyped` set — still logged to JSONL under actual type, no
terminal print.

`_reconcile_once` previously printed one line per cancelled orphan.
Collapsed into one summary line per tick:
`[v17 RECON:A] cancelled N orphan(s) (capped at K/total)`.

### Knobs reference (v17 execution polish)

| knob                                | value | location                |
|-------------------------------------|-------|-------------------------|
| `ioc_on_reveal_enabled`             | True  | `strategy17.py` config  |
| `ioc_on_reveal_edge_ticks`          | 2.0   | `strategy17.py` config  |
| `ioc_on_reveal_max_slice`           | 10    | `strategy17.py` config  |
| `ioc_on_reveal_position_pad`        | 5     | `strategy17.py` config  |
| `max_ioc_distance_ticks`            | 50.0  | `strategy17.py` config  |
| `pre_reveal_use_park`               | True  | `strategy17.py` config  |
| `pre_reveal_park_offset_ticks`      | 30    | `strategy17.py` config  |
| `post_backoff_sec`                  | 5.0   | `strategy17.py` config  |
| `mm_min_interval_sec`               | 0.5   | `strategy17.py` config  |
| `reconcile_check_sec`               | 3.0   | `strategy17.py` config  |
| `reconcile_max_cancels_per_tick`    | 8     | `strategy17.py` config  |
| `reconcile_bulk_cancel_threshold`   | 20    | `strategy17.py` config  |

### What's outstanding (v17)

- Validate by grepping next `combined_log_v17_*.jsonl` for
  `[v17 REVEAL-IOC:` lines (sweep firing) and shrinking RECON cap
  patterns. `BULK cancel_all fired` log indicates the circuit breaker
  tripped — investigate whether root cause is back.
- 403 backoff treats symptoms, not cause. Real driver was likely cash
  exhaustion from B `tick_settlement` losses depleting account margin —
  separate problem.
- `tick_settlement` events stream per reveal on B with PnL delta data —
  could feed into strategy cash awareness to avoid posting when margin
  is tight (currently silent).

## Strategy v18 — SEAN-style MM + private-feed exploit + BBO claim (2026-05-20)

`strategy18/` is a clean re-package of the v17 polish + several new
edges. Same `Posterior` core, same dual-symbol MM mirroring SEAN bot
widths (A: 8t / size 5 / flip 50, B: 4t / size 5 / flip 100), but the
hot path, reactive logic, and infra robustness are all rewritten. Run
with `python day1/strategy18/run_combined18.py`. Probe is a passive
wrapper (`probe18.py`) so the strategy owns all order placement.

### Precompute split: slow (Bayesian) + fast (live thresholds)

- **Slow loop** (`_precompute_loop`) — builds the per-scenario table
  for the NEXT reveal: Bayesian posterior update over every plausible
  X, fair/sigma per branch, base sweep thresholds (lift_to / hit_to).
  Heavy work, posterior-driven, runs on reveal + on `_precompute_request`
  signal.
- **Fast loop** (`_fast_precompute_loop`, `_build_live_thresholds`) —
  keeps the CURRENT-state live snipe thresholds fresh. Re-derives every
  `fast_precompute_tick_sec=0.05s` from cached fair_a/fair_b + current
  position + flatten-bias window. Cost ~50µs/tick (cached posterior
  generation = lookup, not recompute). Consumed by `_maybe_inter_sweep`
  and `_try_cross_arb` so they don't re-derive fair on every book event.
  See `_LiveThresholds` dataclass for the snapshot shape — includes
  both normal `lift_to/hit_to` and flatten-relaxed `lift_to_flat/hit_to_flat`.

### Public-feed 15ms lag exploit

Documented constraint of the day1 exchange: the public WS feed lags the
private feed by ~15ms. Private: reveals, our fills, our acks/rejects.
Public: book updates, others' quote_add/cancel, trade prints. On every
private event we have ~12-15ms of exclusive information.

`_try_cross_arb(source)` fires from two private hooks:

- **Fill hook** (`on_fill_event`): after the position update but before
  any defensive cancel. Walks BOTH books at FRESH fair, lifts/hits
  anything past `cross_arb_edge_ticks=2.0t`. Counterparties are still
  pricing off pre-fill book.
- **Reveal hook** (`on_reveal`): after the per-symbol post-reveal
  sweep. Same scan, again against the new posterior.

Walks `cross_arb_levels=5` deep, max `cross_arb_max_slice=8` per
level. Bypasses `inter_sweep_throttle` (this is a private-event hook,
not a book-flap reaction) but still honors `_in_lockout`,
`_can_send_now`, `max_ioc_distance_ticks`, and the inventory pad.

### Cross-symbol arb (math)

A settles at `running_sum + Σ remaining X`; B settles at the NEXT X.
So `implied_X_from_A = (mid_A - running_sum) / n_rem` should equal
`implied_X_from_B = mid_B` in equilibrium. Any divergence is arb.
`_try_cross_arb` walks both symbol books and lifts/hits anything past
the edge. Tagged `[v18 ARB(fill):...]` / `[v18 ARB(reveal):...]` in
logs.

### Endgame burst mode

After the LAST reveal, A's settlement = `running_sum_final` is KNOWN
exactly — posterior collapses, all uncertainty is gone. `_endgame_active`
flips → MM stops refreshing A (its resting quotes get cancelled — they'd
be picked off by other snipers), and `_endgame_loop` ticks every
`endgame_tick_sec=0.04s` (25Hz).

Each tick walks `endgame_max_levels=20` into both sides of A's book.
Asks below `running_sum - endgame_edge_ticks_a=0.5t` get a buy IOC;
bids above settlement + 0.5t get a sell IOC. Slice size up to
`endgame_max_slice=25`. Reserves `endgame_min_tokens=3.0` of REST bucket
so the burst doesn't trip the server's 20/s lockout. Tagged
`[v18 ENDGAME:A] ...`.

### Connection pre-warm

Idle TCP can be reset by NAT or server. `_prewarm_loop` (separate
thread) sends a cheap `my_orders` GET every `prewarm_interval_sec=25s`
to keep the Session pool warm. Also fires a burst pre-warm
`prewarm_before_reveal_sec=1.0s` BEFORE each reveal — confirms the pool
is live so the reveal IOC fan-out doesn't pay a TCP handshake.

### Flatten-bias before high-uncertainty events

Pre-first-reveal: the posterior IS the raw prior, sigma_A is huge. An
outlier reveal can blow PnL on any sized inventory. In the window
`pre_first_reveal_flatten_sec=8.0s` before the first reveal (or any
reveal where `sigma_a >= flatten_bias_sigma_a_min=12.0`), bias snipes
toward `|position|` reduction:

- `lift_to_flat` raised when SHORT (cover at slightly worse asks).
- `hit_to_flat` lowered when LONG (dump at slightly worse bids).
- Edge reduction `flatten_edge_reduction_ticks=1.5t`, floored at
  `flatten_min_edge_ticks=0.6t` so we still beat the 0.5/lot taker fee.
- Never forced — only fires if a position-reducing trade still has
  positive expected value at the relaxed edge.

`_in_flatten_bias_window` is consumed by both inter_sweep and
cross_arb. Tagged `[v18 INTER-FLAT:...]` / `[v18 ARB(...)-FLAT:...]`.

### BBO claim — dime defense + absent + outside (book-event driven)

The MM refresh loop has `mm_min_interval_sec=0.4s` throttle, so for up
to 0.4s after any book change we sit at our default width, potentially
outside the inside BBO. `_maybe_dime_defense` (called from
`on_book_event`) handles three cases per side, on every book event:

- **Dimed** — we have resting, competitor stepped strictly inside us.
- **Absent** — no resting on this side (post-fill gap before next MM
  refresh). Claim BBO immediately.
- **Outside** — we have resting but the inside has tightened past us
  without an explicit dime.

For each case, target = `best ± 1 tick`, gated by:
- `target ≤ fair - penny_min_edge_*` (preserves maker profit after fees).
- Don't cross opposite side.
- `(step distance) ≤ penny_max_step_ticks=5` (caps how far a one-side
  collapse can drag us).

Throttle: `dime_defense_throttle_sec=0.15s` per symbol, only burned
when we actually requote. Tagged `[v18 BBO:sym/side dimed]` or
`[v18 BBO:sym/side absent]`.

### 403 robustness — central handler, phase recheck, circuit breaker

v17 used per-`(sym, side)` 5s backoff for 403s, which meant after game
end we'd loop forever: 403 → 5s backoff → retry → 403. Spammed stdout
with `[v17 POST:A/ask@50] HTTPError: 403 Client Error: Forbidden ...
→ backoff 5.0s` lines.

v18 centralizes 403 handling through `_handle_forbidden(ctx, sym, side,
target_px, e)`. Three things:

1. **Extract response body** via `_http_status_and_body(e)` — old log
   only had the URL and status. Now: `[v18 FORBIDDEN:POST:A/ask@50]
   status=403 body='{"detail":"trading_closed"}'` so the actual server
   reason is visible.
2. **Authoritative phase re-check** — on each 403, fire `c.game_state()`
   (throttled `forbidden_phase_recheck_throttle_sec=0.5s`) to ask the
   server. If server says NOT running but our local `self.phase` lagged,
   set `_post_terminal_blocked=True` immediately. Self-corrects in ≤1
   REST RTT instead of looping on backoff.
3. **Circuit breaker** — if `forbidden_circuit_max=3` 403s in
   `forbidden_circuit_window_sec=10.0s`, hard-block sends even if phase
   looks fine. Covers persistent reject causes (auth issue, position-limit
   edge case, server policy).

All cleared on phase→running. Single `_post_terminal_blocked` flag
gates POST/MODIFY/IOC via `_can_post()`; cancels still work so flatten
cleanup runs across phase transitions.

### Where `_can_post()` is wired

Replaced raw `self.phase != "running"` checks in:

- `_apply_side` (POST + MODIFY fast paths).
- `_ioc` (taker snipes).
- `_refresh_mm_quotes`, `_park_tick`.
- `_maybe_inter_sweep`, `_try_cross_arb`, `_maybe_dime_defense`.
- `_emergency_flatten_if_needed` (gates the IOC; cancel_all still runs).

### Reconcile noise gate (`reconcile_print_min=3`)

Routine cleanup of 1-2 orphans per tick is expected (POST ack vs
local-dict update race). Log only when `total ≥ reconcile_print_min`
or when cancels get capped at `reconcile_max_cancels_per_tick=8`
(real leak indicator). Eliminates the
`[v18 RECON:A] cancelled 1 orphan(s)` spam.

### Knobs reference (v18 additions)

| knob                                  | value | location                |
|---------------------------------------|-------|-------------------------|
| `cross_arb_enabled`                   | True  | `strategy18.py` Config  |
| `cross_arb_edge_ticks`                | 2.0   | `strategy18.py` Config  |
| `cross_arb_levels`                    | 5     | `strategy18.py` Config  |
| `cross_arb_max_slice`                 | 8     | `strategy18.py` Config  |
| `public_feed_lag_ms`                  | 15.0  | `strategy18.py` Config  |
| `endgame_enabled`                     | True  | `strategy18.py` Config  |
| `endgame_tick_sec`                    | 0.04  | `strategy18.py` Config  |
| `endgame_edge_ticks_a`                | 0.5   | `strategy18.py` Config  |
| `endgame_max_levels`                  | 20    | `strategy18.py` Config  |
| `endgame_max_slice`                   | 25    | `strategy18.py` Config  |
| `prewarm_enabled`                     | True  | `strategy18.py` Config  |
| `prewarm_interval_sec`                | 25.0  | `strategy18.py` Config  |
| `prewarm_before_reveal_sec`           | 1.0   | `strategy18.py` Config  |
| `flatten_bias_enabled`                | True  | `strategy18.py` Config  |
| `pre_first_reveal_flatten_sec`        | 8.0   | `strategy18.py` Config  |
| `flatten_bias_sigma_a_min`            | 12.0  | `strategy18.py` Config  |
| `flatten_edge_reduction_ticks`        | 1.5   | `strategy18.py` Config  |
| `flatten_min_edge_ticks`              | 0.6   | `strategy18.py` Config  |
| `fast_precompute_tick_sec`            | 0.05  | `strategy18.py` Config  |
| `dime_defense_enabled`                | True  | `strategy18.py` Config  |
| `dime_defense_throttle_sec`           | 0.15  | `strategy18.py` Config  |
| `forbidden_circuit_max`               | 3     | `strategy18.py` Config  |
| `forbidden_circuit_window_sec`        | 10.0  | `strategy18.py` Config  |
| `forbidden_phase_recheck_throttle_sec`| 0.5   | `strategy18.py` Config  |
| `reconcile_print_min`                 | 3     | `strategy18.py` Config  |
| `ioc_parallel_workers`                | 10    | `strategy18.py` Config  |

### Bot-by-bot exploit map (`bot_config_dump.json` + v18 paths)

| Bot   | Class              | v18 exploit |
|-------|--------------------|-------------|
| VALKA | naive_mm w20/r1s   | inter_sweep + post-reveal sweep on default-mean stale quotes |
| VALKO | stale_quoter r5s s25 e2-6t | **biggest target.** Post-reveal IOC at 1.5t edge fires before VALKO re-quotes. inter_sweep_min_size=4 matches. |
| VALKF/L/H | predictive/bayes/inventory mm | penny inside; inter_sweep on >3t drift |
| VALKD/KVALK | dimer s1-3 | `_maybe_dime_defense` reclaims BBO ≤0.15s |
| VALKC/CVALK | informed_sniper(_next), lead 1-1.5s | we park at T-0.8s → they lift air |
| VALKE/M/J | mixed/bayes/slow informed taker | inter_sweep + dime defense reprice instantly after their fires |
| VALKN | pull_event 0.7p 5s | max_ioc_distance filter blocks the bait quote |
| HVALK | spoofer linger 300ms | inter_sweep_throttle=0.3s > linger → don't react to post-then-pull |
| GVALK | informed_twapper 3-slice | 15ms cross-arb window between their slices |
| IVALK | true_mean_taker | flatten-bias reduces exposure when sigma_b high |
| SEAN  | identical widths + skew | beat via 0.2s earlier park, 15ms cross-arb, dime defense, endgame burst |

### What's outstanding (v18)

- Validate by running a full round and grepping
  `combined_log_v18_*.jsonl` for `[v18 BBO:` (claim firing),
  `[v18 ARB(fill)` / `[v18 ARB(reveal)` (cross-arb hits),
  `[v18 ENDGAME:A]` (burst firing), `[v18 FORBIDDEN:` (403 reasons),
  `[v18 INTER-FLAT:` (flatten-bias firing). Absence of `[v18 POST:...
  HTTPError: 403 ... backoff 5.0s` confirms 403 cleanup.
- Spoofer fade — currently we just filter HVALK via
  max_ioc_distance_ticks. Could *take the opposite side* when the
  bait appears (followthrough_prob=0.5 implies 50% chance of price
  moving away from the spoof).
- VALKN blackout exploit — during the 5s pull window someone could
  ladder bids deep to catch the rebound. Currently we just stay out.
- Consider lowering `penny_min_edge_*` from 1.0t to 0.6t (matches
  flatten_min_edge_ticks) for tighter MM presence — would require
  validation that maker fee + adverse selection still nets positive.
