package de.tuberlin.dima.geotrellisbaseline.runner

import java.io.PrintWriter
import java.lang.management.ManagementFactory
import java.nio.file.{Files, Path, Paths}
import java.time.Instant

import scala.collection.JavaConverters._

/** Samples this JVM every `intervalMs` on a daemon thread: wall clock (µs since epoch), RSS (/proc/self/status), process
  * CPU time and used heap (all pools). Rows go to `out` (resources.csv: t_us, rss_kb, cpu_ticks, heap_used_bytes;
  * cpu_ticks in units of 10 ms, as scripts/lib/sample.sh with clk_tck 100). `mark` takes an extra sample, e.g. at the
  * span boundaries; `span(t0, t1)` gives peak / mean RSS and peak used heap of the samples inside it. */
final class ResourceSampler(out: Path, intervalMs: Long = 100) extends AutoCloseable {
  final case class Sample(tUs: Long, rssKb: Long, cpuNs: Long, heapUsed: Long)

  private val os = ManagementFactory.getOperatingSystemMXBean.asInstanceOf[com.sun.management.OperatingSystemMXBean]
  private val memory = ManagementFactory.getMemoryMXBean
  private val samples = scala.collection.mutable.ArrayBuffer.empty[Sample]
  @volatile private var running = true

  private val thread = new Thread(new Runnable {
    def run(): Unit = while (running) {
      mark()
      try Thread.sleep(intervalMs) catch { case _: InterruptedException => }
    }
  }, "resource-sampler")
  thread.setDaemon(true)
  thread.start()

  def nowUs: Long = { val i = Instant.now(); i.getEpochSecond * 1000000L + i.getNano / 1000 }

  def cpuNs: Long = os.getProcessCpuTime

  def mark(): Sample = {
    val s = Sample(nowUs, ResourceSampler.status("VmRSS"), cpuNs, memory.getHeapMemoryUsage.getUsed)
    samples.synchronized(samples += s)
    s
  }

  def span(t0Us: Long, t1Us: Long): (Long, Long, Long) = {
    val in = samples.synchronized(samples.toVector).filter(s => s.tUs >= t0Us && s.tUs <= t1Us)
    if (in.isEmpty) (0L, 0L, 0L)
    else (in.map(_.rssKb).max * 1024, in.map(_.rssKb).sum * 1024 / in.size, in.map(_.heapUsed).max)
  }

  def close(): Unit = {
    running = false
    thread.interrupt()
    thread.join(1000)
    val all = samples.synchronized(samples.toVector).sortBy(_.tUs)
    val w = new PrintWriter(Files.newBufferedWriter(out))
    try {
      w.println("t_us,rss_kb,cpu_ticks,heap_used_bytes")
      all.foreach(s => w.println(s"${s.tUs},${s.rssKb},${s.cpuNs / 10000000L},${s.heapUsed}"))
    } finally w.close()
  }
}

object ResourceSampler {
  /** A kB field of /proc/self/status (VmRSS, VmHWM); 0 where /proc is not available. */
  def status(field: String): Long =
    try Files.readAllLines(Paths.get("/proc/self/status")).asScala.find(_.startsWith(field + ":"))
      .map(_.split("\\s+")(1).toLong).getOrElse(0L)
    catch { case _: java.io.IOException => 0L }
}
