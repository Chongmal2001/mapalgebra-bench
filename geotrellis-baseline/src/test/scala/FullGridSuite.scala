package de.tuberlin.dima.geotrellisbaseline

import java.nio.file.Files

import org.scalatest.funsuite.AnyFunSuite

import de.tuberlin.dima.geotrellisbaseline.focal.FocalWorkload
import de.tuberlin.dima.geotrellisbaseline.runner.{FullGridRunner, Main}
import de.tuberlin.dima.geotrellisbaseline.tilestore.TileStore

/** Full-grid GeoTrellis against the focal definition evaluated cell by cell, on the fixture `ramp`
  * (50x30, 2 bands, 16-px tiles, NoData block x < 5, y < 5). */
class FullGridSuite extends AnyFunSuite {
  private val (w, h) = (50, 30)
  private val input: Map[(Int, Int, Int), Double] = {
    val s = TileStore.open(Fixtures.path("ramp"))
    (for (r <- s.readFrame(s.manifest.frames.head); b <- 0 until 2; row <- 0 until 16; col <- 0 until 16)
      yield (b, r.header.tileX * 16 + col, r.header.tileY * 16 + row) -> r.cells(b * 256 + row * 16 + col)).toMap
  }

  private def expected(wl: FocalWorkload, b: Int, x: Int, y: Int): Double = {
    val r = wl.radius
    def v(x: Int, y: Int): Double = input((b, x, y))
    val vals = for {
      dy <- -r to r; dx <- -r to r
      if wl.shape == "square" || dx * dx + dy * dy <= r * r
      xx = x + dx; yy = y + dy
      if xx >= 0 && xx < w && yy >= 0 && yy < h && !v(xx, yy).isNaN
    } yield v(xx, yy)
    if (vals.isEmpty) Double.NaN
    else wl.op match {
      case "sum" => vals.sum
      case "mean" => vals.sum / vals.size
      case "min" => vals.min
      case "max" => vals.max
    }
  }

  for (spec <- Seq("mean:square:2", "sum:circle:3", "min:circle:2", "max:square:1", "mean:square:20"); threads <- Seq(1, 3)) {
    test(s"full grid equals the definition: focal:$spec, $threads thread(s)") {
      val wl = FocalWorkload.parse(s"focal:$spec")
      val out = Files.createTempDirectory("gtb").resolve("out")
      FullGridRunner.run(Main.Args("fullgrid", Fixtures.path("ramp"), wl, out, verifyInput = true, threads = threads,
                                   stripeRows = 7), _ => ())
      val store = TileStore.open(out)
      val in = TileStore.open(Fixtures.path("ramp")).manifest
      assert(store.manifest.raster == in.raster && store.manifest.tiling == in.tiling)
      store.verifyChecksums()
      var bad = 0
      var first = ""
      for (rec <- store.readFrame(store.manifest.frames.head); b <- 0 until 2; row <- 0 until 16; col <- 0 until 16) {
        val x = rec.header.tileX * 16 + col
        val y = rec.header.tileY * 16 + row
        val got = rec.cells(b * 256 + row * 16 + col)
        val ok =
          if (x >= w || y >= h) got.isNaN
          else {
            val e = expected(wl, b, x, y)
            if (e.isNaN || got.isNaN) e.isNaN && got.isNaN
            else if (wl.op == "min" || wl.op == "max") got == e
            else math.abs(got - e) <= 1e-12 * math.max(1.0, math.abs(e)) * (2 * wl.radius + 1) * (2 * wl.radius + 1)
          }
        if (!ok) { bad += 1; if (first.isEmpty) first = s"band $b ($x,$y): got $got, expected ${if (x < w && y < h) expected(wl, b, x, y) else Double.NaN}" }
      }
      assert(bad == 0, s"$bad cells differ; first: $first")
    }
  }
}

class FullGridGapSuite extends AnyFunSuite {
  test("a tile missing from the input is still an output tile, with values next to present tiles") {
    import de.tuberlin.dima.geotrellisbaseline.focal.{FullGrid, Workers}
    val s = TileStore.open(Fixtures.path("ramp"))
    val recs = s.readFrame(s.manifest.frames.head).filterNot(r => r.header.tileX == 1 && r.header.tileY == 0)
    val f = FullGrid.assemble(recs, 50, 30)
    assert(f.headers.size == 4 * 2 && f.headers.exists(h => h.tileX == 1 && h.tileY == 0))
    val wl = FocalWorkload.parse("focal:max:square:1")
    val out = FullGrid.split(f, FullGrid.compute(f, wl, Workers.Serial)).map(r => (r.header.tileX, r.header.tileY) -> r.cells).toMap
    val gap = out((1, 0))
    assert(!gap(5 * 16 + 0).isNaN, "left column of the gap sees tile (0, 0)")       // row 5, col 0
    assert(gap(5 * 16 + 5).isNaN, "inside the gap, out of reach of present tiles")
  }
}
