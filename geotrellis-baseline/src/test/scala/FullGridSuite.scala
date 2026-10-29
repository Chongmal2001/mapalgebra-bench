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

  for (spec <- Seq("mean:square:2", "sum:circle:3", "min:circle:2", "max:square:1", "mean:square:20")) {
    test(s"full grid equals the definition: focal:$spec") {
      val wl = FocalWorkload.parse(s"focal:$spec")
      val out = Files.createTempDirectory("gtb").resolve("out")
      FullGridRunner.run(Main.Args("fullgrid", Fixtures.path("ramp"), wl, out, verifyInput = true), _ => ())
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
