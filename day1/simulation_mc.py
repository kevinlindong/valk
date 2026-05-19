"""
Full Monte Carlo simulation for Day 1 trading strategies.

Compared to day1/simulation.py (which runs each strategy against a single
NoiseMM and reports point estimates), this:

  - Populates the market with THREE opponent types:
      * NoiseMM     -- wide-spread market maker (existing)
      * TightMM     -- tighter-spread MM (more competitive liquidity)
      * ValueSniper -- adversarial value-aware sniper (competes for arbitrage)
  - Computes statistical rigor: bootstrap CIs on mean PnL, paired bootstrap CI
    on (Improved - Original) per-round difference, Sharpe-like ratio, max
    drawdown, and VaR_5 (5th-percentile of round PnL).
  - Tracks per-round detail: position trajectory, settle, opponent PnL.
  - Produces a 6-panel summary plot:
      1. Cumulative PnL across all rounds
      2. Per-round PnL distribution
      3. Drawdown curves
      4. PnL by range-width w (regime analysis)
      5. Paired (Improved - Original) difference distribution
      6. Statistical summary table

Run:
    python day1/simulation_mc.py                          # 200 rounds x 10 seeds
    python day1/simulation_mc.py --rounds 500 --seeds 20  # heavier MC
    python day1/simulation_mc.py --quick                  # 50 rounds x 3 seeds
"""

from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys
from typing import Optional

# Allow running both as `python day1/simulation_mc.py` and from inside day1/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from simulation import (  # noqa: E402
    N_PRIOR_SIM,
    TAKER_FEE,
    POSITION_LIMIT,
    TICK,
    Posterior,
    ImprovedPosterior,
    OrderBook,
    TraderState,
    NoiseMM,
    OurBot,
    ImprovedBot,
    sample_round_params,
)


# ---------------------------------------------------------------------------
# Additional opponents
# ---------------------------------------------------------------------------
class TightMM:
    """Tighter-spread market maker.

    Quotes a narrower spread than NoiseMM around the true (informed) fair, with
    less price noise. Reduces the mispricing left on the book for our bot to
    snipe, so a strategy that beats this MM has real edge -- not just a wide-
    spread cushion.
    """

    def __init__(self, name: str, rng: random.Random):
        self.name = name
        self.rng = rng
        self.bid_id: Optional[int] = None
        self.ask_id: Optional[int] = None

    def step(self, book: OrderBook, true_fair: float, sigma: float, state: TraderState) -> None:
        noise = self.rng.gauss(0, max(sigma * 0.2, 1.0))
        mm_fair = true_fair + noise
        spread = max(1, int(sigma * 0.15))
        bid_px = max(1, int(mm_fair - spread))
        ask_px = bid_px + spread * 2

        if self.bid_id is not None:
            book.cancel(self.bid_id)
        if self.ask_id is not None:
            book.cancel(self.ask_id)

        qty = 8
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


class ValueSniper:
    """Adversarial value-aware sniper.

    Has its own (noisy) read of the same informed posterior the MMs use and
    takes obvious mispricings. Crucially, it COMPETES with us for arbitrage:
    when our bot is slow or wide, this bot may grab the mispricing first.

    Has no market-making leg -- pure taker, taker fee on every fill.
    """

    def __init__(self, name: str, rng: random.Random, posterior: Posterior):
        self.name = name
        self.rng = rng
        self.posterior = posterior  # shared market posterior, updated by round loop
        self.size = 6
        self.min_edge = 1.5

    def step(self, book: OrderBook, n_total: int, state: TraderState) -> None:
        running = sum(self.posterior.reveals)
        remaining = max(n_total - len(self.posterior.reveals), 0)
        fair, sigma = self.posterior.predict_settle(running, remaining)
        fair_noisy = fair + self.rng.gauss(0, max(sigma * 0.4, 1.5))
        edge_req = TAKER_FEE + max(self.min_edge, sigma * 0.4)

        for level in book.levels("sell", depth=5):
            if level["owner"] == self.name:
                continue
            if fair_noisy - level["price"] <= edge_req:
                break
            headroom = POSITION_LIMIT - state.position
            qty = min(level["qty"], headroom, self.size)
            if qty <= 0:
                break
            _, fills = book.submit_limit(self.name, "buy", level["price"], qty)
            for f in fills:
                state.apply_fill(f)

        for level in book.levels("buy", depth=5):
            if level["owner"] == self.name:
                continue
            if level["price"] - fair_noisy <= edge_req:
                break
            headroom = POSITION_LIMIT + state.position
            qty = min(level["qty"], headroom, self.size)
            if qty <= 0:
                break
            _, fills = book.submit_limit(self.name, "sell", level["price"], qty)
            for f in fills:
                state.apply_fill(f)


# ---------------------------------------------------------------------------
# Multi-opponent round
# ---------------------------------------------------------------------------
def simulate_round_mc(
    rng: random.Random,
    bot,  # OurBot or ImprovedBot
    n_draws: int,
    market_posterior: Posterior,
) -> dict:
    """One round with NoiseMM + TightMM + ValueSniper as opponents.

    market_posterior is shared by the MMs and the sniper (the "market view").
    bot.posterior is the strategy's own posterior (independent estimator).
    Both posteriors receive reveals; the bot reads only its own.
    """
    a, w = sample_round_params(rng)
    book = OrderBook()

    state_us = TraderState(bot.name)
    state_nmm = TraderState("nmm")
    state_tmm = TraderState("tmm")
    state_snp = TraderState("snp")

    bot.bid_id = None
    bot.ask_id = None
    bot.posterior.reset()
    if market_posterior is not bot.posterior:
        market_posterior.reset()

    noise_mm = NoiseMM("nmm", rng)
    tight_mm = TightMM("tmm", rng)
    sniper = ValueSniper("snp", rng, market_posterior)

    running_sum = 0.0
    position_path: list[int] = []

    for k in range(n_draws):
        x = rng.randint(a, a + w)
        running_sum += x

        bot.posterior.update(float(x))
        if market_posterior is not bot.posterior:
            market_posterior.update(float(x))

        fair, sigma = market_posterior.predict_settle(running_sum, n_draws - (k + 1))

        noise_mm.step(book, fair, sigma, state_nmm)
        tight_mm.step(book, fair, sigma, state_tmm)
        sniper.step(book, n_draws, state_snp)
        bot.step(book, state_us, n_draws)

        position_path.append(state_us.position)

    settle_price = int(running_sum)
    if bot.bid_id is not None:
        book.cancel(bot.bid_id)
    if bot.ask_id is not None:
        book.cancel(bot.ask_id)

    n_trades = state_us.n_trades  # capture before settle() resets it

    return {
        "a": a,
        "w": w,
        "settle": settle_price,
        "pnl_us": state_us.settle(settle_price),
        "n_trades": n_trades,
        "position_path": position_path,
        "pnl_nmm": state_nmm.settle(settle_price),
        "pnl_tmm": state_tmm.settle(settle_price),
        "pnl_snp": state_snp.settle(settle_price),
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def bootstrap_ci_mean(values: list[float], n_resamples: int = 1000,
                       alpha: float = 0.05, seed: int = 42) -> tuple[float, float]:
    """Bootstrap two-sided (1-alpha) CI for the mean of `values`."""
    if not values:
        return 0.0, 0.0
    n = len(values)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += values[rng.randint(0, n - 1)]
        means.append(s / n)
    means.sort()
    lo = means[int(n_resamples * alpha / 2)]
    hi = means[int(n_resamples * (1 - alpha / 2))]
    return lo, hi


def bootstrap_paired_ci(values_a: list[float], values_b: list[float],
                         n_resamples: int = 1000, alpha: float = 0.05,
                         seed: int = 42) -> tuple[float, float, float]:
    """Paired bootstrap CI on mean(B - A). Returns (mean_diff, lo, hi)."""
    n = min(len(values_a), len(values_b))
    if n == 0:
        return 0.0, 0.0, 0.0
    diffs = [values_b[i] - values_a[i] for i in range(n)]
    mean_diff = sum(diffs) / n
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += diffs[rng.randint(0, n - 1)]
        means.append(s / n)
    means.sort()
    lo = means[int(n_resamples * alpha / 2)]
    hi = means[int(n_resamples * (1 - alpha / 2))]
    return mean_diff, lo, hi


def sharpe_like(values: list[float]) -> float:
    """Mean / std (no annualization -- per-round basis)."""
    if len(values) < 2:
        return 0.0
    sd = statistics.stdev(values)
    return (sum(values) / len(values)) / max(sd, 1e-9)


def max_drawdown(cumulative: list[float]) -> float:
    """Largest peak-to-trough drop in a cumulative-PnL series."""
    if not cumulative:
        return 0.0
    peak = cumulative[0]
    worst = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > worst:
            worst = dd
    return worst


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolation-free quantile (q in [0, 1])."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[idx]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_mc(rounds: int, n_draws: int, seeds: list[int]) -> tuple[list[dict], list[dict]]:
    """Run the full MC. Returns (all_orig_rounds, all_imp_rounds), flattened
    across all seeds."""
    print(f"Building priors ({N_PRIOR_SIM:,} MC samples per posterior)...")
    posterior_orig = Posterior(random.Random(7777))
    posterior_imp = ImprovedPosterior(random.Random(7777))
    posterior_market = ImprovedPosterior(random.Random(7777))
    print(f"  Prior has {len(posterior_orig.prior):,} distinct (a, w) pairs.")
    print(f"Market makeup: NoiseMM + TightMM + ValueSniper")
    print(f"Running {len(seeds)} seeds x {rounds} rounds = "
          f"{len(seeds)*rounds:,} rounds per strategy "
          f"({2*len(seeds)*rounds:,} total).\n")

    bot_orig = OurBot("us", posterior_orig)
    bot_imp = ImprovedBot("us", posterior_imp)

    all_orig: list[dict] = []
    all_imp: list[dict] = []

    print(f"{'Seed':>6s}  {'Orig PnL':>11s}  {'Imp PnL':>11s}  {'Diff':>10s}  "
          f"{'Imp/Orig':>10s}")
    print(f"{'-'*54}")

    for seed in seeds:
        rng = random.Random(seed)
        round_orig = [simulate_round_mc(rng, bot_orig, n_draws, posterior_market)
                      for _ in range(rounds)]

        rng = random.Random(seed)
        round_imp = [simulate_round_mc(rng, bot_imp, n_draws, posterior_market)
                     for _ in range(rounds)]

        tot_o = sum(r["pnl_us"] for r in round_orig)
        tot_i = sum(r["pnl_us"] for r in round_imp)
        diff = tot_i - tot_o
        sign = "+" if diff >= 0 else ""
        ratio = (tot_i / tot_o) if abs(tot_o) > 1e-6 else float("inf")
        ratio_str = f"{ratio:>9.2f}x" if math.isfinite(ratio) else "      n/a"
        print(f"{seed:>6d}  {tot_o:>11.1f}  {tot_i:>11.1f}  {sign}{diff:>9.1f}  "
              f"{ratio_str}")

        all_orig.extend(round_orig)
        all_imp.extend(round_imp)

    print()
    return all_orig, all_imp


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot_mc(all_orig: list[dict], all_imp: list[dict], output_path: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib/numpy not installed. Install with: pip install matplotlib numpy")
        return

    pnls_o = np.array([r["pnl_us"] for r in all_orig])
    pnls_i = np.array([r["pnl_us"] for r in all_imp])
    ws = np.array([r["w"] for r in all_orig])  # same a/w sequence either bot

    n = len(pnls_o)
    cum_o = np.cumsum(pnls_o)
    cum_i = np.cumsum(pnls_i)
    rounds_x = np.arange(1, n + 1)

    # Drawdown
    peak_o = np.maximum.accumulate(cum_o)
    peak_i = np.maximum.accumulate(cum_i)
    dd_o = peak_o - cum_o
    dd_i = peak_i - cum_i
    max_dd_o = float(dd_o.max())
    max_dd_i = float(dd_i.max())

    # Stats
    lo_o, hi_o = bootstrap_ci_mean(pnls_o.tolist())
    lo_i, hi_i = bootstrap_ci_mean(pnls_i.tolist())
    diff_mean, diff_lo, diff_hi = bootstrap_paired_ci(pnls_o.tolist(), pnls_i.tolist())
    sh_o = sharpe_like(pnls_o.tolist())
    sh_i = sharpe_like(pnls_i.tolist())
    var5_o = quantile(pnls_o.tolist(), 0.05)
    var5_i = quantile(pnls_i.tolist(), 0.05)
    sig = (diff_lo > 0) or (diff_hi < 0)
    rel_imp = ((pnls_i.mean() - pnls_o.mean()) /
               max(abs(pnls_o.mean()), 1e-9) * 100.0)

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle(
        f"Full Monte Carlo: {n:,} rounds  |  "
        f"Market = NoiseMM + TightMM + ValueSniper",
        fontsize=14, fontweight="bold",
    )

    # 1. Cumulative PnL
    ax = axes[0, 0]
    ax.plot(rounds_x, cum_o, label=f"Original  (final {cum_o[-1]:+.0f})",
            color="tab:blue", lw=1.8)
    ax.plot(rounds_x, cum_i, label=f"Improved  (final {cum_i[-1]:+.0f})",
            color="tab:orange", lw=1.8)
    ax.fill_between(rounds_x, cum_o, cum_i,
                    where=(cum_i >= cum_o), alpha=0.15, color="tab:orange",
                    interpolate=True)
    ax.fill_between(rounds_x, cum_o, cum_i,
                    where=(cum_i < cum_o), alpha=0.15, color="tab:blue",
                    interpolate=True)
    ax.axhline(0, color="gray", lw=0.5)
    ax.set_xlabel("Round (across all seeds)")
    ax.set_ylabel("Cumulative PnL")
    ax.set_title("Cumulative PnL")
    ax.legend(loc="best", fontsize=9)
    ax.grid(alpha=0.3)

    # 2. PnL distribution
    ax = axes[0, 1]
    lo = float(min(pnls_o.min(), pnls_i.min()))
    hi = float(max(pnls_o.max(), pnls_i.max()))
    bins = np.linspace(lo, hi, 40)
    ax.hist(pnls_o, bins=bins, alpha=0.55, color="tab:blue", edgecolor="white",
            linewidth=0.4, label=f"Original  (mu={pnls_o.mean():.2f})")
    ax.hist(pnls_i, bins=bins, alpha=0.55, color="tab:orange", edgecolor="white",
            linewidth=0.4, label=f"Improved  (mu={pnls_i.mean():.2f})")
    ax.axvline(pnls_o.mean(), color="tab:blue", linestyle="--", lw=1.5)
    ax.axvline(pnls_i.mean(), color="tab:orange", linestyle="--", lw=1.5)
    ax.axvline(0, color="gray", lw=0.5)
    ax.set_xlabel("Per-round PnL")
    ax.set_ylabel("Count")
    ax.set_title("Per-Round PnL Distribution")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)

    # 3. Drawdown
    ax = axes[0, 2]
    ax.fill_between(rounds_x, 0, -dd_o, alpha=0.45, color="tab:blue",
                    label=f"Original  max DD = {max_dd_o:.0f}")
    ax.fill_between(rounds_x, 0, -dd_i, alpha=0.45, color="tab:orange",
                    label=f"Improved  max DD = {max_dd_i:.0f}")
    ax.set_xlabel("Round")
    ax.set_ylabel("Drawdown from peak (negative)")
    ax.set_title("Drawdown Curves")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)

    # 4. PnL by w (regime)
    ax = axes[1, 0]
    W_CAP = 8
    ws_bucketed = np.minimum(ws, W_CAP)
    w_values = sorted(set(ws_bucketed.tolist()))
    box_data_o = [pnls_o[ws_bucketed == w].tolist() for w in w_values]
    box_data_i = [pnls_i[ws_bucketed == w].tolist() for w in w_values]
    positions = np.arange(len(w_values), dtype=float)
    width = 0.36
    bp_o = ax.boxplot(box_data_o, positions=positions - width / 2,
                      widths=width * 0.9, patch_artist=True, showfliers=False)
    bp_i = ax.boxplot(box_data_i, positions=positions + width / 2,
                      widths=width * 0.9, patch_artist=True, showfliers=False)
    for patch in bp_o["boxes"]:
        patch.set_facecolor("tab:blue")
        patch.set_alpha(0.55)
    for patch in bp_i["boxes"]:
        patch.set_facecolor("tab:orange")
        patch.set_alpha(0.55)
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{int(w)}{'+' if int(w) == W_CAP else ''}" for w in w_values])
    ax.set_xlabel("Range width w  (bucketed at >=8)")
    ax.set_ylabel("Per-round PnL")
    ax.set_title("PnL by Range Width w (regime)")
    ax.axhline(0, color="gray", lw=0.5)
    ax.grid(alpha=0.3, axis="y")
    # Legend proxy
    import matplotlib.patches as mpatches
    leg_o = mpatches.Patch(color="tab:blue", alpha=0.55, label="Original")
    leg_i = mpatches.Patch(color="tab:orange", alpha=0.55, label="Improved")
    ax.legend(handles=[leg_o, leg_i], fontsize=9, loc="best")

    # 5. Paired diff distribution
    ax = axes[1, 1]
    diffs = pnls_i - pnls_o
    ax.hist(diffs, bins=40, color="tab:purple", alpha=0.7, edgecolor="white",
            linewidth=0.4)
    ax.axvline(0, color="gray", lw=1)
    ax.axvline(float(diffs.mean()), color="tab:red", linestyle="--", lw=1.5,
               label=f"mu = {diffs.mean():+.2f}")
    ax.axvspan(diff_lo, diff_hi, alpha=0.18, color="tab:red",
               label=f"95% CI [{diff_lo:+.2f}, {diff_hi:+.2f}]")
    ax.set_xlabel("Improved - Original PnL  (per round)")
    ax.set_ylabel("Count")
    ax.set_title("Paired Difference Distribution")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)

    # 6. Stats table
    ax = axes[1, 2]
    ax.axis("off")
    win_o = float((pnls_o > 0).mean())
    win_i = float((pnls_i > 0).mean())

    stats = (
        f"Total rounds: {n:,}\n\n"
        f"{'':<18s}{'Original':>13s}{'Improved':>13s}\n"
        f"{'-' * 44}\n"
        f"{'Total PnL':<18s}{pnls_o.sum():>13.1f}{pnls_i.sum():>13.1f}\n"
        f"{'Mean / round':<18s}{pnls_o.mean():>13.3f}{pnls_i.mean():>13.3f}\n"
        f"{'95% CI low':<18s}{lo_o:>13.3f}{lo_i:>13.3f}\n"
        f"{'95% CI high':<18s}{hi_o:>13.3f}{hi_i:>13.3f}\n"
        f"{'Std / round':<18s}{pnls_o.std():>13.2f}{pnls_i.std():>13.2f}\n"
        f"{'Median':<18s}"
        f"{float(np.median(pnls_o)):>13.1f}{float(np.median(pnls_i)):>13.1f}\n"
        f"{'Max round':<18s}{pnls_o.max():>13.1f}{pnls_i.max():>13.1f}\n"
        f"{'Min round':<18s}{pnls_o.min():>13.1f}{pnls_i.min():>13.1f}\n"
        f"{'Sharpe-like':<18s}{sh_o:>13.3f}{sh_i:>13.3f}\n"
        f"{'VaR_5 (5th pct)':<18s}{var5_o:>13.1f}{var5_i:>13.1f}\n"
        f"{'Max drawdown':<18s}{max_dd_o:>13.1f}{max_dd_i:>13.1f}\n"
        f"{'Win rate':<18s}{f'{win_o:.1%}':>13s}{f'{win_i:.1%}':>13s}\n"
        f"\n"
        f"Paired (Imp - Orig) per-round:\n"
        f"  mean   = {diff_mean:+.3f}\n"
        f"  95% CI = [{diff_lo:+.3f}, {diff_hi:+.3f}]\n"
        f"  signif = {'YES (CI excludes 0)' if sig else 'NO  (CI includes 0)'}\n"
        f"  rel.   = {rel_imp:+.1f}%\n"
    )
    ax.text(0.02, 0.98, stats, family="monospace", fontsize=10, va="top", ha="left",
            transform=ax.transAxes)
    ax.set_title("Statistical Summary")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    print(f"Saved plot to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full Monte Carlo simulation for Day 1 strategies"
    )
    parser.add_argument("--rounds", type=int, default=200,
                        help="Rounds per seed (default 200)")
    parser.add_argument("--n-draws", type=int, default=10,
                        help="Reveals per round (default 10)")
    parser.add_argument("--seeds", type=int, default=10,
                        help="Number of seeds (default 10)")
    parser.add_argument("--seed-base", type=int, default=1,
                        help="Base seed (default 1)")
    parser.add_argument("--output", default="day1/mc_results.png",
                        help="Plot output path")
    parser.add_argument("--quick", action="store_true",
                        help="Quick mode: 50 rounds x 3 seeds (overrides above)")
    args = parser.parse_args()

    if args.quick:
        args.rounds = 50
        args.seeds = 3

    seeds = [args.seed_base + i * 7919 for i in range(args.seeds)]
    all_orig, all_imp = run_mc(args.rounds, args.n_draws, seeds)

    pnls_o = [r["pnl_us"] for r in all_orig]
    pnls_i = [r["pnl_us"] for r in all_imp]
    n = len(pnls_o)

    lo_o, hi_o = bootstrap_ci_mean(pnls_o)
    lo_i, hi_i = bootstrap_ci_mean(pnls_i)
    diff_mean, diff_lo, diff_hi = bootstrap_paired_ci(pnls_o, pnls_i)
    sig = (diff_lo > 0) or (diff_hi < 0)

    print(f"{'='*72}")
    print(f"AGGREGATE  ({n:,} rounds total)")
    print(f"{'='*72}")
    print(f"  Original :  total={sum(pnls_o):+10.1f}  "
          f"mean/rnd={sum(pnls_o)/n:+8.3f}  95% CI=[{lo_o:+.3f}, {hi_o:+.3f}]")
    print(f"  Improved :  total={sum(pnls_i):+10.1f}  "
          f"mean/rnd={sum(pnls_i)/n:+8.3f}  95% CI=[{lo_i:+.3f}, {hi_i:+.3f}]")
    print(f"  Diff (I-O): mean={diff_mean:+.3f}/rnd  "
          f"95% CI=[{diff_lo:+.3f}, {diff_hi:+.3f}]  "
          f"significant={'YES' if sig else 'no'}")
    print(f"{'='*72}\n")

    plot_mc(all_orig, all_imp, args.output)


if __name__ == "__main__":
    main()
