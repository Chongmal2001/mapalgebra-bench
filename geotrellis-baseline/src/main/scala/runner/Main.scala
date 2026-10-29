package de.tuberlin.dima.geotrellisbaseline.runner

import java.io.PrintWriter
import java.nio.file.{Files, Path, Paths}
import java.time.Instant

import io.circe.Json

import de.tuberlin.dima.geotrellisbaseline.focal.FocalWorkload
import de.tuberlin.dima.geotrellisbaseline.tilestore.TileStoreWriter

/** {{{
  * sbt "run --mode fullgrid|tiled|microbatch --store DIR --workload (FILE.json | focal:<op>:<shape>:<r>) --out DIR [--no-verify]
  *          [--window-ms N] [--rate FPS] [--trigger frame|window] [--output checksum|store] [--warmup N]"
  * }}}
  * fullgrid: correctness reference (FullGridRunner). tiled: GeoTrellis layer focal per frame (BatchRunner).
  * microbatch: performance baseline (MicroBatchRunner); the bracketed options apply to it only.
  */
object Main {

  final case class Args(mode: String, store: Path, workload: FocalWorkload, out: Path, verifyInput: Boolean,
                        microbatch: MicroBatchRunner.Options = MicroBatchRunner.Options())

  final case class FrameTiming(frameId: Int, tiles: Int, bands: Int, cells: Long, computeNanos: Long)

  val Modes: Set[String] = Set("fullgrid", "tiled", "microbatch")

  def parseArgs(argv: Array[String]): Args = {
    def usage(msg: String): Nothing = {
      System.err.println(s"$msg\nusage: run --mode ${Modes.mkString("|")} --store DIR --workload (FILE.json | focal:<op>:<shape>:<r>) --out DIR " +
        "[--no-verify] [--window-ms N] [--rate FPS] [--trigger frame|window] [--output checksum|store] [--warmup N]")
      sys.exit(2)
    }
    var mode, store, workload, out: Option[String] = None
    var verify = true
    var mb = MicroBatchRunner.Options()
    def num[T](s: String, f: String => T, name: String): T = try f(s) catch { case _: NumberFormatException => usage(s"$name: not a number: $s") }
    val it = argv.iterator
    while (it.hasNext) it.next() match {
      case "--mode" if it.hasNext => mode = Some(it.next())
      case "--store" if it.hasNext => store = Some(it.next())
      case "--workload" if it.hasNext => workload = Some(it.next())
      case "--out" if it.hasNext => out = Some(it.next())
      case "--no-verify" => verify = false
      case "--window-ms" if it.hasNext => mb = mb.copy(windowMs = num(it.next(), _.toLong, "--window-ms"))
      case "--rate" if it.hasNext => mb = mb.copy(rate = num(it.next(), _.toDouble, "--rate"))
      case "--trigger" if it.hasNext => mb = mb.copy(trigger = it.next())
      case "--output" if it.hasNext => mb = mb.copy(output = it.next())
      case "--warmup" if it.hasNext => mb = mb.copy(warmup = num(it.next(), _.toInt, "--warmup"))
      case other => usage(s"unknown argument '$other'")
    }
    val md = mode.getOrElse(usage("--mode is required"))
    if (!Modes.contains(md)) usage(s"unknown mode '$md'")
    if (!Set("frame", "window").contains(mb.trigger)) usage(s"--trigger must be frame or window")
    if (!Set("checksum", "store").contains(mb.output)) usage(s"--output must be checksum or store")
    val w = workload.getOrElse(usage("--workload is required"))
    Args(md,
      Paths.get(store.getOrElse(usage("--store is required"))),
      if (w.endsWith(".json")) FocalWorkload.load(Paths.get(w)) else FocalWorkload.parse(w),
      Paths.get(out.getOrElse(usage("--out is required"))),
      verify, mb)
  }

  /** Stops before reading anything if `factor` x the largest frame does not fit the heap. */
  def checkHeap(frameBytes: Long, factor: Int, log: String => Unit): Unit = {
    val gib = 1L << 30
    val need = frameBytes * factor
    val max = Runtime.getRuntime.maxMemory
    log(f"largest frame ${frameBytes.toDouble / gib}%.2f GiB, heap ${max.toDouble / gib}%.1f GiB, estimated need ~${need.toDouble / gib}%.1f GiB")
    if (need > max)
      throw new IllegalStateException(f"heap too small: about ${need.toDouble / gib}%.1f GiB needed, ${max.toDouble / gib}%.1f GiB available; " +
        s"rerun with GT_HEAP=${math.ceil(need.toDouble / gib).toInt + 1}g")
  }

  def provenance(args: Args): Json = Json.obj(
    "role" -> Json.fromString("geotrellis"),
    "mode" -> Json.fromString(args.mode),
    "geotrellis_version" -> Json.fromString(Option(classOf[geotrellis.raster.Tile].getPackage.getImplementationVersion).getOrElse("unknown")),
    "workload" -> args.workload.toJson,
    "input" -> Json.fromString(args.store.toString),
    "created_utc" -> Json.fromString(Instant.now().toString))

  /** Manifest with provenance, plus timings.csv. */
  def finish(writer: TileStoreWriter, args: Args, timings: Vector[FrameTiming]): Unit = {
    writer.finish(provenance(args))
    val csv = new PrintWriter(Files.newBufferedWriter(args.out.resolve("timings.csv")))
    try {
      csv.println("frame_id,tiles,bands,cells,compute_ns")
      timings.foreach(t => csv.println(s"${t.frameId},${t.tiles},${t.bands},${t.cells},${t.computeNanos}"))
    } finally csv.close()
  }

  def main(argv: Array[String]): Unit = {
    val args = parseArgs(argv)
    args.mode match {
      case "fullgrid" => FullGridRunner.run(args)
      case "tiled" => BatchRunner.run(args)
      case "microbatch" => MicroBatchRunner.run(args)
    }
    System.err.println(s"wrote ${args.mode} ${args.workload.name} to ${args.out}")
  }
}
