package de.tuberlin.dima.geotrellisbaseline.focal

import geotrellis.raster.{DoubleArrayTile, GridBounds}

import de.tuberlin.dima.geotrellisbaseline.tilestore.Format.{MapletHeader, Record}

/** Focal workload on a whole frame as one GeoTrellis tile per band: no tiling, no halos (correctness reference).
  *
  * Missing input tiles stay NoData in the assembled frame. Output tiles are cut at the input positions.
  * Memory per frame: input bands + result bands (2x the frame) plus one band of GeoTrellis temporaries.
  *
  * Parallel (`stripeRows` > 0): each band is cut into stripes of `stripeRows` rows; a stripe is computed from its
  * rows plus `radius` rows above and below (the kernel restricted to the stripe, as for a buffered tile), stripes
  * and bands on the worker pool. Same cells, same edge truncation; Min / Max bit-identical to the whole frame, Sum /
  * Mean may differ in the last bits (GeoTrellis' cursor restarts per stripe). Stripes do not depend on the thread
  * count, so neither does the result.
  */
object FullGrid {

  final case class Frame(width: Int, height: Int, headers: Vector[MapletHeader], bands: Vector[Array[Double]])

  def assemble(records: Vector[Record], width: Int, height: Int): Frame = {
    require(records.nonEmpty, "empty frame")
    val nBands = records.head.header.bands
    val bands = Vector.fill(nBands)(Array.fill(width * height)(Double.NaN))
    records.foreach { r =>
      val h = r.header
      for (b <- 0 until nBands; row <- 0 until h.validRows)
        System.arraycopy(r.cells, b * h.planeCells + row * h.cols, bands(b), (h.tileY * h.rows + row) * width + h.tileX * h.cols, h.validCols)
    }
    Frame(width, height, records.map(_.header), bands)
  }

  def compute(f: Frame, w: FocalWorkload, workers: Workers = Workers.Serial, stripeRows: Int = 0): Vector[Array[Double]] =
    if (stripeRows <= 0 || stripeRows >= f.height)
      workers.map(f.bands)(b => w.kernel(DoubleArrayTile(b, f.width, f.height), None).toArrayDouble())
    else {
      val res = Vector.fill(f.bands.size)(new Array[Double](f.width * f.height))
      val items = for (b <- f.bands.indices; y0 <- 0 until f.height by stripeRows) yield (b, y0, math.min(f.height, y0 + stripeRows))
      workers.map(items) { case (b, y0, y1) =>
        val s0 = math.max(0, y0 - w.radius)
        val s1 = math.min(f.height, y1 + w.radius)
        val tile = DoubleArrayTile(java.util.Arrays.copyOfRange(f.bands(b), s0 * f.width, s1 * f.width), f.width, s1 - s0)
        val out = w.kernel(tile, Some(GridBounds(0, y0 - s0, f.width - 1, y1 - s0 - 1))).toArrayDouble()
        require(out.length == (y1 - y0) * f.width, s"stripe $y0..$y1: ${out.length} cells")
        System.arraycopy(out, 0, res(b), y0 * f.width, out.length)
      }
      res
    }

  def split(f: Frame, result: Vector[Array[Double]]): Iterator[Record] =
    f.headers.iterator.map { h =>
      val cells = Array.fill(h.cellCount)(Double.NaN)
      for (b <- result.indices; row <- 0 until h.validRows)
        System.arraycopy(result(b), (h.tileY * h.rows + row) * f.width + h.tileX * h.cols, cells, b * h.planeCells + row * h.cols, h.validCols)
      Record(h, cells)
    }
}
