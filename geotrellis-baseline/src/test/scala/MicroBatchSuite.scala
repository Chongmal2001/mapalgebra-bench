package de.tuberlin.dima.geotrellisbaseline

import java.nio.charset.StandardCharsets.UTF_8
import java.nio.file.{Files, Path}

import io.circe.Json
import org.scalatest.funsuite.AnyFunSuite

import de.tuberlin.dima.geotrellisbaseline.focal.{FocalEngine, FocalWorkload, Workers}
import de.tuberlin.dima.geotrellisbaseline.runner.{FullGridRunner, Main, MicroBatchRunner}
import de.tuberlin.dima.geotrellisbaseline.tilestore.TileStore

/** Micro-batch runner on the fixture `normal` (61x47, 2 bands, 2 frames 1000 ms apart, 16-px tiles, NoData). */
class MicroBatchSuite extends AnyFunSuite {
  private val wl = FocalWorkload.parse("focal:mean:square:2")

  private def run(o: MicroBatchRunner.Options, threads: Int = 1): (Path, Json) = {
    val out = Files.createTempDirectory("gtb").resolve("out")
    val summary = MicroBatchRunner.run(Main.Args("microbatch", Fixtures.path("normal"), wl, out, verifyInput = false,
                                                 microbatch = o, threads = threads), _ => ())
    (out, summary)
  }

  private def same(a: Double, b: Double): Boolean = if (a.isNaN || b.isNaN) a.isNaN && b.isNaN else a == b

  private def rows(out: Path): Int = Files.readAllLines(out.resolve("batches.csv"), UTF_8).size - 1

  private def num(j: Json, field: String): Double = j.hcursor.downField(field).as[Double].fold(e => fail(e.getMessage), identity)

  test("output store matches the full-grid reference for r <= tile size") {
    val (out, _) = run(MicroBatchRunner.Options(output = "store"))
    val ref = Files.createTempDirectory("gtb").resolve("ref")
    FullGridRunner.run(Main.Args("fullgrid", Fixtures.path("normal"), wl, ref, verifyInput = false), _ => ())
    val (a, b) = (TileStore.open(out), TileStore.open(ref))
    a.verifyChecksums()
    assert(rows(out) == 2)
    var bad = 0
    for ((fa, fb) <- a.manifest.frames.zip(b.manifest.frames)) {
      val ra = a.readFrame(fa).map(r => (r.header.tileX, r.header.tileY) -> r.cells).toMap
      for (r <- b.readFrame(fb); i <- r.cells.indices) {
        val (x, y) = (ra((r.header.tileX, r.header.tileY))(i), r.cells(i))
        if (!(if (x.isNaN || y.isNaN) x.isNaN && y.isNaN else math.abs(x - y) <= 1e-9 * math.max(1.0, math.abs(y)))) bad += 1
      }
    }
    assert(bad == 0, s"$bad cells differ from the full-grid reference")
  }

  test("trigger window groups the frames of a window into one batch; results do not depend on the trigger") {
    val (o1, s1) = run(MicroBatchRunner.Options(trigger = "frame"))
    val (o2, s2) = run(MicroBatchRunner.Options(trigger = "window", windowMs = 2000))
    assert(rows(o1) == 2 && rows(o2) == 1)
    assert(num(s1, "checksum") == num(s2, "checksum") && num(s1, "checksum") != 0.0)
    assert(num(s2, "window_ms") == 2000 && num(s1, "window_ms") == 1000)
  }

  test("output none discards the results, computes the same tiles") {
    val (o1, s1) = run(MicroBatchRunner.Options(output = "none"))
    val (_, s2) = run(MicroBatchRunner.Options(output = "checksum"))
    assert(num(s1, "checksum") == 0.0 && num(s2, "checksum") != 0.0)
    assert(num(s1, "tiles_out") == 2 * 2 * 12)       // frames x bands x tiles (4 x 3 grid)
    assert(num(s1, "gc_ms") >= 0 && num(s1, "compute_ms") > 0)
    assert(!Files.exists(o1.resolve("resources.csv")))      // memory is not measured
  }

  test("compute (BufferTiles + kernel per tile, 1 and 4 threads) equals CollectionFocalOperation bit for bit") {
    val s = TileStore.open(Fixtures.path("normal"))
    val bounds = FocalEngine.layerBounds(s.manifest.tiling.tiles_x, s.manifest.tiling.tiles_y)
    val p = FocalEngine.prepare(s.readFrame(s.manifest.frames.head))
    val pool = new Workers(4)
    try for (spec <- Seq("mean:square:2", "sum:circle:3", "min:square:1", "max:circle:2", "mean:square:16")) {
      val w = FocalWorkload.parse(s"focal:$spec")
      val ref = FocalEngine.computeLibrary(p, w, bounds)
      for (r <- Seq(FocalEngine.compute(p, w, bounds), FocalEngine.compute(p, w, bounds, pool)); b <- ref.bands.indices) {
        assert(r.bands(b).keySet == ref.bands(b).keySet, spec)
        for ((k, t) <- ref.bands(b)) {
          val (x, y) = (r.bands(b)(k).toArrayDouble(), t.toArrayDouble())
          assert(x.length == y.length && x.indices.forall(i => same(x(i), y(i))), s"$spec band $b tile $k")
        }
      }
    } finally pool.close()
  }

  test("threads: same checksum as one thread, buffer time reported") {
    val (_, s1) = run(MicroBatchRunner.Options(), threads = 1)
    val (_, s4) = run(MicroBatchRunner.Options(), threads = 4)
    assert(num(s1, "checksum") == num(s4, "checksum"))
    assert(num(s4, "threads") == 4 && num(s4, "buffer_ms") >= 0)
  }

  test("arrival rate paces the frames") {
    val (_, s) = run(MicroBatchRunner.Options(rate = 10))
    assert(num(s, "elapsed_ms") >= 100.0)
    assert(num(s, "cpu_ms") > 0)
    assert(num(s, "span_end_us") - num(s, "span_start_us") >= 100000)
  }
}
