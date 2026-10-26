"""Plot a ``summarize-experiment`` CSV as two report figures (PDF and PNG).

Usage (needs the optional ``analysis`` extra, i.e. matplotlib):

    python scripts/plot_experiment.py --summary summary.csv --output-dir figures/

Figure 1: total cost relative to the hindsight oracle versus c_r/c_v.
Figure 2: cost relative to the oracle by demand group at one cost ratio.
Error bars are 95% t-intervals across seeds. Costs are nominal units.
"""
import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Fixed categorical order; color follows the policy, never its rank.
POLICIES = {
    "verify-only": ("Verify-Only", "#2a78d6", "o"),
    "eager-refresh": ("Eager Refresh", "#eb6834", "s"),
    "repair-on-first-access": ("Repair-on-First-Access", "#1baf7a", "^"),
    "periodic-refresh": ("Periodic Refresh", "#eda100", "v"),
    "oldest-first": ("Oldest-First", "#e87ba4", "D"),
    "p1": ("P1 threshold", "#008300", "P"),
    "p2": ("P2 predicted", "#6250d6", "X"),
}
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3de"


def load(path):
    with open(path, newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def value(row, metric):
    mean, ci = row[f"{metric}_mean"], row[f"{metric}_ci95"]
    return float(mean), float(ci) if ci else 0.0


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelcolor=INK, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def cost_vs_ratio(rows, out):
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    for policy, (label, color, marker) in POLICIES.items():
        points = sorted((float(r["cost_ratio"]), *value(r, "cost_vs_oracle"))
                        for r in rows if r["policy"] == policy and r["group"] == "all")
        if not points:
            continue
        xs, ys, cis = zip(*points)
        ax.errorbar(xs, ys, yerr=cis, color=color, marker=marker, markersize=6,
                    linewidth=2, capsize=3, label=label)
    ax.axhline(1.0, color=MUTED, linewidth=1, linestyle="--")
    ax.text(ax.get_xlim()[1], 1.0, " oracle", color=MUTED, fontsize=8, va="center")
    ax.set_xlabel(r"cost ratio $\rho = c_r / c_v$", color=INK, fontsize=9)
    ax.set_ylabel(r"$C(\pi) / C(\mathrm{oracle})$", color=INK, fontsize=9)
    ax.set_xticks(sorted({float(r["cost_ratio"]) for r in rows}))
    style(ax)
    ax.legend(fontsize=7, frameon=False, loc="upper left", bbox_to_anchor=(1.02, 1))
    fig.tight_layout()
    for suffix in ("pdf", "png"):
        fig.savefig(out / f"cost_vs_ratio.{suffix}", dpi=200)
    plt.close(fig)


def cost_by_group(rows, out, ratio):
    groups = ("cold", "warm", "hot")
    fig, axes = plt.subplots(1, 3, figsize=(7, 2.8), sharey=True)
    names = list(POLICIES)
    for ax, group in zip(axes, groups):
        selected = {r["policy"]: r for r in rows
                    if r["group"] == group and float(r["cost_ratio"]) == ratio}
        ys, cis = zip(*(value(selected[p], "cost_vs_oracle") for p in names))
        positions = range(len(names))
        ax.barh(positions, ys, xerr=cis, color="#2a78d6", height=0.7,
                error_kw={"ecolor": INK, "capsize": 2, "linewidth": 0.8})
        ax.axvline(1.0, color=MUTED, linewidth=1, linestyle="--")
        ax.set_title(group, color=INK, fontsize=9)
        ax.set_yticks(list(positions), [POLICIES[p][0] for p in names])
        ax.invert_yaxis()
        style(ax)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
    axes[1].set_xlabel(rf"$C(\pi) / C(\mathrm{{oracle}})$ at $\rho = {ratio:g}$", color=INK, fontsize=9)
    fig.tight_layout()
    for suffix in ("pdf", "png"):
        fig.savefig(out / f"cost_by_group.{suffix}", dpi=200)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--group-ratio", type=float, default=5.0)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = load(args.summary)
    cost_vs_ratio(rows, args.output_dir)
    cost_by_group(rows, args.output_dir, args.group_ratio)


if __name__ == "__main__":
    main()
