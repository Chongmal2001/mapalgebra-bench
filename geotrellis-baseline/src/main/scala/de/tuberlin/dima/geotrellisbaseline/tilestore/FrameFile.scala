package de.tuberlin.dima.geotrellisbaseline.tilestore

import java.io.{EOFException, IOException}
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.{Files, Path, StandardCopyOption, StandardOpenOption}
import java.security.MessageDigest

import Format._

/** Streaming reader and writer for one frame file.
  *
  * Records are read one at a time through a FileChannel instead of a single mmap, so frames
  * larger than 2 GiB (e.g. GOES band 2 at float64) work. No GeoTrellis dependency.
  */
object FrameFile {

  private def readFully(ch: FileChannel, buf: ByteBuffer): Unit = {
    while (buf.hasRemaining) {
      if (ch.read(buf) < 0) throw new EOFException(s"frame file truncated (${buf.remaining()} bytes missing)")
    }
    buf.flip()
  }

  /** All records of a frame, in file order. Returns (frameId, records). */
  def read(path: Path): (Int, Vector[Record]) = {
    val ch = FileChannel.open(path, StandardOpenOption.READ)
    try {
      val head = littleEndian(FrameHeaderSize)
      readFully(ch, head)
      val (frameId, n) = readFrameHeader(head)
      val out = Vector.newBuilder[Record]
      out.sizeHint(n)
      val rh = littleEndian(RecordHeaderSize)
      var i = 0
      while (i < n) {
        rh.clear()
        readFully(ch, rh)
        val h = readRecordHeader(rh)
        val payload = littleEndian(h.payloadBytes)
        readFully(ch, payload)
        out += Record(h, readBlob(payload, h))
        i += 1
      }
      if (ch.position() != ch.size()) throw new FormatException(s"$path has ${ch.size() - ch.position()} trailing bytes")
      (frameId, out.result())
    } finally ch.close()
  }

  /** What a written frame contributes to the manifest. */
  final case class Written(nRecords: Int, nbytes: Long, sha256: String, validCells: Long)

  /** Writes a frame atomically (temp file + rename) and returns its size, checksum and valid-cell count.
    *
    * `validCell(record, band, row, col)` decides which cells lie inside the true raster extent; only
    * those count as valid (padding must already be NaN, see `TileStore.maskPadding`).
    */
  def write(path: Path, frameId: Int, records: Seq[Record], validExtent: (RecordHeader, Int, Int) => Boolean): Written =
    write(path, frameId, records.size, records.iterator, validExtent)

  /** Streaming variant: records are produced and written one at a time, so a whole output frame never
    * has to exist as records in memory. `n` must equal the number of records the iterator yields. */
  def write(path: Path, frameId: Int, n: Int, records: Iterator[Record],
            validExtent: (RecordHeader, Int, Int) => Boolean): Written = {
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
      writeFrameHeader(head, frameId, n)
      emit(head)
      var count = 0
      records.foreach { r =>
        count += 1
        val h = r.header
        val buf = littleEndian(RecordHeaderSize + h.payloadBytes)
        writeRecordHeader(buf, h)
        writeBlob(buf, h, r.cells)
        emit(buf)
        var b = 0
        while (b < h.bands) {
          var row = 0
          while (row < h.rows) {
            var col = 0
            while (col < h.cols) {
              if (validExtent(h, row, col) && !r.cells(b * h.planeCells + row * h.cols + col).isNaN) valid += 1
              col += 1
            }
            row += 1
          }
          b += 1
        }
      }
      if (count != n) throw new IllegalStateException(s"frame $frameId: header announces $n records, $count were written")
      ch.force(true)
    } catch {
      case e: Throwable =>
        ch.close(); Files.deleteIfExists(tmp); throw e
    }
    ch.close()
    try Files.move(tmp, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
    catch { case _: IOException => Files.move(tmp, path, StandardCopyOption.REPLACE_EXISTING) }
    Written(n, nbytes, digest.digest().map("%02x".format(_)).mkString, valid)
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
