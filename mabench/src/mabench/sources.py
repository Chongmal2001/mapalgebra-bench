"""Raster sources: where frames come from before tiling.

Every source exposes the same contract: a fixed geometry and a sequence of
frames, each of which can read any rectangular window as a float64 array of
shape (bands, rows, cols) with NaN for NoData. The tiler never needs to know
whether a frame came from a GeoTIFF, a NetCDF file, a generator or memory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import numpy as np

from .manifest import RasterInfo


class FrameReader(ABC):
    @abstractmethod
    def read(self, row: int, col: int, rows: int, cols: int) -> np.ndarray:
        """Window inside the raster extent -> float64 (bands, rows, cols), NaN = NoData."""


@dataclass
class Frame:
    frame_id: int
    event_time_ms: int
    open: Callable[[], Any]                   # context manager yielding a FrameReader
    description: dict[str, Any] = field(default_factory=dict)


class RasterSource(ABC):
    geometry: RasterInfo

    @abstractmethod
    def frames(self) -> Iterator[Frame]: ...

    def describe(self) -> dict[str, Any]:
        return {"type": type(self).__name__}


def _check_window(geom: RasterInfo, row: int, col: int, rows: int, cols: int) -> None:
    if row < 0 or col < 0 or rows <= 0 or cols <= 0 or row + rows > geom.height or col + cols > geom.width:
        raise ValueError(f"window ({row},{col},{rows},{cols}) outside raster {geom.height}x{geom.width}")


# --------------------------------------------------------------------------- rasterio


class RasterioSource(RasterSource):
    """One file per frame; anything GDAL opens (GeoTIFF, COG, VRT, NetCDF subdatasets).

    NoData comes from the dataset's nodata value and masks. Scale and offset
    metadata (e.g. GOES CMI) are applied by default so cells hold physical values.
    """

    def __init__(self, paths: list[str], bands: list[int] | None = None,
                 event_times_ms: list[int] | None = None, frame_period_ms: int = 0,
                 apply_scale_offset: bool = True):
        import rasterio

        if not paths:
            raise ValueError("no input files")
        if event_times_ms is not None and len(event_times_ms) != len(paths):
            raise ValueError("event_times_ms must have one entry per file")
        self.paths = list(paths)
        self.event_times_ms = event_times_ms
        self.frame_period_ms = frame_period_ms
        self.apply_scale_offset = apply_scale_offset

        with rasterio.open(self.paths[0]) as src:
            if src.count == 0:
                # a container such as NetCDF/HDF with several variables: GDAL exposes no bands at the top level
                names = [sd.replace("/", ":").rsplit(":", 1)[-1] for sd in (src.subdatasets or [])]
                hint = (f"it contains the variables {', '.join(names)}; choose one with --variable NAME"
                        ) if names else "it has no raster bands"
                raise ValueError(f"{self.paths[0]}: no raster to read at the top level; {hint}")
            self.bands = list(bands) if bands else list(range(1, src.count + 1))
            self.geometry = RasterInfo(
                width=src.width, height=src.height, bands=len(self.bands),
                transform=tuple(src.transform)[:6],
                crs=src.crs.to_string() if src.crs else None,
            )
            self._check_bands(src)

    def _check_bands(self, src) -> None:
        bad = [b for b in self.bands if not 1 <= b <= src.count]
        if bad:
            raise ValueError(f"{src.name}: band(s) {bad} not in 1..{src.count}")

    def _check_same_grid(self, src) -> None:
        g = self.geometry
        crs = src.crs.to_string() if src.crs else None
        if (src.width, src.height) != (g.width, g.height) or tuple(src.transform)[:6] != g.transform or crs != g.crs:
            raise ValueError(f"{src.name}: grid differs from first frame; all frames must share one grid")
        self._check_bands(src)

    def describe(self) -> dict[str, Any]:
        return {"type": "rasterio", "files": self.paths, "bands": self.bands,
                "apply_scale_offset": self.apply_scale_offset}

    def frames(self) -> Iterator[Frame]:
        for i, path in enumerate(self.paths):
            t = self.event_times_ms[i] if self.event_times_ms else i * self.frame_period_ms
            yield Frame(i, t, lambda p=path: self._open(p), {"file": path})

    @contextmanager
    def _open(self, path: str):
        import rasterio

        with rasterio.open(path) as src:
            self._check_same_grid(src)
            yield _RasterioReader(src, self.bands, self.geometry, self.apply_scale_offset)


class _RasterioReader(FrameReader):
    def __init__(self, src, bands, geometry, apply_scale_offset):
        self.src, self.bands, self.geometry = src, bands, geometry
        scales = np.array([src.scales[b - 1] for b in bands], dtype=np.float64)
        offsets = np.array([src.offsets[b - 1] for b in bands], dtype=np.float64)
        self.affine = apply_scale_offset and (np.any(scales != 1.0) or np.any(offsets != 0.0))
        self.scales, self.offsets = scales[:, None, None], offsets[:, None, None]

    def read(self, row, col, rows, cols):
        from rasterio.windows import Window

        _check_window(self.geometry, row, col, rows, cols)
        data = self.src.read(self.bands, window=Window(col, row, cols, rows), masked=True)
        data = data.astype(np.float64)
        if self.affine:
            data = data * self.scales + self.offsets
        return np.ma.filled(data, np.nan)


# --------------------------------------------------------------------------- in-memory


class ArraySource(RasterSource):
    """Frames already in memory, e.g. oracle outputs that are written back as a tile store."""

    def __init__(self, geometry: RasterInfo, frames: list[tuple[int, int, np.ndarray]],
                 description: dict[str, Any] | None = None):
        self.geometry = geometry
        self._frames = frames
        self._description = description or {"type": "array"}
        for _, _, a in frames:
            if a.shape != (geometry.bands, geometry.height, geometry.width):
                raise ValueError(f"array shape {a.shape} does not match geometry")

    def describe(self):
        return self._description

    def frames(self):
        for fid, t, arr in self._frames:
            yield Frame(fid, t, lambda a=arr: _nullcontext(_ArrayReader(a)), {})


class _ArrayReader(FrameReader):
    def __init__(self, arr):
        self.arr = arr

    def read(self, row, col, rows, cols):
        return np.asarray(self.arr[:, row:row + rows, col:col + cols], dtype=np.float64)


@contextmanager
def _nullcontext(value):
    yield value


# --------------------------------------------------------------------------- synthetic


def _splitmix64(z: np.ndarray) -> np.ndarray:
    z = z + np.uint64(0x9E3779B97F4A7C15)
    z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return z ^ (z >> np.uint64(31))


def _unit(seed: int, stream: int, index: np.ndarray) -> np.ndarray:
    """Counter-based uniform [0, 1): depends only on (seed, stream, cell index)."""
    key = np.uint64((seed * 0x100000001B3 + stream * 0x9E3779B1) & 0xFFFFFFFFFFFFFFFF)
    with np.errstate(over="ignore"):
        z = _splitmix64(_splitmix64(index.astype(np.uint64) ^ key))
    return (z >> np.uint64(11)).astype(np.float64) * (1.0 / 9007199254740992.0)


GENERATORS = ("constant", "ramp", "checker", "uniform", "normal", "waves")


class SyntheticSource(RasterSource):
    """Deterministic synthetic rasters of any size.

    Every cell value is a pure function of (seed, frame, band, y, x), so any
    window read in any order yields the same values. This keeps the tiler, the
    oracle and a re-run consistent without storing the full raster.

    Generators and parameters (defaults in brackets):
      constant  c [1.0]
      ramp      a [0.01] * x + b [0.02] * y + c [0.0]
      checker   k [8] cells per square, lo [0.0], hi [1.0]
      uniform   lo [0.0], hi [1.0]
      normal    mu [0.0], sigma [1.0]
      waves     n [6] superposed sinusoids, amplitude [100.0], wavelength [256.0]
    NoData overlays, applied after the generator:
      nodata_fraction p [0.0]          each cell independently NoData with probability p
      nodata_block y0,x0,y1,x1 [none]  half-open rectangle of NoData in global cell coordinates
    Outlier overlay (high dynamic range, e.g. to break plain running sums), applied before NoData:
      outlier_fraction p [0.0]         each cell independently replaced by +-outlier_scale with probability p
      outlier_scale s [1e12]
    """

    def __init__(self, width: int, height: int, generator: str, params: dict[str, Any] | None = None,
                 bands: int = 1, n_frames: int = 1, seed: int = 0, frame_period_ms: int = 1000):
        if generator not in GENERATORS:
            raise ValueError(f"unknown generator {generator!r}; choose from {GENERATORS}")
        self.geometry = RasterInfo(width=width, height=height, bands=bands,
                                   transform=(1.0, 0.0, 0.0, 0.0, -1.0, float(height)), crs=None)
        self.generator, self.params = generator, dict(params or {})
        self.n_frames, self.seed, self.frame_period_ms = n_frames, seed, frame_period_ms
        if generator == "waves":
            rng = np.random.default_rng(seed)
            n = int(self.params.get("n", 6))
            self._waves = (rng.uniform(0, 2 * np.pi, n), rng.uniform(0, 2 * np.pi, n), rng.uniform(0.5, 2.0, n))

    def describe(self):
        return {"type": "synthetic", "generator": self.generator, "params": self.params,
                "seed": self.seed, "width": self.geometry.width, "height": self.geometry.height,
                "bands": self.geometry.bands, "n_frames": self.n_frames}

    def frames(self):
        for f in range(self.n_frames):
            yield Frame(f, f * self.frame_period_ms,
                        lambda f=f: _nullcontext(_SyntheticReader(self, f)), {"frame": f})

    def values(self, frame: int, band: int, yy: np.ndarray, xx: np.ndarray) -> np.ndarray:
        p, g = self.params, self.generator
        W, H, B = self.geometry.width, self.geometry.height, self.geometry.bands
        index = ((frame * B + band) * H + yy.astype(np.int64)) * W + xx.astype(np.int64)
        if g == "constant":
            v = np.full(yy.shape, float(p.get("c", 1.0)))
        elif g == "ramp":
            v = float(p.get("a", 0.01)) * xx + float(p.get("b", 0.02)) * yy + float(p.get("c", 0.0))
        elif g == "checker":
            k = int(p.get("k", 8))
            v = np.where(((yy // k) + (xx // k)) % 2 == 0, float(p.get("lo", 0.0)), float(p.get("hi", 1.0)))
        elif g == "uniform":
            lo, hi = float(p.get("lo", 0.0)), float(p.get("hi", 1.0))
            v = lo + (hi - lo) * _unit(self.seed, 1, index)
        elif g == "normal":
            u1 = np.maximum(_unit(self.seed, 2, index), 1e-300)
            u2 = _unit(self.seed, 3, index)
            v = float(p.get("mu", 0.0)) + float(p.get("sigma", 1.0)) * np.sqrt(-2 * np.log(u1)) * np.cos(2 * np.pi * u2)
        else:  # waves: smooth, terrain-like global field
            amp, wl = float(p.get("amplitude", 100.0)), float(p.get("wavelength", 256.0))
            ang, phase, scale = self._waves
            v = np.zeros(yy.shape)
            for a, ph, s in zip(ang, phase, scale):
                v += np.sin(2 * np.pi * s * (np.cos(a) * xx + np.sin(a) * yy) / wl + ph + 0.1 * frame)
            v *= amp / len(ang)
        v = v.astype(np.float64)
        of = float(p.get("outlier_fraction", 0.0))
        if of > 0:
            hit = _unit(self.seed, 5, index) < of
            sign = np.where(_unit(self.seed, 6, index) < 0.5, -1.0, 1.0)
            v[hit] = (float(p.get("outlier_scale", 1e12)) * sign)[hit]
        frac = float(p.get("nodata_fraction", 0.0))
        if frac > 0:
            v[_unit(self.seed, 4, index) < frac] = np.nan
        block = p.get("nodata_block")
        if block:
            y0, x0, y1, x1 = (int(t) for t in block)
            v[(yy >= y0) & (yy < y1) & (xx >= x0) & (xx < x1)] = np.nan
        return v


class _SyntheticReader(FrameReader):
    def __init__(self, source: SyntheticSource, frame: int):
        self.source, self.frame = source, frame

    def read(self, row, col, rows, cols):
        _check_window(self.source.geometry, row, col, rows, cols)
        yy, xx = np.mgrid[row:row + rows, col:col + cols]
        return np.stack([self.source.values(self.frame, b, yy, xx) for b in range(self.source.geometry.bands)])
