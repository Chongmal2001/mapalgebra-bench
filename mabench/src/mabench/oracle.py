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
    # square: two 1-D passes, O(r) per cell
    if fp.all():
        ones = np.ones(fp.shape[0])
        rows = ndi.correlate1d(x, ones, axis=1, mode="constant", cval=0.0)
        return ndi.correlate1d(rows, ones, axis=0, mode="constant", cval=0.0)
    return ndi.correlate(x, fp.astype(np.float64), mode="constant", cval=0.0)


def focal(a: np.ndarray, op: str, fp: np.ndarray) -> np.ndarray:
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


def focal_nested(a: np.ndarray, op: str, fp: np.ndarray, nest: int = 1) -> np.ndarray:
    for _ in range(nest):
        a = focal(a, op, fp)
    return a


def footprint_nested(fp: np.ndarray, nest: int = 1) -> np.ndarray:
    out = fp
    for _ in range(nest - 1):
        r_out, r = out.shape[0] // 2, fp.shape[0] // 2
        grown = np.zeros((2 * (r_out + r) + 1,) * 2, bool)
        for dy, dx in np.argwhere(fp):
            grown[dy:dy + out.shape[0], dx:dx + out.shape[1]] |= out
        out = grown
    return out


def naive_focal(a: np.ndarray, op: str, fp: np.ndarray) -> np.ndarray:
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
