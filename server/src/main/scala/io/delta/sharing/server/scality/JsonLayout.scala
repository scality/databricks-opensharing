/*
 * Copyright (2021) The Delta Lake Project Authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package io.delta.sharing.server.scality

import java.time.Instant

import scala.collection.JavaConverters._

import org.apache.log4j.Layout
import org.apache.log4j.spi.LoggingEvent

/**
 * Scality fork: a log4j 1.x (reload4j) layout that writes one JSON object per line.
 *
 * Every event has `time` (ISO-8601, UTC, milliseconds), `level`, `logger`, `thread` and
 * `message`; a throwable adds `exception` with the whole stack trace as one string. When
 * the logged message is a `java.util.Map` (the audit events of [[AccessAudit]]), its
 * entries are written as top-level fields instead of being rendered into `message`, so a
 * log shipper can index them without parsing a string.
 */
class JsonLayout extends Layout {

  override def format(event: LoggingEvent): String = {
    val fields = new java.util.LinkedHashMap[String, Any]()
    fields.put("time", Instant.ofEpochMilli(event.getTimeStamp).toString)
    fields.put("level", String.valueOf(event.getLevel))
    fields.put("logger", event.getLoggerName)
    fields.put("thread", event.getThreadName)
    event.getMessage match {
      case m: java.util.Map[_, _] =>
        m.asScala.foreach { case (k, v) => fields.put(String.valueOf(k), v) }
        if (!fields.containsKey("message")) fields.put("message", "")
      case other =>
        fields.put("message", String.valueOf(other))
    }
    val thrown = event.getThrowableStrRep
    if (thrown != null && thrown.nonEmpty) {
      fields.put("exception", thrown.mkString("\n"))
    }
    JsonLayout.render(fields) + "\n"
  }

  /** The layout writes the throwable itself, inside the JSON object. */
  override def ignoresThrowable(): Boolean = false

  override def activateOptions(): Unit = {}
}

object JsonLayout {

  /** A flat JSON object: strings, numbers, booleans and null. Anything else is a string. */
  def render(fields: java.util.Map[String, Any]): String = {
    val sb = new StringBuilder("{")
    var first = true
    fields.asScala.foreach { case (k, v) =>
      if (!first) sb.append(',')
      first = false
      quote(sb, k)
      sb.append(':')
      v match {
        case null => sb.append("null")
        case n: java.lang.Integer => sb.append(n.toString)
        case n: java.lang.Long => sb.append(n.toString)
        case n: Int => sb.append(n.toString)
        case n: Long => sb.append(n.toString)
        case b: Boolean => sb.append(b.toString)
        case b: java.lang.Boolean => sb.append(b.toString)
        case other => quote(sb, String.valueOf(other))
      }
    }
    sb.append('}').toString
  }

  private def quote(sb: StringBuilder, s: String): Unit = {
    sb.append('"')
    s.foreach {
      case '"' => sb.append("\\\"")
      case '\\' => sb.append("\\\\")
      case '\n' => sb.append("\\n")
      case '\r' => sb.append("\\r")
      case '\t' => sb.append("\\t")
      case c if c < ' ' || c.toInt == 0x2028 || c.toInt == 0x2029 =>
        sb.append(f"\\u${c.toInt}%04x")
      case c => sb.append(c)
    }
    sb.append('"')
  }
}
