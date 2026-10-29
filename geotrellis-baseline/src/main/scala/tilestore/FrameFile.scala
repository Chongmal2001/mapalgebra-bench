package de.tuberlin.dima.geotrellisbaseline.tilestore

import java.io.{EOFException, IOException}
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.{Files, Path, StandardCopyOption, StandardOpenOption}
import java.security.MessageDigest

import Format._

/** Streaming reader and writer for one frame file.
  *
  * Maplets are read one at a time through a FileChannel instead of a single mmap, so frames
  * larger than 2 GiB (e.g. GOES band 2 at float64) work. No GeoTrellis dependency.
  */
object FrameFile {

  private def readFully(ch: FileChannel, buf: ByteBuffer): Unit = {
    while (buf.hasRemaining) {
      if (ch.read(buf) < 0) throw new EOFException(s"frame file truncated (${buf.remaining()} bytes missing)")
    }
    buf.flip()
  }

  /** Only the 112-byte frame header (cheap: used to copy CRS / transform into output frames). */
  def readHeader(path: Path): FrameHeader = {
    val ch = FileChannel.open(path, StandardOpenOption.READ)
    try {
      val head = littleEndian(FrameHeaderSize)
      readFully(ch, head)
      readFrameHeader(head)
    } finally ch.close()
  }

  /** Frame header and all maplets of a frame, in file order, each checked against the header. */
  def read(path: Path): (FrameHeader, Vector[Record]) = {
    val ch = FileChannel.open(path, StandardOpenOption.READ)
    try {
      val head = littleEndian(FrameHeaderSize)
      readFully(ch, head)
      val fh = readFrameHeader(head)
      if (ch.size() != fh.fileBytes)
        throw new FormatException(s"$path is ${ch.size()} bytes, its header implies ${fh.fileBytes} " +
                                  (if (ch.size() > fh.fileBytes) "(trailing bytes)" else "(truncated)"))
      val out = Vector.newBuilder[Record]
      out.sizeHint(fh.nTiles)
      val buf = littleEndian(fh.mapletBytes)
      var i = 0
      while (i < fh.nTiles) {
        buf.clear()
        readFully(ch, buf)
        val h = readMapletHeader(buf)
        checkMaplet(fh, h)
        out += Record(h, readCells(buf, h))
        i += 1
      }
      (fh, out.result())
    } finally ch.close()
  }

  /** What a written frame contributes to the manifest. */
  final case class Written(nRecords: Int, nbytes: Long, sha256: String, validCells: Long)

  /** Writes a frame atomically (temp file + rename) and returns its size, checksum and valid-cell count.
    * Only cells inside the true raster extent count as valid (padding must already be NaN, see
    * `TileStore.maskPadding`). `header.nTiles` must equal the number of records. */
  def write(path: Path, header: FrameHeader, records: Seq[Record]): Written =
    write(path, header.copy(nTiles = records.size), records.iterator)

  /** Streaming variant: records are produced and written one at a time, so a whole output frame never
    * has to exist as records in memory. The iterator must yield exactly `header.nTiles` records. */
  def write(path: Path, header: FrameHeader, records: Iterator[Record]): Written = {
    Files.createDirectories(path.getParent)
    val tmp = Files.createTempFile(path.getParent, s".${path.getFileName}.", ".tmp")
    val digest = MessageDigest.getInstance("SHA-256")
    var nbytes = 0L
    var valid = 0L
    val ch = FileChannel.open(tmp, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING)
    def emit(buf: ByteBuffer): Unit = {
      buf.flip()
      digest.update(buf.duplicate())
      nbytes += buf.remaining()
      while (buf.hasRemaining) ch.write(buf)
    }
    try {
      val head = littleEndian(FrameHeaderSize)
      writeFrameHeader(head, header)
      emit(head)
      val buf = littleEndian(header.mapletBytes)
      var count = 0
      records.foreach { r =>
        count += 1
        val h = r.header
        checkMaplet(header, h)
        buf.clear()
        writeMaplet(buf, h, r.cells)
        emit(buf)
        val vr = h.validRows
        val vc = h.validCols
        var b = 0
        while (b < h.bands) {
          var row = 0
          while (row < vr) {
            var col = 0
            val base = b * h.planeCells + row * h.cols
            while (col < vc) {
              if (!r.cells(base + col).isNaN) valid += 1
              col += 1
            }
            row += 1
          }
          b += 1
        }
      }
      if (count != header.nTiles)
        throw new IllegalStateException(s"frame ${header.frameId}: header announces ${header.nTiles} maplets, $count were written")
      ch.force(true)
    } catch {
      case e: Throwable =>
        ch.close(); Files.deleteIfExists(tmp); throw e
    }
    ch.close()
    try Files.move(tmp, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    catch { case _: IOException => Files.move(tmp, path, StandardCopyOption.REPLACE_EXISTING) }
    Written(header.nTiles, nbytes, digest.digest().map("%02x".format(_)).mkString, valid)
  }
}

object Checksums {
  /** SHA-256 of a file, streamed (frames can exceed 2 GiB). */
  def sha256(path: Path): String = {
    val digest = MessageDigest.getInstance("SHA-256")
    val ch = FileChannel.open(path, StandardOpenOption.READ)
    try {
      val buf = ByteBuffer.allocate(1 << 20)
      while (ch.read(buf) >= 0) { buf.flip(); digest.update(buf); buf.clear() }
    } finally ch.close()
    digest.digest().map("%02x".format(_)).mkString
  }
}
