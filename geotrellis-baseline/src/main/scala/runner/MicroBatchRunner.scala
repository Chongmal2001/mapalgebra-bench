package de.tuberlin.dima.geotrellisbaseline.runner

import java.io.PrintWriter
import java.lang.management.ManagementFactory
import java.nio.file.Files

import scala.collection.JavaConverters._

import io.circe.Json

import de.tuberlin.dima.geotrellisbaseline.focal.{FocalEngine, Workers}
import de.tuberlin.dima.geotrellisbaseline.tilestore.{FrameInfo, TileStore, TileStoreWriter}

/** Micro-batch performance baseline, modelled on a NES `WINDOW TUMBLING(ts, size windowMs ms)` query.
  *
  * Frames arrive complete at `rate` frames/s (0 = all available at once) and fall into event-time tumbling windows.
  * Trigger `frame`: each frame is computed when it arrives. Trigger `window`: the frames of a window are computed
  * together when the window closes (a frame of a later window arrives, or the stream ends). Each frame is one tiled
  * GeoTrellis layer (`FocalEngine`); kernel calls on `--threads` worker threads, reading and preparing frames on the
  * main thread. Latency of a batch = end of its computation minus the scheduled arrival of its last frame, so reading
  * the frame and waiting behind earlier batches count.
  *
  * Writes batches.csv (one row per batch) and summary.json; with output `store` also a tile store of the results.
  * Output `none` discards the result tiles (as a NES Void sink); `checksum` sums them inside the timed span.
  * Summary: `elapsed_ms` (first frame scheduled -> last batch done), `cpu_ms` (process CPU inside that span, all
  * threads incl. GC and JIT). Memory is not measured.
  */
object MicroBatchRunner {

  final case class Options(windowMs: Long = 0, rate: Double = 0, trigger: String = "frame", output: String = "checksum",
                           warmup: Int = 0)

  final case class Batch(windowStartMs: Long, frames: Int, tiles: Int, lastArrivalNs: Long, startNs: Long, doneNs: Long,
                         computeNs: Long)

  // accumulated collection time of all collectors (ms)
  private def gcMs: Long = ManagementFactory.getGarbageCollectorMXBeans.asScala.map(b => math.max(0L, b.getCollectionTime)).sum

  private val os = ManagementFactory.getOperatingSystemMXBean.asInstanceOf[com.sun.management.OperatingSystemMXBean]

  private def nowUs: Long = { val i = java.time.Instant.now(); i.getEpochSecond * 1000000L + i.getNano / 1000 }

  def run(args: Main.Args, log: String => Unit = s => System.err.println(s)): Json = {
    val o = args.microbatch
    val store = TileStore.open(args.store)
    val m = store.manifest
    val w = args.workload
    val frames = m.frames.toVector.sortBy(_.event_time_ms)
    val steps = frames.map(_.event_time_ms).sliding(2).collect { case Vector(a, b) if b > a => b - a }.toVector
    val windowMs = if (o.windowMs > 0) o.windowMs else if (steps.nonEmpty) steps.min else 1L
    def windowOf(f: FrameInfo): Long = Math.floorDiv(f.event_time_ms, windowMs)
    val perWindow = frames.groupBy(windowOf).values.map(_.size).max
    if (w.radius > m.tiling.tile_size)
      log(s"note: radius ${w.radius} > tile size ${m.tiling.tile_size}; GeoTrellis gathers halos from the 8 adjacent tiles only")
    Main.checkHeap(m.frames.map(_.nbytes).max, (if (o.trigger == "window") perWindow else 1) + 3, log)
    if (args.verifyInput) store.verifyChecksums()

    val bounds = FocalEngine.layerBounds(m.tiling.tiles_x, m.tiling.tiles_y)
    val writer = if (o.output == "store") Some(new TileStoreWriter(args.out, store)) else None
    Files.createDirectories(args.out)
    val workers = new Workers(args.threads)
    frames.take(o.warmup).foreach(f => FocalEngine.compute(FocalEngine.prepare(store.readFrame(f)), w, bounds, workers))

    var checksum = 0.0
    var tilesOut = 0L
    var bufferNs = 0L
    val batches = Vector.newBuilder[Batch]
    def process(items: Vector[(FrameInfo, FocalEngine.Prepared, Long)]): Unit = {
      val start = System.nanoTime()
      var compute = 0L
      items.foreach { case (info, p, _) =>
        val c0 = System.nanoTime()
        val r = FocalEngine.compute(p, w, bounds, workers)
        compute += System.nanoTime() - c0
        bufferNs += r.bufferNs
        writer match {
          case Some(wr) => wr.writeFrame(info, p.keys.size, FocalEngine.toRecords(p, r))
          case None if o.output == "checksum" => r.bands.foreach(_.values.foreach(_.foreachDouble(v => if (!v.isNaN) checksum += v)))
          case None => tilesOut += r.bands.map(_.size).sum
        }
      }
      batches += Batch(windowOf(items.head._1) * windowMs, items.size, items.map(_._2.keys.size).sum, items.map(_._3).max,
                       start, System.nanoTime(), compute)
    }

    System.gc()                // warm-up garbage collected before the span, not inside it
    val gc0 = gcMs
    val span0Us = nowUs
    val cpu0 = os.getProcessCpuTime
    val t0 = System.nanoTime()
    def arrival(i: Int): Long = if (o.rate > 0) t0 + (i / o.rate * 1e9).toLong else t0
    var pending = Vector.empty[(FrameInfo, FocalEngine.Prepared, Long)]
    frames.zipWithIndex.foreach { case (info, i) =>
      val a = arrival(i)
      val wait = a - System.nanoTime()
      if (wait > 0) Thread.sleep(wait / 1000000, (wait % 1000000).toInt)
      if (pending.nonEmpty && windowOf(pending.head._1) != windowOf(info)) { process(pending); pending = Vector.empty }
      val item = (info, FocalEngine.prepare(store.readFrame(info)), a)
      if (o.trigger == "frame") process(Vector(item)) else pending :+= item
    }
    if (pending.nonEmpty) process(pending)
    val wallNs = System.nanoTime() - t0
    val cpuNs = os.getProcessCpuTime - cpu0
    val span1Us = nowUs
    workers.close()
    val gcInWall = gcMs - gc0

    val bs = batches.result()
    val ms = (ns: Long) => ns / 1e6
    val csv = new PrintWriter(Files.newBufferedWriter(args.out.resolve("batches.csv")))
    try {
      csv.println("window_start_ms,frames,tiles,last_arrival_ms,start_ms,done_ms,latency_ms,queue_ms,compute_ms")
      bs.foreach(b => csv.println(Seq(b.windowStartMs, b.frames, b.tiles, ms(b.lastArrivalNs - t0), ms(b.startNs - t0),
        ms(b.doneNs - t0), ms(b.doneNs - b.lastArrivalNs), ms(b.startNs - b.lastArrivalNs), ms(b.computeNs)).mkString(",")))
    } finally csv.close()

    val lat = bs.map(b => ms(b.doneNs - b.lastArrivalNs)).sorted
    def pct(q: Double): Double = lat(math.max(0, math.ceil(q * lat.size).toInt - 1))
    val cells = bs.map(_.tiles.toLong).sum * m.raster.bands * m.tiling.tile_size * m.tiling.tile_size
    val summary = Json.obj(
      "mode" -> Json.fromString("microbatch"),
      "workload" -> w.toJson,
      "input" -> Json.fromString(args.store.toString),
      "window_ms" -> Json.fromLong(windowMs), "rate_fps" -> Json.fromDoubleOrNull(o.rate),
      "trigger" -> Json.fromString(o.trigger), "output" -> Json.fromString(o.output), "warmup_frames" -> Json.fromInt(o.warmup),
      "frames" -> Json.fromInt(frames.size), "batches" -> Json.fromInt(bs.size), "cells" -> Json.fromLong(cells),
      "elapsed_ms" -> Json.fromDoubleOrNull(ms(wallNs)),
      "span_start_us" -> Json.fromLong(span0Us), "span_end_us" -> Json.fromLong(span1Us),
      "cpu_ms" -> Json.fromDoubleOrNull(ms(cpuNs)),
      "threads" -> Json.fromInt(args.threads),
      "compute_ms" -> Json.fromDoubleOrNull(ms(bs.map(_.computeNs).sum)),
      "buffer_ms" -> Json.fromDoubleOrNull(ms(bufferNs)),
      "gc_ms" -> Json.fromLong(gcInWall),
      "throughput_cells_per_s" -> Json.fromDoubleOrNull(cells / (wallNs / 1e9)),
      "latency_ms" -> Json.obj("p50" -> Json.fromDoubleOrNull(pct(0.5)), "p95" -> Json.fromDoubleOrNull(pct(0.95)),
                               "p99" -> Json.fromDoubleOrNull(pct(0.99)), "max" -> Json.fromDoubleOrNull(lat.last)),
      "checksum" -> Json.fromDoubleOrNull(checksum),
      "tiles_out" -> Json.fromLong(tilesOut))
    Files.write(args.out.resolve("summary.json"), summary.spaces2.getBytes("UTF-8"))
    writer.foreach(_.finish(Main.provenance(args)))
    log(f"${bs.size} batches, ${frames.size} frames, elapsed ${ms(wallNs)}%.1f ms (GC $gcInWall ms, " +
        f"CPU ${ms(cpuNs)}%.1f ms), latency p50 ${pct(0.5)}%.1f ms, p99 ${pct(0.99)}%.1f ms")
    summary
  }
}
