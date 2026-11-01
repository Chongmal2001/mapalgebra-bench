package de.tuberlin.dima.geotrellisbaseline.focal

import java.util.concurrent.{Callable, ExecutorService, Executors, ThreadFactory}

import scala.collection.JavaConverters._

/** Fixed pool of `threads` worker threads for independent kernel calls; 1 = everything on the calling thread.
  * Results keep the order of the inputs, so the output does not depend on the thread count. */
final class Workers(val threads: Int) extends AutoCloseable {
  require(threads >= 1, "threads must be >= 1")

  private val pool: Option[ExecutorService] =
    if (threads == 1) None
    else Some(Executors.newFixedThreadPool(threads, new ThreadFactory {
      private var n = 0
      def newThread(r: Runnable): Thread = synchronized {
        n += 1
        val t = new Thread(r, s"focal-$n")
        t.setDaemon(true)
        t
      }
    }))

  def map[A, B](items: IndexedSeq[A])(f: A => B): Vector[B] = pool match {
    case None => items.iterator.map(f).toVector
    case Some(p) =>
      val tasks = items.map(a => new Callable[B] { def call(): B = f(a) })
      p.invokeAll(tasks.asJava).asScala.iterator.map(_.get).toVector
  }

  def close(): Unit = pool.foreach(_.shutdown())
}

object Workers {
  val Serial: Workers = new Workers(1)
}
