package de.tuberlin.dima.geotrellisbaseline.tilestore

import java.nio.{ByteBuffer, ByteOrder}
import java.nio.charset.StandardCharsets.US_ASCII

/** Byte layout of the tile store, record version 3.
  *
  * The written spec (pipeline.md §2) is authoritative; this mirrors mabench/src/mabench/format.py
  * and NES `MapletCodec` (`WireFormat::Binary`). No GeoTrellis dependency, so it can be tested alone.
  *
  * {{{
  * frame file = frame header (16 B) + n_records records
  * frame header: char[8] "MPLFRM03", u32 frame_id, u32 n_records
  * record      = record header (32 B) + MapletCodec blob
  * record header: u64 event_time_ms, u32 frame_id, u16 bands, u16 flags,
  *                i32 tile_x, i32 tile_y, u32 cols, u32 rows
  * blob:        u8 format=2, u8 cellType=1, u8 bands, u8 0, u32 height, u32 width, u32 0,
  *              f64 noData=NaN, then bands*height*width f64 cells, band-major, row-major in a band
  * }}}
  * Everything is little-endian.
  */
object Format {
  val RecordVersion: Int = 3
  val FrameMagic: Array[Byte] = "MPLFRM03".getBytes(US_ASCII)
  val FrameHeaderSize: Int = 16
  val RecordHeaderSize: Int = 32
  val BlobPreambleSize: Int = 24
  val WireFormatBinary: Int = 2
  val CellTypeFloat64: Int = 1
  val MaxBands: Int = 255

  final case class RecordHeader(eventTimeMs: Long, frameId: Int, bands: Int, flags: Int,
                                tileX: Int, tileY: Int, cols: Int, rows: Int) {
    def planeCells: Int = rows * cols
    def cellCount: Int = {
      val n = bands.toLong * rows * cols
      require(n <= Int.MaxValue / 8, s"tile too large: $n cells")
      n.toInt
    }
    def payloadBytes: Int = BlobPreambleSize + cellCount * 8
  }

  /** One tile: header plus all cells, band-major (band b occupies [b*rows*cols, (b+1)*rows*cols)). */
  final case class Record(header: RecordHeader, cells: Array[Double]) {
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

  /** Returns (frameId, nRecords). */
  def readFrameHeader(buf: ByteBuffer): (Int, Int) = {
    val magic = new Array[Byte](8)
    buf.get(magic)
    if (!java.util.Arrays.equals(magic, FrameMagic))
      fail(s"not a record-version-$RecordVersion frame file (magic '${new String(magic, US_ASCII)}')")
    (u32(buf.getInt(), "frame_id"), u32(buf.getInt(), "n_records"))
  }

  def writeFrameHeader(buf: ByteBuffer, frameId: Int, nRecords: Int): Unit = {
    buf.put(FrameMagic).putInt(frameId).putInt(nRecords)
  }

  // ---------------------------------------------------------------- record header

  def readRecordHeader(buf: ByteBuffer): RecordHeader = {
    val t = buf.getLong()
    val frame = u32(buf.getInt(), "frame_id")
    val bands = buf.getShort() & 0xFFFF
    val flags = buf.getShort() & 0xFFFF
    val tx = buf.getInt()
    val ty = buf.getInt()
    val cols = u32(buf.getInt(), "cols")
    val rows = u32(buf.getInt(), "rows")
    if (bands < 1 || bands > MaxBands) fail(s"record has $bands bands; expected 1..$MaxBands")
    RecordHeader(t, frame, bands, flags, tx, ty, cols, rows)
  }

  def writeRecordHeader(buf: ByteBuffer, h: RecordHeader): Unit = {
    buf.putLong(h.eventTimeMs).putInt(h.frameId).putShort(h.bands.toShort).putShort(h.flags.toShort)
      .putInt(h.tileX).putInt(h.tileY).putInt(h.cols).putInt(h.rows)
  }

  // ---------------------------------------------------------------- MapletCodec blob

  /** Reads a blob for `h`, validating its preamble the way `Raster::decode` does. */
  def readBlob(buf: ByteBuffer, h: RecordHeader): Array[Double] = {
    val format = buf.get() & 0xFF
    val cellType = buf.get() & 0xFF
    val bands = buf.get() & 0xFF
    buf.get() // reserved
    val height = u32(buf.getInt(), "height")
    val width = u32(buf.getInt(), "width")
    buf.getInt() // reserved
    val noData = buf.getDouble()
    if (format != WireFormatBinary) fail(s"blob wire format $format != $WireFormatBinary (binary)")
    if (cellType != CellTypeFloat64) fail(s"blob cell type $cellType != $CellTypeFloat64 (float64)")
    if (bands != h.bands || height != h.rows || width != h.cols)
      fail(s"blob is ${bands}x${height}x$width (bands x rows x cols), record header says ${h.bands}x${h.rows}x${h.cols}")
    if (!noData.isNaN) fail(s"blob noData is $noData; tile stores use NaN")
    val cells = new Array[Double](h.cellCount)
    buf.asDoubleBuffer().get(cells) // asDoubleBuffer keeps buf's little-endian order
    buf.position(buf.position() + cells.length * 8)
    cells
  }

  def writeBlob(buf: ByteBuffer, h: RecordHeader, cells: Array[Double]): Unit = {
    require(cells.length == h.cellCount, s"${cells.length} cells for a ${h.bands}x${h.rows}x${h.cols} tile")
    buf.put(WireFormatBinary.toByte).put(CellTypeFloat64.toByte).put(h.bands.toByte).put(0.toByte)
      .putInt(h.rows).putInt(h.cols).putInt(0).putDouble(Double.NaN)
    buf.asDoubleBuffer().put(cells)
    buf.position(buf.position() + cells.length * 8)
  }
}
