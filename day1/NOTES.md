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
