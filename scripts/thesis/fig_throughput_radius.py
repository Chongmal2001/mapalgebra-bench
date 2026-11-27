import hashlib
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
PERF = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "data" / "results" / "final" / "perf.csv"
OUT = HERE / "out"
NAME = "fig-throughput-radius"
REPS = 3
TEXTWIDTH_IN = 6.0
HOST = "vm-ach"
STORE = "s2-berlin-sub4"
RADII = [1, 2, 4, 8, 16]
PANELS = [("max", "square"), ("max", "circle"), ("mean", "square"), ("mean", "circle")]
SYSTEMS = {
    "nes":        dict(label="NES",        color="#2a78d6", marker="o", ls="-"),
    "geotrellis": dict(label="GeoTrellis micro-batch", color="#eb6834", marker="s", ls="--"),
}

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
    d = d[(d.host == HOST) & (d.store == STORE) & (d.threads == 1) & (d.frames_per_window == 1)
          & (d.rate_fps.fillna(0) == 0) & d.system.isin(SYSTEMS)].copy()
    # Definition 6.1: cells of the input grid per band and frame, edge padding excluded
    d["mcells_s"] = d.width * d.height * d.bands * d.frames / d.elapsed_s / 1e6
    keys = ["op", "shape", "radius", "system"]
    return d.groupby(keys).mcells_s.agg(["median", "min", "max", "count"]).reset_index()


def main():
    OUT.mkdir(exist_ok=True)
    s = load(PERF)
    s = s[s.radius.isin(RADII)]
    expected = len(PANELS) * len(SYSTEMS) * len(RADII)
    if len(s) != expected or (s["count"] != REPS).any():
        raise SystemExit(f"expected {expected} configurations with {REPS} runs each:\n{s.to_string()}")

    fig, axes = plt.subplots(2, 2, figsize=(TEXTWIDTH_IN, 4.2), sharex=True, sharey=True)
    for ax, (op, shape) in zip(axes.flat, PANELS):
        for sysname, st in SYSTEMS.items():
            g = s[(s.op == op) & (s["shape"] == shape) & (s.system == sysname)].sort_values("radius")
            ax.errorbar(g.radius, g["median"], yerr=[g["median"] - g["min"], g["max"] - g["median"]],
                        color=st["color"], marker=st["marker"], ls=st["ls"], label=st["label"],
                        capsize=2, elinewidth=0.8)
        ax.set_title(f"{op.capitalize()}, {shape}")
        ax.set_xscale("log", base=2)
        ax.set_xticks(RADII, [str(r) for r in RADII])
        ax.xaxis.set_minor_locator(mpl.ticker.NullLocator())
        ax.tick_params(labelbottom=True)
        ax.set_yscale("log")
        ticks = [0.3, 1, 3, 10, 30]
        ax.set_yticks(ticks, [f"{t:g}" for t in ticks])
        ax.yaxis.set_minor_formatter(mpl.ticker.NullFormatter())
    for ax in axes.flat:
        ax.set_xlabel("Radius")
    for ax in axes[:, 0]:
        ax.set_ylabel("Cell throughput (Mcells/s)")
    h, l = axes[0, 0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=len(SYSTEMS), frameon=False, bbox_to_anchor=(0.5, 1.03))
    fig.tight_layout(rect=(0, 0, 1, 0.96))

    s.assign(host=HOST, store=STORE, threads=1, frames_per_window=1).to_csv(OUT / f"{NAME}.csv", index=False)
    fig.savefig(OUT / f"{NAME}.pdf")
    fig.savefig(OUT / f"{NAME}.png", dpi=200)
    sha = hashlib.sha256(PERF.read_bytes()).hexdigest()
    (OUT / f"{NAME}.sources.txt").write_text(f"{PERF.name}\t{len(pd.read_csv(PERF))} runs\tsha256 {sha}\n")
    print("ok", NAME, sha[:12])


if __name__ == "__main__":
    main()
