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

  /** All records of a frame; checks them against the manifest. */
  def readFrame(f: FrameInfo): Vector[Record] = {
    val (id, records) = FrameFile.read(framePath(f))
    val m = manifest
    if (id != f.frame_id || records.size != f.n_records)
      throw new FormatException(s"frame ${f.frame_id}: file says id $id with ${records.size} records, manifest says ${f.frame_id}/${f.n_records}")
    records.foreach { r =>
      val h = r.header
      if (h.bands != m.raster.bands || h.cols != tileSize || h.rows != tileSize || h.frameId != f.frame_id)
        throw new FormatException(s"frame ${f.frame_id}: tile (${h.tileX},${h.tileY}) has header $h")
    }
    records
  }

  /** True if (row, col) of the tile described by `h` lies inside the true raster extent (not padding). */
  def insideExtent(h: RecordHeader, row: Int, col: Int): Boolean =
    h.tileY * tileSize + row < manifest.raster.height && h.tileX * tileSize + col < manifest.raster.width

  /** Sets every padding cell (outside the raster extent) to NaN, in place, for all bands. */
  def maskPadding(r: Record): Record = {
    val h = r.header
    val validRows = math.min(tileSize, manifest.raster.height - h.tileY * tileSize)
    val validCols = math.min(tileSize, manifest.raster.width - h.tileX * tileSize)
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
    val w = FrameFile.write(root.resolve(key), info.frame_id, n, records.map(input.maskPadding), input.insideExtent)
    frames += info.copy(key = key, n_records = w.nRecords, nbytes = w.nbytes, sha256 = w.sha256, valid_cells = w.validCells)
  }

  def finish(provenance: io.circe.Json): Manifest = {
    val m = input.manifest.copy(frames = frames.result(), provenance = provenance)
    Manifest.save(root, m)
    m
  }
}
