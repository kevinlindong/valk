"""
Day 1 live trading strategy v5.

Adds PUBLIC MARKET DATA integration on top of v4 (which fixed the v3
private-fill regression and added the informed-trader override). The
v4 trials showed two patterns worth addressing:

  1. TAIL-DOWN ROUND LOSSES. combined_log_v4_20260519_150816.jsonl R2
     settled at 11 (reveals all 1-2). Our K-stage posterior fair stayed
     near 19 and we kept buying at 13-17 across the round, ending +21
     long for a -65 PnL. Meanwhile the PUBLIC TRADE TAPE was screaming
     the truth: VWAP went 18.5 -> 9.5 -> 8.3 across three 30s windows,
     and aggressor flow was 60% sell-side. v4 used the BOOK midpoint
     as its only market signal -- but the book mid tracks the visible
     spread center and lags actual price discovery in trending markets.
     v5 adds a trade-tape VWAP + aggressor-imbalance signal that
     captures real transactions across the whole market (including
     trades we are not part of).

  2. COUNTERPARTY POSITION LIMITS. The earlier model assumed bots had
     no position limits; the user clarified they share the +/-100 cap.
     This means each counterparty can do at most ~100 lots in either
     direction before they MUST flatten. Late-round fills against a CP
     who has already moved >70 lots one-way are very likely flattening
     traffic (information-poor), and the next 30 lots they take must
     be in the OPPOSITE direction of their net build. v5 tracks a
     per-CP signed net flow against us and uses it to (a) decay the
     informed-strike bonus on a CP who is near their cap (so we stop
     widening against a forced-flatten counterparty) and (b) skew
     our own quote pricing to lean into the side they are about to
     unwind.

CHANGES vs v4:
  - NEW `_recent_trades` ring buffer populated by `on_trade(msg)`. Stores
    (t, price, qty, aggressor) for the last `trade_window_sec` (60s).
  - NEW `_trade_vwap(window)` and `_aggressor_imbalance(window)` helpers.
  - NEW `_market_signal()` blends book mid with trade VWAP:
      * Both fresh -> 50/50 weight (VWAP captures dynamics book mid misses)
      * Only book mid -> book mid (v4 behavior).
      * Only VWAP    -> VWAP (book is stale).
      * Neither      -> None (no anchor).
    The disagreement gate / market-anchor / penny-skip logic in
    `desired_quotes`, `maybe_snipe`, and `_try_direct_snipe` now uses
    this blended signal in place of `_market_mid_from_book` alone.
  - NEW per-CP exposure tracking. `_cp_net_flow[cp]` is signed:
    positive when CP has net-BOUGHT from us this round, negative when
    they net-SOLD. Updated on every fill (private feed).
  - UPDATED `informed_counterparties`: removed VALKM (per-CP PnL of
    -16 across 4 lots, we WIN against them -- widening was misclass);
    added AARU (+54 net over 107 lots, +0.5/lot consistent -- they are
    a small but consistent net-positive against us). Kept VALKC/VALKN/
    VALKJ which were small samples but trending informed.
  - NEW strike-decay on CP cap proximity: when |_cp_net_flow[cp]| >=
    `cp_cap_threshold`, decay the strike weight by 50% (the CP is near
    forced flatten -- their next fill is likely information-poor).

CARRIED FORWARD from v4:
  - Regression-fixed `_market_mid_from_book` (book-only, no biased
    private-fill branch).
  - INFORMED-TRADER OVERRIDE with `INFORMED_TRADER_ID` /
    `--informed-trader` flag. Truth-bound clamp `_effective_fair` and
    per-side cool-off remain unchanged.
  - K=0 sit-out, confidence-gated maker reprice cadence (12/8/4s),
    strike-decay edge model, FIFO-priority modify guard, sigma-scaled
    position cap, symmetric snipe disagreement gate (5.0 ticks),
    no-counterparty maker gate.

Run:
    python day1/run_combined5.py --informed-trader=ORACLE
While running: type 's'+Enter for status, 'f'+Enter to flatten,
'i'+Enter for informed-trader state, 'm'+Enter for market-tape state,
'q'+Enter to quit. Ctrl-C also flattens and exits cleanly.
"""

from __future__ import annotations

import math
import os
import random
import sys
import threading
import time
from collections import defaultdict
from typing import Optional

# Allow `python day1/strategy.py` from project root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from sdk.client import GameClient  # noqa: E402


# ---------------------------------------------------------------------------
# Connection (edit URL / API_KEY here)
# ---------------------------------------------------------------------------
URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"


# ---------------------------------------------------------------------------
# Informed trader (edit this string to point at the bot that has perfect
# knowledge of `a` and `w`). Setting to "" or None disables the feature.
# run_combined4.py also accepts --informed-trader=<NAME> to override at
# launch without editing this file.
# ---------------------------------------------------------------------------
INFORMED_TRADER_ID: Optional[str] = ""


# ---------------------------------------------------------------------------
# Posterior over (a, w)
# ---------------------------------------------------------------------------
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8   # a = floor(LogN(mu, sigma))
W_LOGN_MU, W_LOGN_SIGMA = 0.5, 0.7   # w = 1 + floor(LogN(mu, sigma))
N_PRIOR_SIM = 1_000_000              # MC samples to build the discrete prior


class Posterior:
    """Discrete joint posterior over (a, w) given reveals X_i ~ Uniform(a, a+w)."""

    def __init__(
        self,
        a_mu: float = A_LOGN_MU,
        a_sigma: float = A_LOGN_SIGMA,
        w_mu: float = W_LOGN_MU,
        w_sigma: float = W_LOGN_SIGMA,
        n_sim: int = N_PRIOR_SIM,
    ):
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for _ in range(n_sim):
            a_v = int(math.floor(random.lognormvariate(a_mu, a_sigma)))
            w_v = 1 + int(math.floor(random.lognormvariate(w_mu, w_sigma)))
            counts[(a_v, w_v)] += 1
        total = sum(counts.values())
        self.prior: dict[tuple[int, int], float] = {k: v / total for k, v in counts.items()}
        self.posterior: dict[tuple[int, int], float] = dict(self.prior)
        self.reveals: list[float] = []

    def reset(self, reveals: Optional[list[float]] = None) -> None:
        self.posterior = dict(self.prior)
        self.reveals = []
        for x in reveals or []:
            self.update(x)

    def update(self, x: float) -> None:
        self.reveals.append(float(x))
        new: dict[tuple[int, int], float] = {}
        total = 0.0
        eps = 1e-9
        for (a, w), p in self.posterior.items():
            b = a + w
            if x < a - eps or x > b + eps:
                continue
            # Discrete-uniform likelihood: support is {a, a+1, ..., a+w} (w+1
            # integers), so probability mass is 1/(w+1), NOT 1/w. The notebook
            # design notes flagged this as a TODO; simulation.py's ImprovedBot
            # confirmed it; ported here.
            new_p = p / (w + 1)
            new[(a, w)] = new_p
            total += new_p
        if total <= 0:
            print(f"WARN: reveal {x} outside posterior support; ignoring update")
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def mean_x(self) -> float:
        return sum(p * (a + w / 2.0) for (a, w), p in self.posterior.items())

    def predict_settle(self, running_sum: float, n_remaining: int) -> tuple[float, float]:
        """Mean and std of the total final settlement.

        Uses the law of total variance:
            Var[S_rem] = E[Var[S_rem | a,w]] + Var[E[S_rem | a,w]]
        """
        if n_remaining <= 0:
            return float(running_sum), 0.0
        e_inner = 0.0
        e2_inner = 0.0
        e_var = 0.0
        for (a, w), p in self.posterior.items():
            inner_mean = n_remaining * (a + w / 2.0)
            # Discrete-uniform variance over {a, ..., a+w} is w(w+2)/12,
            # not w^2/12. Same notebook-flagged TODO as the likelihood fix above.
            inner_var = n_remaining * (w * (w + 2)) / 12.0
            e_inner += p * inner_mean
            e2_inner += p * inner_mean * inner_mean
            e_var += p * inner_var
        var_total = e_var + (e2_inner - e_inner * e_inner)
        return running_sum + e_inner, math.sqrt(max(var_total, 0.0))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class Strategy:
    def __init__(self, client: GameClient, posterior: Posterior, symbol: str = "A",
                 informed_trader_id: Optional[str] = None):
        self.c = client
        self.symbol = symbol
        self.posterior = posterior
        self.lock = threading.RLock()

        # v4: id of a trader who knows the underlying (a, w) perfectly. If
        # set, every fill against them is treated as a high-confidence
        # signal about settlement direction (see _handle_informed_fill).
        # Constructor arg overrides the module constant; empty/None
        # disables the feature entirely.
        _iti = (informed_trader_id if informed_trader_id is not None
                else INFORMED_TRADER_ID)
        self.informed_trader_id: Optional[str] = _iti or None

        gs = client.game_state()
        self.duration = gs["duration"]
        self.reveal_interval = gs["reveal_interval"]
        self.n_total = self.duration // self.reveal_interval
        instr = gs["instruments"][symbol]
        self.tick = instr["tick_size"]
        self.position_limit = instr["position_limit"]
        fees = gs.get("fees", {})
        self.maker_fee = fees.get("maker_per_lot", 0.5)
        self.taker_fee = fees.get("taker_per_lot", 0.5)

        # Seed posterior with whatever reveals already happened in the current round.
        self.posterior.reset(gs.get("reveals") or [])

        # Position from server, not a local guess.
        self.position = int(client.positions()["positions"].get(symbol, 0))

        # Locally tracked phase, kept in sync via on_phase_change. Avoids
        # making a REST game_state() call on every WS event.
        self.phase: Optional[str] = gs.get("phase")

        # Throttle for on_quote_event: with ~17 quote_add events/sec in the
        # live market, running maybe_snipe (book REST + maybe IOC) on every
        # one would saturate. Skip if last check was < this many seconds ago.
        # Lowered to 50ms (was 100ms): the direct snipe path is unthrottled
        # already, but the THROTTLED path also drives maker reprice via
        # _apply_target_quotes. At 100ms we react to top-of-book moves in
        # ~50ms avg; at 50ms we react in ~25ms avg. Halves the queue-tail
        # window before we join the new inside. REST load doubles to ~20
        # book.fetch/sec but each call is ~30-50ms server-side, and most
        # events return early (no fair/sigma cache miss thanks to the
        # gen-keyed cache).
        self._last_quote_event_t: float = 0.0
        self.quote_event_throttle_sec: float = 0.05

        # Fair/sigma cache. predict_settle iterates over ~200-500 posterior
        # items doing log-of-total-variance math; called from desired_quotes,
        # maybe_snipe, _try_direct_snipe, _current_quote_qty. Without caching
        # we recompute it 4-6x per quote_event. Invalidated by bumping
        # _posterior_gen on every reveal / reset (the only events that move
        # the posterior). Stable across many WS events between reveals, so
        # the hit rate is very high in a FIFO market.
        self._posterior_gen: int = 0
        self._fair_sigma_cache: Optional[tuple[int, float, float]] = None

        # resting[side] -> {"order_id", "price", "qty"} or None
        self.resting: dict[str, Optional[dict]] = {"bid": None, "ask": None}

        # Per-order_id timestamp of the last successful modify. Log analysis
        # showed 87.9% of modify_acks fire <200ms after the previous modify
        # on the same order -- faster than the market reacts, pure REST waste
        # that starves the rate-limit budget. With min_modify_interval=0.25s
        # we cut modify traffic ~6x without measurably worsening fill
        # quality. SDK token-bucket is the global cap; this is the per-order
        # debounce.  When tokens_low_threshold tokens or fewer remain, skip
        # the modify entirely so an in-flight IOC snipe can fire.
        self._last_modify_t: dict[int, float] = {}
        self.min_modify_interval_sec: float = 0.25
        self.tokens_low_threshold: float = 3.0

        # v2: confidence-gated maker reprice interval. Maker quote refreshes
        # are throttled per-phase since fair barely moves between reveals
        # (every 30s) but the WS event stream pushes ~17 quote events/sec.
        # Reveal arrival resets this timer to 0 so the post-reveal reprice
        # fires immediately. Snipes do NOT consult this timer -- they fire
        # on every quote_add via the unthrottled direct-snipe path.
        self._last_maker_apply_t: float = 0.0

        # v2: counterparty-aware width. Strikes accumulate when an "informed"
        # bot picks us off, decay exponentially over ~60s, and add extra
        # ticks of edge to the side that was hit. last_t is used for the
        # decay clock.
        self._informed_strikes_bid: float = 0.0
        self._informed_strikes_ask: float = 0.0
        self._informed_strikes_last_t: float = time.time()

        # ---------- knobs ----------
        # Derived from fees + adverse-selection buffer, NOT from a specific
        # session's spread. Per-fill economics:
        #   - Maker fill earns (price - fair) - maker_fee per lot. Want this
        #     positive in expectation.
        #   - With maker_fee=0.5, min positive-edge quote is fair +/- 1.0
        #     (gross 1.0, net 0.5 per lot after fees). That's tight_floor_edge.
        #   - Default min_edge sits 0.5 wider than the floor for adverse-
        #     selection buffer (faster traders picking us off when fair moves).
        # Snipe knobs stay conservative -- the disaster session showed that
        # aggressive sniping (snipe_min_edge=0.5) in a 2-tick market generates
        # fee runaway. snipe_min_edge=1.5 + snipe_buffer_sigma=0.60 means
        # edge_required >= taker_fee + 1.5 = 2.0 ticks even in late round.
        # quote_qty=2 (was 5): the "many small fills" model. Log analysis
        # showed recent conservative settings captured only ~36% of the
        # free edge from bad counter-quotes (vs 77% in older qty=5 rounds);
        # capture rate is what matters, not per-fill size. qty=2 lets us
        # be top-of-book on BOTH sides without one bad fill costing us the
        # round (max-loss-per-fill scales linearly with qty, capture scales
        # super-linearly with priority). Combined with the sigma-scaled
        # variance tier (mid_round_quote_qty=1), per-fill risk is bounded.
        self.quote_qty = 2
        self.min_edge = 1.0            # default fair-edge: maker_fee + 0.5 buffer
        self.edge_per_sigma = 0.25
        self.skew_per_unit = 0.20      # passive flatten preferred over taker flatten
        # snipe_buffer_sigma=0.40 (was 0.60): logs show counter-bots posting
        # bad quotes at avg edge 6.5-7.3 ticks. At sigma~8 (K=1), old buffer
        # required taker_fee + max(1.0, 4.8) = 5.3 ticks edge; new buffer
        # requires 0.5 + max(1.0, 3.2) = 3.7 ticks. That gap is exactly the
        # 36% capture we were leaving on the table. The market_mid
        # disagreement gate still blocks snipes in biased rounds.
        self.snipe_buffer_sigma = 0.40
        # snipe_min_edge=1.0: allows late-round 1-tick mispricings through
        # when sigma is small (taker_fee + 1.0 = 1.5 edge required at low
        # sigma; sigma-scaled buffer still binds at higher sigma).
        self.snipe_min_edge = 1.0
        self.snipe_book_depth = 10
        # Per-snipe size cap. We have a +/-100 trader position limit; bots
        # don't. Without this cap, a low-sigma snipe at a 25-lot bad quote
        # would slam our entire position_limit headroom into ONE level --
        # one wrong fair calc and we eat 100 lots of adverse selection.
        # Cap each snipe at 10 lots; if the level has more, we'll come back
        # for it on the next quote_add/cancel event (event-driven). Log
        # analysis showed 50% of bad trades sat resting >2s -- bots repost
        # the same bad quote repeatedly, so coming back is reliable. Replaces
        # the old snipe_full_size_sigma branch which used position_limit at
        # low sigma -- "be intentional with sizing".
        self.snipe_max_qty_per_level = 10
        # HARD trade-rate ceiling (per round) -- defense against fee runaway
        # if our edge calc is wrong. Raised to 120 (from 60): the disagreement
        # gate now blocks the wrong-direction snipe runaway that hit 100+ in
        # the disaster round, AND snipe_max_qty_per_level caps per-fill size,
        # so the cap can be relaxed further to let pattern-exploit volume
        # through. Combined safety: 120 snipes * 10 qty cap = 1200 lots max,
        # but position_limit=100 binds first -- so the count cap is really
        # just a guard against fee-runaway loops, not edge-direction risk.
        self.max_snipes_per_round = 120
        self._snipe_count_this_round = 0
        # Penny/dime: step exactly tight_step_inside_ticks INSIDE the market's
        # best on each side IFF we'd still keep >= tight_floor_edge gross
        # margin per fill (1.0 ticks = 0.5 net after maker fee). Designed to
        # only activate when ANOTHER trader is between our default quote and
        # our floor -- typically because our sigma-scaled edge widens us in
        # early round, or because the other trader is taking a thinner edge
        # than is economically rational. Skipped when we are already at-or-
        # better-than the market best (preserves queue priority).
        self.tight_quoting_enabled = True
        self.tight_floor_edge = 1.0    # never quote tighter than maker_fee + 0.5
        self.tight_step_inside_ticks = 1  # how many ticks inside market best
        # tight_penny_sigma_max=10.0 (was 4.0): the old threshold gated
        # penny off at K=1 (sigma~8-9), which is exactly when the freshest
        # mispricings appear (other bots haven't adjusted to the first
        # reveal yet). Log analysis: K=1 maker capture dropped from 77% to
        # 28-38% after the tight gate. The tight_floor_edge of 1.0
        # protects per-fill economics; the position cap protects
        # cumulative exposure. Re-enabling penny at K=1 unlocks the early
        # window where most "free money" trades happen.
        self.tight_penny_sigma_max = 10.0
        # tight_penny_min_reveals=0 (was 1): compete for top-of-book even
        # pre-reveal. Older-log analysis showed inside-maker fills pay
        # ~42% more per fill than deeper fills, and other traders are
        # making 36-103 pre-reveal trades / round while we sit deep
        # (only 9 pre-reveal fills total for us across 6 rounds). The
        # bias gate above (set when |fair - market_mid| >= 5) still
        # blocks penny in tail-prior rounds where our anchor is wrong;
        # the pre-reveal pull-side defense additionally suppresses the
        # exposed side at |diff| >= 10. So penny pre-reveal only fires
        # when our prior is roughly in line with market consensus --
        # exactly when stepping inside is safe.
        self.tight_penny_min_reveals = 0
        self._book_cache: Optional[dict] = None
        self._book_cache_t: float = 0.0
        self._book_cache_max_age_sec: float = 1.0
        # v3 private-feed signal: the public WS/book feed lags the private
        # feed by tens-of-ms to ~200ms. Whenever we observe a fill (our own
        # IOC trade response OR the WS fill event), capture the price as
        # the freshest possible market signal. Callers consult it via
        # _recent_private_price(). Used by _market_mid_from_book to prefer
        # the private signal when the public book cache is older than the
        # private fill, and to break ties on the disagreement gate.
        self._last_fill_px: float = 0.0
        self._last_fill_t: float = 0.0
        self._private_signal_max_age_sec: float = 2.0
        # Pre-reveal MM RE-ENABLED (min_reveals_to_quote=0) with explicit
        # tail defense. Earlier disabled it after a tail-prior round
        # (reveals 14 14 14 12... settle=131) where our prior-only fair
        # (~45) was far below market consensus (~60). With min_edge=10
        # ALONE, our ask landed at 55 -- INSIDE the market bid of 58,
        # marketable post, taker-filled at 58 for -5343 over 30s.
        #
        # The fix is the market-mid anchor (already in desired_quotes for
        # K>=1) extended to pre-reveal: when market_mid >> prior_fair,
        # ask is WIDENED to ceil(market_mid + edge). With market_mid=60
        # and edge=10 our ask = 70 (outside market spread), so we can't
        # taker-fill on post. We only sell to someone aggressively paying
        # 70+. In tail-DOWN, prior is HIGH and market_mid is LOW; the
        # symmetric clamp widens bid down. Pre-reveal MM in NORMAL rounds
        # (market ~= prior) is profitable -- earlier sessions logged
        # +1119 and +795 from pre-reveal quoting at qty=1; the disaster
        # came purely from the missing market-mid clamp in tail rounds.
        #
        # If market_mid is unavailable (book is empty pre-warmup), we
        # skip quoting -- no anchor, no safety. Warmup gives the market
        # 2s to form before we participate.
        self.min_reveals_to_quote = 1
        self.min_reveals_to_snipe = 1
        self.pre_reveal_quote_qty = 1
        # pre_reveal_min_edge=4.0 (was 10.0): the old value parked us at
        # prior +/- 10, way outside the typical 3-tick pre-reveal spread
        # ALL other traders are inside. Log analysis: across 6 rounds,
        # other traders made 36-103 pre-reveal trades / round while we
        # made 9 TOTAL (and lost -26 on them). At edge=4 we're at prior
        # +/- 4 -- inside the median spread but with the tail-pull
        # defense + market_mid anchor as catastrophe protection. The
        # disaster mode (market_mid=60, prior=45 -> our ask=49 vs market
        # bid=58) is now blocked by the pull defense BEFORE we'd post.
        self.pre_reveal_min_edge = 4.0
        self.pre_reveal_skew_disabled = True
        self.pre_reveal_warmup_sec = 2.0
        # Pre-reveal "pull the wrong side" defense. When market_mid
        # disagrees with prior_fair by MORE than this, the dangerous side
        # of the spread is suppressed entirely. tail-UP (market_mid >
        # fair + threshold) pulls ASK (avoid selling cheap below truth);
        # tail-DOWN pulls BID (avoid buying high above truth). Tightened
        # to 10 (was 15) since lowering pre_reveal_min_edge from 10 to 4
        # narrows the safety margin -- need to react to a borderline
        # disagreement sooner. The original disaster had |diff|=15
        # exactly; 10 catches it earlier.
        self.pre_reveal_pull_disagreement = 10.0
        # When ONE side of the pre-reveal book is empty, post an
        # extra-wide quote on that side. Log analysis showed empty-side
        # cases get filled within ~100ms (a walker comes in with a
        # wide-marketable order). Quote at fair +/- empty_side_edge so
        # the walker pays a fat premium. Tail-pull still applies via
        # the surviving side's best price.
        self.empty_side_edge = 15.0
        self._phase_running_t: Optional[float] = None
        # Sigma-scaled position cap. When sigma is large (early reveals,
        # posterior still wide), each adverse-pickoff fill could cost
        # ~sigma ticks. Cap inventory so the worst-case loss is bounded
        # by `cap * (loss per lot)`. At sigma <= target_sigma, full
        # position_limit. Above that, cap = limit * target / sigma, with
        # a floor so we can always carry SOME position. Applied to both
        # passive quoting (suppress bid/ask at the cap) and sniping
        # (cap snipe headroom against the same level). Without this,
        # the user's outlier round let us accumulate -92 in the first
        # 30 seconds; with target_sigma=2 and sigma~25 pre-first-reveal,
        # cap = max(10, 100*2/25) = 10, capping early-round loss roughly
        # 10x lower.
        self.max_pos_target_sigma = 2.0
        self.max_pos_floor = 10
        # Bias-aware position-cap relaxation. When market_mid is fresh
        # AND agrees with fair (|fair - market_mid| < 5), we have
        # consensus-confirmation that our fair is right, so the
        # adverse-pickoff risk is much lower. In that regime use the
        # relaxed cap below. When biased (or no signal) we fall back
        # to the conservative target/floor above. Log analysis: max|pos|
        # was 15/35 in two settled rounds vs cap=25 (K=1) -- the
        # conservative cap was barely binding, leaving inventory
        # headroom unused. Bumping target_sigma to 3.0 raises K=1 cap
        # (sigma~8) from 25 -> ~38 and K=2 cap from 33 -> 50; floor=15
        # bumps pre-reveal cap from 10 -> 15. Max-pos was NOT
        # correlated with worst-PnL rounds, so the cap is dead-weight
        # in the regime where edges concentrate.
        self.max_pos_target_sigma_normal = 3.0
        self.max_pos_floor_normal = 15
        # Market-mid quote anchor. When our posterior fair and the
        # market consensus mid disagree by more than this threshold, we
        # bracket our quotes around BOTH estimators rather than just our
        # own fair. Concretely: bid = min(default_bid, market_mid - edge),
        # ask = max(default_ask, market_mid + edge). This is a one-sided
        # safety floor that protects against the case where our posterior
        # is biased (tail-prior round) and the market is closer to truth.
        # In normal rounds where fair ~= market_mid, no clamping fires.
        self.market_anchor_disagreement_min = 5.0
        # Snipe disagreement gate. When our fair and market mid disagree
        # significantly, sniping is risky -- our edge calc uses our fair,
        # but if market is closer to truth, our snipe fires in the WRONG
        # direction (selling at "high" prices that are actually below
        # truth). Skip snipes entirely when disagreement exceeds this.
        # v3: tightened from 10.0 -> 5.0 to align with the maker-side
        # market_anchor_disagreement_min. second_round_loss.jsonl had a
        # tail-DOWN round where |fair - mid| ~7 leaked through, fired
        # SELL snipes into correct bids, and gave back ~$330.
        self.snipe_max_disagreement = 5.0
        # Asymmetric disagreement override (v3: DISABLED).
        # v2 allowed "with-conviction" snipes through when the symmetric
        # gate above blocked. In a tail-DOWN round (settle below the prior
        # mode, posterior collapsed tight) our fair was far below market_mid
        # so the override fired SELL snipes that all turned out to be
        # against truth -- the bids we hit were RIGHT and we were WRONG.
        # Setting this False makes the gate purely symmetric: if |fair-mid|
        # > snipe_max_disagreement, NO snipes fire (either side). Maker
        # quotes still run through their own anchor logic in desired_quotes.
        self.snipe_disagreement_directional = False
        # Variance-scaled quote SIZE (third layer of variance scaling).
        # mid_round_quote_qty=1 (was 2): at K=1-2 sigma is ~8-9 and even
        # though we now compete for top-of-book here, per-fill exposure
        # must stay tiny since a wrong-direction fair shift on the next
        # reveal costs ~sigma per lot. qty=1 means a single fill is at
        # most a sigma-sized loss, but we still capture many fills via
        # top-of-book queue priority.
        self.full_qty_sigma_max = 3.0
        self.mid_round_quote_qty = 1
        # Bias-aware quote-size relaxation. When market_mid is fresh
        # and confirms our fair (not biased), upsize maker quotes:
        # low-sigma 2 -> 5, mid-round 1 -> 2. Total volume = fills *
        # per-fill size; log analysis showed avg|pos|=5 with 49 fills
        # in a settled round -- many small fills, lots of headroom
        # unused. With the bias gate blocking the dangerous regime,
        # the per-fill upside in normal regime is worth the modest
        # extra exposure (mid_round qty=2 caps per-fill adverse loss
        # at 2*sigma vs 1*sigma at qty=1; at sigma~8 that's 16 vs 8
        # ticks worst-case per fill). The bias detector + market-mid
        # anchor + pre-reveal pull defense remain the catastrophic-
        # loss-prevention layer.
        self.quote_qty_normal = 5
        self.mid_round_quote_qty_normal = 2
        # Asymmetric inventory dampener. When |position| exceeds
        # inv_dampen_threshold (fraction of current max_pos), shrink the
        # quote on the side that would ADD to the existing position.
        # Round-2 (-950) and round-23 (-470) losses in the sim were both
        # caused by the strategy fillling at ~5 lots/quote on the SAME side
        # (sell-side) for 4-5 reveals while a wrong-confident posterior
        # held. Skew (linear in position) pulls the price but doesn't shrink
        # the size; this knob does. inv_dampen_factor=0 = hard-suppress
        # (quote None on adverse side); >0 = scale qty down.
        self.inv_dampen_threshold_frac = 0.40
        self.inv_dampen_factor = 0.0  # adverse side qty -> 1 (floor) when |pos| past threshold
        # Hard kill: when |pos| exceeds inv_hard_kill_frac of max_pos, drop
        # the adverse-side quote entirely (return None). Even a 1-lot adverse
        # quote bled in tail rounds: round-2 of seed-1 showed pos going
        # -17 -> -35 -> -52 across 3 reveals from many 1-lot fills accumulating.
        # 0.55 means at sigma=5, max_pos=60, kill adverse side once |pos|>33.
        self.inv_hard_kill_frac = 0.55
        # Inventory-aware snipe edge: when a snipe would ADD to existing
        # position, require more edge than usual. value_sniper-style
        # counterparties (passive maker, qty_taker=0) feed us bad takes
        # when we're already exposed. Adverse adds need
        # snipe_min_edge_adverse extra ticks of margin to survive.
        self.snipe_adverse_extra_edge = 1.5
        # Post-final-reveal behavior: when n_remaining == 0, fair is no longer
        # theoretical -- it's the exact settlement (sigma == 0). At that point:
        #   - quoting passive spread on a known value gives away free edge to
        #     anyone who hits us at fair-edge (and we have no informational
        #     advantage anymore), so cancel quotes.
        #   - sniping is still pure profit -- any visible mispricing is someone
        #     ELSE making a mistake on a value we both know, so keep snipping.
        self.quote_after_final_reveal = False
        self.snipe_after_final_reveal = True

        # v2 knobs ------------------------------------------------------
        # Confidence-gated maker reprice interval (seconds). Keyed by the
        # number of revealed values K = len(reveals). K=0 is held at +inf
        # but is also blocked by the K=0 early-return in step() / on_*.
        # Calibrated from log analysis: sigma_pop(fair) ~ sqrt(10-K)*4.3,
        # ~13 at K=1, ~6 at K=8, 0 at K=10. Fair moves at ~30s cadence
        # (each reveal). Maker quotes within a phase mostly chase noise.
        self.maker_reprice_interval_by_k: list[tuple[int, float]] = [
            (3, 12.0),    # K=1-3:  every 12 s
            (7, 8.0),     # K=4-7:  every 8 s
            (99, 4.0),    # K>=8:   every 4 s
        ]
        # Counterparty-strike model. Names below were tagged as "informed"
        # by analyze_for_v2.py across 11 sessions (net loss to us). Each
        # fill against one of them adds 1.0 strike on the side we lost on;
        # strikes decay with this rate (per second). Strike count is
        # capped so a long burst can't push edge to infinity.
        # v5: per-CP PnL audit of combined_log_v4_*.jsonl across the
        # 2026-05-19 trial set showed:
        #   VALKM: -16 over 4 lots (we WIN against them) -- DROP from
        #          informed set; widening their strike was misclass.
        #   AARU : +54 over 107 lots (~+0.5/lot, consistent positive
        #          edge against us) -- ADD to informed set.
        #   VALKC, VALKN, VALKJ: small samples but trending informed.
        # The strike-decay logic still bounds runaway adds, so a small-
        # sample false positive only adds a few ticks of edge.
        self.informed_counterparties: set[str] = {
            "VALKC", "VALKN", "VALKJ", "AARU"}
        self.informed_strike_decay_per_sec: float = math.log(2) / 60.0  # 60s half-life
        self.informed_strike_edge_per_strike: float = 0.5  # ticks added per strike
        self.informed_strike_max: float = 4.0  # cap (max 2.0 ticks extra)
        # K=0 hard gate: anything pre-reveal early-returns. Setting this
        # False would re-enable the legacy pre-reveal logic in
        # desired_quotes() for debugging, but the default is OFF (per
        # user spec: do not trade when no info is available).
        self.skip_pre_reveal: bool = True
        # v3: position buffer enforced on snipes only. Snipe IOCs go
        # straight to fill, so a burst of them can push |position| through
        # the ±position_limit hard cap before reconcile catches up.
        # second_round_loss.jsonl recorded |pos|=127 vs the ±100 limit;
        # the exchange clamped the overflow but left ~$190 of misaligned
        # forced fills behind. snipe_position_buffer requires every IOC
        # (both book-scan maybe_snipe and direct quote_add path) to leave
        # at least this many lots of headroom before the hard limit.
        # Maker quotes still use the full sigma-scaled cap; we keep their
        # passive edge intact.
        self.snipe_position_buffer = 5

        # v4 informed-trader state. Each fill against `self.informed_trader_id`
        # establishes a one-sided truth bound (they sold to us at P -> truth
        # <= P; they bought from us at P -> truth >= P). The picked-off side
        # is cancelled and held idle for `informed_cooloff_sec`; the truth
        # bound is then applied as an `_effective_fair` clamp for the rest
        # of the round (cleared in on_phase_change). The clamp affects both
        # maker quotes and snipe edge math, so a posterior that disagrees
        # with the informed trader cannot drive us into a wrong-direction
        # trade.
        self.informed_cooloff_sec: float = 15.0
        self._informed_cooloff_until: dict[str, float] = {"bid": 0.0, "ask": 0.0}
        self._informed_last_t: float = 0.0
        self._informed_last_their_side: Optional[str] = None  # "buy" or "sell"
        self._informed_last_px: Optional[float] = None
        self._informed_fair_lo: Optional[float] = None  # truth >= this
        self._informed_fair_hi: Optional[float] = None  # truth <= this
        self._informed_total_qty: int = 0
        self._informed_fill_count: int = 0

        # v5 public trade tape. Ring buffer of (t, price, qty, aggressor)
        # for the last `trade_window_sec`. Populated by Strategy.on_trade
        # (wired from the SDK on_trade callback in run_combined5). Used
        # by _trade_vwap / _aggressor_imbalance / _market_signal.
        # The book midpoint lags real price discovery in trending rounds
        # (combined_log_v4_20260519_150816 R2: VWAP 18.5->8.3 while book
        # mid stayed near 18). The tape is a faster, more honest signal.
        self._recent_trades: list[tuple[float, float, int, str]] = []
        self.trade_window_sec: float = 60.0
        self._max_recent_trades: int = 500

        # v5 per-CP signed flow (from our perspective): positive = CP
        # net-BOUGHT from us this round, negative = CP net-SOLD to us.
        # Updated from our own fill events: when CP is the maker side
        # of OUR buy (we lifted them), they sold -> -qty.
        # When CP is the maker side of OUR sell (they hit our bid),
        # they bought -> +qty. Bots share the +/-100 position limit,
        # so |flow|>=cp_cap_threshold means the CP is near forced
        # flatten -- their next fill is information-poor (mechanical).
        # We use this to decay the informed-strike weight for that CP
        # (see _informed_extra_edge) so we stop widening against a
        # bot who is about to dump inventory regardless of (a, w).
        self._cp_net_flow: dict[str, int] = defaultdict(int)
        self.cp_cap_threshold: int = 70
        self.cp_near_cap_strike_decay: float = 0.5
        # ----------------------------

    # -------- helpers --------

    def _running_sum(self) -> float:
        return sum(self.posterior.reveals)

    def _n_remaining(self) -> int:
        return max(self.n_total - len(self.posterior.reveals), 0)

    # ---- v2 helpers: cadence + counterparty awareness ----

    def _maker_reprice_interval(self) -> float:
        """Minimum seconds between maker reprice calls, based on K. Returns
        +inf when K=0 so the cadence gate naturally suppresses pre-reveal
        repricing (belt and suspenders alongside the K=0 early-return)."""
        k = len(self.posterior.reveals)
        if k == 0:
            return float("inf")
        for k_max, interval in self.maker_reprice_interval_by_k:
            if k <= k_max:
                return interval
        return self.maker_reprice_interval_by_k[-1][1]

    def _decay_strikes(self) -> None:
        """Apply exponential decay to the informed-strike counters. Called
        before every read and every increment so the counts reflect the
        current moment without needing a background timer."""
        now = time.time()
        elapsed = now - self._informed_strikes_last_t
        if elapsed <= 0:
            return
        factor = math.exp(-elapsed * self.informed_strike_decay_per_sec)
        self._informed_strikes_bid *= factor
        self._informed_strikes_ask *= factor
        self._informed_strikes_last_t = now

    def _record_informed_fill(self, counterparty: Optional[str],
                              fill_side: str) -> None:
        """If the counterparty is in informed_counterparties, add 1 strike
        on the side we got picked off. fill_side is OUR side: 'buy' means
        we bought (our bid was hit), so the informed flow is sniping our
        BID -> widen the bid further away. Symmetric for 'sell'.

        v5: bots share the +/-100 position limit. When the CP's signed
        net flow vs us already exceeds cp_cap_threshold one-way, their
        next fill is likely a forced flatten (mechanical, info-poor),
        not an informed pickoff. Halve the strike weight in that regime
        so we don't keep widening against a counterparty who is about
        to dump inventory regardless of (a, w).
        """
        if not counterparty or counterparty not in self.informed_counterparties:
            return
        self._decay_strikes()
        flow = self._cp_net_flow.get(counterparty, 0)
        strike_weight = (self.cp_near_cap_strike_decay
                         if abs(flow) >= self.cp_cap_threshold else 1.0)
        if fill_side == "buy":
            self._informed_strikes_bid = min(
                self.informed_strike_max,
                self._informed_strikes_bid + strike_weight)
        else:
            self._informed_strikes_ask = min(
                self.informed_strike_max,
                self._informed_strikes_ask + strike_weight)

    def _informed_extra_edge(self) -> tuple[float, float]:
        """(extra_bid_edge_ticks, extra_ask_edge_ticks) from the strike
        counters. Decay is applied at read time so values reflect now."""
        self._decay_strikes()
        return (self._informed_strikes_bid * self.informed_strike_edge_per_strike,
                self._informed_strikes_ask * self.informed_strike_edge_per_strike)

    def fair_and_sigma(self) -> tuple[float, float]:
        # Hot path: called from desired_quotes, maybe_snipe, _try_direct_snipe,
        # _current_quote_qty -- potentially 4-6x per WS event. The underlying
        # predict_settle iterates the full posterior (~200-500 (a,w) cells)
        # doing law-of-total-variance math. Cache by generation; invalidated
        # when posterior state changes (on_reveal / on_phase_change reset).
        gen = self._posterior_gen
        cached = self._fair_sigma_cache
        if cached is not None and cached[0] == gen:
            return cached[1], cached[2]
        fair, sigma = self.posterior.predict_settle(
            self._running_sum(), self._n_remaining())
        self._fair_sigma_cache = (gen, fair, sigma)
        return fair, sigma

    def _max_position_for_sigma(self, sigma: float, *,
                                 relaxed: bool = False) -> int:
        """Sigma-scaled inventory cap. At low sigma the posterior is tight, so
        each fill represents close-to-known edge -- full position_limit is OK.
        At high sigma each fill has wide uncertainty; cap inventory linearly
        in 1/sigma so the worst-case adverse-pickoff loss (~ pos * sigma) is
        bounded. Floor at max_pos_floor so we always have SOME headroom.
        Returns position_limit when sigma <= target.

        `relaxed=True` activates the higher cap (target_sigma_normal /
        floor_normal). Caller must ONLY set relaxed=True when market_mid
        confirms our fair (|fair - market_mid| < market_anchor_disagreement_min).
        When biased or no market signal, leave relaxed=False -- this is the
        tail-defense path."""
        if relaxed:
            target = self.max_pos_target_sigma_normal
            floor = self.max_pos_floor_normal
        else:
            target = self.max_pos_target_sigma
            floor = self.max_pos_floor
        if sigma <= target:
            return self.position_limit
        cap = int(self.position_limit * target / sigma)
        return max(floor, cap)

    def _refresh_book_cache_if_stale(self) -> None:
        """Refresh _book_cache via REST if it's empty or older than the TTL.
        Used pre-reveal where maybe_snipe (the normal cache-populator) is
        gated off by min_reveals_to_snipe. desired_quotes' market-mid anchor
        and the pull-side defense BOTH need a fresh book pre-reveal.
        Idempotent and safe to call frequently -- the TTL check avoids
        REST spam.
        """
        if (self._book_cache is not None and
                time.time() - self._book_cache_t <= self._book_cache_max_age_sec):
            return
        try:
            self._book_cache = self.c.book(
                self.symbol, depth=self.snipe_book_depth)
            self._book_cache_t = time.time()
        except Exception:
            pass

    def _other_makers_present(self) -> bool:
        """v3: True iff the public book shows at least one quote that
        isn't our own resting order. Market making earns its spread by
        intermediating OTHER participants' flow; when the book is empty
        or shows only our own resting prices, there's no counterparty
        flow to capture and any quote we post either decays into the
        void or matches against ourselves on a price tweak. Used as a
        hard gate in desired_quotes to suppress maker posts in that
        regime. Sniping is unaffected -- it stays reactive to specific
        quote_add events when they fire.

        Resting orders aggregate into the level qty, so we distinguish:
          - a price level NOT at our resting price -> someone else
          - a level AT our resting price with qty > our own resting qty
            -> at least one other participant queued alongside us
        Refreshes the book cache lazily; absence of a fresh book
        returns False (no signal -> don't quote, conservative default).
        """
        self._refresh_book_cache_if_stale()
        book = self._book_cache
        if book is None:
            return False
        if (time.time() - self._book_cache_t
                > self._book_cache_max_age_sec):
            return False

        def side_has_other(levels, our_px, our_qty):
            for lvl in levels:
                lvl_px = lvl.get("price")
                if lvl_px != our_px:
                    return True
                if our_qty is None:
                    return True
                if lvl.get("qty", 0) > our_qty:
                    return True
            return False

        our_bid = self.resting.get("bid")
        our_ask = self.resting.get("ask")
        our_bid_px = our_bid["price"] if our_bid else None
        our_bid_qty = our_bid["qty"] if our_bid else None
        our_ask_px = our_ask["price"] if our_ask else None
        our_ask_qty = our_ask["qty"] if our_ask else None

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        return (side_has_other(bids, our_bid_px, our_bid_qty) or
                side_has_other(asks, our_ask_px, our_ask_qty))

    def _recent_private_price(self) -> Optional[float]:
        """Most-recent observed fill price from the PRIVATE feed (our
        IOC responses + WS fill events), within `_private_signal_max_age_sec`.

        v4: retained for observability only -- the status command surfaces
        it so operators can sanity-check that the private stream is alive.
        It is NOT consulted by `_market_mid_from_book`, because our own
        fill price sits at our maker quote (one side of the spread, not
        the mid), and using it as "market mid" biased the v3
        disagreement gate by ~half a spread and shut off snipes entirely
        across two trial runs.
        """
        if self._last_fill_t == 0.0:
            return None
        if (time.time() - self._last_fill_t
                > self._private_signal_max_age_sec):
            return None
        return self._last_fill_px

    def _effective_fair(self, fair: float) -> float:
        """v4: clamp posterior fair into the truth bounds implied by
        informed-trader fills, if any. The informed trader knows the
        underlying, so every fill against them is a one-sided bound:
        truth >= `_informed_fair_lo` (they bought from us) and truth <=
        `_informed_fair_hi` (they sold to us). The posterior may still
        say something outside those bounds early on -- we trust the
        informed trader over the prior. Returns `fair` unchanged if the
        feature is disabled or no informed fill has happened yet.
        """
        if (self._informed_fair_lo is not None
                and fair < self._informed_fair_lo):
            return self._informed_fair_lo
        if (self._informed_fair_hi is not None
                and fair > self._informed_fair_hi):
            return self._informed_fair_hi
        return fair

    def _handle_informed_fill(self, msg: dict) -> None:
        """v4: react to a fill against the configured informed trader.

        Side decoding (msg['side'] is OUR side):
          - We BOUGHT at P (they sold)  -> truth <= P. Cancel our bid;
            cool off the bid side; raise `_informed_fair_hi`.
          - We SOLD at P (they bought)  -> truth >= P. Cancel our ask;
            cool off the ask side; raise `_informed_fair_lo`.

        Cool-off suppresses the picked-off side's maker post for
        `informed_cooloff_sec`; the fair clamp persists for the rest of
        the round (cleared in on_phase_change). Counterparty cooloff is
        ALSO recorded for taker-side fills (liq='taker'): we IOC'd into
        an informed maker, which still pins truth on one side of the
        traded price.

        Must be called with self.lock held.
        """
        if not self.informed_trader_id:
            return
        counterparty = msg.get("counterparty")
        if counterparty != self.informed_trader_id:
            return
        our_side = msg.get("side")
        price = msg.get("price")
        qty = msg.get("qty", 0)
        if our_side not in ("buy", "sell") or price is None:
            return
        now = time.time()
        self._informed_last_t = now
        self._informed_last_px = float(price)
        self._informed_total_qty += int(qty)
        self._informed_fill_count += 1
        if our_side == "buy":
            # We bought; informed sold at P. Truth <= P.
            self._informed_last_their_side = "sell"
            if (self._informed_fair_hi is None
                    or price < self._informed_fair_hi):
                self._informed_fair_hi = float(price)
            self._informed_cooloff_until["bid"] = now + self.informed_cooloff_sec
            try:
                self._safe_cancel("bid")
            except Exception:
                pass
            print(f"INFORMED  {counterparty} SOLD to us @ {price} "
                  f"(we bought {qty}). truth<={price}. "
                  f"cancel bid; cooloff {self.informed_cooloff_sec:.0f}s.")
        else:  # our_side == "sell"
            # We sold; informed bought at P. Truth >= P.
            self._informed_last_their_side = "buy"
            if (self._informed_fair_lo is None
                    or price > self._informed_fair_lo):
                self._informed_fair_lo = float(price)
            self._informed_cooloff_until["ask"] = now + self.informed_cooloff_sec
            try:
                self._safe_cancel("ask")
            except Exception:
                pass
            print(f"INFORMED  {counterparty} BOUGHT from us @ {price} "
                  f"(we sold {qty}). truth>={price}. "
                  f"cancel ask; cooloff {self.informed_cooloff_sec:.0f}s.")

    def _market_mid_from_book(self) -> Optional[float]:
        """Best-bid/best-ask midpoint from the cached book, or None if the
        cache is missing/stale/one-sided. Used by the market-anchor logic
        in desired_quotes and the snipe-disagreement gate in maybe_snipe.
        Returns None silently when no sane mid is available -- callers
        treat that as 'no disagreement signal, proceed normally'.

        v4: reverted from v3's private-fill-preferred variant after the
        strategy3 trials showed ZERO IOC snipes fired (vs 14-23 in v2 and
        pre-v3 logs) because `_last_fill_px` rode the same side of the
        spread as our maker quote, tripping the |fair - mid| > 5 gate
        almost every time we got hit.
        """
        book = self._book_cache
        if book is None:
            return None
        if time.time() - self._book_cache_t > self._book_cache_max_age_sec:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return (bids[0]["price"] + asks[0]["price"]) / 2.0

    def on_trade(self, msg: dict) -> None:
        """v5: public trade tape. Append every public `trade` event to a
        rolling buffer. Each entry is (t, price, qty, aggressor).
        `aggressor` is "buy" when the taker bought (so the market lifted
        an ask) and "sell" when the taker sold (so the market hit a bid).
        Trim to trade_window_sec and _max_recent_trades to bound memory.
        """
        try:
            price = float(msg.get("price"))
            qty = int(msg.get("qty") or 0)
        except (TypeError, ValueError):
            return
        if qty <= 0:
            return
        aggressor = msg.get("aggressor")
        if aggressor not in ("buy", "sell"):
            aggressor = ""
        now = time.time()
        with self.lock:
            self._recent_trades.append((now, price, qty, aggressor))
            # Trim by age and by hard cap so the buffer can't grow.
            cutoff = now - self.trade_window_sec
            if (len(self._recent_trades) > self._max_recent_trades
                    or (self._recent_trades and
                        self._recent_trades[0][0] < cutoff)):
                self._recent_trades = [
                    e for e in self._recent_trades if e[0] >= cutoff
                ][-self._max_recent_trades:]

    def _trade_vwap(self, window: float = 30.0) -> Optional[float]:
        """Volume-weighted average price across the last `window` seconds
        of public trades. None if no qualifying trades.
        """
        if window <= 0:
            return None
        cutoff = time.time() - window
        num = 0.0
        den = 0
        for t, px, q, _ in self._recent_trades:
            if t < cutoff:
                continue
            num += px * q
            den += q
        if den <= 0:
            return None
        return num / den

    def _aggressor_imbalance(self, window: float = 30.0) -> Optional[float]:
        """Signed aggressor imbalance in [-1, 1] across the last `window`
        seconds of public trades. +1 = all taker buys (lifting asks),
        -1 = all taker sells (hitting bids). None if no trades.
        """
        if window <= 0:
            return None
        cutoff = time.time() - window
        buy_q = 0
        sell_q = 0
        for t, _, q, agg in self._recent_trades:
            if t < cutoff:
                continue
            if agg == "buy":
                buy_q += q
            elif agg == "sell":
                sell_q += q
        total = buy_q + sell_q
        if total <= 0:
            return None
        return (buy_q - sell_q) / total

    def _market_signal(self) -> Optional[float]:
        """Best available market price anchor: blend of book midpoint and
        recent trade VWAP. Used in place of `_market_mid_from_book()` for
        the disagreement gates in desired_quotes / maybe_snipe /
        _try_direct_snipe.

        Both fresh -> 50/50 weighted average (VWAP captures dynamics the
                      static book mid misses; book mid anchors VWAP when
                      the tape is sparse).
        Only book mid -> book mid (v4 behavior).
        Only VWAP     -> VWAP (book stale / one-sided).
        Neither       -> None.
        """
        book_mid = self._market_mid_from_book()
        vwap = self._trade_vwap(window=30.0)
        if book_mid is not None and vwap is not None:
            return 0.5 * book_mid + 0.5 * vwap
        if vwap is not None:
            return vwap
        return book_mid

    def reconcile_position(self) -> None:
        try:
            self.position = int(self.c.positions()["positions"].get(self.symbol, 0))
        except Exception as e:
            print(f"reconcile_position failed: {e}")

    def desired_quotes(self) -> tuple[Optional[int], Optional[int], float, float]:
        fair, sigma = self.fair_and_sigma()
        # v4: clamp fair to informed-trader truth bounds for all
        # downstream pricing math. Posterior `fair`/`sigma` returned to
        # caller stay raw (used for logging and the snipe-disagreement
        # gate which compares to market_mid, not truth).
        fair_eff = self._effective_fair(fair)
        # v2: hard K=0 gate -- pre-reveal we have no info; do not quote.
        # Belt-and-suspenders with the step()/on_quote_event K=0 short-circuits.
        if self.skip_pre_reveal and len(self.posterior.reveals) == 0:
            return None, None, fair, sigma
        if len(self.posterior.reveals) < self.min_reveals_to_quote:
            return None, None, fair, sigma
        # All reveals in -- fair is the exact settle, not a theoretical.
        # Don't market-make on it (we'd give away edge for no informational gain).
        if self._n_remaining() == 0 and not self.quote_after_final_reveal:
            return None, None, fair, sigma

        # v3: no-counterparty gate. Market making earns its spread by
        # intermediating other participants -- if the public book is
        # empty or only shows our own resting prices, there's nothing
        # to make a market against. Refreshes book lazily. Sniping is
        # unaffected (handled in on_quote_event). Most relevant at the
        # very start of a round before other bots have warmed up.
        if not self._other_makers_present():
            return None, None, fair, sigma

        pre_reveal = len(self.posterior.reveals) == 0

        # Pre-reveal warmup: don't quote in the first pre_reveal_warmup_sec
        # after phase=running. Lets the market form before we offer
        # liquidity (avoids the cold-start pickoff where the book is thin
        # and our first quote gets swept by faster bots within ~0.5s).
        if pre_reveal and self._phase_running_t is not None:
            elapsed = time.time() - self._phase_running_t
            if elapsed < self.pre_reveal_warmup_sec:
                return None, None, fair, sigma

        # Pre-reveal HARD safety: require a fresh book cache. Without
        # any market signal the prior-only fair becomes an unchecked
        # anchor -- the exact configuration that caused the -5343
        # disaster. If both sides are empty or the cache is stale, skip
        # quoting entirely. One-side-empty IS allowed: when asks are
        # empty we quote ASK extra-wide (empty_side_edge), using the
        # surviving bid as a single-sided tail signal; symmetric when
        # bids are empty. Log analysis: empty-side cases get filled
        # within ~100ms by a walker, who pays the fat premium.
        pre_bids: list = []
        pre_asks: list = []
        if pre_reveal:
            book = self._book_cache
            book_stale = (book is None or
                          time.time() - self._book_cache_t
                          > self._book_cache_max_age_sec)
            if book_stale:
                return None, None, fair, sigma
            pre_bids = book.get("bids") or []
            pre_asks = book.get("asks") or []
            if not pre_bids and not pre_asks:
                return None, None, fair, sigma

        edge = max(self.min_edge, self.edge_per_sigma * sigma)
        # Pre-reveal: enforce a hard wide floor that keeps us out of the
        # market's inside spread. Without it, edge_per_sigma * sigma ~ 6
        # puts our asks INSIDE the typical market bid (around prior fair +
        # 12), where any taker can sweep them.
        if pre_reveal:
            edge = max(edge, self.pre_reveal_min_edge)

        # Skew normally pulls quotes toward flat; pre-reveal it amplifies
        # initial inventory (start short -> ask down -> more shorts), so
        # disable it until we have any information.
        if pre_reveal and self.pre_reveal_skew_disabled:
            skew = 0.0
        else:
            skew = -self.position * self.skew_per_unit

        # v2: per-side extra edge from informed-counterparty strikes. Decays
        # exponentially; recent picks-off widen the side that was hit.
        extra_bid, extra_ask = self._informed_extra_edge()

        bid_px = int(math.floor(fair_eff - edge - extra_bid + skew))
        ask_px = int(math.ceil(fair_eff + edge + extra_ask + skew))

        # Pre-reveal one-side-empty exploit. When ONE side of the book
        # is unfilled, quote the missing side EXTRA wide (empty_side_edge,
        # ~15 ticks instead of the default ~4) so any incoming walker
        # pays a fat premium. The surviving side is still used as a
        # single-sided tail signal: a high lone bid suggests truth is
        # above prior (tail-UP -> pull our ASK); a low lone ask suggests
        # tail-DOWN (pull our BID). When the surviving side is in line
        # with prior, we collect the wide quote profit if a walker hits.
        if pre_reveal:
            if not pre_asks:
                ask_px = int(math.ceil(fair_eff + self.empty_side_edge + skew))
                if (pre_bids and
                        pre_bids[0]["price"] >
                        fair_eff + self.pre_reveal_pull_disagreement):
                    ask_px = None
            elif not pre_bids:
                bid_px = int(math.floor(fair_eff - self.empty_side_edge + skew))
                if (pre_asks and
                        pre_asks[0]["price"] <
                        fair_eff - self.pre_reveal_pull_disagreement):
                    bid_px = None

        # Check posterior vs market consensus BEFORE pennying. When our fair
        # disagrees with market_mid by a lot, our posterior is likely biased
        # (tail-prior round) and the market is closer to truth. The penny
        # floors use OUR fair, so they're biased too -- pennying inside the
        # market in that regime would tighten quotes toward an anchor we
        # don't trust. Skip penny entirely when biased; let the market-mid
        # anchor below pull our quotes outward instead.
        # v5: blend book midpoint with public-trade VWAP. The book mid
        # alone misses fast price discovery in trending rounds; the
        # blended signal catches it.
        market_mid = self._market_signal()
        # v4: use the (possibly clamped) fair against market consensus.
        # The informed-trader bound supersedes the posterior, so
        # disagreement is measured from the clamped fair onward.
        biased = (market_mid is not None and
                  abs(fair_eff - market_mid) >= self.market_anchor_disagreement_min)

        # Penny/dime: opportunistically step inside the market's best when it
        # preserves >= tight_floor_edge per-fill margin AND uncertainty is
        # small enough that the tight quote isn't exposed to a sigma-scale
        # fair shift on the next reveal. Always safe to skip (default quote
        # is still a valid post). Also gated by tight_penny_min_reveals: in
        # a FIFO market, sitting deeper with queue priority often beats
        # competing for top-of-book in the first 1-2 reveals (posterior
        # still adjusting from prior; better to fill less but at our terms).
        if (self.tight_quoting_enabled and sigma <= self.tight_penny_sigma_max
                and not biased
                and len(self.posterior.reveals) >= self.tight_penny_min_reveals):
            bid_px, ask_px = self._apply_penny(bid_px, ask_px, fair_eff, skew)

        # Market-mid anchor: when posterior disagrees with market consensus,
        # widen quotes on whichever side is at risk. This is one-sided safety
        # only -- we MAX the ask up to market_mid + edge (preventing us from
        # selling below truth in a tail-up round) and MIN the bid down to
        # market_mid - edge (preventing us from buying above truth in a
        # tail-down round). Never tightens quotes vs the default.
        if biased:
            safe_bid = int(math.floor(market_mid - edge))
            safe_ask = int(math.ceil(market_mid + edge))
            bid_px = min(bid_px, safe_bid)
            ask_px = max(ask_px, safe_ask)

        # Pre-reveal "pull the dangerous side". The market_mid anchor
        # WIDENS quotes but still leaves a marketable risk: a fast taker
        # walking past our widened ask still hits us at our (high but
        # still-below-truth) ask price. Pull the side entirely when
        # disagreement is large -- gives up the opportunity in non-tail
        # rounds where market and prior happen to disagree, but cuts the
        # tail-round catastrophic loss to zero. Asymmetric: market_mid
        # ABOVE fair suggests tail-UP (pull ASK), BELOW suggests
        # tail-DOWN (pull BID). v4: compares against the informed-clamped
        # fair so an informed bound that already moved fair into the
        # market's regime doesn't trip the pull.
        if pre_reveal and market_mid is not None:
            if market_mid > fair_eff + self.pre_reveal_pull_disagreement:
                ask_px = None
            elif market_mid < fair_eff - self.pre_reveal_pull_disagreement:
                bid_px = None

        if (ask_px is not None and bid_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + self.tick

        # Quote size shrinks in the pre-reveal regime where prior-only sigma
        # is large -- smaller bet per fill until we have info. Side-aware:
        # the inventory dampener may shrink the qty on the adding side.
        bid_qty = self._current_quote_qty("bid")
        ask_qty = self._current_quote_qty("ask")
        # Sigma-scaled position cap: at high sigma, cap inventory below the
        # position_limit. Suppresses the bid/ask that would push us over.
        # `relaxed` reuses the same market-mid-agreement test as the qty
        # picker -- match the regime consistently across cap and qty so the
        # post/modify size aligns with the headroom check.
        relaxed = (market_mid is not None and not biased)
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        if bid_px is not None and self.position + bid_qty > max_pos:
            bid_px = None
        if ask_px is not None and self.position - ask_qty < -max_pos:
            ask_px = None

        # Hard-kill the adverse-side quote when |position| is large -- even
        # 1-lot adverse quotes accumulate when there's heavy passive flow.
        kill_thresh = int(max_pos * self.inv_hard_kill_frac)
        if self.position > kill_thresh and bid_px is not None:
            bid_px = None
        if self.position < -kill_thresh and ask_px is not None:
            ask_px = None

        # v4: informed-trader cool-off. After we trade against the
        # informed bot, the picked-off side sits idle for
        # `informed_cooloff_sec` so we don't immediately re-post into
        # the same one-sided pickoff. The fair clamp above (fair_eff)
        # already ensures any re-post after cool-off respects the
        # truth bound.
        now = time.time()
        if (bid_px is not None and
                now < self._informed_cooloff_until.get("bid", 0.0)):
            bid_px = None
        if (ask_px is not None and
                now < self._informed_cooloff_until.get("ask", 0.0)):
            ask_px = None

        return bid_px, ask_px, fair, sigma

    def _apply_penny(self, bid_px: int, ask_px: int, fair: float,
                     skew: float) -> tuple[int, int]:
        """Step inside the market's best bid/ask when safe and beneficial.

        Uses the book cache populated by maybe_snipe (which fires on every
        throttled quote_event and on every step). Penny only when:
          1. The cache is fresh (else fall through to default).
          2. We are NOT already at-or-better than the market best (else we'd
             undercut ourselves and lose queue priority for no reason).
          3. The stepped-in price is strictly tighter than our default (else
             our default is already at/inside, no benefit).
          4. The stepped-in price preserves tight_floor_edge against
             (fair + skew), so per-fill expected margin stays positive.

        Returns possibly-tightened (bid_px, ask_px). Never widens.
        """
        book = self._book_cache
        if book is None:
            return bid_px, ask_px
        if time.time() - self._book_cache_t > self._book_cache_max_age_sec:
            return bid_px, ask_px

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        step = self.tight_step_inside_ticks * self.tick

        if bids:
            market_best_bid = bids[0]["price"]
            our_bid = self.resting.get("bid")
            already_top = our_bid is not None and our_bid["price"] >= market_best_bid
            if not already_top:
                penny_bid = market_best_bid + step
                max_safe_bid = int(math.floor(fair + skew - self.tight_floor_edge))
                if penny_bid > bid_px and penny_bid <= max_safe_bid:
                    bid_px = penny_bid
                elif market_best_bid > bid_px and market_best_bid <= max_safe_bid:
                    # Stepping inside violates the floor, but JOINING the
                    # inside is still safe. FIFO queue: we're at the back
                    # at this price, but we get filled when the existing
                    # queue clears and someone walks through. Far better
                    # than sitting deeper and missing volume entirely --
                    # which is what was happening in recent logs (top-ask
                    # capture only 7% in one round).
                    bid_px = market_best_bid

        if asks:
            market_best_ask = asks[0]["price"]
            our_ask = self.resting.get("ask")
            already_top = our_ask is not None and our_ask["price"] <= market_best_ask
            if not already_top:
                penny_ask = market_best_ask - step
                min_safe_ask = int(math.ceil(fair + skew + self.tight_floor_edge))
                if penny_ask < ask_px and penny_ask >= min_safe_ask:
                    ask_px = penny_ask
                elif market_best_ask < ask_px and market_best_ask >= min_safe_ask:
                    ask_px = market_best_ask

        return bid_px, ask_px

    def _apply_target_quotes(self, bid_px: Optional[int],
                              ask_px: Optional[int]) -> None:
        """Issue the modify/post/cancel calls to make our resting quotes
        match (bid_px, ask_px). Caller must hold self.lock. _reprice is
        idempotent (no-op when price unchanged) so this is safe to call on
        every quote event without queue churn from unnecessary modifies.

        Order matters to avoid self-cross when both quotes move in the same
        direction. After a reveal, fair can jump; if we reprice bid first
        and new_bid >= our old resting ask, the new buy crosses the old
        sell -> self-trade (server rejects via SMP, sim eats fees). Fix:
        when new_bid >= current_ask, cancel ask first; when new_ask <=
        current_bid, cancel bid first.
        """
        cur_ask = self.resting["ask"]["price"] if self.resting["ask"] else None
        cur_bid = self.resting["bid"]["price"] if self.resting["bid"] else None
        bid_would_cross = (bid_px is not None and cur_ask is not None
                           and bid_px >= cur_ask)
        ask_would_cross = (ask_px is not None and cur_bid is not None
                           and ask_px <= cur_bid)
        if bid_would_cross:
            self._safe_cancel("ask")
            cur_ask = None
        if ask_would_cross:
            self._safe_cancel("bid")
            cur_bid = None
        if bid_px is None:
            self._safe_cancel("bid")
        else:
            self._reprice("bid", bid_px)
        if ask_px is None:
            self._safe_cancel("ask")
        else:
            self._reprice("ask", ask_px)

    # -------- order plumbing --------

    def _record_fill_from_trades(self, side: str, trades: list[dict]) -> int:
        filled = sum(t["qty"] for t in trades)
        if filled:
            self.position += filled if side == "buy" else -filled
            # v3: capture private-feed market signal (IOC response is the
            # most-private path -- this returns before any public book
            # tick reflects the trade). Use the most recent leg's price.
            last_px = None
            for t in trades:
                if t.get("qty"):
                    last_px = t.get("price")
            if last_px is not None:
                self._last_fill_px = float(last_px)
                self._last_fill_t = time.time()
        return filled

    def _safe_cancel(self, side: str) -> None:
        rest = self.resting[side]
        if rest is None:
            return
        try:
            self.c.cancel(rest["order_id"])
        except Exception:
            pass
        self.resting[side] = None

    def _current_quote_qty(self, side: Optional[str] = None) -> int:
        """Active quote size, scaled by posterior uncertainty, market-mid
        agreement, AND (when `side` is given) inventory pressure. _post and
        _reprice both use this so the order on the book reflects the regime
        at the time of the post/modify call.

        Tiers (sigma):
          - K=0 (pre-reveal, prior-only, sigma ~25):       pre_reveal_quote_qty
          - K>=1 but sigma > full_qty_sigma_max (~3):      mid_round_quote_qty[_normal]
          - sigma <= full_qty_sigma_max (posterior tight): quote_qty[_normal]

        Within K>=1: if market_mid is fresh and |fair - market_mid| < 5
        (consensus confirms our fair, "relaxed" regime) use the *_normal
        upsized values; otherwise fall back to the conservative values --
        treats "no signal" the same as "biased" (no confidence to upsize).

        Inventory dampener (`side` provided): when |position| exceeds the
        threshold fraction of the current max_pos, the side that would ADD
        to the position is shrunk by `inv_dampen_factor`. Side="bid" adds
        positive qty (long-building), side="ask" adds negative qty.
        """
        if len(self.posterior.reveals) == 0:
            return self.pre_reveal_quote_qty
        fair, sigma = self.fair_and_sigma()
        # v5: use blended book mid + trade VWAP as the consensus signal.
        market_mid = self._market_signal()
        relaxed = (market_mid is not None and
                   abs(fair - market_mid) <
                   self.market_anchor_disagreement_min)
        if sigma > self.full_qty_sigma_max:
            base = (self.mid_round_quote_qty_normal
                    if relaxed else self.mid_round_quote_qty)
        else:
            base = self.quote_qty_normal if relaxed else self.quote_qty
        if side is not None:
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
            threshold = int(max_pos * self.inv_dampen_threshold_frac)
            adding = (side == "bid" and self.position > threshold) or \
                     (side == "ask" and self.position < -threshold)
            if adding:
                base = max(1, int(round(base * self.inv_dampen_factor)))
        return base

    def _post(self, side: str, price: int) -> None:
        method = self.c.buy if side == "bid" else self.c.sell
        sgn_side = "buy" if side == "bid" else "sell"
        qty = self._current_quote_qty(side)
        try:
            r = method(self.symbol, price=price, qty=qty)
        except Exception as e:
            self.resting[side] = None
            print(f"  post {side} @ {price} failed: {e}")
            return
        o = r["order"]
        self._record_fill_from_trades(sgn_side, r.get("trades", []))
        if o["status"] in ("open", "partial"):
            self.resting[side] = {
                "order_id": o["order_id"],
                "price": o["price"],
                "qty": o["remaining"],
            }
        else:
            self.resting[side] = None

    def _reprice(self, side: str, want_px: int) -> None:
        rest = self.resting[side]
        if rest is None:
            self._post(side, want_px)
            return
        qty = self._current_quote_qty(side)
        # FIFO-priority preservation: at the same price, only allow modifies
        # that keep queue priority (pure qty-down, rest > target). Refills
        # (rest < target) and exact matches (rest == target) get skipped --
        # log analysis showed 700-800/session same-price modifies, each one
        # forfeiting our spot in the queue to "top up" by 1 lot after a
        # partial fill. Letting the residual fill at preserved priority is
        # strictly better; we only post a fresh quote once it's fully filled.
        if rest["price"] == want_px and rest["qty"] <= qty:
            return

        # Per-order debounce: skip modifies that fire too soon after the
        # previous one. Most "fast" modifies were chasing market_mid jitter,
        # not real fair shifts. The fair-shift path (reveal) cancels and
        # reposts via _post, which doesn't go through this debounce.
        order_id = rest["order_id"]
        now = time.time()
        last = self._last_modify_t.get(order_id, 0.0)
        if now - last < self.min_modify_interval_sec:
            return

        # Rate-limit budget guard: when the token bucket is low, skip the
        # modify so a snipe IOC can grab the next token. Snipes are 98.7%
        # profitable in the analyzed logs; maker requotes are not.
        try:
            tokens = self.c.tokens_available()
        except AttributeError:
            tokens = self.tokens_low_threshold + 1.0  # SDK without bucket
        if tokens < self.tokens_low_threshold:
            return

        sgn_side = "buy" if side == "bid" else "sell"
        try:
            res = self.c.modify(order_id, price=want_px, qty=qty)
        except Exception:
            self._safe_cancel(side)
            self._post(side, want_px)
            return
        o = res["order"]
        self._record_fill_from_trades(sgn_side, res.get("trades", []))
        if o["status"] in ("open", "partial"):
            new_id = o["order_id"]
            self.resting[side] = {
                "order_id": new_id,
                "price": o["price"],
                "qty": o["remaining"],
            }
            self._last_modify_t[new_id] = now
            # Old id (if changed) is dead; drop its entry so the dict
            # doesn't grow unbounded over a session.
            if new_id != order_id:
                self._last_modify_t.pop(order_id, None)
        else:
            self.resting[side] = None
            self._last_modify_t.pop(order_id, None)

    # -------- sniping --------

    def maybe_snipe(self, fair: float, sigma: float) -> bool:
        """Take any mispriced level. Returns True if we took anything.

        Skips levels matching our own resting prices (the public book doesn't
        expose owner, so we look at self.resting). Avoids self_match_prevention
        round-trips that the probe showed happening 46-69 times per session.

        Per-snipe size is capped at snipe_max_qty_per_level. Walks deeper
        book (snipe_book_depth) so a single call can hit multiple levels.
        """
        if len(self.posterior.reveals) < self.min_reveals_to_snipe:
            return False
        if self._n_remaining() == 0 and not self.snipe_after_final_reveal:
            return False
        # Hard rate limit -- defense against fee runaway if our edge calc is
        # mid-round wrong. Post-final-reveal sweep is exempted (settle is
        # known, every snipe is risk-free profit).
        if (self._n_remaining() > 0 and
                self._snipe_count_this_round >= self.max_snipes_per_round):
            return False
        try:
            book = self.c.book(self.symbol, depth=self.snipe_book_depth)
            self._book_cache = book  # share with desired_quotes
            self._book_cache_t = time.time()
        except Exception:
            return False

        # v4: use clamped fair for ALL snipe pricing/edge math. Posterior
        # may say `fair`, but an informed-trader fill earlier in the
        # round establishes a one-sided truth bound; ignore the posterior
        # outside that bound or we'd snipe against truth.
        fair_eff = self._effective_fair(fair)

        # Snipe disagreement gate. When fair and market_mid disagree
        # significantly the risk is asymmetric: snipes in the direction
        # AGAINST our conviction are the dangerous ones (we'd be buying
        # at a high market price our biased-low fair calls cheap). Snipes
        # WITH our conviction (selling to a high bid when our fair is
        # below mid) are the user-requested "buy high sell low" capture.
        # Directional mode keeps the safe direction open. Post-final
        # sweep is exempt regardless (fair == settle).
        # v5: market_mid is now the book-mid + trade-VWAP blend, so the
        # disagreement gate fires on actual transaction flow too.
        market_mid = self._market_signal()
        block_buy = False
        block_sell = False
        if self._n_remaining() > 0 and market_mid is not None:
            diff = fair_eff - market_mid
            if abs(diff) > self.snipe_max_disagreement:
                if self.snipe_disagreement_directional:
                    # Block the snipe direction that BETS WITH market_mid
                    # over our fair. If fair < mid we trust the lower fair,
                    # so buying at mid-anchored asks is the wrong-side bet.
                    if diff < 0:
                        block_buy = True
                    else:
                        block_sell = True
                else:
                    return False
        if block_buy and block_sell:
            return False

        # Our own resting prices -- skip these in the snipe loop.
        our_ask_px = self.resting["ask"]["price"] if self.resting["ask"] else None
        our_bid_px = self.resting["bid"]["price"] if self.resting["bid"] else None

        if self._n_remaining() == 0:
            # All reveals in: settle is known exactly. Any mispricing > taker_fee
            # is a risk-free profitable trade. No min_edge or sigma buffer --
            # take everything visible. This is the "take a position that is
            # profitable immediately" sweep.
            edge_required_buy = self.taker_fee
            edge_required_sell = self.taker_fee
        else:
            base_edge = self.taker_fee + max(
                self.snipe_min_edge, self.snipe_buffer_sigma * sigma)
            # Inventory-aware snipe edge: when the take would ADD to the
            # existing position (buy while long, sell while short), require
            # the adverse extra edge. Defends against value_sniper traps
            # that feed us bad fills when we're already exposed.
            max_pos_for_threshold = self._max_position_for_sigma(
                sigma, relaxed=(market_mid is not None and
                                abs(fair_eff - market_mid) <
                                self.market_anchor_disagreement_min))
            threshold = int(max_pos_for_threshold * self.inv_dampen_threshold_frac)
            edge_required_buy = base_edge + (
                self.snipe_adverse_extra_edge if self.position > threshold else 0.0)
            edge_required_sell = base_edge + (
                self.snipe_adverse_extra_edge if self.position < -threshold else 0.0)
        # Per-snipe size cap. We have a +/-100 trader position limit; bots
        # don't. Take many small bites instead of one big bite -- leaves
        # headroom for the NEXT snipe at a different level (log analysis
        # showed 50% of bad trades sat >2s; bots repost the same bad quote
        # multiple times). Same cap at low/high sigma; the sigma-scaled
        # position cap (max_pos_for_sigma) bounds total accumulation
        # independently.
        snipe_cap = self.snipe_max_qty_per_level
        # Sigma-scaled inventory cap also applies to sniping. At high sigma
        # we cap how much we'll accumulate from snipes against the same side.
        # Snipes in the "consensus confirms fair" regime use the relaxed
        # cap; in the biased / no-signal regime they use the conservative
        # cap. The disagreement gate above already blocks |diff|>10 snipes
        # entirely; this just controls accumulation in the 0<|diff|<10
        # window vs the |diff|<5 confirmed-normal window.
        relaxed = (market_mid is not None and
                   abs(fair_eff - market_mid) <
                   self.market_anchor_disagreement_min)
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        # v3: snipe IOCs respect a buffer below the hard ±position_limit.
        # We saw |pos|=127 in second_round_loss.jsonl because successive
        # IOCs each computed headroom against possibly-stale position;
        # snipe_max_pos = min(sigma cap, limit - buffer) is the absolute
        # ceiling any snipe leg can push toward.
        snipe_max_pos = min(max_pos,
                            self.position_limit - self.snipe_position_buffer)
        took_any = False

        if not block_buy:
            for level in book.get("asks") or []:
                if our_ask_px is not None and level["price"] == our_ask_px:
                    continue  # don't snipe our own ask
                mispricing = fair_eff - level["price"]
                if mispricing <= edge_required_buy:
                    break
                headroom = snipe_max_pos - self.position
                if headroom <= 0:
                    break
                qty = min(level["qty"], headroom, snipe_cap)
                if qty <= 0:
                    break
                try:
                    res = self.c.buy_ioc(self.symbol, price=level["price"], qty=qty)
                except Exception:
                    break
                filled = self._record_fill_from_trades("buy", res.get("trades", []))
                if filled:
                    took_any = True
                    # Mid-round snipes count against max_snipes_per_round; the
                    # post-final-reveal sweep is exempt (settle is known).
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  SNIPE buy  {filled} @ {level['price']}  fair={fair_eff:.1f}  edge={mispricing:.1f}")
                else:
                    break

        if not block_sell:
            for level in book.get("bids") or []:
                if our_bid_px is not None and level["price"] == our_bid_px:
                    continue  # don't snipe our own bid
                mispricing = level["price"] - fair_eff
                if mispricing <= edge_required_sell:
                    break
                headroom = snipe_max_pos + self.position
                if headroom <= 0:
                    break
                qty = min(level["qty"], headroom, snipe_cap)
                if qty <= 0:
                    break
                try:
                    res = self.c.sell_ioc(self.symbol, price=level["price"], qty=qty)
                except Exception:
                    break
                filled = self._record_fill_from_trades("sell", res.get("trades", []))
                if filled:
                    took_any = True
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  SNIPE sell {filled} @ {level['price']}  fair={fair_eff:.1f}  edge={mispricing:.1f}")
                else:
                    break

        return took_any

    def _try_direct_snipe(self, msg: dict) -> bool:
        """Snipe a freshly-posted quote directly off the WS quote_add event,
        WITHOUT fetching the book first. Returns True if we filled.

        Why: maybe_snipe does book.fetch + IOC = 2 round-trips (~50-100ms).
        Other bots IOC in <20ms and beat us to the mispriced level. This
        path fires one IOC immediately on the WS event, ~5-15ms latency,
        so we win the race against competing snipers for the same level.

        Safety: all the same gates as maybe_snipe -- phase, min_reveals,
        rate cap, own-quote skip, disagreement gate (REQUIRES fresh cache,
        otherwise we skip and let the throttled book-scan handle it).
        Position cap + variance cap still apply.
        """
        side = msg.get("side")
        price = msg.get("price")
        qty_avail = msg.get("qty")
        if side not in ("buy", "sell") or price is None or not qty_avail:
            return False

        with self.lock:
            if self.phase != "running":
                return False
            if len(self.posterior.reveals) < self.min_reveals_to_snipe:
                return False
            if self._n_remaining() == 0 and not self.snipe_after_final_reveal:
                return False
            if (self._n_remaining() > 0 and
                    self._snipe_count_this_round >= self.max_snipes_per_round):
                return False

            # Skip our own quotes -- WS broadcasts quote_add for everyone
            # including us. self_match_prevention would reject anyway, but
            # this saves the round-trip.
            our_rest = self.resting.get("bid" if side == "buy" else "ask")
            if our_rest is not None and our_rest["price"] == price:
                return False

            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return False

            # v4: clamp to informed-trader truth bounds for the edge math.
            fair_eff = self._effective_fair(fair)

            # Disagreement gate -- here we REQUIRE a fresh signal because
            # direct snipe bypasses the book.fetch in maybe_snipe. If
            # neither the book cache nor the trade tape is fresh we have
            # no consensus signal, so defer to the throttled path (which
            # will refetch). Directional mode (matches maybe_snipe) lets
            # the with-conviction side through so we can capture fat
            # bids when market_mid is biased high.
            # v5: _market_signal blends book mid with trade VWAP.
            market_mid = self._market_signal()
            if self._n_remaining() > 0:
                if market_mid is None:
                    return False
                diff = fair_eff - market_mid
                if abs(diff) > self.snipe_max_disagreement:
                    if not self.snipe_disagreement_directional:
                        return False
                    # side=="buy" means a quote_add bid -- we'd SELL to it.
                    # side=="sell" means an ask -- we'd BUY from it.
                    # diff<0 (fair below mid) blocks our BUY snipes (against
                    # conviction); diff>0 blocks SELL snipes.
                    if diff < 0 and side == "sell":
                        return False
                    if diff > 0 and side == "buy":
                        return False

            snipe_cap = self.snipe_max_qty_per_level
            relaxed = (market_mid is not None and
                       abs(fair_eff - market_mid) <
                       self.market_anchor_disagreement_min)
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
            # v3: same buffer as maybe_snipe -- direct IOC must leave
            # snipe_position_buffer lots below the hard ±position_limit.
            snipe_max_pos = min(
                max_pos,
                self.position_limit - self.snipe_position_buffer)
            threshold = int(max_pos * self.inv_dampen_threshold_frac)

            if self._n_remaining() == 0:
                edge_required_buy = self.taker_fee
                edge_required_sell = self.taker_fee
            else:
                base_edge = (self.taker_fee +
                             max(self.snipe_min_edge,
                                 self.snipe_buffer_sigma * sigma))
                edge_required_buy = base_edge + (
                    self.snipe_adverse_extra_edge
                    if self.position > threshold else 0.0)
                edge_required_sell = base_edge + (
                    self.snipe_adverse_extra_edge
                    if self.position < -threshold else 0.0)

            if side == "buy":
                # They posted a BID at `price`. If price > fair + edge they
                # are buying high -- sell to them via IOC at their price.
                mispricing = price - fair_eff
                if mispricing <= edge_required_sell:
                    return False
                headroom = snipe_max_pos + self.position
                if headroom <= 0:
                    return False
                qty = min(qty_avail, headroom, snipe_cap)
                if qty <= 0:
                    return False
                try:
                    res = self.c.sell_ioc(self.symbol, price=price, qty=qty)
                except Exception:
                    return False
                filled = self._record_fill_from_trades("sell",
                                                      res.get("trades", []))
                if filled:
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  DIRECT-SNIPE sell {filled} @ {price}  "
                          f"fair={fair_eff:.1f}  edge={mispricing:.1f}")
                return bool(filled)
            else:  # side == "sell"
                # They posted an ASK at `price`. If price < fair - edge they
                # are selling low -- buy from them via IOC at their price.
                mispricing = fair_eff - price
                if mispricing <= edge_required_buy:
                    return False
                headroom = snipe_max_pos - self.position
                if headroom <= 0:
                    return False
                qty = min(qty_avail, headroom, snipe_cap)
                if qty <= 0:
                    return False
                try:
                    res = self.c.buy_ioc(self.symbol, price=price, qty=qty)
                except Exception:
                    return False
                filled = self._record_fill_from_trades("buy",
                                                      res.get("trades", []))
                if filled:
                    if self._n_remaining() > 0:
                        self._snipe_count_this_round += 1
                    print(f"  DIRECT-SNIPE buy  {filled} @ {price}  "
                          f"fair={fair_eff:.1f}  edge={mispricing:.1f}")
                return bool(filled)

    # -------- top-level step --------

    def step(self, *, reconcile: bool = False) -> None:
        with self.lock:
            # self.phase is kept in sync by on_phase_change (WS-driven). Hitting
            # game_state() REST here adds ~30-50ms per step on the hot path
            # (called on every fill, reveal, status). Trust the cached value.
            phase = self.phase
            if phase != "running":
                if phase == "settled":
                    self.resting = {"bid": None, "ask": None}
                return

            # v2: K=0 is silent. No quote, no snipe, no book fetch.
            if self.skip_pre_reveal and len(self.posterior.reveals) == 0:
                if reconcile:
                    self.reconcile_position()
                return

            if reconcile:
                self.reconcile_position()

            # Snipe first so the book cache is fresh for the penny logic
            # inside desired_quotes. maybe_snipe also refreshes _book_cache.
            fair, sigma = self.fair_and_sigma()
            self.maybe_snipe(fair, sigma)

            bid_px, ask_px, fair, sigma = self.desired_quotes()
            self._apply_target_quotes(bid_px, ask_px)
            self._last_maker_apply_t = time.time()  # v2: cadence timer

            print(
                f"QUOTE  fv={fair:6.1f} +/-{sigma:4.1f}  pos={self.position:+4d}  "
                f"bid={bid_px}  ask={ask_px}  k={len(self.posterior.reveals)}/{self.n_total}"
            )

    # -------- event handlers --------

    def on_reveal(self, value: float) -> None:
        with self.lock:
            # Pull ALL outstanding orders BEFORE the posterior shifts. Other
            # bots see the same reveal we do, and the brief window between
            # posterior.update and the step()->reprice that follows is
            # exactly when a faster taker can lift our stale-priced order.
            # cancel_all is preferred over two _safe_cancel calls: one REST
            # round-trip instead of two (smaller adverse-pickoff window),
            # and it sweeps any straggler order we might not be tracking
            # locally. We re-post at the new fair inside step() below;
            # the cost is queue priority, the gain is no adverse-pickoff
            # window. Pre-reveal -> K=1 is the worst case (fair can shift
            # ~sigma=25 on the first reveal), so this matters most then.
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.posterior.update(value)
            self._posterior_gen += 1  # invalidate fair_and_sigma cache
            # v2: force an immediate maker reprice after each reveal -- the
            # 4-12s cadence timer doesn't gate the post-reveal repost.
            self._last_maker_apply_t = 0.0
            k = len(self.posterior.reveals)
            running = sum(self.posterior.reveals)
            is_final = (k >= self.n_total)
            if is_final:
                # All info is in: fair == settle exactly. Quotes were already
                # cancelled above; just log the transition. step() below will
                # honor quote_after_final_reveal and skip reposting.
                print(f"FINAL  all {k}/{self.n_total} reveals in. "
                      f"settle={int(running)}  passive quotes -> cancel; sweeping book.")
        self.step(reconcile=True)
        if is_final and self.snipe_after_final_reveal:
            # Sweep the book of any profitable level. Settle is known, so
            # every mispricing > taker_fee is risk-free. Keep calling
            # maybe_snipe until it stops finding things.
            self._sweep_at_settle(running_sum=float(sum(self.posterior.reveals)))

    def _sweep_at_settle(self, running_sum: float) -> None:
        """Take a position that is profitable immediately at the known settle.

        Calls maybe_snipe in a loop until no more profitable levels remain or
        we hit the position limit. Each call walks snipe_book_depth levels;
        looping handles the case where someone reposts a mispriced quote in
        the brief window after we sweep.
        """
        for sweep_pass in range(6):  # ~600ms worth of attempts at 100ms book ttl
            with self.lock:
                took = self.maybe_snipe(running_sum, 0.0)
            if not took:
                return
            # Tiny pause so we don't slam REST. Other bots may repost; we
            # come back around to grab the new mispriced level.
            time.sleep(0.05)

    def on_fill_event(self, msg: dict) -> None:
        # Two paths:
        #  - Matched: WS fill belongs to one of our resting orders. Update
        #    self.position locally from the fill msg and skip the positions()
        #    REST. Saves ~30-50ms in the post-fill reprice path; matters more
        #    when fills cluster (a 10-lot quote getting partial-walked).
        #  - Unmatched: WS fill is for an IOC (snipe) we sent — we already
        #    updated self.position synchronously via _record_fill_from_trades
        #    on the IOC response — OR a fill arrived before self.resting
        #    caught up after a modify. Keep reconcile=True as a safety net.
        matched = False
        with self.lock:
            side = msg["side"]
            qty = msg["qty"]
            order_id = msg["order_id"]
            # v3: private-feed market signal. WS fill is the freshest
            # transaction price available -- ahead of the public book
            # tick. Always capture, regardless of liquidity side.
            fill_px = msg.get("price")
            if fill_px is not None:
                self._last_fill_px = float(fill_px)
                self._last_fill_t = time.time()
            # v2: counterparty-strike accounting. Only maker fills carry a
            # meaningful counterparty (the other side is who hit us). For
            # IOC fills we initiated, the "counterparty" is the resting
            # maker, which is informational but doesn't represent
            # adverse-selection in the same way -- still credit it since
            # the strike sign is correct (an informed maker we tried to
            # snipe was still mispriced for us to take, neutral).
            # v5: per-CP signed net flow vs us this round. Updated for
            # every fill (maker or taker) -- the flow accumulates across
            # both liq sides because the CP's REAL inventory shifts
            # regardless of who initiated. side is OUR side: when we
            # buy, the CP sold to us so flow decreases by qty; when we
            # sell, the CP bought from us so flow increases.
            cp_id = msg.get("counterparty")
            if cp_id:
                if side == "buy":
                    self._cp_net_flow[cp_id] -= qty
                elif side == "sell":
                    self._cp_net_flow[cp_id] += qty
            if msg.get("liquidity") == "maker":
                self._record_informed_fill(msg.get("counterparty"), side)
            # v4: informed-trader handler. Runs for BOTH maker and taker
            # liq, because the truth bound is established by the fill
            # itself regardless of who initiated. Maker fills are the
            # primary signal (informed actively chose to lift us); taker
            # fills mean we IOC'd into an informed maker -- also bound-
            # establishing, just less common.
            self._handle_informed_fill(msg)
            for key, expected_side in (("bid", "buy"), ("ask", "sell")):
                rest = self.resting[key]
                if rest is not None and rest["order_id"] == order_id and side == expected_side:
                    rest["qty"] -= qty
                    if side == "buy":
                        self.position += qty
                    else:
                        self.position -= qty
                    if rest["qty"] <= 0:
                        self.resting[key] = None
                    matched = True
                    break
        self.step(reconcile=not matched)

    def on_quote_event(self, msg: dict) -> None:
        """React to a new (or cancelled) quote on the book.

        v2 split:
          1. SNIPE PATH (unthrottled): on every quote_add, run the direct
             IOC snipe path. Snipes are the signal -- never throttled.
          2. SNIPE BOOK-SCAN (50 ms throttle): periodic maybe_snipe call to
             catch any level the direct path missed (e.g., cache stale on
             arrival). Still fast.
          3. MAKER REPRICE (phase-based, 4-12 s): only attempt to reprice
             our resting maker quotes at the confidence-gated cadence.
             Reveal arrival force-resets the timer (see on_reveal).

        Pre-reveal (K=0): all three paths short-circuit. v2 sits out the
        unmoderated regime entirely.
        """
        # K=0 hard gate: no snipes, no maker work, no book fetches.
        if self.skip_pre_reveal and len(self.posterior.reveals) == 0:
            return
        if self.phase != "running":
            return

        # Path 1: direct snipe on every quote_add. The mispricing gate
        # naturally rate-limits -- most quote_adds are at normal prices.
        if msg.get("type") == "quote_add":
            self._try_direct_snipe(msg)

        now = time.time()
        # Path 2: throttled book scan for fallback snipes.
        do_book_scan = (now - self._last_quote_event_t >=
                        self.quote_event_throttle_sec)
        # Path 3: confidence-gated maker reprice.
        do_maker_reprice = (now - self._last_maker_apply_t >=
                            self._maker_reprice_interval())

        if not do_book_scan and not do_maker_reprice:
            return

        if do_book_scan:
            self._last_quote_event_t = now

        with self.lock:
            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return
            if do_book_scan and len(self.posterior.reveals) >= self.min_reveals_to_snipe:
                # maybe_snipe refreshes _book_cache as a side effect; the
                # maker reprice below relies on a fresh cache for penny.
                self.maybe_snipe(fair, sigma)
            if do_maker_reprice and len(self.posterior.reveals) >= self.min_reveals_to_quote:
                bid_px, ask_px, _, _ = self.desired_quotes()
                self._apply_target_quotes(bid_px, ask_px)
                self._last_maker_apply_t = now

    def on_phase_change(self, phase: Optional[str], reveals: list[float]) -> None:
        with self.lock:
            self.phase = phase
            if phase == "running":
                # CRITICAL: cancel any orders left over from a previous round.
                # The local self.resting dict is only our handle list -- the server
                # may still have stale orders on the book that will be lifted
                # the instant the new round starts (most common cause of
                # "instant +-position_limit at game start").
                try:
                    self.c.cancel_all()
                    print("CANCEL_ALL on phase->running")
                except Exception as e:
                    print(f"cancel_all on phase change failed: {e}")
                self.posterior.reset(reveals)
                self._posterior_gen += 1  # invalidate fair_and_sigma cache
                self.resting = {"bid": None, "ask": None}
                # Reset the per-round snipe cap counter. Without this, the
                # cap accumulates across rounds and eventually blocks ALL
                # future snipes for the rest of the session.
                self._snipe_count_this_round = 0
                # v2: reset strike counters and cadence timer at each round.
                # Strikes are within-round informational signal; carrying
                # them across rounds would conflate counterparty regimes.
                self._informed_strikes_bid = 0.0
                self._informed_strikes_ask = 0.0
                self._informed_strikes_last_t = time.time()
                self._last_maker_apply_t = 0.0
                # v4: clear informed-trader state. Truth bounds + cool-off
                # only apply within the round that produced them; the
                # next round has new (a, w) and no prior fills.
                self._informed_cooloff_until = {"bid": 0.0, "ask": 0.0}
                self._informed_last_t = 0.0
                self._informed_last_their_side = None
                self._informed_last_px = None
                self._informed_fair_lo = None
                self._informed_fair_hi = None
                self._informed_total_qty = 0
                self._informed_fill_count = 0
                # v5: trade tape and per-CP flow are per-round. CP
                # positions reset at each round, so flow must too; the
                # trade tape from the prior round is stale because (a,w)
                # changed.
                self._recent_trades = []
                self._cp_net_flow = defaultdict(int)
                # Mark phase->running so desired_quotes can enforce a
                # pre_reveal_warmup_sec delay before posting the first
                # pre-reveal quotes (cold-start pickoff defense).
                self._phase_running_t = time.time()
                self.reconcile_position()
        if phase == "running":
            self.step()

    def flatten(self) -> None:
        """Emergency: cancel all and flatten to zero via market orders."""
        with self.lock:
            try:
                self.c.cancel_all()
            except Exception:
                pass
            self.resting = {"bid": None, "ask": None}
            self.reconcile_position()
            pos = self.position
            if pos > 0:
                try:
                    res = self.c.sell_market(self.symbol, qty=pos)
                    self._record_fill_from_trades("sell", res.get("trades", []))
                except Exception as e:
                    print(f"flatten sell failed: {e}")
            elif pos < 0:
                try:
                    res = self.c.buy_market(self.symbol, qty=-pos)
                    self._record_fill_from_trades("buy", res.get("trades", []))
                except Exception as e:
                    print(f"flatten buy failed: {e}")
            print(f"FLAT  pos={self.position}")


# ---------------------------------------------------------------------------
# Inspect helpers (formerly notebook cells)
# ---------------------------------------------------------------------------
def print_status(strat: Strategy, c: GameClient) -> None:
    fair, sigma = strat.fair_and_sigma()
    bid_px, ask_px, _, _ = strat.desired_quotes()
    print("-" * 60)
    print(f"phase          : {c.game_state().get('phase')}")
    print(f"reveals so far : {strat.posterior.reveals}")
    print(f"fair value     : {fair:.2f}  +/- {sigma:.2f}")
    print(f"local position : {strat.position}")
    try:
        print(f"server position: {c.positions()}")
        print(f"open orders    : {c.my_orders()}")
        print(f"book           : {c.book(strat.symbol)}")
    except Exception as e:
        print(f"(server query failed: {e})")
    print(f"desired quotes : bid={bid_px}  ask={ask_px}")
    print(f"resting        : {strat.resting}")
    print("-" * 60)


# ---------------------------------------------------------------------------
# Main: wire up WS feed and run until interrupted
# ---------------------------------------------------------------------------
def main() -> None:
    c = GameClient(URL, API_KEY)
    print(f"Connected. game_state = {c.game_state()}")

    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    post = Posterior()
    print(f"Prior support has {len(post.prior)} distinct (a, w) pairs.")
    mean0, std0 = post.predict_settle(running_sum=0, n_remaining=10)
    print(f"Prior settle estimate (N=10, k=0): {mean0:.1f} +/- {std0:.1f}")

    strat = Strategy(c, post, symbol="A")

    def on_reveal(msg):
        print(f"REVEAL #{msg['index']} = {msg['value']}  running_sum={msg['running_sum']}")
        strat.on_reveal(msg["value"])

    def on_fill(msg):
        print(
            f"FILL   {msg['side']:>4s} {msg['qty']} @ {msg['price']}  "
            f"liq={msg.get('liquidity')} vs {msg.get('counterparty')}"
        )
        strat.on_fill_event(msg)

    def on_trade(msg):
        pass  # public tape; ignored

    def on_book(msg):
        pass  # too noisy to step on every delta; snipes happen on reveal/fill

    def on_game_state(msg):
        phase = msg.get("phase")
        reveals = msg.get("reveals") or []
        print(f"STATE  phase={phase}  reveals={len(reveals)}")
        strat.on_phase_change(phase, reveals)

    def on_settlement(msg):
        print(f"SETTLE prices={msg.get('prices')}  pnl={msg.get('pnl')}")

    def on_ack(msg):
        pass

    def on_message(msg):
        # The SDK's catch-all sees raw events INCLUDING quote_add / quote_cancel
        # (which the typed handlers don't dispatch). These are the per-order book
        # delta stream -- when a fresh quote shows up that's mispriced relative
        # to our fair, we want to snipe it BEFORE the posting bot reprices.
        t = msg.get("type")
        if t in ("quote_add", "quote_cancel"):
            strat.on_quote_event(msg)

    c.on_reveal = on_reveal
    c.on_fill = on_fill
    c.on_trade = on_trade
    c.on_book = on_book
    c.on_game_state = on_game_state
    c.on_settlement = on_settlement
    c.on_ack = on_ack
    c.on_message = on_message

    c.start()

    if c.game_state().get("phase") == "running":
        strat.step(reconcile=True)

    print("\nBot started.")
    print("Commands: 's' = status, 'f' = flatten now, 'q' = quit (auto-flattens), Ctrl-C to quit.\n")

    try:
        while True:
            try:
                cmd = input().strip().lower()
            except EOFError:
                # Detached stdin (e.g. running under nohup) -- just sleep forever.
                while True:
                    time.sleep(60)
            if cmd == "s":
                print_status(strat, c)
            elif cmd == "f":
                strat.flatten()
            elif cmd in ("q", "quit", "exit"):
                break
            elif cmd == "":
                continue
            else:
                print(f"unknown command {cmd!r}; try 's', 'f', 'q'")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Flattening and exiting...")
        try:
            strat.flatten()
        except Exception as e:
            print(f"flatten on exit failed: {e}")


if __name__ == "__main__":
    main()
