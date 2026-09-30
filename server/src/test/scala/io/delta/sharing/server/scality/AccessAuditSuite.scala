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

import java.net.{HttpURLConnection, ServerSocket, URL}
import java.util.concurrent.CopyOnWriteArrayList

import scala.collection.JavaConverters._

import com.linecorp.armeria.server.Server
import org.apache.log4j.{AppenderSkeleton, Logger}
import org.apache.log4j.spi.LoggingEvent
import org.scalatest.{BeforeAndAfterAll, BeforeAndAfterEach, FunSuite}

import io.delta.sharing.server.DeltaSharingService
import io.delta.sharing.server.config.{Authorization, ServerConfig}

/** The real `DeltaSharingService.start`, with a capturing appender on the audit logger. */
class AccessAuditSuite extends FunSuite with BeforeAndAfterAll with BeforeAndAfterEach {
  private val token = "access-audit-suite-token-0123456789"
  private var server: Server = _
  private var port: Int = _
  private val captured = new CopyOnWriteArrayList[LoggingEvent]()
  private val layout = new JsonLayout

  private val appender = new AppenderSkeleton {
    override def append(event: LoggingEvent): Unit = captured.add(event)
    override def close(): Unit = {}
    override def requiresLayout(): Boolean = false
  }

  override def beforeAll(): Unit = {
    Logger.getLogger(AccessAudit.LoggerName).addAppender(appender)
    val socket = new ServerSocket(0)
    port = socket.getLocalPort
    socket.close()
    val config = new ServerConfig()
    config.setVersion(1)
    config.setHost("127.0.0.1")
    config.setPort(port)
    config.setAuthorization(Authorization(token))
    config.checkConfig()
    server = DeltaSharingService.start(config)
  }

  override def afterAll(): Unit = {
    if (server != null) server.stop().get()
    Logger.getLogger(AccessAudit.LoggerName).removeAppender(appender)
  }

  override def beforeEach(): Unit = captured.clear()

  private def request(
      path: String,
      method: String = "GET",
      headers: Map[String, String] = Map.empty): Int = {
    val conn = new URL(s"http://127.0.0.1:$port$path").openConnection()
      .asInstanceOf[HttpURLConnection]
    conn.setRequestMethod(method)
    headers.foreach { case (k, v) => conn.setRequestProperty(k, v) }
    if (method == "POST") {
      conn.setDoOutput(true)
      conn.setRequestProperty("Content-Type", "application/json")
      conn.getOutputStream.write("{}".getBytes("UTF-8"))
    }
    val code = conn.getResponseCode
    conn.disconnect()
    code
  }

  private def bearer(t: String) = Map("Authorization" -> s"Bearer $t")

  /** Audit events are written when the response completes, just after the client sees it. */
  private def events(n: Int): Seq[Map[String, Any]] = {
    val deadline = System.currentTimeMillis() + 5000
    while (captured.size < n && System.currentTimeMillis() < deadline) Thread.sleep(20)
    captured.asScala.map { e =>
      e.getMessage.asInstanceOf[java.util.Map[String, Any]].asScala.toMap
    }.toList
  }

  test("an authorised request is one event naming the recipient by fingerprint") {
    assert(request("/delta-sharing/shares", headers = bearer(token)) == 200)
    val Seq(e) = events(1)
    assert(e("type") == "audit")
    assert(e("action") == "share.list")
    assert(e("principal") == s"recipient:${AccessAudit.fingerprint(token)}")
    assert(e("status") == 200)
    assert(e("result") == "success")
    assert(e("sourceIp") == "127.0.0.1")
    assert(e("method") == "GET")
    assert(e.contains("time") && e.contains("requestId") && e.contains("durationMs"))
  }

  test("refusals are audited: no token and a wrong token") {
    assert(request("/delta-sharing/shares") == 401)
    assert(request("/delta-sharing/shares", headers = bearer("wrong")) == 401)
    val principals = events(2).map(e => (e("principal"), e("result"), e("status")))
    assert(principals.toSet == Set(("anonymous", "denied", 401), ("invalid-token", "denied", 401)))
  }

  test("a table query names the share, schema and table") {
    val code = request("/delta-sharing/shares/s1/schemas/sc1/tables/t1/query",
      method = "POST", headers = bearer(token))
    assert(code == 404)
    val Seq(e) = events(1)
    assert(e("action") == "table.query")
    assert(e("resource") == "s1/sc1/t1")
    assert((e("share"), e("schema"), e("table")) == (("s1", "sc1", "t1")))
    assert(e("result") == "not_found")
  }

  test("an incoming request id and forwarded-for are carried") {
    request("/delta-sharing/shares", headers = bearer(token) ++
      Map("X-Request-Id" -> "req-abc.123", "X-Forwarded-For" -> "203.0.113.7"))
    val Seq(e) = events(1)
    assert(e("requestId") == "req-abc.123")
    assert(e("forwardedFor") == "203.0.113.7")
  }

  test("a malformed request id is replaced by the server's own") {
    request("/delta-sharing/shares", headers = bearer(token) ++ Map("X-Request-Id" -> "a b\"c"))
    val Seq(e) = events(1)
    assert(e("requestId") != "a b\"c")
    assert(e("requestId").toString.nonEmpty)
  }

  test("the health endpoint is not audited") {
    assert(request("/healthz") == 200)
    Thread.sleep(300)
    assert(captured.isEmpty)
  }

  test("the rendered event is one JSON line and never carries the token") {
    request("/delta-sharing/shares", headers = bearer(token))
    request("/delta-sharing/shares", headers = bearer(token + "x"))
    events(2)
    captured.asScala.foreach { e =>
      val line = layout.format(e)
      assert(line.endsWith("\n") && line.indexOf('\n') == line.length - 1)
      assert(line.startsWith("{") && line.contains("\"type\":\"audit\""))
      assert(!line.contains(token))
    }
  }

  test("classify maps every protocol route") {
    val ep = "/delta-sharing"
    def a(m: String, p: String): String = AccessAudit.classify(m, ep + p, ep)._1
    assert(a("GET", "/shares") == "share.list")
    assert(a("GET", "/shares/s") == "share.get")
    assert(a("GET", "/shares/s/schemas") == "schema.list")
    assert(a("GET", "/shares/s/all-tables") == "table.list_all")
    assert(a("GET", "/shares/s/schemas/c/tables") == "table.list")
    assert(a("HEAD", "/shares/s/schemas/c/tables/t") == "table.version")
    assert(a("GET", "/shares/s/schemas/c/tables/t/version") == "table.version")
    assert(a("GET", "/shares/s/schemas/c/tables/t/metadata") == "table.metadata")
    assert(a("POST", "/shares/s/schemas/c/tables/t/query") == "table.query")
    assert(a("POST", "/shares/s/schemas/c/tables/t/queries/q1") == "table.query_status")
    assert(a("GET", "/shares/s/schemas/c/tables/t/changes") == "table.changes")
    assert(a("POST", "/shares/s/schemas/c/tables/t/temporary-table-credentials") ==
      "table.credentials")
    assert(AccessAudit.classify("GET", "/other", ep)._1 == "unknown")
    assert(AccessAudit.classify("GET", "/delta-sharingX/shares", ep)._1 == "unknown")
  }

  test("principal never depends on anything but the configured token") {
    assert(AccessAudit.principal(null, None) == "unauthenticated")
    assert(AccessAudit.principal("Bearer x", None) == "unauthenticated")
    assert(AccessAudit.principal(null, Some(token)) == "anonymous")
    assert(AccessAudit.principal("Basic abc", Some(token)) == "anonymous")
    assert(AccessAudit.principal("bearer " + token, Some(token)).startsWith("recipient:"))
  }
}
