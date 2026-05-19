"""
Day 1 live trading strategy.

Game model (handout): settlement = sum of N draws X_i ~ Uniform(a, b),
where (a, w = b-a) are drawn once at the start of each round from two
LogNormal-floored priors and never change for that round. We see one X_i
every reveal_interval seconds.

Strategy:

  1. Posterior over (a, w): discrete grid built from the prior; Bayes update
     on each reveal with uniform likelihood plus the support constraint
     a <= x <= a+w. Exposes predict_settle(running_sum, n_remaining) using
     law-of-total-variance to combine within-round draw variance and
     posterior uncertainty over (a, w).
  2. Fair value F and uncertainty sigma drive both quoting and sniping.
     - Passive quotes: bid at F - edge + skew, ask at F + edge + skew, where
       edge scales with sigma and skew is linear in inventory.
     - Pre-reveal quoting: with min_reveals_to_quote=0 we make a market on
       the prior-only fair (~45) BUT defensively, since the market consensus
       (~55-60) and the prior typically disagree by 10+ ticks and we have
       no info to choose between them. Defenses: pre_reveal_min_edge=10
       hard floor (keeps us out of the market's inside spread),
       pre_reveal_quote_qty=1 (cap inventory accumulation), skew disabled
       (so initial inventory doesn't pull our quotes into the market and
       amplify position), pre_reveal_warmup_sec=2.0 delay (lets the market
       form before we offer liquidity, defeats cold-start pickoff).
     - Penny/dime: when the market's best is between our default and the
       per-fill safety floor (tight_floor_edge), step inside by 1 tick.
       Skip if we are already at/inside the market's best (preserves queue
       priority) OR if sigma > tight_penny_sigma_max (high uncertainty
       means pennying inside the market would expose us to a sigma-scale
       fair shift on the next reveal). Reacts to quote_add/quote_cancel
       events within ~100ms.
     - Snipes any quote whose mispricing vs F exceeds taker_fee + buffer * sigma.
  3. Safety guards (added to prevent the "instant -100" failure mode):
     - cancel_all on the server at every phase->running transition (stale
       resting orders from the previous round get auto-lifted otherwise).
     - Cancel passive quotes BEFORE applying each reveal to the posterior:
       eliminates the brief window in which a faster taker could lift a
       stale-priced order at the OLD fair while we're still processing
       the new reveal. Costs queue priority on each reveal; gains immunity
       to adverse pickoff at the fair-shift moment.
     - No sniping until we have at least one reveal in this round
       (prior-only fair is too noisy to identify mispricings).
     - Hard per-round snipe cap so even a wrong edge calc can't run away in fees.

Run:
    python day1/strategy.py
While running: type 's'+Enter for status, 'f'+Enter to flatten, 'q'+Enter to quit.
Ctrl-C also flattens and exits cleanly.
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
    def __init__(self, client: GameClient, posterior: Posterior, symbol: str = "A"):
        self.c = client
        self.symbol = symbol
        self.posterior = posterior
        self.lock = threading.RLock()

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
        self.min_reveals_to_quote = 0
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
        self.snipe_max_disagreement = 10.0
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
        # Post-final-reveal behavior: when n_remaining == 0, fair is no longer
        # theoretical -- it's the exact settlement (sigma == 0). At that point:
        #   - quoting passive spread on a known value gives away free edge to
        #     anyone who hits us at fair-edge (and we have no informational
        #     advantage anymore), so cancel quotes.
        #   - sniping is still pure profit -- any visible mispricing is someone
        #     ELSE making a mistake on a value we both know, so keep snipping.
        self.quote_after_final_reveal = False
        self.snipe_after_final_reveal = True
        # ----------------------------

    # -------- helpers --------

    def _running_sum(self) -> float:
        return sum(self.posterior.reveals)

    def _n_remaining(self) -> int:
        return max(self.n_total - len(self.posterior.reveals), 0)

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

    def _market_mid_from_book(self) -> Optional[float]:
        """Best-bid/best-ask midpoint from the cached book, or None if the
        cache is missing/stale/one-sided. Used by the market-anchor logic
        in desired_quotes and the snipe-disagreement gate in maybe_snipe.
        Returns None silently when no sane mid is available -- callers
        treat that as 'no disagreement signal, proceed normally'."""
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

    def reconcile_position(self) -> None:
        try:
            self.position = int(self.c.positions()["positions"].get(self.symbol, 0))
        except Exception as e:
            print(f"reconcile_position failed: {e}")

    def desired_quotes(self) -> tuple[Optional[int], Optional[int], float, float]:
        fair, sigma = self.fair_and_sigma()
        if len(self.posterior.reveals) < self.min_reveals_to_quote:
            return None, None, fair, sigma
        # All reveals in -- fair is the exact settle, not a theoretical.
        # Don't market-make on it (we'd give away edge for no informational gain).
        if self._n_remaining() == 0 and not self.quote_after_final_reveal:
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

        bid_px = int(math.floor(fair - edge + skew))
        ask_px = int(math.ceil(fair + edge + skew))

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
                ask_px = int(math.ceil(fair + self.empty_side_edge + skew))
                if (pre_bids and
                        pre_bids[0]["price"] >
                        fair + self.pre_reveal_pull_disagreement):
                    ask_px = None
            elif not pre_bids:
                bid_px = int(math.floor(fair - self.empty_side_edge + skew))
                if (pre_asks and
                        pre_asks[0]["price"] <
                        fair - self.pre_reveal_pull_disagreement):
                    bid_px = None

        # Check posterior vs market consensus BEFORE pennying. When our fair
        # disagrees with market_mid by a lot, our posterior is likely biased
        # (tail-prior round) and the market is closer to truth. The penny
        # floors use OUR fair, so they're biased too -- pennying inside the
        # market in that regime would tighten quotes toward an anchor we
        # don't trust. Skip penny entirely when biased; let the market-mid
        # anchor below pull our quotes outward instead.
        market_mid = self._market_mid_from_book()
        biased = (market_mid is not None and
                  abs(fair - market_mid) >= self.market_anchor_disagreement_min)

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
            bid_px, ask_px = self._apply_penny(bid_px, ask_px, fair, skew)

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
        # tail-DOWN (pull BID).
        if pre_reveal and market_mid is not None:
            if market_mid > fair + self.pre_reveal_pull_disagreement:
                ask_px = None
            elif market_mid < fair - self.pre_reveal_pull_disagreement:
                bid_px = None

        if (ask_px is not None and bid_px is not None
                and ask_px <= bid_px):
            ask_px = bid_px + self.tick

        # Quote size shrinks in the pre-reveal regime where prior-only sigma
        # is large -- smaller bet per fill until we have info.
        qty = self._current_quote_qty()
        # Sigma-scaled position cap: at high sigma, cap inventory below the
        # position_limit. Suppresses the bid/ask that would push us over.
        # `relaxed` reuses the same market-mid-agreement test as the qty
        # picker -- match the regime consistently across cap and qty so the
        # post/modify size aligns with the headroom check.
        relaxed = (market_mid is not None and not biased)
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        if bid_px is not None and self.position + qty > max_pos:
            bid_px = None
        if ask_px is not None and self.position - qty < -max_pos:
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
        """
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

    def _current_quote_qty(self) -> int:
        """Active quote size, scaled by posterior uncertainty AND market-mid
        agreement. _post and _reprice both use this so the order on the book
        reflects the regime at the time of the post/modify call.

        Tiers (sigma):
          - K=0 (pre-reveal, prior-only, sigma ~25):       pre_reveal_quote_qty
          - K>=1 but sigma > full_qty_sigma_max (~3):      mid_round_quote_qty[_normal]
          - sigma <= full_qty_sigma_max (posterior tight): quote_qty[_normal]

        Within K>=1: if market_mid is fresh and |fair - market_mid| < 5
        (consensus confirms our fair, "relaxed" regime) use the *_normal
        upsized values; otherwise fall back to the conservative values --
        treats "no signal" the same as "biased" (no confidence to upsize).

        Sigma drops monotonically as reveals accumulate, so qty only grows
        within a round -- each upward step costs one modify (queue priority),
        but happens at most twice (1->2->5) so the priority cost is bounded.
        """
        if len(self.posterior.reveals) == 0:
            return self.pre_reveal_quote_qty
        fair, sigma = self.fair_and_sigma()
        market_mid = self._market_mid_from_book()
        relaxed = (market_mid is not None and
                   abs(fair - market_mid) <
                   self.market_anchor_disagreement_min)
        if sigma > self.full_qty_sigma_max:
            return (self.mid_round_quote_qty_normal
                    if relaxed else self.mid_round_quote_qty)
        return self.quote_qty_normal if relaxed else self.quote_qty

    def _post(self, side: str, price: int) -> None:
        method = self.c.buy if side == "bid" else self.c.sell
        sgn_side = "buy" if side == "bid" else "sell"
        qty = self._current_quote_qty()
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
        qty = self._current_quote_qty()
        if rest["price"] == want_px and rest["qty"] == qty:
            return  # leave it alone -- keep queue priority
        sgn_side = "buy" if side == "bid" else "sell"
        try:
            res = self.c.modify(rest["order_id"], price=want_px, qty=qty)
        except Exception:
            self._safe_cancel(side)
            self._post(side, want_px)
            return
        o = res["order"]
        self._record_fill_from_trades(sgn_side, res.get("trades", []))
        if o["status"] in ("open", "partial"):
            self.resting[side] = {
                "order_id": o["order_id"],
                "price": o["price"],
                "qty": o["remaining"],
            }
        else:
            self.resting[side] = None

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

        # Snipe disagreement gate: if our fair and the market_mid are far apart
        # (tail-prior regime), our edge calc is based on a likely-biased fair.
        # Sniping in that regime can fire snipes in the wrong direction --
        # e.g. our biased-low fair would say all the market's offers are
        # mispriced cheap, and we'd buy a stack of contracts at market prices
        # that are actually close to truth. Skip sniping entirely until the
        # posterior catches up. Exempt the post-final-reveal sweep: fair ==
        # settle exactly there, so disagreement IS profit.
        market_mid = self._market_mid_from_book()
        if self._n_remaining() > 0:
            if (market_mid is not None and
                    abs(fair - market_mid) > self.snipe_max_disagreement):
                return False

        # Our own resting prices -- skip these in the snipe loop.
        our_ask_px = self.resting["ask"]["price"] if self.resting["ask"] else None
        our_bid_px = self.resting["bid"]["price"] if self.resting["bid"] else None

        if self._n_remaining() == 0:
            # All reveals in: settle is known exactly. Any mispricing > taker_fee
            # is a risk-free profitable trade. No min_edge or sigma buffer --
            # take everything visible. This is the "take a position that is
            # profitable immediately" sweep.
            edge_required = self.taker_fee
        else:
            edge_required = self.taker_fee + max(self.snipe_min_edge, self.snipe_buffer_sigma * sigma)
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
                   abs(fair - market_mid) <
                   self.market_anchor_disagreement_min)
        max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)
        took_any = False

        for level in book.get("asks") or []:
            if our_ask_px is not None and level["price"] == our_ask_px:
                continue  # don't snipe our own ask
            mispricing = fair - level["price"]
            if mispricing <= edge_required:
                break
            headroom = max_pos - self.position
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
                print(f"  SNIPE buy  {filled} @ {level['price']}  fair={fair:.1f}  edge={mispricing:.1f}")
            else:
                break

        for level in book.get("bids") or []:
            if our_bid_px is not None and level["price"] == our_bid_px:
                continue  # don't snipe our own bid
            mispricing = level["price"] - fair
            if mispricing <= edge_required:
                break
            headroom = max_pos + self.position
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
                print(f"  SNIPE sell {filled} @ {level['price']}  fair={fair:.1f}  edge={mispricing:.1f}")
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

            # Disagreement gate -- but here we REQUIRE a fresh book cache,
            # because direct snipe bypasses the book.fetch in maybe_snipe.
            # If cache is stale we have no disagreement signal, so defer
            # to the throttled path (which will refetch).
            market_mid = self._market_mid_from_book()
            if self._n_remaining() > 0:
                if market_mid is None:
                    return False
                if abs(fair - market_mid) > self.snipe_max_disagreement:
                    return False

            if self._n_remaining() == 0:
                edge_required = self.taker_fee
            else:
                edge_required = (self.taker_fee +
                                 max(self.snipe_min_edge,
                                     self.snipe_buffer_sigma * sigma))

            snipe_cap = self.snipe_max_qty_per_level
            relaxed = (market_mid is not None and
                       abs(fair - market_mid) <
                       self.market_anchor_disagreement_min)
            max_pos = self._max_position_for_sigma(sigma, relaxed=relaxed)

            if side == "buy":
                # They posted a BID at `price`. If price > fair + edge they
                # are buying high -- sell to them via IOC at their price.
                mispricing = price - fair
                if mispricing <= edge_required:
                    return False
                headroom = max_pos + self.position
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
                          f"fair={fair:.1f}  edge={mispricing:.1f}")
                return bool(filled)
            else:  # side == "sell"
                # They posted an ASK at `price`. If price < fair - edge they
                # are selling low -- buy from them via IOC at their price.
                mispricing = fair - price
                if mispricing <= edge_required:
                    return False
                headroom = max_pos - self.position
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
                          f"fair={fair:.1f}  edge={mispricing:.1f}")
                return bool(filled)

    # -------- top-level step --------

    def step(self, *, reconcile: bool = False) -> None:
        with self.lock:
            try:
                phase = self.c.game_state().get("phase")
            except Exception:
                return
            self.phase = phase  # keep local phase in sync (read by on_quote_event)
            if phase != "running":
                if phase == "settled":
                    self.resting = {"bid": None, "ask": None}
                return

            if reconcile:
                self.reconcile_position()

            # Snipe first so the book cache is fresh for the penny logic
            # inside desired_quotes. Pre-reveal maybe_snipe is a no-op
            # (gated by min_reveals_to_snipe), so refresh the book cache
            # explicitly -- desired_quotes' pre-reveal market_mid anchor
            # and tail-pull defense both need it.
            fair, sigma = self.fair_and_sigma()
            if len(self.posterior.reveals) == 0:
                self._refresh_book_cache_if_stale()
            self.maybe_snipe(fair, sigma)

            bid_px, ask_px, fair, sigma = self.desired_quotes()
            self._apply_target_quotes(bid_px, ask_px)

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
        with self.lock:
            side = msg["side"]
            qty = msg["qty"]
            order_id = msg["order_id"]
            for key, expected_side in (("bid", "buy"), ("ask", "sell")):
                rest = self.resting[key]
                if rest is not None and rest["order_id"] == order_id and side == expected_side:
                    rest["qty"] -= qty
                    if rest["qty"] <= 0:
                        self.resting[key] = None
                    break
        self.step(reconcile=True)

    def on_quote_event(self, msg: dict) -> None:
        """React to a new (or cancelled) quote on the book.

        Two paths, ordered by latency:

          1. Direct snipe (no throttle) -- on quote_add only, fires one IOC
             against the new quote if it's mispriced. ~5-15ms latency, beats
             competing snipers who book.fetch first. Self-rate-limited by
             the mispricing gate -- only fires when there's real edge.
          2. Throttled book scan -- regular maybe_snipe (~50-100ms) plus
             reprice of our maker quotes via the penny path. Catches any
             level the direct path missed (e.g., cache wasn't fresh) and
             keeps our maker quotes aligned with shifting top-of-book.
             Throttle is 10/sec to bound REST load.

        Discovered from probe data: the server pushes 'quote_add' /
        'quote_cancel' on the private WS for EVERY order, not just ours.
        """
        # Path 1: direct snipe on every quote_add. The mispricing gate
        # naturally rate-limits -- most quote_adds are at normal prices.
        if msg.get("type") == "quote_add":
            self._try_direct_snipe(msg)

        # Path 2: throttled book scan + maker reprice.
        now = time.time()
        if now - self._last_quote_event_t < self.quote_event_throttle_sec:
            return
        if self.phase != "running":
            return
        self._last_quote_event_t = now
        with self.lock:
            fair, sigma = self.fair_and_sigma()
            if fair == 0.0 and sigma == 0.0:
                return
            if len(self.posterior.reveals) >= self.min_reveals_to_snipe:
                # maybe_snipe also refreshes _book_cache as a side effect.
                self.maybe_snipe(fair, sigma)
            else:
                # Pre-reveal: maybe_snipe is a no-op, but desired_quotes
                # still needs a fresh book for the market_mid anchor and
                # the tail-pull defense.
                self._refresh_book_cache_if_stale()
            # Reprice maker quotes to catch top-of-book changes (penny/dime
            # or pre-reveal market-mid shifts). desired_quotes returns
            # (None, None) when quotes are suppressed (warmup, no market,
            # sweep mode); _apply_target_quotes then cancels gracefully.
            if len(self.posterior.reveals) >= self.min_reveals_to_quote:
                bid_px, ask_px, _, _ = self.desired_quotes()
                self._apply_target_quotes(bid_px, ask_px)

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
