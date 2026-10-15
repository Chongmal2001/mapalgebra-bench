package de.tuberlin.dima.geotrellisbaseline

import java.nio.file.Files

import org.scalatest.funsuite.AnyFunSuite

import de.tuberlin.dima.geotrellisbaseline.focal.FocalWorkload
import de.tuberlin.dima.geotrellisbaseline.runner.BatchRunner
import de.tuberlin.dima.geotrellisbaseline.tilestore.TileStore

/** GeoTrellis vs the mabench oracle on the fixture store `normal`
  * (61x47, 2 bands, 2 frames, tile 16, 20 % random NoData plus a NoData block).
  * References were produced with `mabench reference` (see fixtures/README.md).
  */
class BaselineSuite extends AnyFunSuite {

  /** (mismatching cells, compared cells) between a GeoTrellis output store and a reference store. */
  private def compare(w: FocalWorkload, refName: String): (Int, Int, String) = {
    val out = Files.createTempDirectory("gtb").resolve("out")
    BatchRunner.run(BatchRunner.Args(Fixtures.path("normal"), w, out, verifyInput = true), _ => ())
    val gt = TileStore.open(out)
    val ref = TileStore.open(Fixtures.path(s"ref/$refName"))
    gt.verifyChecksums()
    var mismatches, compared = 0
    var first = ""
    for ((gf, rf) <- gt.manifest.frames.zip(ref.manifest.frames)) {
      val g = gt.readFrame(gf).map(r => (r.header.tileX, r.header.tileY) -> r).toMap
      for (r <- ref.readFrame(rf); i <- r.cells.indices) {
        val a = g((r.header.tileX, r.header.tileY)).cells(i)
        val b = r.cells(i)
        val ok =
          if (b.isNaN || a.isNaN) a.isNaN && b.isNaN
          else if (w.op == "min" || w.op == "max") a == b                    // exact
          else math.abs(a - b) <= 1e-9 * math.max(1.0, math.abs(b))          // sliding sums drift in the last ULPs
        compared += 1
        if (!ok) {
          mismatches += 1
          if (first.isEmpty) first = s"frame ${gf.frame_id} tile (${r.header.tileX},${r.header.tileY}) cell $i: geotrellis $a, oracle $b"
        }
      }
    }
    (mismatches, compared, first)
  }

  for (spec <- Seq("mean:square:2", "sum:square:2", "min:square:2", "max:square:2", "mean:circle:3", "max:circle:3")) {
    test(s"GeoTrellis matches the oracle: focal:$spec") {
      val (bad, n, first) = compare(FocalWorkload.parse(s"focal:$spec"), spec.replace(':', '-'))
      assert(bad == 0, s"$bad of $n cells differ; first: $first")
    }
  }

  // Hypothesis from reading the GeoTrellis 3.8.1 source (baseline-design.md §2.1): BufferTiles only gathers
  // the 8 adjacent tiles and clamps each slice to one tile, so a radius beyond the tile size silently loses
  // cells. If this test fails, GeoTrellis handles r > tile size correctly and the finding must be dropped.
  test("hypothesis: GeoTrellis deviates from the oracle when radius > tile size (r = 20, tile = 16)") {
    val (bad, n, first) = compare(FocalWorkload.parse("focal:mean:square:20"), "mean-square-20")
    info(s"$bad of $n cells differ from the oracle; first: $first")
    assert(bad > 0)
  }

  test("output store keeps raster, tiling and frame metadata, and pads with NoData") {
    val out = Files.createTempDirectory("gtb").resolve("out")
    BatchRunner.run(BatchRunner.Args(Fixtures.path("normal"), FocalWorkload.parse("focal:max:square:1"), out, verifyInput = false), _ => ())
    val in = TileStore.open(Fixtures.path("normal")).manifest
    val m = TileStore.open(out).manifest
    assert(m.raster == in.raster && m.tiling == in.tiling)
    assert(m.frames.map(f => (f.frame_id, f.event_time_ms, f.n_records)) == in.frames.map(f => (f.frame_id, f.event_time_ms, f.n_records)))
    assert(m.provenance.hcursor.get[String]("role").contains("geotrellis"))
    assert(Files.exists(out.resolve("timings.csv")))
  }
}
