"""
Offline simulation of the Day 1 trading game.

Game mechanics:
  - Each round: (a, w) drawn once from LogNormal-floored priors.
  - N = duration // reveal_interval integer draws X_i ~ DiscreteUniform({a, ..., a+w}).
  - Settlement = sum of all X_i (identity settlement).

Two strategies are bundled:
  - OurBot       : the strategy from strategy.ipynb, unchanged.
  - ImprovedBot  : refinements (see ImprovedBot docstring).

Run:
    python day1/simulation.py                                 # OurBot only
    python day1/simulation.py --rounds 100 --compare          # both + plot
    python day1/simulation.py --rounds 50 --compare --seed 7
"""

from __future__ import annotations

import argparse
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Prior parameters (match strategy.ipynb)
# ---------------------------------------------------------------------------
A_LOGN_MU, A_LOGN_SIGMA = 1.0, 0.8
W_LOGN_MU, W_LOGN_SIGMA = 0.5, 0.7
N_PRIOR_SIM = 200_000

MAKER_FEE = 0.5
TAKER_FEE = 0.5
POSITION_LIMIT = 100
TICK = 1


# ---------------------------------------------------------------------------
# Posterior: continuous-uniform likelihood (original from strategy.ipynb)
# ---------------------------------------------------------------------------
class Posterior:
    """Joint posterior over (a, w) given reveals X_i ~ Uniform(a, a+w)."""

    def __init__(self, rng: random.Random):
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for _ in range(N_PRIOR_SIM):
            a_v = int(math.floor(rng.lognormvariate(A_LOGN_MU, A_LOGN_SIGMA)))
            w_v = 1 + int(math.floor(rng.lognormvariate(W_LOGN_MU, W_LOGN_SIGMA)))
            counts[(a_v, w_v)] += 1
        total = sum(counts.values())
        self.prior: dict[tuple[int, int], float] = {k: v / total for k, v in counts.items()}
        self.posterior: dict[tuple[int, int], float] = dict(self.prior)
        self.reveals: list[float] = []

    def reset(self) -> None:
        self.posterior = dict(self.prior)
        self.reveals = []

    def update(self, x: float) -> None:
        self.reveals.append(float(x))
        new: dict[tuple[int, int], float] = {}
        total = 0.0
        for (a, w), p in self.posterior.items():
            if x < a or x > a + w:
                continue
            lp = p / w  # continuous-uniform likelihood 1/(b-a) = 1/w
            new[(a, w)] = lp
            total += lp
        if total <= 0:
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def predict_settle(self, running_sum: float, n_remaining: int) -> tuple[float, float]:
        if n_remaining <= 0:
            return float(running_sum), 0.0
        e_inner = e2_inner = e_var = 0.0
        for (a, w), p in self.posterior.items():
            mu_rem = n_remaining * (a + w / 2.0)
            var_rem = n_remaining * (w * w) / 12.0
            e_inner += p * mu_rem
            e2_inner += p * mu_rem * mu_rem
            e_var += p * var_rem
        var_total = e_var + (e2_inner - e_inner * e_inner)
        return running_sum + e_inner, math.sqrt(max(var_total, 0.0))


# ---------------------------------------------------------------------------
# ImprovedPosterior: discrete-uniform likelihood
# ---------------------------------------------------------------------------
class ImprovedPosterior(Posterior):
    """Discrete-uniform likelihood. Reveals are integers, so X ~ DiscreteUniform({a, ..., a+w}).

    Differences from Posterior:
      - likelihood is 1/(w+1) not 1/w (the support has w+1 integers, not w)
      - per-draw variance is w*(w+2)/12 not w^2/12
      - support check is the same (x in [a, a+w]) but interpreted as integer endpoints
    """

    def update(self, x: float) -> None:
        self.reveals.append(float(x))
        new: dict[tuple[int, int], float] = {}
        total = 0.0
        for (a, w), p in self.posterior.items():
            if x < a or x > a + w:
                continue
            lp = p / (w + 1)  # discrete-uniform mass
            new[(a, w)] = lp
            total += lp
        if total <= 0:
            return
        self.posterior = {k: v / total for k, v in new.items()}

    def predict_settle(self, running_sum: float, n_remaining: int) -> tuple[float, float]:
        if n_remaining <= 0:
            return float(running_sum), 0.0
        e_inner = e2_inner = e_var = 0.0
        for (a, w), p in self.posterior.items():
            mu_rem = n_remaining * (a + w / 2.0)
            var_rem = n_remaining * (w * (w + 2)) / 12.0  # discrete-uniform variance
            e_inner += p * mu_rem
            e2_inner += p * mu_rem * mu_rem
            e_var += p * var_rem
        var_total = e_var + (e2_inner - e_inner * e_inner)
        return running_sum + e_inner, math.sqrt(max(var_total, 0.0))


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------
@dataclass
class Order:
    order_id: int
    owner: str
    side: str
    price: int
    qty: int
    remaining: int = field(init=False)

    def __post_init__(self):
        self.remaining = self.qty


@dataclass
class Fill:
    buyer: str
    seller: str
    price: int
    qty: int
    aggressor: str


class OrderBook:
    def __init__(self):
        self._next_id = 1
        self.bids: list[Order] = []
        self.asks: list[Order] = []

    def _new_id(self) -> int:
        oid = self._next_id
        self._next_id += 1
        return oid

    def _match(self, agg: Order) -> list[Fill]:
        fills: list[Fill] = []
        passive = self.asks if agg.side == "buy" else self.bids
        while agg.remaining > 0 and passive:
            best = passive[0]
            if agg.side == "buy" and agg.price < best.price:
                break
            if agg.side == "sell" and agg.price > best.price:
                break
            qty = min(agg.remaining, best.remaining)
            fills.append(Fill(
                buyer=agg.owner if agg.side == "buy" else best.owner,
                seller=best.owner if agg.side == "buy" else agg.owner,
                price=best.price,
                qty=qty,
                aggressor=agg.side,
            ))
            agg.remaining -= qty
            best.remaining -= qty
            if best.remaining == 0:
                passive.pop(0)
        return fills

    def submit_limit(self, owner: str, side: str, price: int, qty: int) -> tuple[Order, list[Fill]]:
        o = Order(self._new_id(), owner, side, price, qty)
        fills = self._match(o)
        if o.remaining > 0:
            lst = self.bids if side == "buy" else self.asks
            lst.append(o)
            if side == "buy":
                lst.sort(key=lambda x: -x.price)
            else:
                lst.sort(key=lambda x: x.price)
        return o, fills

    def submit_market(self, owner: str, side: str, qty: int) -> tuple[Order, list[Fill]]:
        price = 10**9 if side == "buy" else 0
        o = Order(self._new_id(), owner, side, price, qty)
        fills = self._match(o)
        return o, fills

    def cancel(self, order_id: int) -> bool:
        for lst in (self.bids, self.asks):
            for i, o in enumerate(lst):
                if o.order_id == order_id:
                    lst.pop(i)
                    return True
        return False

    def best_bid(self) -> Optional[int]:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Optional[int]:
        return self.asks[0].price if self.asks else None

    def levels(self, side: str, depth: int = 5) -> list[dict]:
        lst = self.bids if side == "buy" else self.asks
        return [{"price": o.price, "qty": o.remaining, "owner": o.owner} for o in lst[:depth]]


# ---------------------------------------------------------------------------
# Trader state
# ---------------------------------------------------------------------------
class TraderState:
    def __init__(self, name: str):
        self.name = name
        self.position = 0
        self.cash = 0.0
        self.n_trades = 0

    def apply_fill(self, fill: Fill) -> None:
        if fill.buyer == self.name:
            self.position += fill.qty
            self.cash -= fill.price * fill.qty
            fee = TAKER_FEE if fill.aggressor == "buy" else MAKER_FEE
            self.cash -= fee * fill.qty
            self.n_trades += 1
        if fill.seller == self.name:
            self.position -= fill.qty
            self.cash += fill.price * fill.qty
            fee = TAKER_FEE if fill.aggressor == "sell" else MAKER_FEE
            self.cash -= fee * fill.qty
            self.n_trades += 1

    def settle(self, settle_price: int) -> float:
        pnl = self.cash + self.position * settle_price
        self.position = 0
        self.cash = 0.0
        self.n_trades = 0
        return pnl


# ---------------------------------------------------------------------------
# Noise market-maker
# ---------------------------------------------------------------------------
class NoiseMM:
    def __init__(self, name: str, rng: random.Random):
        self.name = name
        self.rng = rng
        self.bid_id: Optional[int] = None
        self.ask_id: Optional[int] = None

    def step(self, book: OrderBook, true_fair: float, sigma: float, state: TraderState) -> None:
        noise = self.rng.gauss(0, max(sigma * 0.4, 3))
        mm_fair = true_fair + noise
        spread = max(2, int(sigma * 0.3))
        bid_px = max(1, int(mm_fair - spread))
        ask_px = bid_px + spread * 2

        if self.bid_id is not None:
            book.cancel(self.bid_id)
        if self.ask_id is not None:
            book.cancel(self.ask_id)

        qty = 15  # bigger MM quotes so snipe size matters
        if POSITION_LIMIT - state.position >= qty:
            o, fills = book.submit_limit(self.name, "buy", bid_px, qty)
            self.bid_id = o.order_id if o.remaining > 0 else None
            for f in fills:
                state.apply_fill(f)
        else:
            self.bid_id = None

        if POSITION_LIMIT + state.position >= qty:
            o, fills = book.submit_limit(self.name, "sell", ask_px, qty)
            self.ask_id = o.order_id if o.remaining > 0 else None
            for f in fills:
                state.apply_fill(f)
        else:
            self.ask_id = None


# ---------------------------------------------------------------------------
# Original strategy bot (mirrors strategy.ipynb)
# ---------------------------------------------------------------------------
class OurBot:
    name = "us"

    def __init__(self, name: str, posterior: Posterior):
        self.name = name
        self.posterior = posterior
        self.quote_qty = 10
        self.min_edge = 1.5
        self.edge_per_sigma = 0.25
        self.skew_per_unit = 0.10
        self.snipe_buffer_sigma = 0.30
        self.snipe_min_edge = 0.5
        self.bid_id: Optional[int] = None
        self.ask_id: Optional[int] = None

    def _fair(self, n_total: int) -> tuple[float, float]:
        running = sum(self.posterior.reveals)
        remaining = max(n_total - len(self.posterior.reveals), 0)
        return self.posterior.predict_settle(running, remaining)

    def step(self, book: OrderBook, state: TraderState, n_total: int) -> None:
        fair, sigma = self._fair(n_total)
        edge_req = TAKER_FEE + max(self.snipe_min_edge, self.snipe_buffer_sigma * sigma)

        for level in book.levels("sell"):
            if level["owner"] == self.name:
                continue
            if fair - level["price"] <= edge_req:
                break
            headroom = POSITION_LIMIT - state.position
            qty = min(level["qty"], headroom, self.quote_qty)
            if qty <= 0:
                break
            _, fills = book.submit_limit(self.name, "buy", level["price"], qty)
            for f in fills:
                state.apply_fill(f)

        for level in book.levels("buy"):
            if level["owner"] == self.name:
                continue
            if level["price"] - fair <= edge_req:
                break
            headroom = POSITION_LIMIT + state.position
            qty = min(level["qty"], headroom, self.quote_qty)
            if qty <= 0:
                break
            _, fills = book.submit_limit(self.name, "sell", level["price"], qty)
            for f in fills:
                state.apply_fill(f)

        if self.bid_id is not None:
            book.cancel(self.bid_id)
            self.bid_id = None
        if self.ask_id is not None:
            book.cancel(self.ask_id)
            self.ask_id = None

        edge = max(self.min_edge, self.edge_per_sigma * sigma)
        skew = -state.position * self.skew_per_unit
        bid_px = int(math.floor(fair - edge + skew))
        ask_px = int(math.ceil(fair + edge + skew))
        if ask_px <= bid_px:
            ask_px = bid_px + TICK

        if state.position + self.quote_qty <= POSITION_LIMIT:
            o, fills = book.submit_limit(self.name, "buy", bid_px, self.quote_qty)
            for f in fills:
                state.apply_fill(f)
            if o.remaining > 0:
                self.bid_id = o.order_id

        if state.position - self.quote_qty >= -POSITION_LIMIT:
            o, fills = book.submit_limit(self.name, "sell", ask_px, self.quote_qty)
            for f in fills:
                state.apply_fill(f)
            if o.remaining > 0:
                self.ask_id = o.order_id


# ---------------------------------------------------------------------------
# Improved strategy bot
# ---------------------------------------------------------------------------
class ImprovedBot:
    """Refinements over OurBot, additive only — strictly no riskier behavior.

    1. **ImprovedPosterior** (discrete-uniform likelihood, 1/(w+1) instead of
       1/w with variance w(w+2)/12 instead of w^2/12). Reveals ARE integers,
       so this is the unbiased estimator. The continuous-uniform likelihood
       slightly biases (a, w) inferences near the support boundary.

    2. **No quote_qty cap on snipes when sigma is small** (sigma < 1.5).
       Late-round, our fair is tight and any mispricing past edge_req is real
       profit. OurBot leaves volume on the table by capping at quote_qty=10
       even when it's near-certain about the fair. Allowing up to position
       headroom captures that volume. Above the threshold, we keep the cap
       so early-round bias can't snowball.

    3. **Deeper book walk for snipes** (depth 10 vs 5). Lossless improvement —
       lets us walk a full book of mispriced levels in one step.

    All other knobs (min_edge, edge_per_sigma, skew_per_unit, snipe_buffer_sigma,
    snipe_min_edge) match OurBot. We take MORE size on the SAME edges, with
    more accurate fair-value estimates. We never trade earlier or wider than
    OurBot does.
    """

    name = "us"

    def __init__(self, name: str, posterior: Posterior):
        self.name = name
        self.posterior = posterior
        self.quote_qty = 10
        self.min_edge = 1.5
        self.edge_per_sigma = 0.25
        self.skew_per_unit = 0.10
        self.snipe_buffer_sigma = 0.30
        self.snipe_min_edge = 0.5
        self.snipe_depth = 10
        self.low_sigma_threshold = 1.5
        self.bid_id: Optional[int] = None
        self.ask_id: Optional[int] = None

    def _fair(self, n_total: int) -> tuple[float, float]:
        running = sum(self.posterior.reveals)
        remaining = max(n_total - len(self.posterior.reveals), 0)
        return self.posterior.predict_settle(running, remaining)

    def step(self, book: OrderBook, state: TraderState, n_total: int) -> None:
        fair, sigma = self._fair(n_total)
        edge_req = TAKER_FEE + max(self.snipe_min_edge, self.snipe_buffer_sigma * sigma)
        # Only uncap snipe size when our fair is tight
        snipe_cap = POSITION_LIMIT if sigma < self.low_sigma_threshold else self.quote_qty

        for level in book.levels("sell", depth=self.snipe_depth):
            if level["owner"] == self.name:
                continue
            if fair - level["price"] <= edge_req:
                break
            headroom = POSITION_LIMIT - state.position
            qty = min(level["qty"], headroom, snipe_cap)
            if qty <= 0:
                break
            _, fills = book.submit_limit(self.name, "buy", level["price"], qty)
            for f in fills:
                state.apply_fill(f)

        for level in book.levels("buy", depth=self.snipe_depth):
            if level["owner"] == self.name:
                continue
            if level["price"] - fair <= edge_req:
                break
            headroom = POSITION_LIMIT + state.position
            qty = min(level["qty"], headroom, snipe_cap)
            if qty <= 0:
                break
            _, fills = book.submit_limit(self.name, "sell", level["price"], qty)
            for f in fills:
                state.apply_fill(f)

        if self.bid_id is not None:
            book.cancel(self.bid_id)
            self.bid_id = None
        if self.ask_id is not None:
            book.cancel(self.ask_id)
            self.ask_id = None

        edge = max(self.min_edge, self.edge_per_sigma * sigma)
        skew = -state.position * self.skew_per_unit
        bid_px = int(math.floor(fair - edge + skew))
        ask_px = int(math.ceil(fair + edge + skew))
        if ask_px <= bid_px:
            ask_px = bid_px + TICK

        if state.position + self.quote_qty <= POSITION_LIMIT:
            o, fills = book.submit_limit(self.name, "buy", bid_px, self.quote_qty)
            for f in fills:
                state.apply_fill(f)
            if o.remaining > 0:
                self.bid_id = o.order_id

        if state.position - self.quote_qty >= -POSITION_LIMIT:
            o, fills = book.submit_limit(self.name, "sell", ask_px, self.quote_qty)
            for f in fills:
                state.apply_fill(f)
            if o.remaining > 0:
                self.ask_id = o.order_id


# ---------------------------------------------------------------------------
# Round simulator
# ---------------------------------------------------------------------------
def sample_round_params(rng: random.Random) -> tuple[int, int]:
    a = int(math.floor(rng.lognormvariate(A_LOGN_MU, A_LOGN_SIGMA)))
    w = 1 + int(math.floor(rng.lognormvariate(W_LOGN_MU, W_LOGN_SIGMA)))
    return a, w


def simulate_round(
    rng: random.Random,
    bot,
    n_draws: int,
    verbose: bool = False,
    ref_posterior: Optional[Posterior] = None,
) -> dict:
    """Run one round with a pre-built bot. If ref_posterior is given, the MM uses
    it for sigma (so two bots can be compared against an identical MM)."""
    a, w = sample_round_params(rng)
    true_settle_per_draw = a + w / 2.0

    book = OrderBook()
    state = TraderState(bot.name)
    mm_state = TraderState("mm")
    bot.bid_id = None
    bot.ask_id = None
    noise_mm = NoiseMM("mm", rng)

    bot.posterior.reset()
    if ref_posterior is not None and ref_posterior is not bot.posterior:
        ref_posterior.reset()

    reveals: list[float] = []
    running_sum = 0.0

    for k in range(n_draws):
        x = rng.randint(a, a + w)
        reveals.append(x)
        running_sum += x
        bot.posterior.update(float(x))
        if ref_posterior is not None and ref_posterior is not bot.posterior:
            ref_posterior.update(float(x))

        if ref_posterior is not None:
            fair_mm, sigma_mm = ref_posterior.predict_settle(running_sum, n_draws - (k + 1))
        else:
            fair_mm, sigma_mm = bot.posterior.predict_settle(running_sum, n_draws - (k + 1))

        noise_mm.step(book, fair_mm, sigma_mm, mm_state)
        bot.step(book, state, n_draws)

        if verbose:
            fair_v, sigma_v = bot.posterior.predict_settle(running_sum, n_draws - (k + 1))
            print(
                f"  k={k+1:2d}  x={x:3d}  "
                f"true_settle={running_sum + (n_draws - k - 1) * true_settle_per_draw:6.1f}  "
                f"fair={fair_v:6.1f} +/-{sigma_v:5.1f}  pos={state.position:+4d}"
            )

    settle_price = int(running_sum)
    if bot.bid_id is not None:
        book.cancel(bot.bid_id)
    if bot.ask_id is not None:
        book.cancel(bot.ask_id)

    n_trades_us = state.n_trades  # capture before settle() resets it
    pnl_us = state.settle(settle_price)
    pnl_mm = mm_state.settle(settle_price)

    return {
        "a": a, "w": w, "settle": settle_price,
        "pnl_us": pnl_us, "pnl_mm": pnl_mm,
        "n_trades": n_trades_us,
        "reveals": reveals,
    }


# ---------------------------------------------------------------------------
# Comparison runner
# ---------------------------------------------------------------------------
def run_comparison(rounds: int, n_draws: int, seed: int) -> tuple[list[dict], list[dict]]:
    print(f"Building priors ({N_PRIOR_SIM:,} MC samples)...")
    posterior_orig = Posterior(random.Random(7777))
    posterior_imp = ImprovedPosterior(random.Random(7777))
    posterior_ref = ImprovedPosterior(random.Random(7777))
    print(f"Prior has {len(posterior_orig.prior):,} distinct (a, w) pairs.\n")

    def _run(label: str, bot) -> list[dict]:
        print(f"=== {label} ===")
        rng = random.Random(seed)
        results: list[dict] = []
        cum = 0.0
        for r in range(rounds):
            res = simulate_round(rng, bot, n_draws, ref_posterior=posterior_ref)
            results.append(res)
            cum += res["pnl_us"]
            if (r + 1) % max(1, rounds // 10) == 0 or r < 2:
                sign = "+" if res["pnl_us"] >= 0 else ""
                print(
                    f"  Round {r+1:3d}  a={res['a']:2d} w={res['w']:2d} settle={res['settle']:4d} "
                    f"trades={res['n_trades']:3d}  pnl={sign}{res['pnl_us']:7.1f}  cum={cum:+8.1f}"
                )
        print()
        return results

    results_orig = _run("Original Strategy", OurBot("us", posterior_orig))
    results_imp = _run("Improved Strategy", ImprovedBot("us", posterior_imp))
    return results_orig, results_imp


def run_comparison_multi(rounds: int, n_draws: int, seeds: list[int]) -> dict:
    """Run comparison across multiple seeds for statistical robustness."""
    print(f"Building priors ({N_PRIOR_SIM:,} MC samples)...")
    posterior_orig = Posterior(random.Random(7777))
    posterior_imp = ImprovedPosterior(random.Random(7777))
    posterior_ref = ImprovedPosterior(random.Random(7777))
    print(f"Prior has {len(posterior_orig.prior):,} distinct (a, w) pairs.\n")
    print(f"Running {len(seeds)} seeds x {rounds} rounds = {len(seeds)*rounds} total rounds\n")

    bot_orig = OurBot("us", posterior_orig)
    bot_imp = ImprovedBot("us", posterior_imp)

    orig_by_seed: list[list[dict]] = []
    imp_by_seed: list[list[dict]] = []

    print(f"{'Seed':>6s}  {'Original':>10s}  {'Improved':>10s}  {'Diff':>10s}")
    print(f"{'-'*42}")
    for seed in seeds:
        rng = random.Random(seed)
        results_o = [simulate_round(rng, bot_orig, n_draws, ref_posterior=posterior_ref)
                     for _ in range(rounds)]

        rng = random.Random(seed)
        results_i = [simulate_round(rng, bot_imp, n_draws, ref_posterior=posterior_ref)
                     for _ in range(rounds)]

        total_o = sum(r["pnl_us"] for r in results_o)
        total_i = sum(r["pnl_us"] for r in results_i)
        diff = total_i - total_o
        marker = "+" if diff >= 0 else ""
        print(f"{seed:>6d}  {total_o:>10.1f}  {total_i:>10.1f}  {marker}{diff:>9.1f}")

        orig_by_seed.append(results_o)
        imp_by_seed.append(results_i)

    print()
    return {"orig": orig_by_seed, "imp": imp_by_seed, "seeds": seeds, "rounds": rounds}


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot_comparison(results_orig: list[dict], results_imp: list[dict], output_path: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib/numpy not installed. Install with: pip install matplotlib numpy")
        return

    pnls_orig = np.array([r["pnl_us"] for r in results_orig])
    pnls_imp = np.array([r["pnl_us"] for r in results_imp])
    cum_orig = np.cumsum(pnls_orig)
    cum_imp = np.cumsum(pnls_imp)
    rounds = np.arange(1, len(pnls_orig) + 1)
    trades_orig = np.array([r["n_trades"] for r in results_orig])
    trades_imp = np.array([r["n_trades"] for r in results_imp])

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Original vs Improved Strategy — Day 1 Game Simulation", fontsize=14, fontweight="bold")

    # 1. Cumulative PnL
    ax = axes[0, 0]
    ax.plot(rounds, cum_orig, label=f"Original (final {cum_orig[-1]:+.0f})", color="tab:blue", lw=2)
    ax.plot(rounds, cum_imp, label=f"Improved (final {cum_imp[-1]:+.0f})", color="tab:orange", lw=2)
    ax.axhline(0, color="gray", lw=0.5)
    ax.fill_between(rounds, cum_orig, cum_imp,
                    where=(cum_imp >= cum_orig), alpha=0.15, color="tab:orange",
                    interpolate=True, label="Improved ahead")
    ax.fill_between(rounds, cum_orig, cum_imp,
                    where=(cum_imp < cum_orig), alpha=0.15, color="tab:blue",
                    interpolate=True, label="Original ahead")
    ax.set_xlabel("Round")
    ax.set_ylabel("Cumulative PnL")
    ax.set_title("Cumulative PnL Over Rounds")
    ax.legend(loc="best", fontsize=9)
    ax.grid(alpha=0.3)

    # 2. Per-round PnL bars
    ax = axes[0, 1]
    width = 0.4
    ax.bar(rounds - width / 2, pnls_orig, width, label="Original", color="tab:blue", alpha=0.8)
    ax.bar(rounds + width / 2, pnls_imp, width, label="Improved", color="tab:orange", alpha=0.8)
    ax.axhline(0, color="gray", lw=0.5)
    ax.set_xlabel("Round")
    ax.set_ylabel("PnL")
    ax.set_title("Per-Round PnL")
    ax.legend()
    ax.grid(alpha=0.3)

    # 3. PnL distribution
    ax = axes[1, 0]
    all_pnls = np.concatenate([pnls_orig, pnls_imp])
    bins = np.linspace(all_pnls.min(), all_pnls.max(), 25)
    ax.hist(pnls_orig, bins=bins, alpha=0.55, label="Original", color="tab:blue", edgecolor="white")
    ax.hist(pnls_imp, bins=bins, alpha=0.55, label="Improved", color="tab:orange", edgecolor="white")
    ax.axvline(0, color="gray", lw=0.5)
    ax.axvline(pnls_orig.mean(), color="tab:blue", linestyle="--", lw=1.5,
               label=f"Orig mean = {pnls_orig.mean():.1f}")
    ax.axvline(pnls_imp.mean(), color="tab:orange", linestyle="--", lw=1.5,
               label=f"Imp mean  = {pnls_imp.mean():.1f}")
    ax.set_xlabel("Per-round PnL")
    ax.set_ylabel("Count")
    ax.set_title("PnL Distribution")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)

    # 4. Summary stats
    ax = axes[1, 1]
    ax.axis("off")
    n = len(pnls_orig)
    wins_o = int((pnls_orig > 0).sum())
    losses_o = int((pnls_orig < 0).sum())
    wins_i = int((pnls_imp > 0).sum())
    losses_i = int((pnls_imp < 0).sum())

    def _fmt(label: str, ov, iv, fmt: str = "{:>12.2f}") -> str:
        return f"{label:<14s}{fmt.format(ov):>12s}{fmt.format(iv):>12s}\n"

    stats = (
        f"Rounds: {n}\n\n"
        f"{'':<14s}{'Original':>12s}{'Improved':>12s}\n"
        f"{'-'*38}\n"
        f"{'Total PnL':<14s}{pnls_orig.sum():>12.1f}{pnls_imp.sum():>12.1f}\n"
        f"{'Mean':<14s}{pnls_orig.mean():>12.2f}{pnls_imp.mean():>12.2f}\n"
        f"{'Median':<14s}{float(np.median(pnls_orig)):>12.1f}{float(np.median(pnls_imp)):>12.1f}\n"
        f"{'Std':<14s}{pnls_orig.std():>12.2f}{pnls_imp.std():>12.2f}\n"
        f"{'Max':<14s}{pnls_orig.max():>12.1f}{pnls_imp.max():>12.1f}\n"
        f"{'Min':<14s}{pnls_orig.min():>12.1f}{pnls_imp.min():>12.1f}\n"
        f"{'Wins':<14s}{wins_o:>12d}{wins_i:>12d}\n"
        f"{'Losses':<14s}{losses_o:>12d}{losses_i:>12d}\n"
        f"{'Win rate':<14s}{f'{wins_o/n:.1%}':>12s}{f'{wins_i/n:.1%}':>12s}\n"
        f"{'Avg trades':<14s}{trades_orig.mean():>12.1f}{trades_imp.mean():>12.1f}\n"
        f"\n"
        f"Improvement: {(pnls_imp.sum() - pnls_orig.sum()):+.1f} total PnL "
        f"({(pnls_imp.mean() - pnls_orig.mean()):+.2f}/round)\n"
        f"Sharpe-like: orig={pnls_orig.mean()/max(pnls_orig.std(),1e-9):.2f}  "
        f"imp={pnls_imp.mean()/max(pnls_imp.std(),1e-9):.2f}"
    )
    ax.text(0.02, 0.98, stats, family="monospace", fontsize=10, va="top", ha="left",
            transform=ax.transAxes)
    ax.set_title("Summary Statistics")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    print(f"Saved plot to {output_path}")


def plot_comparison_multi(results: dict, output_path: str) -> None:
    """Plot multi-seed comparison: per-seed bars, aggregate cumulative PnL with bands,
    flattened distribution, and summary statistics."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib/numpy not installed.")
        return

    seeds = results["seeds"]
    n_seeds = len(seeds)
    n_rounds = results["rounds"]

    totals_orig = np.array([sum(r["pnl_us"] for r in s) for s in results["orig"]])
    totals_imp = np.array([sum(r["pnl_us"] for r in s) for s in results["imp"]])
    cum_orig = np.array([np.cumsum([r["pnl_us"] for r in s]) for s in results["orig"]])
    cum_imp = np.array([np.cumsum([r["pnl_us"] for r in s]) for s in results["imp"]])
    all_pnls_orig = np.array([r["pnl_us"] for s in results["orig"] for r in s])
    all_pnls_imp = np.array([r["pnl_us"] for s in results["imp"] for r in s])

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(
        f"Original vs Improved — {n_seeds} seeds x {n_rounds} rounds = {n_seeds*n_rounds} total rounds",
        fontsize=14, fontweight="bold",
    )

    # 1. Per-seed total PnL bars
    ax = axes[0, 0]
    x = np.arange(n_seeds)
    width = 0.4
    ax.bar(x - width / 2, totals_orig, width, label="Original", color="tab:blue", alpha=0.85)
    ax.bar(x + width / 2, totals_imp, width, label="Improved", color="tab:orange", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels([str(s) for s in seeds], rotation=45 if n_seeds > 5 else 0, fontsize=9)
    ax.set_xlabel("Seed")
    ax.set_ylabel(f"Total PnL ({n_rounds} rounds)")
    ax.set_title("Per-Seed Total PnL")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3, axis="y")
    wins = int((totals_imp > totals_orig).sum())
    ax.text(
        0.5, 0.97,
        f"Improved beats Original in {wins}/{n_seeds} seeds",
        transform=ax.transAxes, ha="center", va="top", fontsize=10,
        bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.9, edgecolor="gray"),
    )

    # 2. Aggregate cumulative PnL with std bands
    ax = axes[0, 1]
    rounds_x = np.arange(1, n_rounds + 1)
    mean_o = cum_orig.mean(axis=0)
    std_o = cum_orig.std(axis=0)
    mean_i = cum_imp.mean(axis=0)
    std_i = cum_imp.std(axis=0)
    ax.plot(rounds_x, mean_o, label=f"Original (avg final {mean_o[-1]:+.0f})",
            color="tab:blue", lw=2)
    ax.fill_between(rounds_x, mean_o - std_o, mean_o + std_o, alpha=0.18, color="tab:blue",
                    label="Original ±1 std")
    ax.plot(rounds_x, mean_i, label=f"Improved (avg final {mean_i[-1]:+.0f})",
            color="tab:orange", lw=2)
    ax.fill_between(rounds_x, mean_i - std_i, mean_i + std_i, alpha=0.18, color="tab:orange",
                    label="Improved ±1 std")
    ax.axhline(0, color="gray", lw=0.5)
    ax.set_xlabel("Round")
    ax.set_ylabel("Cumulative PnL")
    ax.set_title(f"Aggregate Cumulative PnL (averaged over {n_seeds} seeds)")
    ax.legend(loc="best", fontsize=9)
    ax.grid(alpha=0.3)

    # 3. Per-round PnL distribution across all seeds + rounds
    ax = axes[1, 0]
    lo = min(all_pnls_orig.min(), all_pnls_imp.min())
    hi = max(all_pnls_orig.max(), all_pnls_imp.max())
    bins = np.linspace(lo, hi, 35)
    ax.hist(all_pnls_orig, bins=bins, alpha=0.55, label="Original", color="tab:blue",
            edgecolor="white", linewidth=0.5)
    ax.hist(all_pnls_imp, bins=bins, alpha=0.55, label="Improved", color="tab:orange",
            edgecolor="white", linewidth=0.5)
    ax.axvline(all_pnls_orig.mean(), color="tab:blue", linestyle="--", lw=1.8,
               label=f"Orig mean = {all_pnls_orig.mean():.2f}")
    ax.axvline(all_pnls_imp.mean(), color="tab:orange", linestyle="--", lw=1.8,
               label=f"Imp  mean = {all_pnls_imp.mean():.2f}")
    ax.axvline(0, color="gray", lw=0.5)
    ax.set_xlabel("Per-round PnL")
    ax.set_ylabel("Count")
    ax.set_title(f"Per-Round PnL Distribution ({n_seeds*n_rounds} rounds total)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)

    # 4. Summary statistics
    ax = axes[1, 1]
    ax.axis("off")
    n_total = n_seeds * n_rounds
    wins_o = int((all_pnls_orig > 0).sum())
    wins_i = int((all_pnls_imp > 0).sum())
    sharpe_o = float(all_pnls_orig.mean() / max(all_pnls_orig.std(), 1e-9))
    sharpe_i = float(all_pnls_imp.mean() / max(all_pnls_imp.std(), 1e-9))
    rel_improvement = (
        (all_pnls_imp.mean() - all_pnls_orig.mean()) / max(abs(all_pnls_orig.mean()), 1e-9) * 100.0
    )

    stats = (
        f"Seeds: {n_seeds}    Rounds/seed: {n_rounds}    Total rounds: {n_total}\n\n"
        f"{'':<16s}{'Original':>13s}{'Improved':>13s}\n"
        f"{'-' * 42}\n"
        f"{'Total PnL':<16s}{all_pnls_orig.sum():>13.1f}{all_pnls_imp.sum():>13.1f}\n"
        f"{'Mean / round':<16s}{all_pnls_orig.mean():>13.2f}{all_pnls_imp.mean():>13.2f}\n"
        f"{'Median':<16s}{float(np.median(all_pnls_orig)):>13.1f}{float(np.median(all_pnls_imp)):>13.1f}\n"
        f"{'Std':<16s}{all_pnls_orig.std():>13.2f}{all_pnls_imp.std():>13.2f}\n"
        f"{'Max round':<16s}{all_pnls_orig.max():>13.1f}{all_pnls_imp.max():>13.1f}\n"
        f"{'Min round':<16s}{all_pnls_orig.min():>13.1f}{all_pnls_imp.min():>13.1f}\n"
        f"{'Win rate':<16s}{f'{wins_o/n_total:.1%}':>13s}{f'{wins_i/n_total:.1%}':>13s}\n"
        f"{'Sharpe-like':<16s}{sharpe_o:>13.3f}{sharpe_i:>13.3f}\n\n"
        f"Improvement: {all_pnls_imp.mean() - all_pnls_orig.mean():+.3f} per round\n"
        f"             {rel_improvement:+.1f}% relative\n"
        f"             Beats Original in {wins}/{n_seeds} seeds"
    )
    ax.text(0.02, 0.98, stats, family="monospace", fontsize=10.5, va="top", ha="left",
            transform=ax.transAxes)
    ax.set_title("Summary Statistics")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    print(f"Saved plot to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Day 1 game offline simulation")
    parser.add_argument("--rounds", type=int, default=20, help="Number of rounds to simulate")
    parser.add_argument("--n-draws", type=int, default=10, help="Reveals per round")
    parser.add_argument("--seed", type=int, default=1, help="Random seed (base if --num-seeds > 1)")
    parser.add_argument("--num-seeds", type=int, default=1,
                        help="Run multi-seed comparison. >1 enables aggregated plot.")
    parser.add_argument("--verbose", action="store_true", help="Per-reveal detail")
    parser.add_argument("--compare", action="store_true",
                        help="Compare OurBot vs ImprovedBot and plot results")
    parser.add_argument("--output", default="day1/results.png", help="Plot output path")
    args = parser.parse_args()

    if args.compare and args.num_seeds > 1:
        # Multi-seed comparison
        seeds = [args.seed + i * 7919 for i in range(args.num_seeds)]
        results = run_comparison_multi(args.rounds, args.n_draws, seeds)

        all_orig = [r["pnl_us"] for s in results["orig"] for r in s]
        all_imp = [r["pnl_us"] for s in results["imp"] for r in s]
        totals_o = [sum(r["pnl_us"] for r in s) for s in results["orig"]]
        totals_i = [sum(r["pnl_us"] for r in s) for s in results["imp"]]
        wins_seeds = sum(1 for o, i in zip(totals_o, totals_i) if i > o)

        print(f"{'='*64}")
        print(f"{'Metric':<22s}{'Original':>20s}{'Improved':>20s}")
        print(f"{'-'*64}")
        print(f"{'Aggregate total PnL':<22s}{sum(all_orig):>20.1f}{sum(all_imp):>20.1f}")
        print(f"{'Mean per round':<22s}{sum(all_orig)/len(all_orig):>20.2f}{sum(all_imp)/len(all_imp):>20.2f}")
        print(f"{'Beats opponent (seeds)':<22s}{args.num_seeds - wins_seeds:>20d}{wins_seeds:>20d}")
        print(f"{'='*64}\n")

        plot_comparison_multi(results, args.output)
        return

    if args.compare:
        results_orig, results_imp = run_comparison(args.rounds, args.n_draws, args.seed)

        pnls_orig = [r["pnl_us"] for r in results_orig]
        pnls_imp = [r["pnl_us"] for r in results_imp]
        wins_o = sum(1 for p in pnls_orig if p > 0)
        wins_i = sum(1 for p in pnls_imp if p > 0)

        print(f"{'='*64}")
        print(f"{'Metric':<22s}{'Original':>20s}{'Improved':>20s}")
        print(f"{'-'*64}")
        print(f"{'Total PnL':<22s}{sum(pnls_orig):>20.1f}{sum(pnls_imp):>20.1f}")
        print(f"{'Avg PnL / round':<22s}{sum(pnls_orig)/args.rounds:>20.2f}{sum(pnls_imp)/args.rounds:>20.2f}")
        print(f"{'Wins':<22s}{wins_o:>20d}{wins_i:>20d}")
        print(f"{'Win rate':<22s}{f'{wins_o/args.rounds:.1%}':>20s}{f'{wins_i/args.rounds:.1%}':>20s}")
        print(f"{'='*64}\n")

        plot_comparison(results_orig, results_imp, args.output)
        return

    # Single-bot mode (backwards compatible)
    rng = random.Random(args.seed)
    print(f"Building prior ({N_PRIOR_SIM:,} MC samples)...")
    posterior = Posterior(random.Random(7777))
    print(f"Prior has {len(posterior.prior):,} distinct (a, w) pairs.\n")

    bot = OurBot("us", posterior)
    total_pnl = 0.0
    results = []
    for r in range(1, args.rounds + 1):
        res = simulate_round(rng, bot, args.n_draws, verbose=args.verbose)
        total_pnl += res["pnl_us"]
        results.append(res)
        sign = "+" if res["pnl_us"] >= 0 else ""
        print(
            f"Round {r:3d}  a={res['a']:3d} w={res['w']:3d}  settle={res['settle']:4d}"
            f"  pnl={sign}{res['pnl_us']:7.1f}  cumulative={total_pnl:+8.1f}"
        )

    wins = sum(1 for r in results if r["pnl_us"] > 0)
    losses = sum(1 for r in results if r["pnl_us"] < 0)
    print(f"\n{'='*60}")
    print(f"Rounds      : {args.rounds}")
    print(f"Win / Loss  : {wins} / {losses}")
    print(f"Total PnL   : {total_pnl:+.1f}")
    print(f"Avg PnL/rnd : {total_pnl / args.rounds:+.2f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
