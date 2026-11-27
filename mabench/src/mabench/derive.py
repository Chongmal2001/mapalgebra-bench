from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Callable

import numpy as np

from . import format as fmt
from .manifest import FrameInfo, Manifest
from .storage import backend_for
from .store import TileStore
from .tiler import frame_key


def parse_frames(spec: str | None, n: int) -> list[int]:
    if not spec:
        return list(range(n))
    if ":" in spec:
        a, _, b = spec.partition(":")
        out = list(range(n))[slice(int(a) if a else None, int(b) if b else None)]
    else:
        out = [int(t) for t in spec.split(",")]
    bad = [i for i in out if not 0 <= i < n]
    if bad or not out:
        raise ValueError(f"--frames {spec!r}: positions {bad or 'none'} outside the store's {n} frame(s)")
    return out


def _frame_sel(spec: str) -> tuple[str, str]:
    f, sep, rest = spec.partition(":")
    if not sep or not rest or not (f == "*" or f.isdigit()):
        raise ValueError(f"expected FRAME:SPEC with FRAME a number or '*', got {spec!r}")
    return f, rest


def _applies(f: str, fid: int) -> bool:
    return f == "*" or int(f) == fid


def _ints(s: str, n: int, what: str) -> list[int]:
    v = [int(t) for t in s.split(",")]
    if len(v) != n:
        raise ValueError(f"{what}: expected {n} integers, got {s!r}")
    return v


def parse_nan(spec: str) -> tuple[str, str, Any]:
    f, rest = _frame_sel(spec)
    kind, sep, val = rest.partition("=")
    if kind == "fraction" and sep:
        p = float(val)
        if not 0 <= p <= 1:
            raise ValueError(f"--nan {spec!r}: fraction outside [0, 1]")
        return f, kind, p
    if kind == "block" and sep:
        x0, y0, w, h = _ints(val, 4, f"--nan {spec!r}")
        if w <= 0 or h <= 0:
            raise ValueError(f"--nan {spec!r}: empty block")
        return f, kind, (x0, y0, w, h)
    if kind == "tile" and sep:
        return f, kind, tuple(_ints(val, 2, f"--nan {spec!r}"))
    raise ValueError(f"--nan {spec!r}: expected fraction=P, block=X0,Y0,W,H or tile=X,Y")


def parse_drop(spec: str) -> tuple[str, str, Any]:
    f, rest = _frame_sel(spec)
    if rest.startswith("row="):
        return f, "row", int(rest[4:])
    if rest.startswith("col="):
        return f, "col", int(rest[4:])
    return f, "tile", tuple(_ints(rest, 2, f"--drop-tiles {spec!r}"))


def parse_late(spec: str) -> tuple[int, tuple[int, int], int]:
    f, rest = _frame_sel(spec)
    tile, sep, g = rest.partition("@")
    if f == "*" or not sep or not g.isdigit() or int(g) <= int(f):
        raise ValueError(f"--late-tiles {spec!r}: expected F:X,Y@G with G > F")
    return int(f), tuple(_ints(tile, 2, f"--late-tiles {spec!r}")), int(g)


def derive(src: TileStore, out_location: str, frames: str | None = None, bands: list[int] | None = None,
           scale: float | None = None, offset: float | None = None, nan: list[str] = (), drop: list[str] = (),
           seed: int = 0, provenance: dict[str, Any] | None = None,
           progress: Callable[[str], None] | None = None, shuffle_tiles: int | None = None,
           late: list[str] = ()) -> Manifest:
    m = src.manifest
    order = sorted(m.frames, key=lambda f: (f.event_time_ms, f.frame_id))
    picked = [order[i] for i in parse_frames(frames, len(order))]
    nb = m.raster.bands
    if bands is not None:
        bad = [b for b in bands if not 1 <= b <= nb]
        if bad or not bands:
            raise ValueError(f"--bands {bands}: indices {bad or 'none'} outside 1..{nb}")
    sel = [b - 1 for b in bands] if bands else list(range(nb))
    nan_specs, drop_specs = [parse_nan(s) for s in nan], [parse_drop(s) for s in drop]
    late_specs = [parse_late(s) for s in late]
    s, tx_n, ty_n = m.tiling.tile_size, m.tiling.tiles_x, m.tiling.tiles_y
    for f, kind, v in drop_specs:
        if kind == "tile" and not (0 <= v[0] < tx_n and 0 <= v[1] < ty_n) or \
                kind == "row" and not 0 <= v < ty_n or kind == "col" and not 0 <= v < tx_n:
            raise ValueError(f"--drop-tiles {f}:{kind} {v} outside the {tx_n}x{ty_n} tile grid")
    for f, _, _ in nan_specs + drop_specs:
        if f != "*" and int(f) >= len(picked):
            raise ValueError(f"frame selector {f}: the derived store has frames 0..{len(picked) - 1}")
    for f, (x, y), g in late_specs:
        if g >= len(picked) or not (0 <= x < tx_n and 0 <= y < ty_n):
            raise ValueError(f"--late-tiles {f}:{x},{y}@{g}: frame or tile outside the derived store")
    pending: dict[int, list[tuple[fmt.MapletHeader, np.ndarray]]] = {}

    backend = backend_for(out_location)
    infos: list[FrameInfo] = []
    dropped_total = 0
    for new_id, info in enumerate(picked):
        fh0 = src.frame_header(info.frame_id)
        header = replace(fh0, frame_id=new_id, bands=len(sel))
        kept: list[tuple[fmt.MapletHeader, np.ndarray]] = []
        for h, cells in src.records(info.frame_id):
            key = (h.tile_x, h.tile_y)
            if any(_applies(f, new_id) and (kind == "tile" and v == key or kind == "row" and v == key[1]
                                            or kind == "col" and v == key[0]) for f, kind, v in drop_specs):
                dropped_total += 1
                continue
            c = cells[sel]
            if scale is not None or offset is not None:
                c = c * (1.0 if scale is None else scale) + (0.0 if offset is None else offset)
            c = np.array(c, dtype=fmt.CELL_DTYPE)
            mask = np.zeros((s, s), bool)
            for f, kind, v in nan_specs:
                if not _applies(f, new_id):
                    continue
                if kind == "fraction":
                    mask |= np.random.default_rng([seed, new_id, key[0], key[1]]).random((s, s)) < v
                elif kind == "tile":
                    if v == key:
                        mask[:] = True
                else:
                    x0, y0, w, hh = v
                    ox, oy = key[0] * s, key[1] * s
                    xa, xb = max(x0 - ox, 0), min(x0 + w - ox, s)
                    ya, yb = max(y0 - oy, 0), min(y0 + hh - oy, s)
                    if xa < xb and ya < yb:
                        mask[ya:yb, xa:xb] = True
            c[:, mask] = np.nan
            if g := next((g for f, k, g in late_specs if f == new_id and k == key), None):
                pending.setdefault(g, []).append((replace(h, bands=len(sel), frame_id=new_id, flags=fmt.FLAG_LATE), c))
                continue
            kept.append((replace(h, bands=len(sel)), c))
        if shuffle_tiles is not None:
            kept = [kept[i] for i in np.random.default_rng([shuffle_tiles, new_id]).permutation(len(kept))]
        own = len(kept)
        kept += pending.pop(new_id, [])
        if len(kept) > tx_n * ty_n:
            raise ValueError(f"--late-tiles: frame {new_id} would hold {len(kept)} maplets, more than the grid's "
                             f"{tx_n * ty_n}; append late tiles to a frame with removed tiles")
        key = frame_key(new_id)
        digest, nbytes, valid = hashlib.sha256(), 0, 0
        with backend.open_write(key) as fh:
            def emit(chunk: bytes) -> None:
                nonlocal nbytes
                fh.write(chunk)
                digest.update(chunk)
                nbytes += len(chunk)

            emit(replace(header, n_tiles=len(kept)).pack())
            for i, (h, c) in enumerate(kept):
                if i < own:
                    valid += int(np.count_nonzero(~np.isnan(c[:, :h.valid_rows, :h.valid_cols])))
                emit(fmt.encode_maplet(h, c))
        infos.append(FrameInfo(new_id, info.event_time_ms, key, len(kept), nbytes, digest.hexdigest(), valid,
                               {"derived_from_frame": info.frame_id, **info.source}))
        if progress:
            progress(f"frame {new_id} (input frame {info.frame_id}): {len(kept)} tiles, {valid} valid cells")

    in_sha = hashlib.sha256(src.backend.read_text("manifest.json").encode()).hexdigest()
    spec = {"frames": frames, "bands": bands, "scale": scale, "offset": offset, "nan": list(nan),
            "drop_tiles": list(drop), "seed": seed, "tiles_dropped": dropped_total, "shuffle_tiles": shuffle_tiles,
            "late_tiles": list(late)}
    tiling = m.tiling if shuffle_tiles is None else replace(m.tiling, tile_order="shuffled")
    manifest = Manifest(raster=replace(m.raster, bands=len(sel)), tiling=tiling, frames=infos,
                        provenance={"derived_from": str(src.location), "input_manifest_sha256": in_sha,
                                    "derive": spec, "input_provenance": m.provenance, **(provenance or {})})
    manifest.save(backend)
    return manifest
