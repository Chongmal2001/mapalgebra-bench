"""Chapter 5 figures, generated only from the benchmark result tables written by `mabench perf-table`.

Usage:
    python figures.py [RESULTS_DIR]

RESULTS_DIR is mapalgebra-bench/data/results (default: ../../data/results relative to this file, i.e. the script
lives in mapalgebra-bench/scripts/thesis/). It must contain
    vm-final/perf.csv   every run of the VM result set (one row per run; paced G6 runs have rate_fps > 0)
    pc-isa/perf.csv     every run of the workstation result set
Nothing else is read; no number is typed in by hand.

Outputs in out/ next to this file:
    <figure>.pdf   for LaTeX          <figure>.png   preview
    <figure>.csv   every plotted value with the configuration it comes from and the number of runs behind it
    SOURCES.txt    SHA-256 of the input tables, so a figure can be matched to the exact data it was drawn from
The script stops if a plotted configuration does not have exactly REPS runs.
"""
import hashlib
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "data" / "results"
SOURCES = {"VM": RESULTS / "vm-final" / "perf.csv", "Workstation": RESULTS / "pc-isa" / "perf.csv"}
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
REPS = 3                     # repetitions per configuration in both result sets
TEXTWIDTH_IN = 6.0           # \the\textwidth of the thesis template, in inches

# ---------------------------------------------------------------- style
mpl.rcParams.update({
    "font.family": "serif", "font.size": 8.5, "axes.titlesize": 8.5, "axes.labelsize": 8.5,
    "legend.fontsize": 7.5, "xtick.labelsize": 7.5, "ytick.labelsize": 7.5,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": "#e3e2dc", "grid.linewidth": 0.6, "axes.axisbelow": True,
    "lines.linewidth": 1.6, "lines.markersize": 4.5,
    "savefig.bbox": "tight", "savefig.pad_inches": 0.02, "pdf.fonttype": 42,
})
# One fixed colour + marker + line style per system, so series stay distinguishable in greyscale print.
SYSTEMS = {
    "nes":         dict(label="NES (exact)",   color="#2a78d6", marker="o", ls="-"),
    "geotrellis":  dict(label="GeoTrellis",    color="#eb6834", marker="s", ls="-"),
    "nes-running": dict(label="NES (running)", color="#1baf7a", marker="^", ls="--"),
    "nes-kahan":   dict(label="NES (Kahan)",   color="#6250d6", marker="v", ls=":"),
}
INK_MUTED = "#6b6a63"
KEYS = ["store", "op", "shape", "radius", "system", "threads", "frames_per_window", "slide_frames", "tile_size"]


# ---------------------------------------------------------------- data
def load(path):
    d = pd.read_csv(path)
    d["real_cells"] = d.width * d.height * d.bands * d.frames     # cells inside the raster; edge padding excluded
    d["mcells_s"] = d.real_cells / d.elapsed_s / 1e6
    return d


ALL_VM = load(SOURCES["VM"])
VM = ALL_VM[ALL_VM.rate_fps.fillna(0) == 0]          # maximum-rate runs (G1-G5, G7, G8)
PACED = ALL_VM[ALL_VM.rate_fps.fillna(0) > 0]        # paced runs (G6)
PC = load(SOURCES["Workstation"])
RECORDS = {}


def summary(d, col, keys=KEYS):
    """Median, minimum, maximum and run count of `col` per configuration."""
    return d.groupby(keys)[col].agg(["median", "min", "max", "count"]).reset_index()


def record(fig, panel, series, frame, x, y="median"):
    """Remember every plotted value; checked and written to out/<fig>.csv by save()."""
    t = frame.copy()
    t.insert(0, "panel", panel)
    t.insert(1, "series", series)
    t["x"] = t[x]
    t["y"] = t[y]
    RECORDS.setdefault(fig, []).append(t)


def save(fig_obj, name):
    t = pd.concat(RECORDS.get(name, []), ignore_index=True)
    if "count" in t:
        bad = t[t["count"] != REPS]
        if len(bad):
            raise SystemExit(f"{name}: configurations without {REPS} runs:\n{bad.to_string()}")
    t.to_csv(OUT / f"{name}.csv", index=False)
    fig_obj.savefig(OUT / f"{name}.pdf")
    fig_obj.savefig(OUT / f"{name}.png", dpi=200)
    plt.close(fig_obj)


# ---------------------------------------------------------------- drawing helpers
def errbar(ax, x, g, sysname, **kw):
    s = SYSTEMS[sysname]
    ax.errorbar(x, g["median"], yerr=[g["median"] - g["min"], g["max"] - g["median"]], color=s["color"],
                marker=s["marker"], ls=s["ls"], label=s["label"], capsize=2, elinewidth=0.8, **kw)


def plain_log_y(ax, ticks):
    ax.set_yscale("log")
    ax.set_yticks(ticks, [f"{t:g}" for t in ticks])
    ax.yaxis.set_minor_formatter(mpl.ticker.NullFormatter())


def log2_x(ax, ticks):
    ax.set_xscale("log", base=2)
    ax.set_xticks(ticks, [str(t) for t in ticks])
    ax.xaxis.set_minor_locator(mpl.ticker.NullLocator())


def shared_xlabel(fig, axes, text, pad_pt=19):
    axes = list(np.ravel(axes))
    y0 = min(a.get_position().y0 for a in axes)
    x = (min(a.get_position().x0 for a in axes) + max(a.get_position().x1 for a in axes)) / 2
    tr = mpl.transforms.offset_copy(fig.transFigure, fig=fig, y=-pad_pt, units="points")
    fig.text(x, y0, text, ha="center", va="top", transform=tr, fontsize=8.5)


def shared_ylabel(fig, axes, text, pad_pt=26):
    axes = list(np.ravel(axes))
    x0 = min(a.get_position().x0 for a in axes)
    y = (min(a.get_position().y0 for a in axes) + max(a.get_position().y1 for a in axes)) / 2
    tr = mpl.transforms.offset_copy(fig.transFigure, fig=fig, x=-pad_pt, units="points")
    fig.text(x0, y, text, ha="right", va="center", rotation=90, transform=tr, fontsize=8.5)


# ---------------------------------------------------------------- 5.3 Processing cost on one core
def fig_throughput_radius():
    """G1. VM, 4 frames, 1 thread, tile 256, 1 frame per window."""
    name = "fig-throughput-radius"
    d = summary(VM[(VM.store == "s2-berlin-sub4") & (VM.threads == 1) & (VM.frames_per_window == 1)
                   & VM.system.isin(SYSTEMS)], "mcells_s")
    fig, axes = plt.subplots(2, 2, figsize=(TEXTWIDTH_IN, 4.2), sharex=True, sharey=True)
    for ax, (op, shape) in zip(axes.flat, [("max", "square"), ("max", "circle"), ("mean", "square"), ("mean", "circle")]):
        for sysname in SYSTEMS:
            g = d[(d.op == op) & (d["shape"] == shape) & (d.system == sysname)].sort_values("radius")
            if len(g):
                errbar(ax, g.radius, g, sysname)
                record(name, f"{op} {shape}", sysname, g, "radius")
        ax.set_title(f"{op.capitalize()}, {shape}")
        log2_x(ax, [1, 2, 4, 8, 16])
        plain_log_y(ax, [0.3, 1, 3, 10, 30])
    h, l = axes[1, 0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=4, frameon=False, bbox_to_anchor=(0.5, 1.03))
    fig.tight_layout(rect=(0.03, 0.05, 1, 0.95))
    shared_xlabel(fig, axes[1], "Radius")
    shared_ylabel(fig, axes[:, 0], "Throughput (Mcells/s)")
    save(fig, name)


def fig_vm_vs_workstation():
    """G5 and machine comparison. 4 frames, 1 thread, square neighbourhoods, both machines."""
    name = "fig-vm-vs-workstation"
    rows = []
    for mname, m in [("VM", VM), ("Workstation", PC)]:
        s = summary(m[(m.store == "s2-berlin-sub4") & (m.threads == 1) & (m["shape"] == "square")
                      & (m.frames_per_window == 1)], "elapsed_s")
        for op, r in [("max", 4), ("max", 16), ("mean", 4), ("mean", 16)]:
            g = s[(s.op == op) & (s.radius == r)].set_index("system")
            for sysname in ["nes", "geotrellis", "geotrellis-noavx"]:
                if sysname in g.index:
                    rows.append(dict(machine=mname, op=op, radius=r, system=sysname,
                                     median_elapsed_s=g.loc[sysname, "median"], count=g.loc[sysname, "count"]))
    t = pd.DataFrame(rows)
    p = t.pivot_table(index=["machine", "op", "radius"], columns="system", values="median_elapsed_s").reset_index()
    p["gt_over_nes"] = p["geotrellis"] / p["nes"]
    p["noavx_over_avx"] = p["geotrellis-noavx"] / p["geotrellis"]
    RECORDS[name] = [t.assign(panel="inputs", series=t.system), p.assign(panel="plotted", series=p.machine, count=REPS)]
    labels = [f"{o.capitalize()}\n$r={r}$" for o, r in [("max", 4), ("max", 16), ("mean", 4), ("mean", 16)]]
    x = np.arange(4)
    w = 0.36
    fig, (a, b) = plt.subplots(1, 2, figsize=(TEXTWIDTH_IN, 2.2))
    for i, (mname, color, hatch) in enumerate([("VM", "#2a78d6", None), ("Workstation", "#eb6834", "////")]):
        v = p[p.machine == mname].sort_values(["op", "radius"])
        a.bar(x + (i - 0.5) * w, v.gt_over_nes, w - 0.03, color=color, hatch=hatch, edgecolor="white", label=mname)
        b.bar(x + (i - 0.5) * w, v.noavx_over_avx, w - 0.03, color=color, hatch=hatch, edgecolor="white", label=mname)
    for ax in (a, b):
        ax.axhline(1, color=INK_MUTED, lw=0.8)
        ax.set_xticks(x, labels)
        ax.grid(axis="x", visible=False)
    plain_log_y(a, [0.2, 0.5, 1, 2, 3])
    a.set_ylabel("GeoTrellis time / NES time")
    a.set_title("(a) GeoTrellis vs. NES (exact)")
    b.set_ylabel("GeoTrellis time, AVX off / on")
    b.set_title("(b) GeoTrellis without vector instructions")
    a.legend(frameon=False)
    fig.tight_layout()
    save(fig, name)


def fig_tile_size():
    """G3. VM, 4 frames, 1 thread, square neighbourhoods, r = 1 and 16."""
    name = "fig-tile-size"
    stores = {"s2-berlin-4-t64": 64, "s2-berlin-4-t128": 128, "s2-berlin-sub4": 256, "s2-berlin-4-t512": 512}
    d = VM[VM.store.isin(stores) & (VM.threads == 1) & (VM["shape"] == "square") & VM.radius.isin([1, 16])
           & (VM.frames_per_window == 1) & VM.system.isin(SYSTEMS)].copy()
    d["T"] = d.store.map(stores)
    s = summary(d, "elapsed_s", ["op", "radius", "system", "T"])
    fig, axes = plt.subplots(1, 4, figsize=(TEXTWIDTH_IN, 2.0))
    for ax, (op, r) in zip(axes, [("max", 1), ("max", 16), ("mean", 1), ("mean", 16)]):
        for sysname in SYSTEMS:
            g = s[(s.op == op) & (s.radius == r) & (s.system == sysname)].sort_values("T")
            if len(g) > 1:
                errbar(ax, g["T"], g, sysname)
                record(name, f"{op} square r={r}", sysname, g, "T")
        log2_x(ax, [64, 128, 256, 512])
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_formatter(mpl.ticker.FuncFormatter(lambda v, _: f"{v:g}" if f"{v:g}"[0] in "235" else ""))
        ax.set_title(f"{op.capitalize()}, square, $r={r}$")
    axes[0].set_ylabel("Elapsed time (s)")
    h, l = axes[2].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=4, frameon=False, bbox_to_anchor=(0.5, 1.12))
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    shared_xlabel(fig, axes, "Tile size (pixels)")
    save(fig, name)


def fig_nes_focal_share():
    """G7. VM, 4 frames, 1 thread, NES with event trace."""
    name = "fig-nes-focal-share"
    d = VM[(VM.store == "s2-berlin-sub4") & (VM.threads == 1) & VM.system.isin(["nes-trace", "nes-passthrough-trace"])]
    s = d.groupby(["op", "shape", "radius", "system"]).agg(
        elapsed_s=("elapsed_s", "median"), task_s=("task_s", "median"), count=("elapsed_s", "count")).reset_index()
    pt = s[s.system == "nes-passthrough-trace"]
    rows = s[s.system == "nes-trace"].sort_values(["op", "radius"])
    RECORDS[name] = [rows.assign(panel="focal", series="nes-trace"), pt.assign(panel="passthrough", series="passthrough")]
    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN * 0.6, 2.2))
    ax.bar(x - 0.2, rows.elapsed_s, 0.37, color="#2a78d6", label="Elapsed time")
    ax.bar(x + 0.2, rows.task_s, 0.37, color="#1baf7a", hatch="////", edgecolor="white", label="Focal task time")
    ax.axhline(pt.elapsed_s.median(), color=INK_MUTED, ls=":", lw=1, label="Passthrough elapsed")
    plain_log_y(ax, [0.5, 1, 5, 10, 50, 100, 200])
    ax.set_xticks(x, [f"{o.capitalize()}\n$r={r}$" for o, r in zip(rows.op, rows.radius)])
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Time (s)")
    ax.legend(frameon=False, fontsize=7, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.18))
    fig.tight_layout()
    save(fig, name)


# ---------------------------------------------------------------- 5.4 Scaling and state
def fig_speedup_threads():
    """G2. VM, 4 frames, cores 0-15. Speedup of the median elapsed time."""
    name = "fig-speedup-threads"
    s = summary(VM[(VM.store == "s2-berlin-sub4") & (VM.frames_per_window == 1) & VM.system.isin(SYSTEMS)], "elapsed_s")
    threads = [1, 2, 4, 8, 16]
    fig, axes = plt.subplots(1, 4, figsize=(TEXTWIDTH_IN, 2.0), sharey=True)
    for ax, (op, shape, r) in zip(axes, [("max", "square", 4), ("max", "square", 16),
                                          ("mean", "square", 4), ("mean", "circle", 16)]):
        ax.plot(threads, threads, color=INK_MUTED, lw=0.8, ls=(0, (2, 2)), label="Ideal")
        for sysname in SYSTEMS:
            g = s[(s.op == op) & (s["shape"] == shape) & (s.radius == r) & (s.system == sysname)
                  & s.threads.isin(threads)].sort_values("threads")
            if len(g) != len(threads):
                continue
            g = g.assign(speedup=g["median"].iloc[0] / g["median"])
            st = SYSTEMS[sysname]
            ax.plot(g.threads, g.speedup, color=st["color"], marker=st["marker"], ls=st["ls"], label=st["label"])
            record(name, f"{op} {shape} r={r}", sysname, g, "threads", "speedup")
        ax.set_title(f"{op.capitalize()}, {shape}, $r={r}$")
        log2_x(ax, threads)
        ax.set_yscale("log", base=2)
        ax.set_yticks(threads, [str(t) for t in threads])
        ax.yaxis.set_minor_locator(mpl.ticker.NullLocator())
    axes[0].set_ylabel("Speedup")
    h, l = axes[2].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=5, frameon=False, bbox_to_anchor=(0.5, 1.12))
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    shared_xlabel(fig, axes, "Threads")
    save(fig, name)


def fig_cpu_utilisation():
    """G2. CPU time / (elapsed time x threads), VM, Mean, square, r = 4."""
    name = "fig-cpu-utilisation"
    d = VM[(VM.store == "s2-berlin-sub4") & (VM.frames_per_window == 1) & (VM.op == "mean") & (VM["shape"] == "square")
           & (VM.radius == 4) & VM.system.isin(SYSTEMS)].copy()
    d["util"] = d.cpu_s / (d.elapsed_s * d.threads)
    s = summary(d, "util", ["system", "threads"])
    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN * 0.5, 2.2))
    for sysname in SYSTEMS:
        g = s[s.system == sysname].sort_values("threads")
        if len(g) > 1:
            st = SYSTEMS[sysname]
            ax.plot(g.threads, g["median"], color=st["color"], marker=st["marker"], ls=st["ls"], label=st["label"])
            record(name, "mean square r=4", sysname, g, "threads")
    log2_x(ax, [1, 2, 4, 8, 16])
    ax.set_ylim(0, 1.15)
    ax.set_xlabel("Threads")
    ax.set_ylabel("CPU utilisation")
    ax.set_title("Mean, square, $r=4$")
    ax.legend(frameon=False, fontsize=7)
    fig.tight_layout()
    save(fig, name)


def fig_time_per_frame():
    """G4. VM, Max, square, r = 4, 1 thread, runs over 2 / 4 / 8 / 16 frames."""
    name = "fig-time-per-frame"
    stores = {"s2-berlin-sub2": 2, "s2-berlin-sub4": 4, "s2-berlin-sub8": 8, "s2-berlin-16": 16}
    d = VM[VM.store.isin(stores) & (VM.threads == 1) & (VM.op == "max") & (VM["shape"] == "square")
           & (VM.radius == 4) & (VM.frames_per_window == 1) & VM.system.isin(["nes", "geotrellis"])].copy()
    d["per_frame_s"] = d.elapsed_s / d.frames
    d["F"] = d.store.map(stores)
    s = summary(d, "per_frame_s", ["system", "F"])
    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN * 0.5, 2.2))
    for sysname in ["nes", "geotrellis"]:
        g = s[s.system == sysname].sort_values("F")
        errbar(ax, g["F"], g, sysname)
        record(name, "max square r=4", sysname, g, "F")
    log2_x(ax, [2, 4, 8, 16])
    ax.set_ylim(0, None)
    ax.set_xlabel("Frames per run")
    ax.set_ylabel("Elapsed time per frame (s)")
    ax.set_title("Max, square, $r=4$")
    ax.legend(frameon=False)
    fig.tight_layout()
    save(fig, name)


def fig_window_size():
    """G8. VM, all 16 frames, 1 thread, square, r = 4, frames per window 1-16 (tumbling) and sliding 4/2, 4/1."""
    name = "fig-window-size"
    d = VM[VM.store == "s2-berlin-16"]
    tumbling = d[d.frames_per_window == d.slide_frames]
    fpw = [1, 2, 4, 8, 16]
    fig, (a, b) = plt.subplots(1, 2, figsize=(TEXTWIDTH_IN, 2.9))
    for op in ["max", "mean"]:
        for sysname in ["nes", "geotrellis"]:
            g = summary(tumbling[(tumbling.op == op) & (tumbling.system == sysname)], "elapsed_s")
            g = g.sort_values("frames_per_window")
            st = SYSTEMS[sysname]
            a.errorbar(g.frames_per_window, g["median"], yerr=[g["median"] - g["min"], g["max"] - g["median"]],
                       color=st["color"], marker=st["marker"], ls="-" if op == "max" else "--",
                       capsize=2, elinewidth=0.8, label=f"{st['label']}, {op.capitalize()}")
            record(name, "(a) elapsed", f"{sysname} {op}", g, "frames_per_window")
    log2_x(a, fpw)
    plain_log_y(a, [30, 50, 100, 200, 300])
    a.set_ylim(35, 420)
    a.set_xlabel("Frames per window")
    a.set_ylabel("Elapsed time (s)")
    a.set_title("(a) Elapsed time, square, $r=4$")
    a.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.3), fontsize=7)

    gib = 2 ** 30
    st = SYSTEMS["nes"]
    nes = summary(tumbling[(tumbling.op == "max") & (tumbling.system == "nes")], "peak_rss_bytes")
    nes = nes.sort_values("frames_per_window")
    pt = summary(tumbling[tumbling.system == "nes-passthrough"], "peak_rss_bytes").sort_values("frames_per_window")
    for frame, series in [(nes, "nes max tumbling"), (pt, "nes passthrough")]:
        record(name, "(b) peak rss", series, frame.assign(median_gib=frame["median"] / gib), "frames_per_window", "median_gib")
    b.plot(nes.frames_per_window, nes["median"] / gib, color=st["color"], marker=st["marker"], label="NES, Max, tumbling")
    b.plot(pt.frames_per_window, pt["median"] / gib, color=INK_MUTED, marker="x", ls=":", label="NES passthrough")
    for sysname, slide, mk in [("nes-s2", 2, "D"), ("nes-s1", 1, "P")]:
        g = summary(d[d.system == sysname], "peak_rss_bytes")
        record(name, "(b) peak rss", f"{sysname} sliding 4/{slide}", g.assign(median_gib=g["median"] / gib),
               "frames_per_window", "median_gib")
        b.plot(g.frames_per_window, g["median"] / gib, ls="none", marker=mk, color=st["color"],
               markerfacecolor="white", label=f"NES, Max, sliding 4/{slide}")
    log2_x(b, fpw)
    b.set_ylim(0, 15)
    b.set_xlabel("Frames per window")
    b.set_ylabel("Peak RSS (GiB)")
    b.set_title("(b) NES memory, Max, square, $r=4$")
    b.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.3), fontsize=7)
    fig.tight_layout()
    save(fig, name)


# ---------------------------------------------------------------- 5.5 Latency under paced arrival
def fig_latency_window():
    """G6. VM, all 16 frames, 1 thread, square, r = 4, frames released at a fixed rate shared by both systems.
    Frame latency: scheduled release of a frame -> its last output tile (NES: FrameLatency sink; GeoTrellis: end of the
    batch holding the frame). First output: release -> first output tile of the frame (NES only; for GeoTrellis it
    equals the frame latency, since a batch is written as a whole). Per-run percentiles over the 16 frames, then
    median and min-max over the repetitions."""
    name = "fig-latency-window"
    s = lambda d, col: summary(d, col, ["op", "system", "rate_fps", "frames_per_window"])
    lat = s(PACED, "frame_latency_p50_ms")
    first = s(PACED, "ttfo_p50_ms")
    p95 = s(PACED, "frame_latency_p95_ms")
    fpw = [1, 4, 16]
    panels = [("max", 0.03, "(a) Max, square, $r=4$, 0.03 frames/s"),
              ("mean", 0.25, "(b) Mean, square, $r=4$, 0.25 frames/s")]
    fig, axes = plt.subplots(1, 2, figsize=(TEXTWIDTH_IN, 2.6), sharey=True)
    for ax, (op, rate, title) in zip(axes, panels):
        for sysname in ["geotrellis", "nes", "nes-running"]:
            sel = lambda t: t[(t.op == op) & (t.rate_fps == rate) & (t.system == sysname)].sort_values("frames_per_window")
            g, f, q = sel(lat), sel(first), sel(p95)
            if not len(g):
                continue
            st = SYSTEMS[sysname]
            for t in (g, f, q):
                t[["median", "min", "max"]] = t[["median", "min", "max"]] / 1000
            errbar(ax, g.frames_per_window, g, sysname)
            record(name, title, f"{sysname} frame latency p50", g, "frames_per_window")
            record(name, title, f"{sysname} frame latency p95", q, "frames_per_window")
            if sysname != "geotrellis":
                ax.plot(f.frames_per_window, f["median"], color=st["color"], marker=st["marker"], ls=":",
                        markerfacecolor="white", label=f"{st['label']}, first output")
                record(name, title, f"{sysname} first output p50", f, "frames_per_window")
        ax.set_title(title)
        log2_x(ax, fpw)
        plain_log_y(ax, [0.5, 1, 3, 10, 30, 100, 300])
    axes[0].set_ylabel("Latency after release (s)")
    handles = {}
    for ax in axes:
        handles.update(zip(reversed(ax.get_legend_handles_labels()[1]), reversed(ax.get_legend_handles_labels()[0])))
    order = [l for l in ["GeoTrellis", "NES (exact)", "NES (exact), first output", "NES (running)",
                         "NES (running), first output"] if l in handles]
    fig.legend([handles[l] for l in order], order, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.1))
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    shared_xlabel(fig, axes, "Frames per window")
    save(fig, name)


if __name__ == "__main__":
    with open(OUT / "SOURCES.txt", "w") as f:
        for label, path in SOURCES.items():
            f.write(f"{label}\t{path}\t{len(pd.read_csv(path))} runs\tsha256 {hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    for fn in [fig_throughput_radius, fig_vm_vs_workstation, fig_tile_size, fig_nes_focal_share,
               fig_speedup_threads, fig_cpu_utilisation, fig_time_per_frame, fig_window_size, fig_latency_window]:
        fn()
        print("ok", fn.__name__)
