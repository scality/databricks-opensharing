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

import org.apache.log4j.{Level, Logger}
import org.apache.log4j.spi.LoggingEvent
import org.scalatest.FunSuite

class JsonLayoutSuite extends FunSuite {
  private val layout = new JsonLayout
  private val logger = Logger.getLogger("json.layout.suite")

  private def event(message: Any, thrown: Throwable = null) =
    new LoggingEvent("fqcn", logger, 1790000000123L, Level.WARN, message, thrown)

  test("one object per line with the standard fields") {
    val line = layout.format(event("hello"))
    assert(line == "{\"time\":\"2026-09-21T14:13:20.123Z\",\"level\":\"WARN\"," +
      "\"logger\":\"json.layout.suite\",\"thread\":\"" + Thread.currentThread.getName +
      "\",\"message\":\"hello\"}\n")
  }

  test("control characters and quotes are escaped, so a message cannot break the line") {
    val line = layout.format(event("a\"b\\c\nd\re\tf\u0001g"))
    assert(line.count(_ == '\n') == 1 && line.endsWith("\n"))
    assert(line.contains("\"message\":\"a\\\"b\\\\c\\nd\\re\\tf\\u0001g\""))
  }

  test("a map message becomes top-level fields") {
    val m = new java.util.LinkedHashMap[String, Any]()
    m.put("type", "audit")
    m.put("status", 401)
    m.put("durationMs", 12L)
    val line = layout.format(event(m))
    assert(line.contains("\"type\":\"audit\""))
    assert(line.contains("\"status\":401"))
    assert(line.contains("\"durationMs\":12"))
    assert(line.contains("\"message\":\"\""))
  }

  test("a throwable is one field holding the whole trace") {
    val line = layout.format(event("boom", new IllegalStateException("bad\nstate")))
    assert(line.count(_ == '\n') == 1)
    assert(line.contains("\"exception\":\"java.lang.IllegalStateException: bad\\nstate"))
    assert(!layout.ignoresThrowable())
  }
}
