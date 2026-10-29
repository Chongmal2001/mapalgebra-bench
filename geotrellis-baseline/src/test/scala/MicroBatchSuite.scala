package de.tuberlin.dima.geotrellisbaseline

import java.nio.charset.StandardCharsets.UTF_8
import java.nio.file.{Files, Path}

import io.circe.Json
import org.scalatest.funsuite.AnyFunSuite

import de.tuberlin.dima.geotrellisbaseline.focal.FocalWorkload
import de.tuberlin.dima.geotrellisbaseline.runner.{FullGridRunner, Main, MicroBatchRunner}
import de.tuberlin.dima.geotrellisbaseline.tilestore.TileStore

/** Micro-batch runner on the fixture `normal` (61x47, 2 bands, 2 frames 1000 ms apart, 16-px tiles, NoData). */
class MicroBatchSuite extends AnyFunSuite {
  private val wl = FocalWorkload.parse("focal:mean:square:2")

  private def run(o: MicroBatchRunner.Options): (Path, Json) = {
    val out = Files.createTempDirectory("gtb").resolve("out")
    val summary = MicroBatchRunner.run(Main.Args("microbatch", Fixtures.path("normal"), wl, out, verifyInput = false, microbatch = o), _ => ())
    (out, summary)
  }

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

  test("arrival rate paces the frames") {
    val (_, s) = run(MicroBatchRunner.Options(rate = 10))
    assert(num(s, "wall_ms") >= 100.0)
    assert(num(s, "peak_heap_bytes") > 0)
  }
}
