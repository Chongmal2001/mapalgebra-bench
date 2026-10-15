"""Compare a candidate output store (NES, GeoTrellis) with a reference output store (oracle).

Implements the correctness metrics of evaluation-spec §3.2 for raster outputs (focal):

- coverage: every expected tile present exactly once;
- NoData agreement: confusion matrix of NoData masks;
- exact-match and tolerance-match rates; max absolute error; RMSE; max ULP distance;
- all of it per stratum: interior, seam band, raster-edge band, near-NoData, near-missing-tile.

Tolerance (spec §3.2, decision D3): Min/Max must match bit for bit. Sum must satisfy
|a - b| <= tau * sum_{V(c)} |v|, Mean the same bound divided by |V(c)|, i.e. tau * mean_{V(c)} |v|.
The bound scales with the magnitudes summed, so values near zero do not inflate relative errors.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from scipy import ndimage as ndi

from . import oracle
from .store import TileStore

DEFAULT_TAU = 1e-12
STRATA = ("all", "interior", "seam", "raster_edge", "near_nodata", "near_missing_tile")


# --------------------------------------------------------------------------- helpers

def ulp_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Number of representable float64 values between a and b (finite inputs; 0 for equal values, incl. +0/-0)."""
    def ordered(x):
        i = np.ascontiguousarray(x, dtype=np.float64).view(np.int64)
        i = np.where(i < 0, np.int64(-(2**63)) - i, i)       # monotone map of doubles onto int64 (-0.0 -> 0)
        return (i.view(np.uint64) ^ np.uint64(1 << 63))       # offset binary: order-preserving, unsigned
    ua, ub = ordered(a), ordered(b)
    d = np.where(ua >= ub, ua - ub, ub - ua)                  # exact in uint64, no overflow
    return d.astype(np.float64)


def strata_masks(input_frame: np.ndarray, tile_size: int, radius: int, missing_tiles: set[tuple[int, int]],
                 fp: np.ndarray, row0: int = 0, height: int | None = None) -> dict[str, np.ndarray]:
    """Boolean (n, W) masks per stratum, from input rows [row0, row0 + n) of a raster of total ``height``.

    seam:              distance to an internal tile boundary < r (the neighbourhood crosses a tile edge)
    raster_edge:       distance to the raster edge < r (the neighbourhood is truncated)
    near_nodata:       some cell of N(c) is NoData in some band of the input
    near_missing_tile: some cell of N(c) lies in a tile missing from the input frame
    interior:          none of the above
    Strata overlap; each one is reported on its own.

    near_nodata / near_missing_tile look r rows beyond a cell, so callers working strip by strip pass input
    rows with an r-row halo and keep only the inner rows of the result.
    """
    _, n, W = input_frame.shape
    H = n if height is None else height
    yy = np.arange(row0, row0 + n)[:, None]          # broadcast instead of full (n, W) coordinate grids
    xx = np.arange(W)[None, :]
    r, s = radius, tile_size
    # the neighbourhood of c crosses the left/right (top/bottom) edge of its tile, and a tile exists beyond it
    internal_x = ((xx % s < r) & (xx >= s)) | ((s - 1 - xx % s < r) & (xx // s < (W - 1) // s))
    internal_y = ((yy % s < r) & (yy >= s)) | ((s - 1 - yy % s < r) & (yy // s < (H - 1) // s))
    seam = internal_x | internal_y
    raster_edge = (xx < r) | (yy < r) | (xx >= W - r) | (yy >= H - r)
    fpf = fp.astype(np.float64)
    nodata = np.isnan(input_frame).any(axis=0).astype(np.float64)
    near_nodata = ndi.correlate(nodata, fpf, mode="constant", cval=0.0) > 0.5
    near_missing = np.zeros((n, W), bool)
    rows_here = [(tx, ty) for tx, ty in missing_tiles if ty * s < row0 + n + r and (ty + 1) * s > row0 - r]
    if rows_here:
        miss = np.zeros((n, W))
        for tx, ty in rows_here:
            miss[max(ty * s - row0, 0):max((ty + 1) * s - row0, 0), tx * s:(tx + 1) * s] = 1.0
        near_missing = ndi.correlate(miss, fpf, mode="constant", cval=0.0) > 0.5
    seam, raster_edge = np.broadcast_to(seam, (n, W)), np.broadcast_to(raster_edge, (n, W))
    interior = ~(seam | raster_edge | near_nodata | near_missing)
    return {"all": np.ones((n, W), bool), "interior": interior, "seam": seam, "raster_edge": raster_edge,
            "near_nodata": near_nodata, "near_missing_tile": near_missing}


# --------------------------------------------------------------------------- metrics

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

    def add(self, other: "StratumMetrics") -> None:
        for k in ("cells", "nodata_both", "nodata_only_candidate", "nodata_only_reference", "data_both",
                  "exact", "within_tolerance", "sum_sq_error"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.max_abs_error = max(self.max_abs_error, other.max_abs_error)
        self.max_ulp = max(self.max_ulp, other.max_ulp)

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
    """cand, ref: (bands, H, W); tol: (bands, H, W) or None for exact ops; mask: (H, W)."""
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
        out.within_tolerance = int((err <= tol[m][both]).sum()) if tol is not None else out.exact
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
            "totals": {s: m.summary() for s, m in self.totals().items()},
            "frames": {str(f): {s: m.summary() for s, m in per.items()} for f, per in self.frames.items()},
        }

    def table(self) -> str:
        rows = [f"{'stratum':<18}{'cells':>10}{'NoData agree':>14}{'exact':>10}{'within tol':>12}{'max |err|':>12}{'max ULP':>10}"]
        for s, m in self.totals().items():
            d = m.summary()
            rows.append(f"{s:<18}{d['cells']:>10}{d['nodata_agreement']:>14.4%}{d['exact_rate']:>10.4%}"
                        f"{d['tolerance_rate']:>12.4%}{d['max_abs_error']:>12.3g}{d['max_ulp']:>10.3g}")
        verdict = "PASS" if self.passed else "FAIL"
        crit = "bitwise equality" if self.exact_op else f"|a-b| <= {self.tau:g} * sum|v| over V(c)"
        rows.append(f"{verdict} ({crit}; NoData masks must agree; all tiles present once)")
        rows += [f"coverage: {p}" for p in self.coverage_problems]
        return "\n".join(rows)


# --------------------------------------------------------------------------- driver

def _workload_of(store: TileStore) -> dict[str, Any] | None:
    return store.manifest.provenance.get("workload")


def compare_focal(input_store: TileStore, reference: TileStore, candidate: TileStore, op: str, shape: str,
                  radius: int, tau: float = DEFAULT_TAU, heatmap: str | None = None) -> CompareReport:
    mi, mr, mc = input_store.manifest, reference.manifest, candidate.manifest
    for name, m in (("reference", mr), ("candidate", mc)):
        if m.raster != mi.raster or m.tiling != mi.tiling:
            raise ValueError(f"{name} store has a different raster or tiling than the input store")
    workload = {"family": "focal", "params": {"op": op, "shape": shape, "radius": radius}}
    for name, store in (("reference", reference), ("candidate", candidate)):
        w = _workload_of(store)
        if w is not None and w.get("params") != workload["params"]:
            raise ValueError(f"{name} was produced by workload {w.get('params')}, not {workload['params']}")

    report = CompareReport(workload=workload, tau=tau)
    fp = oracle.footprint(shape, radius)
    s, tx_n, ty_n = mi.tiling.tile_size, mi.tiling.tiles_x, mi.tiling.tiles_y
    expected = {(x, y) for y in range(ty_n) for x in range(tx_n)}
    ref_ids = {f.frame_id for f in mr.frames}
    cand_ids = {f.frame_id for f in mc.frames}
    for fid in sorted(ref_ids - cand_ids):
        report.coverage_problems.append(f"frame {fid} missing from candidate")
    for fid in sorted(cand_ids - ref_ids):
        report.coverage_problems.append(f"frame {fid} in candidate but not in reference")

    H, W, r = mi.raster.height, mi.raster.width, radius
    for info in mi.frames:
        fid = info.frame_id
        if fid not in ref_ids or fid not in cand_ids:
            continue
        in_keys, cand_keys = input_store.keys(fid), candidate.keys(fid)
        missing_input = expected - set(in_keys)
        dup = len(cand_keys) - len(set(cand_keys))
        lost = (expected - missing_input) - set(cand_keys)
        if dup:
            report.coverage_problems.append(f"frame {fid}: {dup} duplicate tile(s) in candidate")
        if lost:
            report.coverage_problems.append(f"frame {fid}: {len(lost)} tile(s) missing from candidate, e.g. {sorted(lost)[:3]}")

        # strip by strip (one tile row each): memory ~ width x (tile_size + 2r) cells per array, not the frame
        per = {name: StratumMetrics() for name in STRATA}
        hm = _Heatmap(H, W) if heatmap is not None and fid == mi.frames[0].frame_id else None
        for y0 in range(0, H, s):
            y1 = min(H, y0 + s)
            a0, a1 = max(0, y0 - r), min(H, y1 + r)           # input rows incl. r-row halo
            inner = slice(y0 - a0, y0 - a0 + (y1 - y0))
            x = input_store.assemble_rows(fid, a0, a1)
            ref, cand = reference.assemble_rows(fid, y0, y1), candidate.assemble_rows(fid, y0, y1)
            tol = None
            if op in ("sum", "mean"):
                absx = np.where(np.isnan(x), np.nan, np.abs(x))
                tol = tau * np.stack([oracle.focal(absx[b], op, fp)[inner] for b in range(x.shape[0])])
                tol = np.where(np.isnan(tol), 0.0, tol)
            masks = strata_masks(x, s, radius, missing_input, fp, row0=a0, height=H)
            for name, mask in masks.items():
                per[name].add(_metrics(cand, ref, tol, mask[inner]))
            if hm is not None:
                hm.add(y0, cand, ref)
        report.frames[fid] = per
        if hm is not None:
            hm.write(heatmap, s, f"{op}:{shape}:{radius}, frame {fid}")
    return report


class _Heatmap:
    """Error map of one frame, accumulated strip by strip and reduced to at most ``max_px`` pixels per side
    (block maximum), so a 20000 x 20000 frame never has to exist as a full error array or a full-size image."""

    def __init__(self, height: int, width: int, max_px: int = 2000):
        self.f = max(1, -(-max(height, width) // max_px))       # block size (ceil division)
        self.err = np.zeros((-(-height // self.f), -(-width // self.f)))
        self.mismatch = np.zeros(self.err.shape, bool)
        self.height, self.width = height, width

    def _blocks(self, y0: int, a: np.ndarray, reduce) -> None:
        """Reduce a (n, W) strip into the block grid, combining with what earlier strips put there."""
        f, n, W = self.f, a.shape[0], a.shape[1]
        cols = -(-W // f)
        pad = np.zeros((n, cols * f), a.dtype)
        pad[:, :W] = a
        colred = pad.reshape(n, cols, f).max(axis=2)              # block max along columns
        rows = (np.arange(y0, y0 + n) // f)
        for g in np.unique(rows):
            reduce(g, colred[rows == g].max(axis=0))

    def add(self, y0: int, cand: np.ndarray, ref: np.ndarray) -> None:
        diff = np.abs(cand - ref)
        err = np.where(np.isnan(diff), 0.0, diff).max(axis=0)     # worst band; equal NoData counts as 0
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
    if tile_px >= 4:                                         # tile lines only while they stay readable
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
