package de.tuberlin.dima.geotrellisbaseline.focal

import java.nio.charset.StandardCharsets.UTF_8
import java.nio.file.{Files, Path}

import geotrellis.raster.{GridBounds, Tile}
import geotrellis.raster.mapalgebra.{focal => gtf}
import io.circe.{Json, parser}

/** A focal workload as written by `mabench workload`, e.g.
  * {{{ {"family": "focal", "output_kind": "raster", "params": {"op": "mean", "shape": "square", "radius": 3}} }}}
  * Semantics follow evaluation-spec §3.1: truncated edges, NoData (NaN) ignored, all-NoData window -> NaN.
  */
final case class FocalWorkload(op: String, shape: String, radius: Int) {
  require(FocalWorkload.Ops.contains(op), s"op must be one of ${FocalWorkload.Ops.mkString(", ")}")
  require(FocalWorkload.Shapes.contains(shape), s"shape must be one of ${FocalWorkload.Shapes.mkString(", ")}")
  require(radius >= 1, "radius must be >= 1")

  /** Square(r): all offsets with max(|dy|,|dx|) <= r. Circle(r): dy^2 + dx^2 <= r^2, boundary included
    * (GeoTrellis masks cells with distance > r), matching the oracle's footprint. */
  val neighborhood: gtf.Neighborhood = shape match {
    case "square" => gtf.Square(radius)
    case "circle" => gtf.Circle(radius.toDouble)
  }

  /** The GeoTrellis kernel, evaluated on a buffered tile restricted to `bounds` (the original tile). */
  val kernel: (Tile, Option[GridBounds[Int]]) => Tile = op match {
    case "mean" => (t, b) => gtf.Mean(t, neighborhood, b, gtf.TargetCell.All)
    case "sum"  => (t, b) => gtf.Sum(t, neighborhood, b, gtf.TargetCell.All)
    case "min"  => (t, b) => gtf.Min(t, neighborhood, b, gtf.TargetCell.All)
    case "max"  => (t, b) => gtf.Max(t, neighborhood, b, gtf.TargetCell.All)
  }

  def name: String = s"focal:$op:$shape:$radius"

  def toJson: Json = Json.obj(
    "family" -> Json.fromString("focal"),
    "output_kind" -> Json.fromString("raster"),
    "params" -> Json.obj("op" -> Json.fromString(op), "shape" -> Json.fromString(shape), "radius" -> Json.fromInt(radius)))
}

object FocalWorkload {
  val Ops: Set[String] = Set("mean", "sum", "min", "max")
  val Shapes: Set[String] = Set("square", "circle")

  def fromJson(json: Json): FocalWorkload = {
    val c = json.hcursor
    c.get[String]("family") match {
      case Right("focal") =>
      case Right(other) => throw new UnsupportedOperationException(s"workload family '$other' is not implemented in the GeoTrellis baseline yet")
      case Left(_) if c.get[String]("format").toOption.contains("mabench-tilestore") =>
        throw new IllegalArgumentException("--workload points at a tile store manifest; pass a workload file " +
          "(mabench workload focal:mean:square:3 -o mean-sq-3.json) or a short spec (focal:mean:square:3)")
      case Left(err) => throw new IllegalArgumentException(s"workload: ${err.getMessage}")
    }
    val p = c.downField("params")
    (for {
      op <- p.get[String]("op")
      shape <- p.get[String]("shape")
      radius <- p.get[Int]("radius")
    } yield FocalWorkload(op, shape, radius)).fold(e => throw new IllegalArgumentException(s"workload params: ${e.getMessage}"), identity)
  }

  def load(path: Path): FocalWorkload =
    parser.parse(new String(Files.readAllBytes(path), UTF_8)).fold(e => throw new IllegalArgumentException(s"$path: ${e.getMessage}"), fromJson)

  /** Short form used on the command line, e.g. "focal:mean:square:3" (same as mabench). */
  def parse(spec: String): FocalWorkload = spec.split(":") match {
    case Array("focal", op, shape, r) => FocalWorkload(op, shape, r.toInt)
    case _ => throw new IllegalArgumentException(s"expected focal:<op>:<shape>:<radius>, got '$spec'")
  }
}
