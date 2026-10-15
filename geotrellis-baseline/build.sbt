ThisBuild / organization := "de.tuberlin.dima"
ThisBuild / scalaVersion := "2.12.21"   // a Scala version GeoTrellis 3.8.1 is cross-built for; align with your scaffold
ThisBuild / version      := "0.1.0"

val geotrellisVersion = "3.8.1"

lazy val root = (project in file("."))
  .settings(
    name := "geotrellis-baseline",
    libraryDependencies ++= Seq(
      // circe (core, generic, parser) arrives transitively through geotrellis-vector
      "org.locationtech.geotrellis" %% "geotrellis-raster" % geotrellisVersion,
      "org.locationtech.geotrellis" %% "geotrellis-layer"  % geotrellisVersion,
      "org.scalatest"               %% "scalatest"         % "3.2.20" % Test
    ),
    scalacOptions ++= Seq("-deprecation", "-feature", "-unchecked", "-Xlint"),
    // fixed heap and GC logging for every run; JMH benchmarks come later in their own module
    run / fork := true,
    // forward the forked JVM's stdout and stderr unchanged; by default sbt prefixes everything the program
    // writes to stderr with [error], even ordinary progress messages
    run / outputStrategy := Some(StdoutOutput),
    // heap: 4g by default, GT_HEAP=24g sbt "run ..." for large frames (GeoTrellis needs ~4x the frame size)
    run / javaOptions ++= {
      val heap = sys.env.getOrElse("GT_HEAP", "4g")
      Seq(s"-Xms$heap", s"-Xmx$heap", "-XX:+AlwaysPreTouch")
    },
    Test / fork := true,
    Compile / mainClass := Some("de.tuberlin.dima.geotrellisbaseline.runner.BatchRunner")
  )
