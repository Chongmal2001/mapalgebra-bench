"""Reference implementation of the focal semantics (evaluation spec §3.1).

Works on the untiled frame: no tiles, halos or windows. Truncate edges plus
NoData-ignoring reductions are expressed as zero-fill (sum, count) and
+/-inf-fill (min, max) filters. ``naive_focal`` is the literal per-cell
definition and exists to validate ``focal`` on small rasters.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

OPS = ("sum", "mean", "min", "max")
SHAPES = ("square", "circle")


def footprint(shape: str, radius: int) -> np.ndarray:
    if radius < 0:
        raise ValueError("radius must be >= 0")
    dy, dx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    if shape == "square":
        return np.ones(dy.shape, dtype=bool)
    if shape == "circle":
        return dy * dy + dx * dx <= radius * radius   # boundary included, as GeoTrellis Circle
    raise ValueError(f"unknown shape {shape!r}")


def _window_sum(x: np.ndarray, fp: np.ndarray) -> np.ndarray:
    """Sum of x over the footprint around every cell, zero outside the array.

    An all-ones (square) footprint is applied as two 1-D passes (rows, then columns): O(r) per cell instead
    of O(r^2). Rounding differs from a cell-by-cell sum by at most ~(2r+1)·u·sum|v|, far inside the spec
    tolerance for the radii in the benchmark; masked footprints (circle) use the direct 2-D correlation.
    """
    if fp.all():
        ones = np.ones(fp.shape[0])
        rows = ndi.correlate1d(x, ones, axis=1, mode="constant", cval=0.0)
        return ndi.correlate1d(rows, ones, axis=0, mode="constant", cval=0.0)
    return ndi.correlate(x, fp.astype(np.float64), mode="constant", cval=0.0)


def focal(a: np.ndarray, op: str, fp: np.ndarray) -> np.ndarray:
    """2-D float64 array with NaN NoData -> focal result, NaN where no valid neighbour exists."""
    valid = ~np.isnan(a)
    if op in ("sum", "mean"):
        s = _window_sum(np.where(valid, a, 0.0), fp)
        c = _window_sum(valid.astype(np.float64), fp)
        c = np.rint(c)  # counts are exact integers; remove float noise before the c > 0 test
        out = s if op == "sum" else s / np.where(c > 0, c, 1.0)
        return np.where(c > 0, out, np.nan)
    if op == "max":
        m = ndi.maximum_filter(np.where(valid, a, -np.inf), footprint=fp, mode="constant", cval=-np.inf)
        return np.where(np.isneginf(m), np.nan, m)
    if op == "min":
        m = ndi.minimum_filter(np.where(valid, a, np.inf), footprint=fp, mode="constant", cval=np.inf)
        return np.where(np.isposinf(m), np.nan, m)
    raise ValueError(f"unknown op {op!r}")


def naive_focal(a: np.ndarray, op: str, fp: np.ndarray) -> np.ndarray:
    """Literal definition, one cell at a time. Slow; for validation only."""
    H, W = a.shape
    r = fp.shape[0] // 2
    reduce = {"sum": sum, "mean": lambda v: sum(v) / len(v), "min": min, "max": max}[op]
    out = np.full(a.shape, np.nan)
    for y in range(H):
        for x in range(W):
            vals = [a[y + dy, x + dx]
                    for dy in range(-r, r + 1) for dx in range(-r, r + 1)
                    if fp[dy + r, dx + r] and 0 <= y + dy < H and 0 <= x + dx < W
                    and not np.isnan(a[y + dy, x + dx])]
            if vals:
                out[y, x] = reduce(vals)
    return out
