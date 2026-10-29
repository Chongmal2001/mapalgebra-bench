package de.tuberlin.dima.geotrellisbaseline.tilestore

import java.nio.{ByteBuffer, ByteOrder}
import java.nio.charset.StandardCharsets.US_ASCII

/** Byte layout of the tile store, record version 4 (self-locating maplets).
  *
  * docs/maplet-format.md section 5 is authoritative; this mirrors mabench/src/mabench/format.py and the NES
  * `MapletCodec` wire format 3. No GeoTrellis dependency, so it can be tested alone.
  *
  * {{{
  * frame file   = frame header (112 B) + n_tiles maplets
  * frame header = char[8] "MPLFRM04", u32 frame_id, u32 n_tiles, u64 event_time_ms,
  *                u32 raster_width, u32 raster_height, u32 tile_width, u32 tile_height,
  *                u8 bands, u8 cellType=1, u16 flags=0, u32 0, f64 noData=NaN, u32 crs_epsg, u32 0,
  *                f64[6] transform (a, b, c, d, e, f)
  * maplet       = u8 format=3, u8 cellType=1, u8 bands, u8 flags, u32 frame_id (0 in files),
  *                i32 tile_x, i32 tile_y, u32 height, u32 width, u32 raster_width, u32 raster_height,
  *                then bands*height*width f64 cells, band-major, row-major within a band
  * }}}
  * Everything is little-endian. Maplet i starts at 112 + i * (32 + 8 * bands * height * width).
  */
object Format {
  val RecordVersion: Int = 4
  val FrameMagic: Array[Byte] = "MPLFRM04".getBytes(US_ASCII)
  val FrameHeaderSize: Int = 112
  val MapletPreambleSize: Int = 32
  val MapletFormat: Int = 3
  val CellTypeFloat64: Int = 1
  val MaxBands: Int = 255
  val FlagFilled: Int = 0x01
  val IdentityTransform: Vector[Double] = Vector(1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

  private val Epsg = """(?i)\s*EPSG:(\d+)\s*""".r

  /** EPSG code of an "EPSG:nnnn" CRS string, 0 otherwise. Output frame headers copy the input's code, so
    * this is only used to check input headers against the manifest. */
  def crsToEpsg(crs: Option[String]): Option[Int] = crs match {
    case None => Some(0)
    case Some(Epsg(code)) => Some(code.toInt)
    case Some(_) => None // WKT / PROJ: mabench resolved it with rasterio; not re-checked here
  }

  final case class FrameHeader(frameId: Int, nTiles: Int, eventTimeMs: Long, rasterWidth: Int, rasterHeight: Int,
                               tileWidth: Int, tileHeight: Int, bands: Int, crsEpsg: Int, transform: Vector[Double],
                               flags: Int = 0) {
    def tilesX: Int = (rasterWidth + tileWidth - 1) / tileWidth
    def tilesY: Int = (rasterHeight + tileHeight - 1) / tileHeight
    def mapletBytes: Int = {
      val n = MapletPreambleSize.toLong + 8L * bands * tileHeight * tileWidth
      require(n <= Int.MaxValue, s"maplet too large: $n bytes")
      n.toInt
    }
    def mapletOffset(i: Int): Long = FrameHeaderSize + i.toLong * mapletBytes
    def fileBytes: Long = mapletOffset(nTiles)
    def mapletHeader(tileX: Int, tileY: Int): MapletHeader =
      MapletHeader(bands, tileX, tileY, tileHeight, tileWidth, rasterWidth, rasterHeight)
  }

  /** Preamble of one maplet. `rows` = height, `cols` = width (names kept from record version 3). */
  final case class MapletHeader(bands: Int, tileX: Int, tileY: Int, rows: Int, cols: Int,
                                rasterWidth: Int, rasterHeight: Int, frameId: Int = 0, flags: Int = 0) {
    def planeCells: Int = rows * cols
    def cellCount: Int = {
      val n = bands.toLong * rows * cols
      require(n <= Int.MaxValue / 8, s"tile too large: $n cells")
      n.toInt
    }
    def nbytes: Int = MapletPreambleSize + cellCount * 8
    def validRows: Int = math.max(0, math.min(rows, rasterHeight - tileY * rows))
    def validCols: Int = math.max(0, math.min(cols, rasterWidth - tileX * cols))
  }

  /** One tile: preamble plus all cells, band-major (band b occupies [b*rows*cols, (b+1)*rows*cols)). */
  final case class Record(header: MapletHeader, cells: Array[Double]) {
    def band(b: Int): Array[Double] = java.util.Arrays.copyOfRange(cells, b * header.planeCells, (b + 1) * header.planeCells)
  }

  final class FormatException(msg: String) extends RuntimeException(msg)

  private def fail(msg: String): Nothing = throw new FormatException(msg)

  private def u32(v: Int, what: String): Int = {
    if (v < 0) fail(s"$what exceeds 2^31-1: ${Integer.toUnsignedLong(v)}")
    v
  }

  def littleEndian(capacity: Int): ByteBuffer = ByteBuffer.allocate(capacity).order(ByteOrder.LITTLE_ENDIAN)

  // ---------------------------------------------------------------- frame header

  def readFrameHeader(buf: ByteBuffer): FrameHeader = {
    val magic = new Array[Byte](8)
    buf.get(magic)
    if (!java.util.Arrays.equals(magic, FrameMagic))
      fail(s"not a record-version-$RecordVersion frame file (magic '${new String(magic, US_ASCII)}'); " +
           "prepare the store again with the current mabench")
    val frameId = u32(buf.getInt(), "frame_id")
    val n = u32(buf.getInt(), "n_tiles")
    val t = buf.getLong()
    val w = u32(buf.getInt(), "raster_width")
    val h = u32(buf.getInt(), "raster_height")
    val tw = u32(buf.getInt(), "tile_width")
    val th = u32(buf.getInt(), "tile_height")
    val bands = buf.get() & 0xFF
    val cellType = buf.get() & 0xFF
    val flags = buf.getShort() & 0xFFFF
    buf.getInt() // reserved
    val noData = buf.getDouble()
    val epsg = u32(buf.getInt(), "crs_epsg")
    buf.getInt() // reserved
    val transform = Vector.fill(6)(buf.getDouble())
    if (cellType != CellTypeFloat64) fail(s"frame cellType $cellType != $CellTypeFloat64 (float64)")
    if (!noData.isNaN) fail(s"frame noData is $noData; tile stores use NaN")
    if (w == 0 || h == 0 || tw == 0 || th == 0 || bands == 0) fail("frame header has a zero dimension")
    val fh = FrameHeader(frameId, n, t, w, h, tw, th, bands, epsg, transform, flags)
    if (n > fh.tilesX * fh.tilesY) fail(s"frame header says $n tiles, grid ${fh.tilesX}x${fh.tilesY} has ${fh.tilesX * fh.tilesY}")
    fh
  }

  def writeFrameHeader(buf: ByteBuffer, h: FrameHeader): Unit = {
    require(h.bands >= 1 && h.bands <= MaxBands, s"band count ${h.bands} outside 1..$MaxBands")
    require(h.transform.size == 6, "transform needs 6 coefficients")
    buf.put(FrameMagic).putInt(h.frameId).putInt(h.nTiles).putLong(h.eventTimeMs)
      .putInt(h.rasterWidth).putInt(h.rasterHeight).putInt(h.tileWidth).putInt(h.tileHeight)
      .put(h.bands.toByte).put(CellTypeFloat64.toByte).putShort(h.flags.toShort).putInt(0)
      .putDouble(Double.NaN).putInt(h.crsEpsg).putInt(0)
    h.transform.foreach(buf.putDouble)
  }

  // ---------------------------------------------------------------- maplet

  def readMapletHeader(buf: ByteBuffer): MapletHeader = {
    val format = buf.get() & 0xFF
    val cellType = buf.get() & 0xFF
    val bands = buf.get() & 0xFF
    val flags = buf.get() & 0xFF
    val frameId = u32(buf.getInt(), "frame_id")
    val tx = buf.getInt()
    val ty = buf.getInt()
    val height = u32(buf.getInt(), "height")
    val width = u32(buf.getInt(), "width")
    val rw = u32(buf.getInt(), "raster_width")
    val rh = u32(buf.getInt(), "raster_height")
    if (format != MapletFormat) fail(s"maplet format $format != $MapletFormat")
    if (cellType != CellTypeFloat64) fail(s"maplet cell type $cellType != $CellTypeFloat64 (float64)")
    if (bands == 0) fail("maplet has zero bands")
    MapletHeader(bands, tx, ty, height, width, rw, rh, frameId, flags)
  }

  def writeMapletHeader(buf: ByteBuffer, h: MapletHeader): Unit = {
    require(h.bands >= 1 && h.bands <= MaxBands, s"band count ${h.bands} outside 1..$MaxBands")
    buf.put(MapletFormat.toByte).put(CellTypeFloat64.toByte).put(h.bands.toByte).put(h.flags.toByte)
      .putInt(h.frameId).putInt(h.tileX).putInt(h.tileY).putInt(h.rows).putInt(h.cols)
      .putInt(h.rasterWidth).putInt(h.rasterHeight)
  }

  /** The source checks of maplet-format.md 5.5 that need no cell decoding (duplicates are checked by the reader). */
  def checkMaplet(f: FrameHeader, h: MapletHeader): Unit = {
    if (h.bands != f.bands || h.rows != f.tileHeight || h.cols != f.tileWidth ||
        h.rasterWidth != f.rasterWidth || h.rasterHeight != f.rasterHeight)
      fail(s"maplet (${h.tileX},${h.tileY}) is ${h.bands}x${h.rows}x${h.cols} of ${h.rasterWidth}x${h.rasterHeight}, " +
           s"frame header says ${f.bands}x${f.tileHeight}x${f.tileWidth} of ${f.rasterWidth}x${f.rasterHeight}")
    if (h.tileX < 0 || h.tileX >= f.tilesX || h.tileY < 0 || h.tileY >= f.tilesY)
      fail(s"maplet position (${h.tileX},${h.tileY}) outside grid ${f.tilesX}x${f.tilesY}")
  }

  /** Cells of a maplet whose preamble has just been read from `buf`. */
  def readCells(buf: ByteBuffer, h: MapletHeader): Array[Double] = {
    val cells = new Array[Double](h.cellCount)
    buf.asDoubleBuffer().get(cells) // asDoubleBuffer keeps buf's little-endian order
    buf.position(buf.position() + cells.length * 8)
    cells
  }

  def writeMaplet(buf: ByteBuffer, h: MapletHeader, cells: Array[Double]): Unit = {
    require(cells.length == h.cellCount, s"${cells.length} cells for a ${h.bands}x${h.rows}x${h.cols} tile")
    writeMapletHeader(buf, h)
    buf.asDoubleBuffer().put(cells)
    buf.position(buf.position() + cells.length * 8)
  }
}
