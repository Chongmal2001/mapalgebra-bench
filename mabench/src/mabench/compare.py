from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from scipy import ndimage as ndi

from . import oracle
from .store import TileStore

# Sum / Mean pass if |a - b| <= tau * sum|v| over V(c) (Mean: divided by |V(c)|), Min / Max bit for bit
DEFAULT_TAU = 1e-12
STRATA = ("all", "interior", "seam", "raster_edge", "near_nodata", "near_missing_tile")


def ulp_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    def ordered(x):
        i = np.ascontiguousarray(x, dtype=np.float64).view(np.int64)
        i = np.where(i < 0, np.int64(-(2**63)) - i, i)  # order-preserving map of doubles onto int64
        return (i.view(np.uint64) ^ np.uint64(1 << 63))
    ua, ub = ordered(a), ordered(b)
    d = np.where(ua >= ub, ua - ub, ub - ua)
    return d.astype(np.float64)


def _near(mask: np.ndarray, fp: np.ndarray) -> np.ndarray:
    r = fp.shape[0] // 2
    runs = []
    for dy in range(-r, r + 1):
        idx = np.flatnonzero(fp[dy + r])
        if idx.size == 0:
            continue
        lo, hi = idx[0] - r, idx[-1] - r
        if idx.size != hi - lo + 1 or lo != -hi:
            return ndi.correlate(mask.astype(np.float64), fp.astype(np.float64), mode="constant", cval=0.0) > 0.5
        runs.append((dy, int(hi)))
    n = mask.shape[0]
    m8 = mask.astype(np.uint8)
    horiz = {w: ndi.maximum_filter1d(m8, 2 * w + 1, axis=1, mode="constant", cval=0) for w in {w for _, w in runs}}
    out = np.zeros(mask.shape, bool)
    for dy, w in runs:
        h = horiz[w]
        if dy >= 0:
            out[:n - dy] |= h[dy:].astype(bool)
        else:
            out[-dy:] |= h[:n + dy].astype(bool)
    return out


def strata_masks(input_frame: np.ndarray, tile_size: int, radius: int, missing_tiles: set[tuple[int, int]],
                 fp: np.ndarray, row0: int = 0, height: int | None = None) -> dict[str, np.ndarray]:
    _, n, W = input_frame.shape
    H = n if height is None else height
    yy = np.arange(row0, row0 + n)[:, None]
    xx = np.arange(W)[None, :]
    r, s = radius, tile_size
    # neighbourhood crosses a tile edge with a tile beyond it
    internal_x = ((xx % s < r) & (xx >= s)) | ((s - 1 - xx % s < r) & (xx // s < (W - 1) // s))
    internal_y = ((yy % s < r) & (yy >= s)) | ((s - 1 - yy % s < r) & (yy // s < (H - 1) // s))
    seam = internal_x | internal_y
    raster_edge = (xx < r) | (yy < r) | (xx >= W - r) | (yy >= H - r)
    near_nodata = _near(np.isnan(input_frame).any(axis=0), fp)
    near_missing = np.zeros((n, W), bool)
    rows_here = [(tx, ty) for tx, ty in missing_tiles if ty * s < row0 + n + r and (ty + 1) * s > row0 - r]
    if rows_here:
        miss = np.zeros((n, W), bool)
        for tx, ty in rows_here:
            miss[max(ty * s - row0, 0):max((ty + 1) * s - row0, 0), tx * s:(tx + 1) * s] = True
        near_missing = _near(miss, fp)
    seam, raster_edge = np.broadcast_to(seam, (n, W)), np.broadcast_to(raster_edge, (n, W))
    interior = ~(seam | raster_edge | near_nodata | near_missing)
    return {"all": np.ones((n, W), bool), "interior": interior, "seam": seam, "raster_edge": raster_edge,
            "near_nodata": near_nodata, "near_missing_tile": near_missing}


@dataclass
class StratumMetrics:
    cells: int = 0
    nodata_both: int = 0
    nodata_only_candidate: int = 0
    nodata_only_reference: int = 0
    data_both: int = 0
    exact: int = 0
    within_tolerance: int = 0
    max_abs_error: float = 0.0
    sum_sq_error: float = 0.0
    max_ulp: float = 0.0
    max_tol_util: float = 0.0          # max |a - b| / bound over data cells; inf where the bound is 0 but a != b

    def add(self, other: "StratumMetrics") -> None:
        for k in ("cells", "nodata_both", "nodata_only_candidate", "nodata_only_reference", "data_both",
                  "exact", "within_tolerance", "sum_sq_error"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.max_abs_error = max(self.max_abs_error, other.max_abs_error)
        self.max_ulp = max(self.max_ulp, other.max_ulp)
        self.max_tol_util = max(self.max_tol_util, other.max_tol_util)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        n = self.data_both
        d["nodata_agreement"] = (self.nodata_both + self.data_both) / self.cells if self.cells else 1.0
        d["exact_rate"] = self.exact / n if n else 1.0
        d["tolerance_rate"] = self.within_tolerance / n if n else 1.0
        d["rmse"] = float(np.sqrt(self.sum_sq_error / n)) if n else 0.0
        del d["sum_sq_error"]
        return d


def _metrics(cand: np.ndarray, ref: np.ndarray, tol: np.ndarray | None, mask: np.ndarray) -> StratumMetrics:
    m = np.broadcast_to(mask, cand.shape)
    c, r = cand[m], ref[m]
    cn, rn = np.isnan(c), np.isnan(r)
    both = ~cn & ~rn
    out = StratumMetrics(cells=int(c.size), nodata_both=int((cn & rn).sum()),
                         nodata_only_candidate=int((cn & ~rn).sum()), nodata_only_reference=int((~cn & rn).sum()),
                         data_both=int(both.sum()))
    if out.data_both:
        a, b = c[both], r[both]
        err = np.abs(a - b)
        out.exact = int((a == b).sum())
        if tol is not None:
            bound = tol[m][both]
            out.within_tolerance = int((err <= bound).sum())
            with np.errstate(divide="ignore", invalid="ignore"):
                util = np.where(bound > 0, err / np.where(bound > 0, bound, 1.0), np.where(err > 0, np.inf, 0.0))
            out.max_tol_util = float(util.max())
        else:
            out.within_tolerance = out.exact
        out.max_abs_error = float(err.max())
        out.sum_sq_error = float((err ** 2).sum())
        out.max_ulp = float(ulp_distance(a, b).max())
    return out


@dataclass
class CompareReport:
    workload: dict[str, Any]
    tau: float
    frames: dict[int, dict[str, StratumMetrics]] = field(default_factory=dict)
    coverage_problems: list[str] = field(default_factory=list)
    missing_input_tiles: dict[int, int] = field(default_factory=dict)

    def totals(self) -> dict[str, StratumMetrics]:
        tot = {s: StratumMetrics() for s in STRATA}
        for per in self.frames.values():
            for s, m in per.items():
                tot[s].add(m)
        return tot

    @property
    def exact_op(self) -> bool:
        return self.workload["params"]["op"] in ("min", "max")

    @property
    def passed(self) -> bool:
        t = self.totals()["all"]
        rate = t.exact if self.exact_op else t.within_tolerance
        return (not self.coverage_problems and t.nodata_only_candidate == 0 and t.nodata_only_reference == 0
                and rate == t.data_both)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload, "tau": self.tau, "passed": self.passed,
            "criterion": "exact" if self.exact_op else "tolerance",
            "coverage_problems": self.coverage_problems,
            "missing_input_tiles": {str(f): n for f, n in self.missing_input_tiles.items()},
            "totals": {s: m.summary() for s, m in self.totals().items()},
            "frames": {str(f): {s: m.summary() for s, m in per.items()} for f, per in self.frames.items()},
        }

    def table(self) -> str:
        rows = [f"{'stratum':<18}{'cells':>10}{'NoData agree':>14}{'exact':>10}{'within tol':>12}{'max |err|':>12}"
                f"{'max ULP':>10}{'tol util':>10}"]
        for s, m in self.totals().items():
            d = m.summary()
            rows.append(f"{s:<18}{d['cells']:>10}{d['nodata_agreement']:>14.4%}{d['exact_rate']:>10.4%}"
                        f"{d['tolerance_rate']:>12.4%}{d['max_abs_error']:>12.3g}{d['max_ulp']:>10.3g}"
                        f"{d['max_tol_util']:>10.3g}")
        verdict = "PASS" if self.passed else "FAIL"
        k = self.workload["params"].get("nest", 1)
        crit = ("bitwise equality" if self.exact_op else f"|a-b| <= {self.tau:g} * sum|v| over V(c)" if k == 1 else
                f"|a-b| <= {k} * {self.tau:g} * op^{k}(|v|)")
        rows.append(f"{verdict} ({crit}; NoData masks must agree; all tiles present once)")
        rows += [f"coverage: {p}" for p in self.coverage_problems]
        if self.missing_input_tiles:
            rows.append(f"not compared: {sum(self.missing_input_tiles.values())} tile(s) missing from the input "
                        "without a candidate output")
        return "\n".join(rows)


def _workload_of(store: TileStore) -> dict[str, Any] | None:
    return store.manifest.provenance.get("workload")


def compare_focal(input_store: TileStore, reference: TileStore, candidate: TileStore, op: str, shape: str,
                  radius: int, tau: float = DEFAULT_TAU, heatmap: str | None = None,
                  jobs: int | str | None = 1, nest: int = 1) -> CompareReport:
    mi, mr, mc = input_store.manifest, reference.manifest, candidate.manifest
    for name, m in (("reference", mr), ("candidate", mc)):
        if m.raster != mi.raster or m.tiling != mi.tiling:
            raise ValueError(f"{name} store has a different raster or tiling than the input store")
    workload = {"family": "focal", "params": {"op": op, "shape": shape, "radius": radius}}
    if nest > 1:
        workload["params"]["nest"] = nest
    for name, store in (("reference", reference), ("candidate", candidate)):
        w = _workload_of(store)
        if w is not None and w.get("params") != workload["params"]:
            raise ValueError(f"{name} was produced by workload {w.get('params')}, not {workload['params']}")

    report = CompareReport(workload=workload, tau=tau)
    nes = mc.provenance.get("nes") or {}
    report.coverage_problems += [f"emission multiplicity: {p}" for p in nes.get("multiplicity_problems", [])]
    ref_ids = {f.frame_id for f in mr.frames}
    cand_ids = {f.frame_id for f in mc.frames}
    for fid in sorted(ref_ids - cand_ids):
        report.coverage_problems.append(f"frame {fid} missing from candidate")
    for fid in sorted(cand_ids - ref_ids):
        report.coverage_problems.append(f"frame {fid} in candidate but not in reference")

    frames = [f.frame_id for f in mi.frames if f.frame_id in ref_ids and f.frame_id in cand_ids]
    first = mi.frames[0].frame_id
    tasks = [(fid, op, shape, radius, tau, heatmap if fid == first else None, nest) for fid in frames]
    stores = (input_store, reference, candidate)
    n = resolve_jobs(jobs, len(tasks), mi, nest * radius)
    if n > 1 and all(st.location for st in stores):
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(n, mp_context=mp.get_context("spawn"), initializer=_worker_init,
                                 initargs=tuple(st.location for st in stores)) as pool:
            results = list(pool.map(_worker_frame, tasks))
    else:
        results = [_compare_frame(stores, *t) for t in tasks]
    for fid, per, problems, n_missing in results:
        report.coverage_problems += problems
        report.frames[fid] = per
        if n_missing:
            report.missing_input_tiles[fid] = n_missing
    return report


def resolve_jobs(jobs: int | str | None, n_frames: int, manifest, radius: int) -> int:
    if jobs in (None, "auto"):
        import os
        cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
        per_worker = 12 * 8 * manifest.raster.bands * (manifest.tiling.tile_size + 2 * radius) * manifest.raster.width
        per_worker += 300 * 2**20
        avail = _mem_available()
        jobs = min(cores, avail // per_worker) if avail else cores
    return max(1, min(int(jobs), n_frames))


def _mem_available() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


_STORES: tuple[TileStore, TileStore, TileStore] | None = None


def _worker_init(*locations) -> None:
    global _STORES
    _STORES = tuple(TileStore.open(loc) for loc in locations)


def _worker_frame(task):
    return _compare_frame(_STORES, *task)


def _compare_frame(stores, fid: int, op: str, shape: str, radius: int, tau: float, heatmap: str | None,
                   nest: int = 1):
    input_store, reference, candidate = stores
    mi = input_store.manifest
    s, H, W, r = mi.tiling.tile_size, mi.raster.height, mi.raster.width, nest * radius
    expected = {(x, y) for y in range(mi.tiling.tiles_y) for x in range(mi.tiling.tiles_x)}
    fp_op = oracle.footprint(shape, radius)
    fp = oracle.footprint_nested(fp_op, nest)
    problems = []
    with input_store.frame_rows(fid) as xin, reference.frame_rows(fid) as xref, candidate.frame_rows(fid) as xcand:
        in_keys, cand_keys = xin.keys, xcand.keys
        missing_input = expected - set(in_keys)
        dup = len(cand_keys) - len(set(cand_keys))
        lost = (expected - missing_input) - set(cand_keys)
        if dup:
            problems.append(f"frame {fid}: {dup} duplicate tile(s) in candidate")
        if lost:
            problems.append(f"frame {fid}: {len(lost)} tile(s) missing from candidate, e.g. {sorted(lost)[:3]}")

        per = {name: StratumMetrics() for name in STRATA}
        hm = _Heatmap(H, W) if heatmap is not None else None
        for y0 in range(0, H, s):
            y1 = min(H, y0 + s)
            a0, a1 = max(0, y0 - r), min(H, y1 + r)
            inner = slice(y0 - a0, y0 - a0 + (y1 - y0))
            x = xin.rows(a0, a1)
            ref, cand = xref.rows(y0, y1), xcand.rows(y0, y1)
            tol = None
            if op in ("sum", "mean"):
                absx = np.where(np.isnan(x), np.nan, np.abs(x))
                tol = (nest * tau) * np.stack([oracle.focal_nested(absx[b], op, fp_op, nest)[inner]
                                               for b in range(x.shape[0])])
                tol = np.where(np.isnan(tol), 0.0, tol)
            masks = strata_masks(x, s, r, missing_input, fp, row0=a0, height=H)
            present = np.ones((y1 - y0, W), bool)
            for tx, ty in missing_input - set(cand_keys):
                if ty * s < y1 and (ty + 1) * s > y0:
                    present[max(ty * s, y0) - y0:min((ty + 1) * s, y1) - y0, tx * s:(tx + 1) * s] = False
            for name, mask in masks.items():
                per[name].add(_metrics(cand, ref, tol, mask[inner] & present))
            if hm is not None:
                hm.add(y0, cand, ref)
    if hm is not None:
        hm.write(heatmap, s, f"{op}:{shape}:{radius}" + (f" nested x{nest}" if nest > 1 else "") + f", frame {fid}")
    return fid, per, problems, len(missing_input - set(cand_keys))


class _Heatmap:
    def __init__(self, height: int, width: int, max_px: int = 2000):
        self.f = max(1, -(-max(height, width) // max_px))
        self.err = np.zeros((-(-height // self.f), -(-width // self.f)))
        self.mismatch = np.zeros(self.err.shape, bool)
        self.height, self.width = height, width

    def _blocks(self, y0: int, a: np.ndarray, reduce) -> None:
        f, n, W = self.f, a.shape[0], a.shape[1]
        cols = -(-W // f)
        pad = np.zeros((n, cols * f), a.dtype)
        pad[:, :W] = a
        colred = pad.reshape(n, cols, f).max(axis=2)
        rows = (np.arange(y0, y0 + n) // f)
        for g in np.unique(rows):
            reduce(g, colred[rows == g].max(axis=0))

    def add(self, y0: int, cand: np.ndarray, ref: np.ndarray) -> None:
        diff = np.abs(cand - ref)
        err = np.where(np.isnan(diff), 0.0, diff).max(axis=0)
        mismatch = (np.isnan(cand) != np.isnan(ref)).any(axis=0)
        self._blocks(y0, err, lambda g, v: np.maximum(self.err[g], v, out=self.err[g]))
        self._blocks(y0, mismatch, lambda g, v: np.logical_or(self.mismatch[g], v, out=self.mismatch[g]))

    def write(self, path: str, tile_size: int, title: str) -> None:
        if self.f > 1:
            title += f"\n(block maximum over {self.f}x{self.f} cells)"
        _write_heatmap(path, self.err, self.mismatch, tile_size / self.f, title)


def _write_heatmap(path: str, err: np.ndarray, nodata_mismatch: np.ndarray, tile_px: float, title: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import LogNorm
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError("--heatmap needs matplotlib: pip install 'mabench[plot]'") from exc
    fig, ax = plt.subplots(figsize=(7, 7 * err.shape[0] / err.shape[1] + 0.8))
    shown = np.where(err > 0, err, np.nan)
    im = ax.imshow(shown, norm=LogNorm() if np.nanmax(shown, initial=0) > 0 else None, cmap="viridis", interpolation="nearest")
    if nodata_mismatch.any():
        ys, xs = np.nonzero(nodata_mismatch)
        ax.scatter(xs, ys, s=2, c="red", label="NoData mismatch")
        ax.legend(loc="upper right", fontsize=8)
    if tile_px >= 4:
        for k in np.arange(tile_px, err.shape[1], tile_px):
            ax.axvline(k - 0.5, color="white", lw=0.4, alpha=0.6)
        for k in np.arange(tile_px, err.shape[0], tile_px):
            ax.axhline(k - 0.5, color="white", lw=0.4, alpha=0.6)
    ax.set_title(f"|candidate - reference|, {title}\n(blank = equal; lines = tile boundaries)", fontsize=9)
    fig.colorbar(im, ax=ax, shrink=0.8, label="absolute error")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def save_report(report: CompareReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
