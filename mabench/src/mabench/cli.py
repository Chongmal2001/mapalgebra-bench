"""mabench command line.

  mabench prepare   raster file(s) -> tile store (one file per frame)
  mabench synth     synthetic raster -> tile store
  mabench derive    store -> store: frame subset, bands, rescale, injected NoData, removed / late tiles, shuffled order
  mabench inspect   print a store's manifest summary
  mabench verify    checksums, completeness, padding (exit 1 on problems)
  mabench workload  write workload.json for NES / GeoTrellis
  mabench reference oracle result for a workload -> output store
  mabench compare   candidate output vs reference output (exit 1 on FAIL)
  mabench nes-query  NES statements that run a workload on a store and write a Binary dump
  mabench import-nes NES Binary dump -> candidate output store
  mabench perf-table throughput runs (scripts/perf/run.sh) -> one CSV row per run
  mabench report    figures (PNG) and markdown tables from perf.csv and compare reports
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any

from . import __version__
from .sources import GENERATORS, RasterioSource, SyntheticSource
from .storage import backend_for
from .store import TileStore
from .tiler import tile_source
from .workloads import load_workload, parse_workload


def _with_nest(w, nest: int | None):
    if nest is None:
        return w
    if getattr(w, "family", None) != "focal":
        raise SystemExit("--nest applies to focal workloads only")
    from dataclasses import replace
    return replace(w, nest=nest)


def _workload(spec: str, nest: int | None = None):
    return _with_nest(load_workload(spec) if spec.endswith(".json") else parse_workload(spec), nest)


def _provenance(argv: list[str]) -> dict[str, Any]:
    from datetime import datetime, timezone
    return {"tool": "mabench", "version": __version__, "argv": argv,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _params(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        k, sep, v = pair.partition("=")
        if not sep:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        if "," in v:
            out[k] = [float(t) for t in v.split(",")]
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def _select_variable(path: str, variable: str) -> str:
    import rasterio

    with rasterio.open(path) as src:
        subdatasets = list(src.subdatasets or [])
    if not subdatasets:
        raise SystemExit(f"{path}: --variable {variable} given, but the file has no variables "
                         "(it is a plain raster; drop --variable)")
    matches = [sd for sd in subdatasets if sd.replace("/", ":").rsplit(":", 1)[-1] == variable]
    if len(matches) != 1:
        names = ", ".join(sd.replace("/", ":").rsplit(":", 1)[-1] for sd in subdatasets)
        raise SystemExit(f"{path}: no single variable named {variable!r}; available: {names}")
    return matches[0]


def _expand_inputs(inputs: list[str], variable: str | None) -> list[str]:
    files: list[str] = []
    for arg in inputs:
        if any(c in arg for c in "*?[") and ":" not in arg.split("/")[0]:
            matches = sorted(glob.glob(arg))
            if not matches:
                raise SystemExit(f"no files match {arg!r}")
            files += matches
        else:
            files.append(arg)
    return [_select_variable(f, variable) for f in files] if variable else files


def _refuse_existing(location: str, force: bool) -> None:
    if backend_for(location).exists("manifest.json") and not force:
        raise SystemExit(f"{location} already holds a tile store; pass --force to overwrite")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="mabench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="tile raster file(s); each file becomes one frame")
    p.add_argument("inputs", nargs="+", help="GDAL-readable files or glob patterns (GeoTIFF, COG, VRT, GRIB2, NetCDF); "
                                         "patterns are expanded and sorted by mabench itself")
    p.add_argument("--variable", help="variable to read from every container file (NetCDF, HDF5, HDF4, ...), "
                                      "e.g. CMI for GOES; not needed for GeoTIFF, COG, VRT or GRIB2")
    p.add_argument("--out", required=True, help="local directory or URL (s3://, gs://, ...)")
    p.add_argument("--tile-size", type=int, required=True)
    p.add_argument("--bands", help="comma-separated 1-based band indexes (default: all); all go into one maplet")
    p.add_argument("--frame-period-ms", type=int, default=0, help="event time step between frames")
    p.add_argument("--no-scale", action="store_true", help="do not apply scale/offset metadata")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("synth", help="synthetic raster -> tile store")
    p.add_argument("--out", required=True)
    p.add_argument("--width", type=int, required=True)
    p.add_argument("--height", type=int, required=True)
    p.add_argument("--generator", required=True, choices=GENERATORS)
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="generator / NoData parameter, e.g. nodata_fraction=0.1 or nodata_block=0,0,64,64")
    p.add_argument("--bands", type=int, default=1)
    p.add_argument("--frames", type=int, default=1)
    p.add_argument("--frame-period-ms", type=int, default=1000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tile-size", type=int, required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("derive", help="derive a store: frame subset, band selection, rescale, injected NoData, "
                                      "removed / late tiles, shuffled tile order (see mabench.derive)")
    p.add_argument("store")
    p.add_argument("--out", required=True)
    p.add_argument("--frames", help="input positions: a:b (slice) or 0,2,5; frames are renumbered 0..n-1")
    p.add_argument("--bands", help="comma-separated 1-based band indexes, in output order (e.g. 3,1)")
    p.add_argument("--scale", type=float, help="cells * scale + offset (NoData stays NoData)")
    p.add_argument("--offset", type=float)
    p.add_argument("--nan", action="append", default=[], metavar="F:SPEC",
                   help="inject NoData in derived frame F (or *): fraction=P | block=X0,Y0,W,H | tile=X,Y")
    p.add_argument("--drop-tiles", action="append", default=[], metavar="F:SPEC",
                   help="leave tiles out of derived frame F (or *): X,Y | row=Y | col=X")
    p.add_argument("--late-tiles", action="append", default=[], metavar="F:X,Y@G",
                   help="move tile (X, Y) of derived frame F to the end of frame G > F, flagged late")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--shuffle-tiles", type=int, metavar="SEED",
                   help="write the maplets of every frame in a random order (seeded per frame)")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("inspect", help="print a store's manifest summary")
    p.add_argument("store")
    p.add_argument("--json", action="store_true", help="print the full manifest")

    p = sub.add_parser("verify", help="check a store")
    p.add_argument("store")

    p = sub.add_parser("workload", help="write a workload.json")
    p.add_argument("--nest", type=int, metavar="N",
                   help="apply the focal operation N times, each on the previous result (case K13); default 1")
    p.add_argument("spec", help="e.g. focal:mean:square:3")
    p.add_argument("-o", "--out", required=True)

    p = sub.add_parser("reference", help="compute the oracle result of a workload")
    p.add_argument("--nest", type=int, metavar="N",
                   help="apply the focal operation N times, each on the previous result (case K13); default 1")
    p.add_argument("store")
    p.add_argument("--workload", required=True, help="workload.json or short spec (focal:mean:square:3)")
    p.add_argument("--out", required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("compare", help="compare a candidate output store with a reference output store")
    p.add_argument("--nest", type=int, metavar="N",
                   help="apply the focal operation N times, each on the previous result (case K13); default 1")
    p.add_argument("candidate", help="output store of the system under test (NES, GeoTrellis)")
    p.add_argument("--reference", required=True, help="oracle output store (mabench reference)")
    p.add_argument("--input", required=True, help="input store both outputs were computed from")
    p.add_argument("--workload", help="workload.json or short spec; default: taken from the reference's provenance")
    p.add_argument("--tau", type=float, default=1e-12, help="Sum/Mean tolerance factor (evaluation spec D3)")
    p.add_argument("--json", help="write the full report (per frame and stratum) to this file")
    p.add_argument("--heatmap", help="write an error heatmap of the first frame (PNG; needs matplotlib)")
    p.add_argument("--jobs", default=os.environ.get("MABENCH_JOBS", "auto"),
                   help="worker processes over frames: N or auto (cores, capped by frames and free memory; "
                        "env MABENCH_JOBS)")

    p = sub.add_parser("nes-query", help="print the NES statements that run a workload on a store into a Binary dump")
    p.add_argument("--nest", type=int, metavar="N",
                   help="apply the focal operation N times, each on the previous result (case K13); default 1")
    p.add_argument("store")
    p.add_argument("--workload", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dump", help="path the NES File sink writes to (absolute, as NES sees it)")
    g.add_argument("--void", action="store_true", help="Void sink instead of the dump (throughput runs; needs --yaml)")
    g.add_argument("--latency", help="FrameLatency sink writing first / last output time per source frame to this path "
                                     "(paced runs; as NES sees it)")
    p.add_argument("--frame-interval-us", type=int, default=0,
                   help="paced source: frame k released at t0 + (k - 1) * interval (default 0: unpaced)")
    p.add_argument("--pacing-log", help="paced source: CSV of scheduled and actual release per frame (as NES sees it)")
    p.add_argument("--source", default="tiles", help="logical source name")
    p.add_argument("--src-id", type=int, default=1)
    p.add_argument("--yaml", action="store_true", help="write a nes-cli topology file instead of SQL statements")
    p.add_argument("--worker", default="localhost:8080", help="worker gRPC address (--yaml)")
    p.add_argument("--data-address", default="localhost:9090", help="worker data address (--yaml)")
    p.add_argument("--store-path", help="store directory as the NES worker sees it (default: absolute local path)")
    p.add_argument("--passthrough", action="store_true", help="source -> sink without the focal window (baseline)")
    p.add_argument("--frames-per-window", type=int, default=1,
                   help="tumbling window size in event-time steps between frames (default 1: one frame per window)")
    p.add_argument("--slide-frames", type=int,
                   help="sliding window: advance in event-time steps (divides --frames-per-window; default: tumbling)")

    p = sub.add_parser("import-nes", help="turn a NES Binary dump into a candidate output store")
    p.add_argument("--nest", type=int, metavar="N",
                   help="apply the focal operation N times, each on the previous result (case K13); default 1")
    p.add_argument("dump", help="file written by the NES File sink with output format Binary, sink schema "
                                "(srcId UINT32, srcFrameId UINT32, ts UINT64, raster VARSIZED), all NOT NULL")
    p.add_argument("--input", required=True, help="input store the NES query read (RasterFile source)")
    p.add_argument("--workload", required=True, help="workload.json or short spec of the query (focal:mean:square:3)")
    p.add_argument("--out", required=True)
    p.add_argument("--src-id", type=int, help="require every record to carry this srcId "
                                              "(default: all records must share one srcId)")
    p.add_argument("--windows-per-frame", type=int,
                   help="expected copies of every output tile; others are reported as multiplicity problems, which "
                        "compare counts as coverage problems")
    p.add_argument("--frames-per-window", type=int,
                   help="window of the query (as for nes-query): expected copies per frame = windows containing it")
    p.add_argument("--slide-frames", type=int, help="slide of the query's window (as for nes-query)")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("perf-table", help="collect throughput runs (scripts/perf/run.sh) into one CSV, one row per run")
    p.add_argument("root", help="directory searched for meta.json files, e.g. data/perf")
    p.add_argument("--out", required=True, help="CSV file")

    p = sub.add_parser("report", help="figures and tables from perf.csv and the compare reports")
    p.add_argument("--perf", default="data/results/perf.csv", help="perf.csv of mabench perf-table")
    p.add_argument("--results", default="data/results", help="directory with <store>/<case>.json compare reports")
    p.add_argument("--out", default="data/results/report")
    p.add_argument("--format", default="png", help="comma-separated: png, pdf, svg")
    p.add_argument("--breakdown-op", default="max", help="op shown in the time-breakdown figure")

    args = ap.parse_args(argv)
    say = lambda msg: print(msg, file=sys.stderr)

    if args.cmd == "prepare":
        _refuse_existing(args.out, args.force)
        bands = [int(b) for b in args.bands.split(",")] if args.bands else None
        src = RasterioSource(_expand_inputs(args.inputs, args.variable), bands=bands, frame_period_ms=args.frame_period_ms,
                             apply_scale_offset=not args.no_scale)
        m = tile_source(src, backend_for(args.out), args.tile_size, _provenance(argv), say)
        say(f"wrote {len(m.frames)} frame(s), {m.tiling.tiles_x}x{m.tiling.tiles_y} tiles each, to {args.out}")
    elif args.cmd == "synth":
        _refuse_existing(args.out, args.force)
        src = SyntheticSource(args.width, args.height, args.generator, _params(args.param),
                              bands=args.bands, n_frames=args.frames, seed=args.seed,
                              frame_period_ms=args.frame_period_ms)
        m = tile_source(src, backend_for(args.out), args.tile_size, _provenance(argv), say)
        say(f"wrote {len(m.frames)} frame(s) to {args.out}")
    elif args.cmd == "derive":
        from .derive import derive
        _refuse_existing(args.out, args.force)
        try:
            m = derive(TileStore.open(args.store), args.out, args.frames,
                       [int(b) for b in args.bands.split(",")] if args.bands else None, args.scale, args.offset,
                       args.nan, args.drop_tiles, args.seed, _provenance(argv), say, args.shuffle_tiles,
                       args.late_tiles)
        except ValueError as exc:
            raise SystemExit(f"derive: {exc}")
        say(f"wrote {len(m.frames)} frame(s), {m.raster.bands} band(s), to {args.out}")
    elif args.cmd == "inspect":
        m = TileStore.open(args.store).manifest
        if args.json:
            print(json.dumps(m.to_dict(), indent=2))
        else:
            r, t = m.raster, m.tiling
            total = sum(f.nbytes for f in m.frames)
            cells = r.width * r.height * r.bands
            print(f"raster   {r.width} x {r.height} x {r.bands} band(s), crs={r.crs}")
            print(f"tiling   {t.tile_size}px, {t.tiles_x} x {t.tiles_y} tiles, {t.tile_order}, padding={t.edge_padding}")
            print(f"frames   {len(m.frames)}, {total / 2**20:.1f} MiB on disk")
            for f in m.frames[:10]:
                print(f"  #{f.frame_id:<5} t={f.event_time_ms:<12} valid={f.valid_cells / cells:6.1%}  {f.sha256[:12]}")
            if len(m.frames) > 10:
                print(f"  ... {len(m.frames) - 10} more")
    elif args.cmd == "verify":
        report = TileStore.open(args.store).verify()
        for problem in report.problems:
            print(problem)
        print("OK" if report.ok else f"{len(report.problems)} problem(s)")
        return 0 if report.ok else 1
    elif args.cmd == "workload":
        w = _workload(args.spec, args.nest)
        w.save(args.out)
        say(f"wrote {w.name} to {args.out}")
    elif args.cmd == "reference":
        _refuse_existing(args.out, args.force)
        store = TileStore.open(args.store)
        w = _workload(args.workload, args.nest)
        for note in w.validate(store.manifest):
            say(f"{note.level}: {note.message}")
        w.reference(store, args.out)
        say(f"wrote reference for {w.name} to {args.out}")
    elif args.cmd == "compare":
        from .compare import save_report
        from .workloads import workload_from_dict
        inp, ref, cand = TileStore.open(args.input), TileStore.open(args.reference), TileStore.open(args.candidate)
        if args.workload:
            w = _workload(args.workload, args.nest)
        elif "workload" in ref.manifest.provenance:
            w = _with_nest(workload_from_dict(ref.manifest.provenance["workload"]), args.nest)
        else:
            raise SystemExit("reference has no workload in its provenance; pass --workload")
        report = w.compare(inp, ref, cand, tau=args.tau, heatmap=args.heatmap,
                           jobs=args.jobs if args.jobs == "auto" else int(args.jobs))
        print(f"{w.name}: {args.candidate} vs {args.reference}")
        print(report.table())
        if args.json:
            save_report(report, args.json)
        return 0 if report.passed else 1
    elif args.cmd == "nes-query":
        from .nes_dump import nes_query
        w = _workload(args.workload, args.nest)
        if args.void and not args.yaml:
            raise SystemExit("nes-query: --void needs --yaml")
        print(nes_query(TileStore.open(args.store), w, args.dump, args.source, args.src_id, yaml=args.yaml,
                        worker=args.worker, data_address=args.data_address, store_path=args.store_path,
                        passthrough=args.passthrough, frames_per_window=args.frames_per_window,
                        slide_frames=args.slide_frames, latency_path=args.latency, frame_interval_us=args.frame_interval_us, pacing_log=args.pacing_log))
    elif args.cmd == "import-nes":
        from .nes_dump import import_nes
        _refuse_existing(args.out, args.force)
        w = _workload(args.workload, args.nest)
        try:
            m, report = import_nes(args.dump, TileStore.open(args.input), w, args.out, src_id=args.src_id,
                                   provenance=_provenance(argv), progress=say,
                                   windows_per_frame=args.windows_per_frame,
                                   frames_per_window=args.frames_per_window, slide_frames=args.slide_frames)
        except ValueError as exc:
            raise SystemExit(f"import-nes: {exc}")
        for note in report.notes():
            say(f"note: {note}")
        say(f"wrote {len(m.frames)} frame(s) from {report.records} record(s) to {args.out}")
    elif args.cmd == "perf-table":
        from .perf import write_table
        n = write_table(args.root, args.out)
        say(f"wrote {n} run(s) to {args.out}")
    elif args.cmd == "report":
        from .report import report
        paths = report(args.perf, args.results, args.out, tuple(args.format.split(",")), args.breakdown_op)
        say(f"wrote {len(paths)} file(s) to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
