package de.tuberlin.dima.geotrellisbaseline

import java.nio.{ByteBuffer, ByteOrder}
import java.nio.charset.StandardCharsets.{US_ASCII, UTF_8}
import java.nio.file.{Files, Path, Paths}

import org.scalatest.funsuite.AnyFunSuite

import de.tuberlin.dima.geotrellisbaseline.tilestore.{Checksums, FrameFile, Manifest, TileStore}
import de.tuberlin.dima.geotrellisbaseline.tilestore.Format._

object Fixtures {
  /** Tile stores written by mabench (see src/test/resources/fixtures/README.md for the commands). */
  def path(name: String): Path = Paths.get(getClass.getResource(s"/fixtures/$name").toURI)

  def le(bytes: Array[Byte]): ByteBuffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
}

class FormatSuite extends AnyFunSuite {
  import Fixtures.le

  // 2 bands band-major, 2x3 tile at (1,0) of a 6x2 raster, frame_id 7.
  private val h2 = MapletHeader(bands = 2, tileX = 1, tileY = 0, rows = 2, cols = 3, rasterWidth = 6, rasterHeight = 2, frameId = 7)
  private val cells2 = Array(1.0, 2, 3, 4, 5, 6, -1, Double.NaN, 0, 10, 20, 30)

  private def encode(h: MapletHeader, cells: Array[Double]): Array[Byte] = {
    val buf = littleEndian(h.nbytes)
    writeMaplet(buf, h, cells)
    buf.array()
  }

  test("maplet layout: 32-byte preamble at the offsets of maplet-format.md 5.2, then band-major cells") {
    val bytes = encode(h2, cells2)
    val b = le(bytes)
    assert(bytes.length == 32 + 12 * 8)
    assert((b.get(0), b.get(1), b.get(2), b.get(3)) == ((3: Byte, 1: Byte, 2: Byte, 0: Byte)))
    assert((b.getInt(4), b.getInt(8), b.getInt(12), b.getInt(16), b.getInt(20), b.getInt(24), b.getInt(28)) == ((7, 1, 0, 2, 3, 6, 2)))
    assert(b.getDouble(32) == 1.0 && b.getDouble(32 + 6 * 8) == -1.0 && b.getDouble(32 + 7 * 8).isNaN)
  }

  test("maplet round trip, FILLED flag, edge extent, format check") {
    val buf = le(encode(h2, cells2))
    assert(readMapletHeader(buf) == h2)
    val cells = readCells(buf, h2)
    assert(cells.indices.forall(i => cells(i) == cells2(i) || (cells(i).isNaN && cells2(i).isNaN)))
    val filled = h2.copy(bands = 1, tileX = 0, tileY = 1, frameId = 3, flags = FlagFilled)
    assert(readMapletHeader(le(encode(filled, new Array[Double](6)))) == filled)
    val edge = MapletHeader(bands = 1, tileX = 1, tileY = 1, rows = 2, cols = 3, rasterWidth = 4, rasterHeight = 3)
    assert((edge.validRows, edge.validCols) == ((1, 1)))
    val bad = encode(h2, cells2); bad(0) = 2
    intercept[FormatException](readMapletHeader(le(bad)))
  }

  test("frame header layout and round trip") {
    val fh = FrameHeader(5, 2, 1700000000000L, 6, 2, 3, 2, 2, 32633, Vector(10.0, 0.0, 370670.0, 0.0, -10.0, 5835040.0))
    assert((fh.tilesX, fh.tilesY, fh.mapletBytes) == ((2, 1, 32 + 2 * 6 * 8)))
    val buf = littleEndian(FrameHeaderSize)
    writeFrameHeader(buf, fh)
    val b = le(buf.array())
    assert(new String(buf.array(), 0, 8, US_ASCII) == "MPLFRM04")
    assert((b.getInt(8), b.getInt(12), b.getLong(16), b.getInt(24), b.getInt(28), b.getInt(32), b.getInt(36)) ==
           ((5, 2, 1700000000000L, 6, 2, 3, 2)))
    assert((b.get(40), b.get(41)) == ((2: Byte, 1: Byte)) && b.getDouble(48).isNaN && b.getInt(56) == 32633)
    assert(b.getDouble(64) == 10.0 && b.getDouble(104) == 5835040.0)
    assert(readFrameHeader(b) == fh)
    intercept[FormatException](checkMaplet(fh, h2.copy(rasterWidth = 5)))
    intercept[FormatException](checkMaplet(fh, h2.copy(tileX = 2)))
  }

  test("reads a mabench store: manifest, frame header, maplets, values, padding") {
    val store = TileStore.open(Fixtures.path("ramp"))   // ramp 0.01*x + 0.02*y, 50x30, 2 bands, NoData block x<5, y<5
    val m = store.manifest
    assert((m.raster.width, m.raster.height, m.raster.bands, m.tiling.tile_size) == ((50, 30, 2, 16)))
    store.verifyChecksums()
    val records = store.readFrame(m.frames.head)
    assert(records.map(r => (r.header.tileX, r.header.tileY)) == (for (y <- 0 until 2; x <- 0 until 4) yield (x, y)))
    assert(records.forall(r => r.header.frameId == 0 && r.header.rasterWidth == 50 && r.header.rasterHeight == 30))
    for (r <- records; b <- 0 until 2; row <- 0 until 16; col <- 0 until 16) {
      val x = r.header.tileX * 16 + col
      val y = r.header.tileY * 16 + row
      val v = r.cells(b * 256 + row * 16 + col)
      if (x >= 50 || y >= 30 || (x < 5 && y < 5)) assert(v.isNaN, s"($b,$y,$x) should be NoData")
      else assert(math.abs(v - (0.01 * x + 0.02 * y)) < 1e-12, s"($b,$y,$x) = $v")
    }
  }

  test("frame write/read round trip, checksum and valid-cell count") {
    val store = TileStore.open(Fixtures.path("normal"))
    val info = store.manifest.frames.head
    val records = store.readFrame(info)
    val tmp = Files.createTempDirectory("gtb")
    val out = tmp.resolve("frames/f000000.mpl")
    val written = FrameFile.write(out, store.readFrameHeader(info), records)
    assert(written.sha256 == info.sha256, "re-written frame must be byte-identical to mabench's")
    assert(written.validCells == info.valid_cells)
    assert(Checksums.sha256(out) == info.sha256)
  }

  test("rejects unsupported stores") {
    val tmp = Files.createTempDirectory("gtb")
    val text = new String(Files.readAllBytes(Fixtures.path("ramp").resolve(Manifest.FileName)), UTF_8)
    Files.write(tmp.resolve(Manifest.FileName), text.replace("\"record_version\": 4", "\"record_version\": 3").getBytes(UTF_8))
    intercept[FormatException](Manifest.load(tmp))
    val old = tmp.resolve("old.mpl")
    Files.write(old, "MPLFRM03".getBytes("US-ASCII") ++ new Array[Byte](FrameHeaderSize - 8))
    intercept[FormatException](FrameFile.read(old))
    val trailing = tmp.resolve("trailing.mpl")
    Files.write(trailing, Files.readAllBytes(TileStore.open(Fixtures.path("ramp")).framePath(TileStore.open(Fixtures.path("ramp")).manifest.frames.head)) ++ Array[Byte](1))
    intercept[FormatException](FrameFile.read(trailing))
  }
}
