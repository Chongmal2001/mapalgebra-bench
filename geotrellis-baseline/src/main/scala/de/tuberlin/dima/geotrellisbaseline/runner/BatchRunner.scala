package de.tuberlin.dima.geotrellisbaseline.runner

import java.io.PrintWriter
import java.nio.file.{Files, Path, Paths}
import java.time.Instant

import io.circe.Json

import de.tuberlin.dima.geotrellisbaseline.focal.{FocalEngine, FocalWorkload}
import de.tuberlin.dima.geotrellisbaseline.tilestore.{TileStore, TileStoreWriter}

/** Correctness baseline: runs a focal workload over every frame of a tile store, one frame = one window,
  * and writes the result as a tile store with the same raster and tiling (so mabench can compare it
  * with the oracle and with NES).
  *
  * {{{
  * sbt "run --store stores/syn --workload workloads/mean-sq-3.json --out out/gt/syn/mean-sq-3"
  * }}}
  *
  * It also writes timings.csv with the GeoTrellis compute time per frame (neighbour assembly + kernel,
  * excluding I/O and conversion). These timings are indicative only: no warm-up control, no JMH.
  * The performance numbers come from the micro-batch driver and JMH benchmarks.
  */
object BatchRunner {

  final case class Args(store: Path, workload: FocalWorkload, out: Path, verifyInput: Boolean)

  def parseArgs(argv: Array[String]): Args = {
    def usage(msg: String): Nothing = {
      System.err.println(s"$msg\nusage: BatchRunner --store DIR --workload (FILE.json | focal:<op>:<shape>:<r>) --out DIR [--no-verify]")
      sys.exit(2)
    }
    var store, workload, out: Option[String] = None
    var verify = true
    val it = argv.iterator
    while (it.hasNext) it.next() match {
      case "--store" if it.hasNext => store = Some(it.next())
      case "--workload" if it.hasNext => workload = Some(it.next())
      case "--out" if it.hasNext => out = Some(it.next())
      case "--no-verify" => verify = false
      case other => usage(s"unknown argument '$other'")
    }
    val w = workload.getOrElse(usage("--workload is required"))
    Args(
      Paths.get(store.getOrElse(usage("--store is required"))),
      if (w.endsWith(".json")) FocalWorkload.load(Paths.get(w)) else FocalWorkload.parse(w),
      Paths.get(out.getOrElse(usage("--out is required"))),
      verify)
  }

  final case class FrameTiming(frameId: Int, tiles: Int, bands: Int, cells: Long, computeNanos: Long)

  def run(args: Args, log: String => Unit = s => System.err.println(s)): Vector[FrameTiming] = {
    val store = TileStore.open(args.store)
    val m = store.manifest
    val w = args.workload
    if (w.radius > m.tiling.tile_size)
      log(s"note: radius ${w.radius} > tile size ${m.tiling.tile_size}; GeoTrellis gathers halos from the 8 adjacent tiles only " +
          "(correctness case C2/C3 in the evaluation spec)")
    if (args.verifyInput) store.verifyChecksums()

    val bounds = FocalEngine.layerBounds(m.tiling.tiles_x, m.tiling.tiles_y)
    checkHeap(m.frames.map(_.nbytes).max, log)
    val writer = new TileStoreWriter(args.out, store)
    val timings = m.frames.toVector.map { info =>
      val prepared = FocalEngine.prepare(store.readFrame(info)) // input records become garbage here
      val t0 = System.nanoTime()
      val result = FocalEngine.compute(prepared, w, bounds)
      val t1 = System.nanoTime()
      writer.writeFrame(info, prepared.keys.size, FocalEngine.toRecords(prepared, result))
      val t = FrameTiming(info.frame_id, prepared.keys.size, m.raster.bands,
                          prepared.keys.size.toLong * m.raster.bands * m.tiling.tile_size * m.tiling.tile_size, t1 - t0)
      log(f"frame ${t.frameId}: ${t.tiles} tiles x ${t.bands} band(s), compute ${t.computeNanos / 1e6}%.1f ms")
      t
    }

    writer.finish(Json.obj(
      "role" -> Json.fromString("geotrellis"),
      "tool" -> Json.fromString("geotrellis-baseline BatchRunner"),
      "geotrellis_version" -> Json.fromString(Option(classOf[geotrellis.raster.Tile].getPackage.getImplementationVersion).getOrElse("unknown")),
      "workload" -> w.toJson,
      "input" -> Json.fromString(args.store.toString),
      "input_frames" -> Json.obj(m.frames.map(f => f.frame_id.toString -> Json.fromString(f.sha256)): _*),
      "created_utc" -> Json.fromString(Instant.now().toString)))

    val csv = new PrintWriter(Files.newBufferedWriter(args.out.resolve("timings.csv")))
    try {
      csv.println("frame_id,tiles,bands,cells,compute_ns")
      timings.foreach(t => csv.println(s"${t.frameId},${t.tiles},${t.bands},${t.cells},${t.computeNanos}"))
    } finally csv.close()
    timings
  }

  /** GeoTrellis holds a whole frame (one micro-batch window) at once: the input layer, the buffered tiles
    * built by `bufferTiles`, and the result layer. Fail early with a clear message instead of an
    * OutOfMemoryError halfway through the first frame. */
  def checkHeap(frameBytes: Long, log: String => Unit): Unit = {
    val gib = 1L << 30
    val need = frameBytes * 4
    val max = Runtime.getRuntime.maxMemory
    log(f"largest frame ${frameBytes.toDouble / gib}%.2f GiB, heap ${max.toDouble / gib}%.1f GiB, " +
        f"estimated need ~${need.toDouble / gib}%.1f GiB (4x frame)")
    if (need > max)
      throw new IllegalStateException(f"heap too small for this store: about ${need.toDouble / gib}%.0f GiB needed, " +
        f"${max.toDouble / gib}%.1f GiB available; rerun with GT_HEAP=${math.ceil(need.toDouble / gib).toInt + 2}g sbt ..., " +
        "or use a smaller raster / fewer bands")
  }

  def main(argv: Array[String]): Unit = {
    val args = parseArgs(argv)
    run(args)
    System.err.println(s"wrote ${args.workload.name} result to ${args.out}")
  }
}
