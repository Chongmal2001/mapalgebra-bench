package de.tuberlin.dima.geotrellisbaseline.focal

import geotrellis.layer.{SpatialKey, TileBounds}
import geotrellis.layer.mapalgebra.focal.CollectionFocalOperation
import geotrellis.raster.{DoubleArrayTile, GridBounds, Tile}
import geotrellis.util._ // identityComponent: SpatialKey is its own SpatialComponent

import de.tuberlin.dima.geotrellisbaseline.tilestore.Format.{MapletHeader, Record}

/** Runs one focal workload over one window (all tiles of one frame) with GeoTrellis.
  *
  * Split into three steps so callers can time `compute` alone:
  *  - `prepare` (untimed): records -> one GeoTrellis layer (Seq[(SpatialKey, Tile)]) per band.
  *    Tiles are DoubleArrayTile with DoubleConstantNoDataCellType, i.e. NaN = NoData.
  *  - `compute` (timed): per band, `CollectionFocalOperation`, which buffers every tile with slices of its
  *    8 neighbours (`bufferTiles`) and runs the kernel on the buffered tile restricted to the original
  *    tile. This is GeoTrellis' standard layer-level focal path, so neighbour assembly is inside the
  *    timed region, as in NES.
  *  - `toRecords` (untimed): back to band-major records with the input maplet headers, produced lazily so the
  *    writer streams them and a full frame of output records never sits on the heap.
  *
  * Memory: `prepare` keeps only the record headers, not the input records. Single-band tiles wrap the
  * record's cell array without copying. Peak heap is then about input layer + GeoTrellis' buffered
  * tiles + result layer, i.e. roughly 3-4x the frame size (`frameBytes`).
  *
  * Multiband: GeoTrellis focal operations are single-band; bands are processed one after another,
  * which is also what its MultibandTile focal methods do.
  */
object FocalEngine {

  final case class Prepared(keys: Vector[SpatialKey], headers: Map[SpatialKey, MapletHeader], bands: Vector[Seq[(SpatialKey, Tile)]])

  final case class Result(bands: Vector[Map[SpatialKey, Tile]])

  def layerBounds(tilesX: Int, tilesY: Int): TileBounds = GridBounds(0, 0, tilesX - 1, tilesY - 1)

  def prepare(records: Vector[Record]): Prepared = {
    require(records.nonEmpty, "empty window")
    val nBands = records.head.header.bands
    val keys = records.map(r => SpatialKey(r.header.tileX, r.header.tileY))
    val bands = Vector.tabulate(nBands) { b =>
      records.map { r =>
        val h = r.header
        val cells = if (nBands == 1) r.cells else r.band(b) // single band: wrap, no copy
        SpatialKey(h.tileX, h.tileY) -> (DoubleArrayTile(cells, h.cols, h.rows): Tile)
      }
    }
    Prepared(keys, keys.zip(records.map(_.header)).toMap, bands)
  }

  /** The timed part. Forces every result tile: in Scala 2.12, `CollectionFocalOperation` returns a lazily
    * evaluated Stream (BufferTiles goes through Map.groupBy(...).mapValues(...).toSeq), so without
    * `toVector` the kernels would run after the timer stops. */
  def compute(p: Prepared, w: FocalWorkload, bounds: TileBounds): Result =
    Result(p.bands.map { layer =>
      CollectionFocalOperation(layer, w.neighborhood, bounds)(w.kernel).toVector.toMap
    })

  def toRecords(p: Prepared, r: Result): Iterator[Record] =
    p.keys.iterator.map { k =>
      val h = p.headers(k)
      val cells = new Array[Double](h.cellCount)
      var b = 0
      while (b < h.bands) {
        val tile = r.bands(b).getOrElse(k, throw new IllegalStateException(s"no output for $k band $b"))
        require(tile.cols == h.cols && tile.rows == h.rows, s"output tile $k is ${tile.cols}x${tile.rows}")
        System.arraycopy(tile.toArrayDouble(), 0, cells, b * h.planeCells, h.planeCells)
        b += 1
      }
      Record(h, cells)
    }
}
