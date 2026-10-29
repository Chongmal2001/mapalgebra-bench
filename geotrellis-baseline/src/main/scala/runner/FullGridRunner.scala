package de.tuberlin.dima.geotrellisbaseline.runner

import de.tuberlin.dima.geotrellisbaseline.focal.FullGrid
import de.tuberlin.dima.geotrellisbaseline.tilestore.{TileStore, TileStoreWriter}

/** Correctness reference: every frame as one untiled grid per band (see `FullGrid`). Output = tile store with the
  * input's raster and tiling, usable as `mabench compare --reference`. */
object FullGridRunner {

  def run(args: Main.Args, log: String => Unit = s => System.err.println(s)): Vector[Main.FrameTiming] = {
    val store = TileStore.open(args.store)
    val m = store.manifest
    if (args.verifyInput) store.verifyChecksums()
    Main.checkHeap(m.frames.map(_.nbytes).max, 4, log)
    val writer = new TileStoreWriter(args.out, store)
    val timings = m.frames.toVector.map { info =>
      val frame = FullGrid.assemble(store.readFrame(info), m.raster.width, m.raster.height)
      val t0 = System.nanoTime()
      val result = FullGrid.compute(frame, args.workload)
      val t1 = System.nanoTime()
      writer.writeFrame(info, frame.headers.size, FullGrid.split(frame, result))
      val t = Main.FrameTiming(info.frame_id, frame.headers.size, m.raster.bands, m.raster.width.toLong * m.raster.height * m.raster.bands, t1 - t0)
      log(f"frame ${t.frameId}: ${m.raster.width}x${m.raster.height} x ${t.bands} band(s), compute ${t.computeNanos / 1e6}%.1f ms")
      t
    }
    Main.finish(writer, args, timings)
    timings
  }
}
