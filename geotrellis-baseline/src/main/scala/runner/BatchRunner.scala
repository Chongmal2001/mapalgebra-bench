package de.tuberlin.dima.geotrellisbaseline.runner

import de.tuberlin.dima.geotrellisbaseline.focal.FocalEngine
import de.tuberlin.dima.geotrellisbaseline.tilestore.{TileStore, TileStoreWriter}

/** GeoTrellis' layer-level focal path over every frame, one frame = one window (`FocalEngine`): tiles buffered with
  * slices of their 8 neighbours. Compute core of the micro-batch baseline; timings.csv is indicative only. */
object BatchRunner {

  def run(args: Main.Args, log: String => Unit = s => System.err.println(s)): Vector[Main.FrameTiming] = {
    val store = TileStore.open(args.store)
    val m = store.manifest
    val w = args.workload
    if (w.radius > m.tiling.tile_size)
      log(s"note: radius ${w.radius} > tile size ${m.tiling.tile_size}; GeoTrellis gathers halos from the 8 adjacent tiles only")
    if (args.verifyInput) store.verifyChecksums()

    val bounds = FocalEngine.layerBounds(m.tiling.tiles_x, m.tiling.tiles_y)
    Main.checkHeap(m.frames.map(_.nbytes).max, 4, log)
    val writer = new TileStoreWriter(args.out, store)
    val timings = m.frames.toVector.map { info =>
      val prepared = FocalEngine.prepare(store.readFrame(info))
      val t0 = System.nanoTime()
      val result = FocalEngine.compute(prepared, w, bounds)
      val t1 = System.nanoTime()
      writer.writeFrame(info, prepared.keys.size, FocalEngine.toRecords(prepared, result))
      val t = Main.FrameTiming(info.frame_id, prepared.keys.size, m.raster.bands,
                               prepared.keys.size.toLong * m.raster.bands * m.tiling.tile_size * m.tiling.tile_size, t1 - t0)
      log(f"frame ${t.frameId}: ${t.tiles} tiles x ${t.bands} band(s), compute ${t.computeNanos / 1e6}%.1f ms")
      t
    }
    Main.finish(writer, args, timings)
    timings
  }
}
