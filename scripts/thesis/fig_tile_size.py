"""Appendix figure: NES cell throughput over the tile size.

Usage: python fig_tile_size.py [path/to/perf.csv]
Defaults to data/results/final/perf.csv. Writes out/fig-tile-size.{pdf,png,csv,sources.txt}.
Style follows fig_throughput_radius.py.
"""
import hashlib
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
PERF = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "data" / "results" / "final" / "perf.csv"
OUT = HERE / "out"
NAME = "fig-tile-size"
REPS = 3
TEXTWIDTH_IN = 6.0
HOST = "vm-ach"
# one store per tile size, all holding the first four Sentinel-2 scenes on the same grid
STORES = {
    16: "s2-berlin-halo4",
    32: "s2-berlin-4-t32",
    64: "s2-berlin-4-t64",
    128: "s2-berlin-4-t128",
    256: "s2-berlin-sub4",
    512: "s2-berlin-4-t512",
    1024: "s2-berlin-4-t1024",
}
TILES = sorted(STORES)
PANELS = ["max", "mean"]
RADII = {
    1:  dict(label="NES, $r=1$",  color="#2a78d6", marker="o", ls="-"),
    16: dict(label="NES, $r=16$", color="#14457f", marker="D", ls="--"),
}
PASSTHROUGH = dict(label="NES, source and sink only", color="#6b6b6b", marker="x", ls=":")

mpl.rcParams.update({
    "font.family": "serif", "font.size": 8.5, "axes.titlesize": 8.5, "axes.labelsize": 8.5,
    "legend.fontsize": 7.5, "xtick.labelsize": 7.5, "ytick.labelsize": 7.5,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": "#e3e2dc", "grid.linewidth": 0.6, "axes.axisbelow": True,
    "lines.linewidth": 1.6, "lines.markersize": 4.5,
    "savefig.bbox": "tight", "savefig.pad_inches": 0.02, "pdf.fonttype": 42,
})


def load(path):
    d = pd.read_csv(path)
    store_tile = {v: k for k, v in STORES.items()}
    d = d[(d.host == HOST) & d.store.isin(store_tile) & (d.threads == 1) & (d.frames_per_window == 1)
          & (d.rate_fps.fillna(0) == 0) & (d["shape"] == "square")].copy()
    d = d[d.tile_size == d.store.map(store_tile)]
    focal = d[(d.system == "nes") & d.radius.isin(RADII) & d.op.isin(PANELS)]
    passthrough = d[(d.system == "nes-passthrough")]
    d = pd.concat([focal, passthrough])
    # Definition 6.1: cells of the input grid per band and frame, edge padding excluded
    d["mcells_s"] = d.width * d.height * d.bands * d.frames / d.elapsed_s / 1e6
    keys = ["system", "op", "radius", "tile_size"]
    s = d.groupby(keys).mcells_s.agg(["median", "min", "max", "count"]).reset_index()
    p = s[s.system == "nes-passthrough"]
    f = s[s.system == "nes"]
    expected = len(PANELS) * len(RADII) * len(TILES)
    if len(f) != expected or len(p) != len(TILES) or (s["count"] != REPS).any():
        raise SystemExit(f"expected {expected} focal and {len(TILES)} passthrough configurations "
                         f"with {REPS} runs each:\n{s.to_string()}")
    return f, p


def errorbar(ax, g, st):
    g = g.sort_values("tile_size")
    ax.errorbar(g.tile_size, g["median"], yerr=[g["median"] - g["min"], g["max"] - g["median"]],
                color=st["color"], marker=st["marker"], ls=st["ls"], label=st["label"],
                capsize=2, elinewidth=0.8)


def main():
    OUT.mkdir(exist_ok=True)
    f, p = load(PERF)

    fig, axes = plt.subplots(1, 2, figsize=(TEXTWIDTH_IN, 2.5), sharey=True)
    for ax, op in zip(axes, PANELS):
        errorbar(ax, p, PASSTHROUGH)
        for r, st in RADII.items():
            errorbar(ax, f[(f.op == op) & (f.radius == r)], st)
        ax.set_title(f"{'Maximum' if op == 'max' else 'Mean'}, square")
        ax.set_xscale("log", base=2)
        ax.set_xticks(TILES, [str(t) for t in TILES])
        ax.xaxis.set_minor_locator(mpl.ticker.NullLocator())
        ax.set_xlabel("Tile size (cells per side)")
        ax.set_yscale("log")
        ticks = [1, 3, 10, 30, 100, 300]
        ax.set_yticks(ticks, [f"{t:g}" for t in ticks])
        ax.yaxis.set_minor_formatter(mpl.ticker.NullFormatter())
    axes[0].set_ylabel("Cell throughput (Mcells/s)")
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.06))
    fig.tight_layout(rect=(0, 0, 1, 0.93))

    pd.concat([f, p]).assign(host=HOST, shape="square", threads=1, frames_per_window=1) \
        .to_csv(OUT / f"{NAME}.csv", index=False)
    fig.savefig(OUT / f"{NAME}.pdf")
    fig.savefig(OUT / f"{NAME}.png", dpi=200)
    sha = hashlib.sha256(PERF.read_bytes()).hexdigest()
    (OUT / f"{NAME}.sources.txt").write_text(f"{PERF.name}\t{len(pd.read_csv(PERF))} runs\tsha256 {sha}\n")
    print("ok", NAME, sha[:12])


if __name__ == "__main__":
    main()
