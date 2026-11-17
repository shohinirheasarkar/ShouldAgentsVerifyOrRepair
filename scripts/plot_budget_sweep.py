"""Plot mean total cost versus repair budget B, with the budget-aware oracle.

Usage: python scripts/plot_budget_sweep.py docs/step4/budget-sweep.csv docs/step4/budget_vs_cost.png
Requires the ``analysis`` extra (matplotlib). Unbudgeted rows are drawn at "∞"
against the unbudgeted oracle.
"""
import csv
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SERIES = [  # fixed categorical order (validated palette, light mode)
    ("p1", "P1 threshold", "#2a78d6"),
    ("p2", "P2 predicted", "#eb6834"),
    ("repair-on-first-access", "Repair-on-first-access", "#1baf7a"),
    ("eager-refresh", "Eager refresh", "#eda100"),
    ("verify-only", "Verify-only", "#e87ba4"),
]
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3de"


def main(source: str, target: str) -> None:
    costs, bounds = defaultdict(list), defaultdict(list)
    for row in csv.DictReader(open(source, encoding="utf-8")):
        if row["group"] != "all":
            continue
        budget = row["budget_units"] or "inf"
        costs[row["policy"], budget].append(float(row["total_cost"]))
        if row["policy"] == "p1":
            bounds[budget].append(float(row["budget_oracle_cost"] or row["oracle_cost"]))
    budgets = sorted((b for b in bounds if b != "inf"), key=float) + (["inf"] if "inf" in bounds else [])
    labels = [f"{float(b):g}" if b != "inf" else "∞" for b in budgets]
    x = list(range(len(budgets)))

    fig, ax = plt.subplots(figsize=(8, 4.6), dpi=160)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=9)

    ends = []

    def draw(values, label, color, style="-"):
        ax.plot(x, values, style, color=color, linewidth=2, marker="o", markersize=5,
                markeredgecolor="white", markeredgewidth=1.2, label=label)
        ends.append([values[-1], label])

    for policy, label, color in SERIES:
        draw([sum(costs[policy, b]) / len(costs[policy, b]) for b in budgets], label, color)
    draw([sum(bounds[b]) / len(bounds[b]) for b in budgets], "Budget-aware oracle", INK, "--")

    # Direct end labels, spread apart so close lines stay readable.
    gap = 0.045 * max(value for value, _ in ends)
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + gap)
    for value, label in ends:
        ax.annotate(label, (x[-1], value), xytext=(10, 0), textcoords="offset points",
                    va="center", fontsize=8.5, color=INK)

    ax.set_xticks(x, labels)
    ax.set_xlim(-0.3, len(x) - 1 + 1.9)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Repair budget B per hour (c_r = 5)", color=MUTED, fontsize=9)
    ax.set_ylabel("Mean total cost (nominal units)", color=MUTED, fontsize=9)
    ax.set_title("Total cost vs. repair budget (4 traces × 100 memories, c_v = 1)", color=INK,
                 fontsize=10.5, loc="left")
    ax.legend(frameon=False, fontsize=8, loc="upper center", ncol=3, bbox_to_anchor=(0.45, -0.16))
    fig.tight_layout()
    fig.savefig(target)


if __name__ == "__main__":
    main(*sys.argv[1:3])
