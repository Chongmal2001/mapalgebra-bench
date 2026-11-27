from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar

import numpy as np

from . import oracle
from .manifest import Manifest
from .sources import Frame, FrameReader, RasterSource, _nullcontext
from .storage import backend_for
from .store import TileStore
from .tiler import tile_source


class OutputKind(str, Enum):
    RASTER = "raster"
    TABLE = "table"


@dataclass(frozen=True)
class SideInput:
    name: str
    location: str
    description: str = ""


@dataclass(frozen=True)
class Note:
    level: str
    message: str


_REGISTRY: dict[str, type["Workload"]] = {}


class Workload(ABC):
    family: ClassVar[str]
    output_kind: ClassVar[OutputKind]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "family" in cls.__dict__:
            _REGISTRY[cls.family] = cls

    @abstractmethod
    def params(self) -> dict[str, Any]: ...

    @property
    def name(self) -> str:
        return self.family + ":" + ":".join(str(v) for v in self.params().values())

    def to_dict(self) -> dict[str, Any]:
        return {"family": self.family, "output_kind": self.output_kind.value, "params": self.params()}

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")

    def validate(self, manifest: Manifest) -> list[Note]:
        return []

    def side_inputs(self) -> list[SideInput]:
        return []

    def prepare_side_inputs(self, manifest: Manifest, out_location: str) -> None:
        ...

    @abstractmethod
    def reference(self, store: TileStore, out_location: str) -> Any:
        ...

    @abstractmethod
    def compare(self, input_store: TileStore, reference: TileStore, candidate: TileStore, **options) -> Any:
        ...


def workload_from_dict(d: dict[str, Any]) -> Workload:
    try:
        cls = _REGISTRY[d["family"]]
    except KeyError:
        raise ValueError(f"unknown workload family {d.get('family')!r}; known: {sorted(_REGISTRY)}")
    return cls(**d["params"])


def load_workload(path: str) -> Workload:
    with open(path, encoding="utf-8") as fh:
        return workload_from_dict(json.load(fh))


def parse_workload(spec: str) -> Workload:
    family, _, rest = spec.partition(":")
    if family == "focal":
        parts = rest.split(":")
        if len(parts) not in (3, 4):
            raise ValueError("focal spec is focal:<op>:<shape>:<radius>[:<nest>], e.g. focal:mean:square:3")
        return FocalWorkload(op=parts[0], shape=parts[1], radius=int(parts[2]),
                             nest=int(parts[3]) if len(parts) == 4 else 1)
    if family == "zonal":
        return ZonalWorkload(zones=rest)
    raise ValueError(f"unknown workload family {family!r}")


@dataclass(frozen=True)
class FocalWorkload(Workload):
    op: str
    shape: str
    radius: int
    nest: int = 1

    family: ClassVar[str] = "focal"
    output_kind: ClassVar[OutputKind] = OutputKind.RASTER

    def __post_init__(self):
        if self.op not in oracle.OPS:
            raise ValueError(f"op must be one of {oracle.OPS}")
        if self.shape not in oracle.SHAPES:
            raise ValueError(f"shape must be one of {oracle.SHAPES}")
        if self.radius < 1:
            raise ValueError("radius must be >= 1")
        if self.nest < 1:
            raise ValueError("nest must be >= 1")

    def params(self):
        p = {"op": self.op, "shape": self.shape, "radius": self.radius}
        if self.nest > 1:
            p["nest"] = self.nest
        return p

    @property
    def reach(self) -> int:
        return self.nest * self.radius

    def validate(self, manifest):
        notes = []
        if self.radius > manifest.tiling.tile_size:
            notes.append(Note("warning",
                f"radius {self.radius} > tile size {manifest.tiling.tile_size}: halos span more than one "
                "tile (correctness case C2/C3; GeoTrellis gathers halos from the 8 adjacent tiles only)"))
        if 2 * self.radius + 1 > min(manifest.raster.width, manifest.raster.height):
            notes.append(Note("warning", "neighbourhood is larger than the raster"))
        return notes

    def reference(self, store, out_location):
        m = store.manifest
        source = FocalReferenceSource(store, self.op, self.shape, self.radius, self.nest)
        return tile_source(source, backend_for(out_location), m.tiling.tile_size,
                           provenance={"role": "reference", "workload": self.to_dict(),
                                       "input_frames": {f.frame_id: f.sha256 for f in m.frames}})

    def compare(self, input_store, reference, candidate, **options):
        from .compare import compare_focal
        return compare_focal(input_store, reference, candidate, self.op, self.shape, self.radius, nest=self.nest,
                             **options)


class FocalReferenceSource(RasterSource):
    def __init__(self, store, op: str, shape: str, radius: int, nest: int = 1):
        self.store, self.op, self.radius, self.nest = store, op, radius, nest
        self.fp = oracle.footprint(shape, radius)
        self.geometry = store.manifest.raster

    def describe(self):
        return {"type": "reference", "input": self.store.backend.location}

    def frames(self):
        for info in self.store.manifest.frames:
            yield Frame(info.frame_id, info.event_time_ms,
                        lambda fid=info.frame_id: _nullcontext(_FocalReferenceReader(self, fid)), {})


class _FocalReferenceReader(FrameReader):
    def __init__(self, src: FocalReferenceSource, frame_id: int):
        self.src, self.frame_id = src, frame_id

    def read(self, row, col, rows, cols):
        src, H = self.src, self.src.geometry.height
        halo = src.nest * src.radius
        a0, a1 = max(0, row - halo), min(H, row + rows + halo)
        x = src.store.assemble_rows(self.frame_id, a0, a1)
        res = np.stack([oracle.focal_nested(x[b], src.op, src.fp, src.nest) for b in range(x.shape[0])])
        return res[:, row - a0:row - a0 + rows, col:col + cols]


@dataclass(frozen=True)
class ZonalWorkload(Workload):
    zones: str
    stats: tuple[str, ...] = ("count", "sum", "min", "max", "mean")
    inclusion: str = "center"

    family: ClassVar[str] = "zonal"
    output_kind: ClassVar[OutputKind] = OutputKind.TABLE

    def __post_init__(self):
        object.__setattr__(self, "stats", tuple(self.stats))

    def params(self):
        return {"zones": self.zones, "stats": list(self.stats), "inclusion": self.inclusion}

    def validate(self, manifest):
        return [Note("warning", "zonal workloads are not implemented yet (evaluation spec §4)")]

    def side_inputs(self):
        return [SideInput("zones", self.zones, "zone polygons; rasterised once per run on all sides")]

    def prepare_side_inputs(self, manifest, out_location):
        raise NotImplementedError("zonal: rasterisation of zone polygons (evaluation spec §4, D6/D7)")

    def reference(self, store, out_location):
        raise NotImplementedError("zonal: reference oracle (GDAL rasterise + group-by, evaluation spec §4)")

    def compare(self, input_store, reference, candidate, **options):
        raise NotImplementedError("zonal: table comparison joined on (window, zone) (evaluation spec §4)")
