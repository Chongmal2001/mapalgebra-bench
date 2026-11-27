from __future__ import annotations

import csv
import json
import re
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any

SYSTEM_ORDER = ["nes", "nes-exact", "nes-running", "geotrellis", "nes-passthrough"]
LABEL = {"nes": "NES", "nes-exact": "NES (exact)", "nes-running": "NES (running sum)",
         "geotrellis": "GeoTrellis micro-batch", "nes-passthrough": "NES source + sink only"}
STYLE = {"nes": ("#1f4e9c", "o", "-"), "nes-exact": ("#7fb3ef", "D", ":"), "nes-running": ("#4f8fdc", "s", "--"),
         "geotrellis": ("#c0392b", "^", "-"), "nes-passthrough": ("#7f7f7f", "", ":")}
CASE = re.compile(r"^(?P<op>[a-z]+)-(?P<shape>[a-z]+)-(?P<r>\d+)(?:-(?P<mode>running|kahan))?"
                  r"(?:-w(?P<w>\d+)(?:s(?P<s>\d+))?)?(?:-n(?P<n>\d+))?(?:-(?P<tag>[A-Za-z0-9_]+))?$")


def load_perf(path: str | Path) -> list[dict[str, Any]]:
    num = {"threads", "rep", "frames", "frames_per_window", "radius", "cells", "bands", "tile_size"}
    flt = {"rate_fps", "task_s", "trace_dropped", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
           "frame_latency_p50_ms", "frame_latency_p95_ms", "frame_latency_p99_ms", "ttfo_p50_ms", "ttfo_p95_ms",
           "release_lag_max_ms", "elapsed_s", "compute_s", "buffer_s", "gc_s", "cpu_s", "throughput_cells_per_s", "cells_per_cpu_s",
           "peak_rss_bytes", "mean_rss_bytes", "rss_hwm_bytes"}
    rows = []
    for r in csv.DictReader(open(path, newline="")):
        if "elapsed_s" not in r:
            r["elapsed_s"] = r.get("wall_s")
            if r.get("mem_kind") != "jvm_heap_used":
                r["rss_hwm_bytes"] = r.get("peak_mem_bytes")
        for k in num:
            r[k] = int(r[k]) if r.get(k) not in (None, "") else None
        for k in flt:
            r[k] = float(r[k]) if r.get(k) not in (None, "") else None
        rows.append(r)
    return rows


def aggregate(rows: list[dict[str, Any]], key: str) -> dict[tuple, tuple[float, float, float, int]]:
    groups: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            groups[(r["store"], r["op"], r["shape"], r["radius"], r["frames_per_window"], r["system"],
                    r["threads"])].append(r[key])
    return {k: (st.median(v), min(v), max(v), len(v)) for k, v in groups.items()}


def load_correctness(results: str | Path) -> list[dict[str, Any]]:
    rows = []
    root = Path(results)
    for f in sorted(list(root.glob("*/*.json")) + list(root.glob("*/*/*.json"))):
        m = CASE.match(f.stem)
        if not m:
            continue
        rep = json.loads(f.read_text())
        t = rep["totals"]["all"]
        rows.append({"store": f.parent.name, "case": f.stem,
                     "case_id": f.parent.parent.name if f.parent.parent != root else "", "tag": m["tag"] or "",
                     "op": m["op"], "shape": m["shape"],
                     "radius": int(m["r"]), "mode": m["mode"] or "exact", "fpw": int(m["w"] or 1),
                     "slide": int(m["s"] or m["w"] or 1), "nest": int(m["n"] or 1), "tol_util": t.get("max_tol_util"),
                     "missing_input_tiles": sum(rep.get("missing_input_tiles", {}).values()),
                     "passed": rep["passed"], "criterion": rep["criterion"], "data_cells": t["data_both"],
                     "exact_rate": t["exact_rate"], "tolerance_rate": t["tolerance_rate"], "max_ulp": t["max_ulp"],
                     "max_abs_error": t["max_abs_error"],
                     "nodata_mismatch": t["nodata_only_candidate"] + t["nodata_only_reference"],
                     "coverage_problems": len(rep["coverage_problems"])})
    return rows


def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError("mabench report needs matplotlib: pip install 'mabench[plot]'") from exc
    plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.3, "figure.dpi": 150})
    return plt


def _axes_style(ax, radii, logy: bool = True, suffix: str = "") -> None:
    from matplotlib.ticker import FuncFormatter, NullFormatter
    ax.set_xscale("log", base=2)
    ax.set_xticks(sorted(radii), [str(r) for r in sorted(radii)])
    ax.xaxis.set_minor_formatter(NullFormatter())
    if logy:
        from matplotlib.ticker import FixedLocator
        ax.set_yscale("log")
        lo, hi = ax.get_ylim()
        nice = [m * 10.0 ** e for e in range(-3, 6) for m in (1, 2, 5)]
        if hi / lo < 10:
            nice = sorted(set(nice + [m * 10.0 ** e for e in range(-3, 6) for m in (1.5, 3, 4, 6, 8)]))
        ticks = [v for v in nice if lo <= v <= hi]
        ax.yaxis.set_major_locator(FixedLocator(ticks))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}{suffix}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_ylim(lo, hi)


def _save(fig, out: Path, name: str, formats: list[str]) -> list[Path]:
    paths = []
    for ext in formats:
        p = out / f"{name}.{ext}"
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    return paths


def _grid(plt, n_rows: int, n_cols: int, w: float = 4.2, h: float = 3.2):
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(w * n_cols, h * n_rows), squeeze=False)
    return fig, axes


def throughput_figure(rows, store, shape, out, formats, threads=1) -> list[Path]:
    plt = _plt()
    agg = aggregate([r for r in rows if r["threads"] == threads or r["system"] == "geotrellis"],
                    "throughput_cells_per_s")
    ops = sorted({k[1] for k in agg if k[0] == store and k[2] == shape and k[5] != "nes-passthrough"})
    fpws = sorted({k[4] for k in agg if k[0] == store and k[2] == shape})
    if not ops:
        return []
    fig, axes = _grid(plt, len(fpws), len(ops))
    for i, fpw in enumerate(fpws):
        for j, op in enumerate(ops):
            ax = axes[i][j]
            for sys in SYSTEM_ORDER:
                pts = sorted((k[3], v) for k, v in agg.items()
                             if k[0] == store and k[1] == op and k[2] == shape and k[4] == fpw and k[5] == sys)
                if sys == "nes-passthrough":
                    pts = sorted((k[3], v) for k, v in agg.items() if k[0] == store and k[4] == fpw and k[5] == sys)
                    if pts:
                        ax.axhline(pts[0][1][0] / 1e6, color=STYLE[sys][0], ls=":", lw=1, label=LABEL[sys])
                    continue
                if not pts:
                    continue
                x = [p[0] for p in pts]
                med = [p[1][0] / 1e6 for p in pts]
                err = [[(p[1][0] - p[1][1]) / 1e6 for p in pts], [(p[1][2] - p[1][0]) / 1e6 for p in pts]]
                c, mk, ls = STYLE[sys]
                ax.errorbar(x, med, yerr=err, color=c, marker=mk, ls=ls, ms=4, capsize=2, label=LABEL[sys])
            _axes_style(ax, {k[3] for k in agg if k[0] == store and k[1] == op and k[2] == shape})
            ax.set_xlabel("radius r (cells)")
            ax.set_ylabel("throughput (Mcells/s)")
            ax.set_title(f"{op} {shape}, {fpw} frame(s)/window")
    seen: dict[str, Any] = {}
    for ax in axes.flat:
        for h, lab in zip(*ax.get_legend_handles_labels()):
            seen.setdefault(lab, h)
    fig.legend(list(seen.values()), list(seen), loc="lower center", ncol=min(len(seen), 3),
               bbox_to_anchor=(0.5, -0.04 - 0.03 * len(fpws)))
    fig.suptitle(f"Throughput vs radius ({store}, {threads} NES worker thread(s); median, min-max bars)")
    fig.tight_layout()
    paths = _save(fig, out, f"throughput_{store}_{shape}", formats)
    plt.close(fig)
    return paths


def speedup_figure(rows, store, shape, out, formats, threads=1) -> list[Path]:
    plt = _plt()
    agg = aggregate(rows, "throughput_cells_per_s")
    ops = sorted({k[1] for k in agg if k[0] == store and k[2] == shape and k[5] == "geotrellis"})
    fpws = sorted({k[4] for k in agg if k[0] == store and k[2] == shape and k[5] == "geotrellis"})
    if not ops:
        return []
    fig, axes = _grid(plt, 1, len(ops))
    for j, op in enumerate(ops):
        ax = axes[0][j]
        for sys in ("nes", "nes-exact", "nes-running"):
            for fpw, ls in zip(fpws, ("-", "--", ":", "-.")):
                pts = []
                for k, v in sorted(agg.items()):
                    if k[:3] == (store, op, shape) and k[4] == fpw and k[5] == sys and k[6] == threads:
                        gt = agg.get((store, op, shape, k[3], fpw, "geotrellis", 1))
                        if gt:
                            pts.append((k[3], v[0] / gt[0]))
                if pts:
                    ax.plot([p[0] for p in pts], [p[1] for p in pts], color=STYLE[sys][0], marker=STYLE[sys][1],
                            ls=ls, ms=4, label=f"{LABEL[sys]}, {fpw} f/w")
        ax.axhline(1.0, color="black", lw=0.8)
        _axes_style(ax, {k[3] for k in agg if k[0] == store and k[1] == op and k[2] == shape}, suffix="x")
        ax.set_xlabel("radius r (cells)")
        ax.set_ylabel("NES / GeoTrellis throughput")
        ax.set_title(f"{op} {shape}")
        ax.legend(fontsize=7)
    fig.suptitle(f"Speedup over GeoTrellis ({store}; > 1: NES faster)")
    fig.tight_layout()
    paths = _save(fig, out, f"speedup_{store}_{shape}", formats)
    plt.close(fig)
    return paths


def window_figure(rows, store, shape, out, formats) -> list[Path]:
    plt = _plt()
    agg = aggregate(rows, "throughput_cells_per_s")
    fpws = sorted({k[4] for k in agg if k[0] == store and k[2] == shape} - {1})
    ops = sorted({k[1] for k in agg if k[0] == store and k[2] == shape and k[5] != "nes-passthrough"})
    if not fpws or not ops:
        return []
    fig, axes = _grid(plt, 1, len(ops))
    for j, op in enumerate(ops):
        ax = axes[0][j]
        for sys in SYSTEM_ORDER[:4]:
            for fpw, ls in zip(fpws, ("-", "--", ":")):
                pts = [(k[3], v[0] / agg[(k[0], k[1], k[2], k[3], 1, k[5], k[6])][0]) for k, v in sorted(agg.items())
                       if k[:3] == (store, op, shape) and k[4] == fpw and k[5] == sys
                       and (k[0], k[1], k[2], k[3], 1, k[5], k[6]) in agg]
                if pts:
                    ax.plot([p[0] for p in pts], [p[1] for p in pts], color=STYLE[sys][0], marker=STYLE[sys][1],
                            ls=ls, ms=4, label=f"{LABEL[sys]}, {fpw} f/w")
        ax.axhline(1.0, color="black", lw=0.8)
        _axes_style(ax, {k[3] for k in agg if k[0] == store and k[1] == op and k[2] == shape}, logy=False)
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(lo, 0.8), max(hi, 1.2))
        ax.set_xlabel("radius r (cells)")
        ax.set_ylabel("throughput relative to 1 frame/window")
        ax.set_title(f"{op} {shape}")
        ax.legend(fontsize=7)
    fig.suptitle(f"Effect of frames per window ({store})")
    fig.tight_layout()
    paths = _save(fig, out, f"window_{store}_{shape}", formats)
    plt.close(fig)
    return paths


def breakdown_figure(rows, store, shape, op, out, formats) -> list[Path]:
    plt = _plt()
    sel = [r for r in rows if r["store"] == store and r["frames_per_window"] == 1]
    wall, comp, gc = (aggregate(sel, k) for k in ("elapsed_s", "compute_s", "gc_s"))
    radii = sorted({k[3] for k in wall if k[1] == op and k[2] == shape})
    if not radii:
        return []
    pt = [v[0] for k, v in wall.items() if k[5] == "nes-passthrough"]
    fig, ax = plt.subplots(figsize=(1.3 * len(radii) + 2.5, 3.4))
    w = 0.38
    for i, r in enumerate(radii):
        nes = wall.get((store, op, shape, r, 1, "nes", 1))
        if nes:
            src = min(pt[0], nes[0]) if pt else 0.0
            ax.bar(i - w / 2, src, w, color="#bfbfbf", label="NES source + sink" if i == 0 else None)
            ax.bar(i - w / 2, nes[0] - src, w, bottom=src, color=STYLE["nes"][0], label="NES focal" if i == 0 else None)
        g = wall.get((store, op, shape, r, 1, "geotrellis", 1))
        if g:
            c = comp.get((store, op, shape, r, 1, "geotrellis", 1), (0.0,))[0]
            q = gc.get((store, op, shape, r, 1, "geotrellis", 1), (0.0,))[0]
            rest = max(0.0, g[0] - c)
            ax.bar(i + w / 2, rest, w, color="#f2b8b0", label="GT read + prepare" if i == 0 else None)
            ax.bar(i + w / 2, c, w, bottom=rest, color=STYLE["geotrellis"][0], label="GT focal compute" if i == 0 else None)
            if q:
                ax.bar(i + w / 2, min(q, c), w, bottom=rest, color="none", edgecolor="black", hatch="//", lw=0,
                       label="GT GC (overlaps compute)" if i == 0 else None)
    ax.set_xticks(range(len(radii)), [f"r={r}" for r in radii])
    ax.set_ylabel("elapsed time (s, median)")
    ax.set_title(f"Time breakdown, {op} {shape}, 1 frame/window ({store})")
    ax.legend(fontsize=7)
    fig.tight_layout()
    paths = _save(fig, out, f"breakdown_{store}_{op}_{shape}", formats)
    plt.close(fig)
    return paths


def memory_figure(rows, store, shape, out, formats) -> list[Path]:
    plt = _plt()
    sel = [r for r in rows if r["store"] == store and r["shape"] == shape and r["system"].startswith("nes")]
    rss: dict[tuple, list[float]] = defaultdict(list)
    for r in sel:
        v = r.get("peak_rss_bytes") or r.get("rss_hwm_bytes")
        if v:
            rss[(r["system"], r["frames_per_window"])].append(v / 2**30)
    if not rss:
        return []
    systems = [s for s in SYSTEM_ORDER if any(k[0] == s for k in rss)]
    fpws = sorted({k[1] for k in rss})
    colors = ["#1f4e9c", "#4f8fdc", "#7fb3ef", "#bcd6f5", "#7f7f7f"]
    fig, ax = plt.subplots(figsize=(1.6 * len(systems) + 2, 3.4))
    w = 0.8 / len(fpws)
    for i, fpw in enumerate(fpws):
        off = (i - (len(fpws) - 1) / 2) * w
        vals = [st.median(rss.get((s, fpw), [float("nan")])) for s in systems]
        ax.bar([x + off for x in range(len(systems))], vals, w, label=f"{fpw} frame(s)/window", color=colors[i % 5])
    ax.set_xticks(range(len(systems)), [LABEL[s].replace(" ", "\n", 1) for s in systems], fontsize=7)
    ax.set_ylabel("peak RSS (GiB)")
    ax.set_title(f"Peak NES worker RSS ({store})")
    ax.legend(fontsize=7)
    fig.tight_layout()
    paths = _save(fig, out, f"memory_{store}_{shape}", formats)
    plt.close(fig)
    return paths


def _span_and_samples(run_dir: Path) -> tuple[int, int, list] | None:
    from .perf import read_resources
    samples = read_resources(run_dir / "resources.csv")
    if not samples or not (run_dir / "status.json").exists():
        return None
    s = json.loads((run_dir / "status.json").read_text())
    s = next((e for e in s if "local_query_id" in e), s[0]) if isinstance(s, list) else s
    return s["running"]["since_epoch"], s["stopped"]["since_epoch"], samples


def timeline_figure(rows, store, shape, op, out, formats) -> list[Path]:
    plt = _plt()
    sel = [r for r in rows if r["store"] == store and r["shape"] == shape and r["op"] == op and r.get("run_dir")
           and r["system"].startswith("nes")]
    if not sel:
        return []
    fpw = max(r["frames_per_window"] for r in sel)
    sel = [r for r in sel if r["frames_per_window"] == fpw]
    radius = min({r["radius"] for r in sel}, key=lambda x: abs(x - 4))
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    drawn = 0
    for system in SYSTEM_ORDER:
        runs = sorted((r for r in sel if r["system"] == system and r["radius"] == radius), key=lambda r: r["rep"])
        for r in runs:
            got = _span_and_samples(Path(r["run_dir"]))
            if got is None:
                continue
            t0, t1, samples = got
            pts = [((t - t0) / 1e6, kb / 2**20) for t, kb, _ in samples if t0 - 1e6 <= t <= t1 + 1e6]
            if pts:
                color, _, ls = STYLE.get(system, ("black", "", "-"))
                ax.plot([p[0] for p in pts], [p[1] for p in pts], ls, color=color, lw=1.2, label=LABEL.get(system, system))
                ax.axvline((t1 - t0) / 1e6, color=color, lw=0.6, ls=":")
                drawn += 1
            break
    if not drawn:
        plt.close(fig)
        return []
    ax.axvline(0, color="black", lw=0.6)
    ax.set_xlabel("time since start of measured span (s); dotted = end")
    ax.set_ylabel("RSS (GiB)")
    ax.set_title(f"NES worker RSS over time, {op} {shape} r={radius}, {fpw} frame(s)/window ({store})")
    ax.legend(fontsize=7)
    fig.tight_layout()
    paths = _save(fig, out, f"timeline_{store}_{op}_{shape}", formats)
    plt.close(fig)
    return paths


def correctness_figure(crows, out, formats) -> list[Path]:
    plt = _plt()
    if not crows:
        return []
    rows_k = sorted({(c.get("case_id", ""), c["store"], c["op"], c["shape"], c["mode"], c.get("nest", 1)) for c in crows})
    cols_k = sorted({(c["radius"], c["fpw"], c.get("slide", c["fpw"])) for c in crows})
    cell = {((c.get("case_id", ""), c["store"], c["op"], c["shape"], c["mode"], c.get("nest", 1)),
             (c["radius"], c["fpw"], c.get("slide", c["fpw"]))): c for c in crows}
    fig, ax = plt.subplots(figsize=(0.75 * len(cols_k) + 3, 0.28 * len(rows_k) + 1.4))
    for i, rk in enumerate(rows_k):
        for j, ck in enumerate(cols_k):
            c = cell.get((rk, ck))
            if c is None:
                continue
            ax.add_patch(plt.Rectangle((j, i), 1, 1, color="#4caf50" if c["passed"] else "#e53935", alpha=0.85))
            txt = "exact" if c["exact_rate"] == 1.0 else f"{c['exact_rate']:.4%}"
            ax.text(j + 0.5, i + 0.5, txt, ha="center", va="center", fontsize=6, color="white")
    ax.set_xlim(0, len(cols_k))
    ax.set_ylim(len(rows_k), 0)
    ax.set_xticks([j + 0.5 for j in range(len(cols_k))], [f"r={r}" + (f"\n{w} f/w" if w > 1 else "") + (f"\nslide {sl}" if sl != w else "")
                                                for r, w, sl in cols_k],
                  fontsize=7)
    ax.set_yticks([i + 0.5 for i in range(len(rows_k))],
                  [(f"{k} " if k else "") + f"{s}: {o} {sh}" + ("" if m == "exact" else f" ({m})")
                   + ("" if n == 1 else f" x{n}") for k, s, o, sh, m, n in rows_k], fontsize=7)
    ax.grid(False)
    n_pass = sum(c["passed"] for c in crows)
    ax.set_title(f"Correctness vs GeoTrellis full-grid reference: {n_pass}/{len(crows)} PASS "
                 "(green = PASS; text = share of bit-identical cells)", fontsize=8)
    fig.tight_layout()
    paths = _save(fig, out, "correctness_matrix", formats)
    plt.close(fig)
    return paths


def perf_markdown(rows) -> str:
    agg = aggregate(rows, "throughput_cells_per_s")
    elapsed, cpu, mem = aggregate(rows, "elapsed_s"), aggregate(rows, "cpu_s"), aggregate(rows, "peak_rss_bytes")
    lines = ["| store | op | shape | r | frames/window | system | threads | runs | elapsed s | Mcells/s (median) | min | max "
             "| CPU s | peak RSS GiB |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    order = {s: i for i, s in enumerate(SYSTEM_ORDER)}
    opt = lambda d, k, f: f.format(d[k][0]) if k in d else ""
    for k in sorted(agg, key=lambda k: (k[0], k[1], k[2], k[3], k[4], order.get(k[5], 99), k[6])):
        med, lo, hi, n = agg[k]
        lines.append(f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | {k[4]} | {k[5]} | {k[6]} | {n} | {elapsed[k][0]:.2f} | "
                     f"{med / 1e6:.1f} | {lo / 1e6:.1f} | {hi / 1e6:.1f} | {opt(cpu, k, '{:.2f}')} | "
                     f"{opt({kk: (v[0] / 2**30,) for kk, v in mem.items()}, k, '{:.2f}')} |")
    return "\n".join(lines)


def trace_markdown(rows) -> str:
    task, elapsed = aggregate(rows, "task_s"), aggregate(rows, "elapsed_s")
    drops = aggregate(rows, "trace_dropped")
    lines = ["| store | op | shape | r | frames/window | system | threads | runs | task time s | elapsed s | dropped events |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k in sorted(task):
        lines.append(f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | {k[4]} | {k[5]} | {k[6]} | {task[k][3]} | {task[k][0]:.3f} | "
                     f"{elapsed[k][0]:.3f} | {drops.get(k, (0,))[0]:.0f} |")
    return "\n".join(lines)


def paced_markdown(rows) -> str:
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["store"], r["op"], r["shape"], r["radius"], r["frames_per_window"], r["system"], r["threads"],
                r["rate_fps"])].append(r)
    cols = [("window latency p50 ms", "latency_p50_ms"), ("p95", "latency_p95_ms"), ("p99", "latency_p99_ms"),
            ("frame latency p50 ms", "frame_latency_p50_ms"), ("p95", "frame_latency_p95_ms"),
            ("first output p50 ms", "ttfo_p50_ms"), ("p95", "ttfo_p95_ms"), ("max release lag ms", "release_lag_max_ms")]
    head = "| store | op | shape | r | frames/window | system | threads | rate (frames/s) | runs | "
    lines = [head + " | ".join(h for h, _ in cols) + " |", "|" + "---|" * (9 + len(cols))]
    med = lambda g, k: f"{st.median(x[k] for x in g if x.get(k) is not None):.1f}" if any(x.get(k) is not None for x in g) else ""
    for k, g in sorted(groups.items()):
        lines.append(f"| {' | '.join(str(v) for v in k)} | {len(g)} | " + " | ".join(med(g, c) for _, c in cols) + " |")
    return "\n".join(lines)


def correctness_markdown(crows) -> str:
    lines = ["| case | store | run | verdict | data cells | exact | max ULP | max abs err | tolerance used | NoData mismatches "
             "| input tiles missing | coverage |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in crows:
        util = "" if c.get("tol_util") is None or c["criterion"] == "exact" else f"{c['tol_util']:.3g}"
        lines.append(f"| {c.get('case_id', '')} | {c['store']} | {c['case']} | {'PASS' if c['passed'] else 'FAIL'} | {c['data_cells']} | "
                     f"{c['exact_rate']:.4%} | {c['max_ulp']:g} | {c['max_abs_error']:.3g} | {util} | "
                     f"{c['nodata_mismatch']} | {c.get('missing_input_tiles', 0)} | "
                     f"{'ok' if not c['coverage_problems'] else c['coverage_problems']} |")
    return "\n".join(lines)


def report(perf_csv: str | Path | None, results: str | Path | None, out: str | Path,
           formats: tuple[str, ...] = ("png",), breakdown_op: str = "max") -> list[Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    formats = list(formats)
    written: list[Path] = []
    md = ["# Benchmark report", ""]
    if perf_csv and Path(perf_csv).exists():
        allrows = load_perf(perf_csv)
        rows = [r for r in allrows if not r.get("rate_fps")]
        paced = [r for r in allrows if r.get("rate_fps")]
        for store in sorted({r["store"] for r in rows}):
            for shape in sorted({r["shape"] for r in rows if r["store"] == store and r["system"] != "nes-passthrough"}):
                written += throughput_figure(rows, store, shape, out, formats)
                written += speedup_figure(rows, store, shape, out, formats)
                written += window_figure(rows, store, shape, out, formats)
                written += memory_figure(rows, store, shape, out, formats)
                written += timeline_figure(rows, store, shape, breakdown_op, out, formats)
                written += breakdown_figure(rows, store, shape, breakdown_op, out, formats)
        md += ["## Performance, maximum rate (median over repetitions)", "", perf_markdown(rows), ""]
        if any(r.get("task_s") for r in rows):
            md += ["## Operator time from the NES event trace", "", trace_markdown(rows), ""]
        if paced:
            md += ["## Paced input (latency, median over repetitions)", "", paced_markdown(paced), ""]
    if results and Path(results).exists():
        crows = load_correctness(results)
        written += correctness_figure(crows, out, formats)
        md += ["## Correctness", "", f"{sum(c['passed'] for c in crows)}/{len(crows)} PASS", "",
               correctness_markdown(crows), ""]
    (out / "report.md").write_text("\n".join(md))
    written.append(out / "report.md")
    return written
