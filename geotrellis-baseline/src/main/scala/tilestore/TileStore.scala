package de.tuberlin.dima.geotrellisbaseline.tilestore

import java.nio.file.{Files, Path}

import Format._

/** A tile store directory: manifest.json plus frames/fNNNNNN.mpl. Read-only here; outputs go through `TileStoreWriter`. */
final class TileStore private (val root: Path, val manifest: Manifest) {
  def tileSize: Int = manifest.tiling.tile_size

  def framePath(f: FrameInfo): Path = root.resolve(f.key)

  /** Recomputes every frame's SHA-256 and compares it with the manifest. Untimed; call before a run. */
  def verifyChecksums(): Unit = manifest.frames.foreach { f =>
    val actual = Checksums.sha256(framePath(f))
    if (actual != f.sha256) throw new FormatException(s"frame ${f.frame_id}: sha256 $actual != manifest ${f.sha256}")
  }

  /** All maplets of a frame; checks the frame header against the manifest and rejects duplicate positions. */
  def readFrame(f: FrameInfo): Vector[Record] = {
    val (fh, records) = FrameFile.read(framePath(f))
    checkFrameHeader(f, fh)
    val seen = new java.util.HashSet[(Int, Int)]()
    records.foreach { r =>
      val h = r.header
      if (!seen.add((h.tileX, h.tileY))) throw new FormatException(s"frame ${f.frame_id}: duplicate tile (${h.tileX},${h.tileY})")
      if (h.frameId != 0) throw new FormatException(s"frame ${f.frame_id}: tile (${h.tileX},${h.tileY}) has frame_id ${h.frameId}; files carry 0")
    }
    records
  }

  /** Frame header of `f` alone (112 bytes). */
  def readFrameHeader(f: FrameInfo): FrameHeader = FrameFile.readHeader(framePath(f))

  private def checkFrameHeader(f: FrameInfo, fh: FrameHeader): Unit = {
    val r = manifest.raster
    def check(ok: Boolean, what: String): Unit =
      if (!ok) throw new FormatException(s"frame ${f.frame_id}: frame header $what disagrees with the manifest ($fh)")
    check(fh.frameId == f.frame_id, "frame_id")
    check(fh.nTiles == f.n_records, "n_tiles")
    check(fh.eventTimeMs == f.event_time_ms, "event_time_ms")
    check(fh.rasterWidth == r.width && fh.rasterHeight == r.height, "raster size")
    check(fh.tileWidth == tileSize && fh.tileHeight == tileSize, "tile size")
    check(fh.bands == r.bands, "bands")
    check(fh.transform == r.transform.map(_.toVector).getOrElse(IdentityTransform), "transform")
    check(crsToEpsg(r.crs).forall(_ == fh.crsEpsg), "crs_epsg")
  }

  /** Sets every padding cell (outside the raster extent) to NaN, in place, for all bands. */
  def maskPadding(r: Record): Record = {
    val h = r.header
    val validRows = h.validRows
    val validCols = h.validCols
    if (validRows < h.rows || validCols < h.cols) {
      var b = 0
      while (b < h.bands) {
        var row = 0
        while (row < h.rows) {
          var col = if (row < validRows) validCols else 0
          while (col < h.cols) { r.cells(b * h.planeCells + row * h.cols + col) = Double.NaN; col += 1 }
          row += 1
        }
        b += 1
      }
    }
    r
  }
}

object TileStore {
  def open(root: Path): TileStore = new TileStore(root, Manifest.load(root))
}

/** Writes an output store with the same raster and tiling as an input store. */
final class TileStoreWriter(val root: Path, input: TileStore) {
  private val frames = List.newBuilder[FrameInfo]

  if (Files.exists(root.resolve(Manifest.FileName)))
    throw new IllegalStateException(s"$root already holds a tile store")

  def writeFrame(info: FrameInfo, records: Seq[Record]): Unit = writeFrame(info, records.size, records.iterator)

  /** Streaming variant: `records` is consumed once, record by record (see `FrameFile.write`). */
  def writeFrame(info: FrameInfo, n: Int, records: Iterator[Record]): Unit = {
    val key = f"frames/f${info.frame_id}%06d.mpl"
    // Output frames keep the input frame's header (event time, raster, CRS, transform), so a GeoTrellis
    // output frame is byte-identical to mabench's reference when the cells agree.
    val header = input.readFrameHeader(info).copy(nTiles = n)
    val w = FrameFile.write(root.resolve(key), header, records.map(input.maskPadding))
    frames += info.copy(key = key, n_records = w.nRecords, nbytes = w.nbytes, sha256 = w.sha256, valid_cells = w.validCells)
  }

  def finish(provenance: io.circe.Json): Manifest = {
    val m = input.manifest.copy(frames = frames.result(), provenance = provenance)
    Manifest.save(root, m)
    m
  }
}
