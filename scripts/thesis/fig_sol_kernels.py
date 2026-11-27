import hashlib
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
PERF = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "data" / "results" / "final" / "perf.csv"
OUT = HERE / "out"
NAME = "fig-sol-kernels"
REPS = 3
HOST = "vm-ach"
STORE = "s2-berlin-sub4"
RADII = [1, 2, 4, 8, 16]
# system nes runs the default kernel, compensated running sums for square Mean
SYSTEMS = {
    "nes":         dict(label="Running sums, compensated", color="#2f5597", marker="o", ls="-"),
    "nes-running": dict(label="Running sums, plain",       color="#1baf7a", marker="^", ls="--"),
    "nes-exact":   dict(label="Exact",                     color="#cc6600", marker="s", ls="-"),
}

mpl.rcParams.update({
    "font.family": ["Helvetica", "TeX Gyre Heros", "Nimbus Sans", "Arial", "sans-serif"],
    "font.size": 9, "axes.labelsize": 9, "legend.fontsize": 8.5, "xtick.labelsize": 8.5, "ytick.labelsize": 8.5,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": "#e0e0e0", "grid.linewidth": 0.6, "axes.axisbelow": True,
    "lines.linewidth": 1.6, "lines.markersize": 4.5,
    "savefig.bbox": "tight", "savefig.pad_inches": 0.02, "pdf.fonttype": 42,
})


def load(path):
    d = pd.read_csv(path)
    d = d[(d.host == HOST) & (d.store == STORE) & (d.op == "mean") & (d["shape"] == "square") & (d.threads == 1)
          & (d.frames_per_window == 1) & (d.rate_fps.fillna(0) == 0) & d.radius.isin(RADII)
          & d.system.isin(SYSTEMS)].copy()
    # cells of the input grid per band and frame, edge padding excluded
    d["mcells_s"] = d.width * d.height * d.bands * d.frames / d.elapsed_s / 1e6
    return d.groupby(["system", "radius"]).agg(median=("mcells_s", "median"), min=("mcells_s", "min"),
                                               max=("mcells_s", "max"), count=("mcells_s", "count"),
                                               elapsed_median_s=("elapsed_s", "median")).reset_index()


def main():
    OUT.mkdir(exist_ok=True)
    s = load(PERF)
    expected = len(SYSTEMS) * len(RADII)
    if len(s) != expected or (s["count"] != REPS).any():
        raise SystemExit(f"expected {expected} configurations with {REPS} runs each:\n{s.to_string()}")

    fig, ax = plt.subplots(figsize=(4.6, 2.6))
    for sysname, st in SYSTEMS.items():
        g = s[s.system == sysname].sort_values("radius")
        ax.errorbar(g.radius, g["median"], yerr=[g["median"] - g["min"], g["max"] - g["median"]],
                    color=st["color"], marker=st["marker"], ls=st["ls"], label=st["label"],
                    capsize=2, elinewidth=0.8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(RADII, [str(r) for r in RADII])
    ax.xaxis.set_minor_locator(mpl.ticker.NullLocator())
    ax.set_yscale("log")
    ax.set_yticks([1, 3, 10, 30], ["1", "3", "10", "30"])
    ax.yaxis.set_minor_formatter(mpl.ticker.NullFormatter())
    ax.set_ylim(0.6, 80)
    ax.set_xlabel("Radius $r$")
    ax.set_ylabel("Throughput (million cells/s)")
    ax.legend(frameon=False, loc="lower left")

    s.assign(host=HOST, store=STORE, op="mean", shape="square", threads=1, frames_per_window=1).to_csv(
        OUT / f"{NAME}.csv", index=False)
    fig.savefig(OUT / f"{NAME}.pdf")
    fig.savefig(OUT / f"{NAME}.png", dpi=300)

    m = s.set_index(["system", "radius"])["median"].unstack(0)
    e = s.set_index(["system", "radius"])["elapsed_median_s"].unstack(0)
    with open(OUT / f"{NAME}-text.txt", "w") as f:
        f.write(f"exact throughput r=1 / r=16 [Mcells/s]: {m['nes-exact'][1]:.1f} / {m['nes-exact'][16]:.2f}\n")
        f.write(f"exact elapsed r=1 / r=16 [s]: {e['nes-exact'][1]:.1f} / {e['nes-exact'][16]:.0f}\n")
        f.write(f"plain running throughput range [Mcells/s]: {m['nes-running'].min():.1f} to {m['nes-running'].max():.1f}\n")
        f.write(f"compensated throughput range [Mcells/s]: {m['nes'].min():.1f} to {m['nes'].max():.1f}\n")
        f.write(f"compensated / plain running elapsed, median over radii: {(e['nes'] / e['nes-running']).median():.2f}\n")
    sha = hashlib.sha256(PERF.read_bytes()).hexdigest()
    (OUT / f"{NAME}.sources.txt").write_text(f"{PERF.name}\t{len(pd.read_csv(PERF))} runs\tsha256 {sha}\n")
    print("ok", NAME, sha[:12])


if __name__ == "__main__":
    main()
