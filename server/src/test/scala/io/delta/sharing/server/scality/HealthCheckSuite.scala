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
import java.nio.charset.StandardCharsets.UTF_8

import scala.io.Source

import com.linecorp.armeria.server.Server
import org.scalatest.{BeforeAndAfterAll, FunSuite}

import io.delta.sharing.server.DeltaSharingService
import io.delta.sharing.server.config.{Authorization, ServerConfig}

/** Runs the real `DeltaSharingService.start` with a bearer token and no shares. */
class HealthCheckSuite extends FunSuite with BeforeAndAfterAll {
  private val token = "health-check-suite-token"
  private var server: Server = _
  private var port: Int = _

  override def beforeAll(): Unit = {
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
  }

  private def request(
      path: String,
      method: String = "GET",
      bearer: Option[String] = None): (Int, String) = {
    val conn = new URL(s"http://127.0.0.1:$port$path").openConnection()
      .asInstanceOf[HttpURLConnection]
    conn.setRequestMethod(method)
    bearer.foreach(t => conn.setRequestProperty("Authorization", s"Bearer $t"))
    val code = conn.getResponseCode
    val stream = if (code < 400) conn.getInputStream else conn.getErrorStream
    val body = if (stream == null) "" else Source.fromInputStream(stream, UTF_8.name).mkString
    conn.disconnect()
    (code, body)
  }

  test("/healthz answers 200 without a token") {
    assert(request("/healthz") == (200, "ok\n"))
  }

  test("/healthz answers HEAD without a token") {
    assert(request("/healthz", method = "HEAD")._1 == 200)
  }

  test("/healthz refuses a write method") {
    assert(request("/healthz", method = "POST")._1 == 405)
  }

  test("the share protocol still requires the token") {
    assert(request("/delta-sharing/shares")._1 == 401)
    assert(request("/delta-sharing/shares", bearer = Some("wrong"))._1 == 401)
    assert(request("/delta-sharing/shares", bearer = Some(token))._1 == 200)
  }

  test("only the exact health path is exempt") {
    assert(request("/healthz/extra")._1 == 401)
    assert(request("/healthzx")._1 == 401)
    assert(request("/unknown")._1 == 401)
  }
}
