package de.tuberlin.dima.geotrellisbaseline.focal

import geotrellis.layer.{SpatialKey, TileBounds}
import geotrellis.layer.buffer.BufferTiles
import geotrellis.layer.mapalgebra.focal.CollectionFocalOperation
import geotrellis.raster._
import geotrellis.util._ // identityComponent: SpatialKey is its own SpatialComponent

import de.tuberlin.dima.geotrellisbaseline.tilestore.Format.{MapletHeader, Record}

/** Runs one focal workload over one window (all tiles of one frame) with GeoTrellis.
  *
  * Split into three steps so callers can time `compute` alone:
  *  - `prepare` (untimed): records -> one GeoTrellis layer (Seq[(SpatialKey, Tile)]) per band.
  *    Tiles are DoubleArrayTile with DoubleConstantNoDataCellType, i.e. NaN = NoData.
  *  - `compute` (timed): per band, the two steps of GeoTrellis' layer-level focal path
  *    (`CollectionFocalOperation`): `BufferTiles` buffers every tile with slices of its 8 neighbours, then the
  *    kernel runs on each buffered tile restricted to the original tile. Neighbour assembly is inside the
  *    timed region, as in NES. With `Workers` of n threads, the bands are buffered in parallel and the kernel
  *    calls (one per output tile and band, as NES' probe tasks) run on the pool; no Spark.
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

  final case class Result(bands: Vector[Map[SpatialKey, Tile]], bufferNs: Long = 0)

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

  /** The timed part. `BufferTiles` returns a lazily evaluated Stream in Scala 2.12 (Map.groupBy(...).mapValues(...)
    * .toSeq), forced by `toVector`; the kernel results are forced by `Workers.map`. `bufferNs` = the buffering step
    * (wall time; whatever GeoTrellis leaves lazy in a buffered tile is paid in the kernel). */
  def compute(p: Prepared, w: FocalWorkload, bounds: TileBounds, workers: Workers = Workers.Serial): Result = {
    val t0 = System.nanoTime()
    val buffered = workers.map(p.bands)(layer => BufferTiles(layer, w.neighborhood.extent, bounds).toVector)
    val bufferNs = System.nanoTime() - t0
    val items = for ((layer, b) <- buffered.zipWithIndex; (k, bt) <- layer) yield (b, k, bt)
    val out = workers.map(items) { case (b, k, bt) => (b, k, w.kernel(bt.tile, Some(bt.targetArea))) }
    Result(Vector.tabulate(p.bands.size)(b => out.iterator.collect { case (`b`, k, t) => k -> t }.toMap), bufferNs)
  }

  /** `CollectionFocalOperation` itself (sequential, forced); `compute` must equal it bit for bit (MicroBatchSuite). */
  def computeLibrary(p: Prepared, w: FocalWorkload, bounds: TileBounds): Result =
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
