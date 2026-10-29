package de.tuberlin.dima.geotrellisbaseline.focal

import geotrellis.raster.DoubleArrayTile

import de.tuberlin.dima.geotrellisbaseline.tilestore.Format.{MapletHeader, Record}

/** Focal workload on a whole frame as one GeoTrellis tile per band: no tiling, no halos (correctness reference).
  *
  * Missing input tiles stay NoData in the assembled frame. Output tiles are cut at the input positions.
  * Memory per frame: input bands + result bands (2x the frame) plus one band of GeoTrellis temporaries.
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

  def compute(f: Frame, w: FocalWorkload): Vector[Array[Double]] =
    f.bands.map(b => w.kernel(DoubleArrayTile(b, f.width, f.height), None).toArrayDouble())

  def split(f: Frame, result: Vector[Array[Double]]): Iterator[Record] =
    f.headers.iterator.map { h =>
      val cells = Array.fill(h.cellCount)(Double.NaN)
      for (b <- result.indices; row <- 0 until h.validRows)
        System.arraycopy(result(b), (h.tileY * h.rows + row) * f.width + h.tileX * h.cols, cells, b * h.planeCells + row * h.cols, h.validCols)
      Record(h, cells)
    }
}
