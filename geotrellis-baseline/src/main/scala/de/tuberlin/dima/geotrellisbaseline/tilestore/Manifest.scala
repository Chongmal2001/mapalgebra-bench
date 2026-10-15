package de.tuberlin.dima.geotrellisbaseline.tilestore

import java.nio.charset.StandardCharsets.UTF_8
import java.nio.file.{Files, Path}

import io.circe.{Decoder, Encoder, Json, Printer}
import io.circe.generic.semiauto.{deriveDecoder, deriveEncoder}
import io.circe.parser.decode
import io.circe.syntax._

/** manifest.json of a tile store. Field names follow the JSON written by mabench (snake_case). */
final case class RasterInfo(width: Int, height: Int, bands: Int, transform: Option[List[Double]], crs: Option[String])

final case class TilingInfo(tile_size: Int, tiles_x: Int, tiles_y: Int, tile_order: String, edge_padding: String)

final case class FrameInfo(frame_id: Int, event_time_ms: Long, key: String, n_records: Int, nbytes: Long,
                           sha256: String, valid_cells: Long, source: Json)

final case class Manifest(raster: RasterInfo, tiling: TilingInfo, frames: List[FrameInfo], provenance: Json,
                          cells: Map[String, String], format: String, format_version: Int, record_version: Int) {

  /** Fail fast on anything this reader was not written for (decision D-P4: never adapt). */
  def validate(): Manifest = {
    def check(ok: Boolean, msg: => String): Unit = if (!ok) throw new Format.FormatException(s"manifest: $msg")
    check(format == Manifest.StoreFormat, s"format '$format' is not '${Manifest.StoreFormat}'")
    check(format_version == Manifest.StoreFormatVersion, s"format_version $format_version != ${Manifest.StoreFormatVersion}")
    check(record_version == Format.RecordVersion, s"record_version $record_version != ${Format.RecordVersion}")
    check(cells.get("byte_order").contains("little"), s"byte_order ${cells.get("byte_order")} is not little")
    check(cells.get("dtype").contains("float64"), s"dtype ${cells.get("dtype")} is not float64")
    check(cells.get("nodata").contains("nan"), s"nodata ${cells.get("nodata")} is not nan")
    check(raster.bands >= 1 && raster.bands <= Format.MaxBands, s"bands ${raster.bands} outside 1..${Format.MaxBands}")
    check(tiling.tiles_x == math.ceil(raster.width.toDouble / tiling.tile_size).toInt &&
          tiling.tiles_y == math.ceil(raster.height.toDouble / tiling.tile_size).toInt,
          "tiles_x/tiles_y do not match width/height/tile_size")
    this
  }
}

object Manifest {
  val FileName = "manifest.json"
  val StoreFormat = "mabench-tilestore"
  val StoreFormatVersion = 1

  implicit val rasterDecoder: Decoder[RasterInfo] = deriveDecoder
  implicit val rasterEncoder: Encoder[RasterInfo] = deriveEncoder
  implicit val tilingDecoder: Decoder[TilingInfo] = deriveDecoder
  implicit val tilingEncoder: Encoder[TilingInfo] = deriveEncoder
  implicit val frameDecoder: Decoder[FrameInfo] = deriveDecoder
  implicit val frameEncoder: Encoder[FrameInfo] = deriveEncoder
  implicit val manifestDecoder: Decoder[Manifest] = deriveDecoder
  implicit val manifestEncoder: Encoder[Manifest] = deriveEncoder

  def load(root: Path): Manifest = {
    val text = new String(Files.readAllBytes(root.resolve(FileName)), UTF_8)
    decode[Manifest](text) match {
      case Right(m) => m.validate()
      case Left(err) => throw new Format.FormatException(s"${root.resolve(FileName)}: ${err.getMessage}")
    }
  }

  def save(root: Path, m: Manifest): Unit = {
    Files.createDirectories(root)
    val text = Printer.spaces2.copy(dropNullValues = false).print(m.asJson) + "\n"
    Files.write(root.resolve(FileName), text.getBytes(UTF_8))
  }
}
