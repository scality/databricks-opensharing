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

import java.nio.charset.StandardCharsets.UTF_8
import java.security.MessageDigest
import java.time.Instant
import java.util.function.{Function => JFunction}

import com.linecorp.armeria.common.{HttpHeaderNames, HttpRequest, HttpResponse}
import com.linecorp.armeria.common.logging.RequestLog
import com.linecorp.armeria.server.{HttpService, ServiceRequestContext, SimpleDecoratingHttpService}
import org.apache.log4j.Logger

/**
 * Scality fork: one audit event per request to the share protocol.
 *
 * Emitted on the logger `io.delta.sharing.audit` once the response is complete, as a
 * `java.util.Map` that [[JsonLayout]] writes as top-level JSON fields:
 *
 *   time, type ("audit"), principal, sourceIp, forwardedFor (when present), action,
 *   resource, share / schema / table (when the path names them), method, path, status,
 *   result, requestId, durationMs
 *
 * `principal` never carries the token: `recipient:<first 12 hex of SHA-256(token)>` for
 * the configured bearer token, `invalid-token` for any other token, `anonymous` for no
 * token, `unauthenticated` when the server has no authorization configured. Refusals are
 * audited too: the decorator wraps the authorization check rather than sitting behind it.
 * `GET /healthz` is not audited.
 */
object AccessAudit {
  val LoggerName = "io.delta.sharing.audit"
  private val logger = Logger.getLogger(LoggerName)
  private val RequestIdPattern = "[A-Za-z0-9._:-]{1,128}".r
  private val MaxFieldLength = 256

  /**
   * The server-wide decorator: audit outermost, then `authorize` (when the server has a
   * bearer token), then the service. Composing both here fixes the order explicitly.
   */
  def decorator(
      endpoint: String,
      bearerToken: Option[String],
      authorize: Option[JFunction[HttpService, HttpService]])
    : JFunction[HttpService, HttpService] = {
    new JFunction[HttpService, HttpService] {
      override def apply(delegate: HttpService): HttpService = {
        val inner: HttpService = authorize.map(_.apply(delegate)).getOrElse(delegate)
        new SimpleDecoratingHttpService(inner) {
          override def serve(ctx: ServiceRequestContext, req: HttpRequest): HttpResponse = {
            if (!HealthCheck.isHealthPath(ctx.path())) {
              val pending = requestFields(ctx, req, endpoint, bearerToken)
              ctx.log().whenComplete().thenAccept(new java.util.function.Consumer[RequestLog] {
                override def accept(log: RequestLog): Unit = emit(pending, log)
              })
            }
            inner.serve(ctx, req)
          }
        }
      }
    }
  }

  private def truncate(s: String): String =
    if (s.length <= MaxFieldLength) s else s.substring(0, MaxFieldLength)

  /** What the request says about itself, captured before it is served. */
  private[scality] def requestFields(
      ctx: ServiceRequestContext,
      req: HttpRequest,
      endpoint: String,
      bearerToken: Option[String]): java.util.LinkedHashMap[String, Any] = {
    val method = req.method().name()
    val path = ctx.decodedPath()
    val (action, names) = classify(method, path, endpoint)
    val fields = new java.util.LinkedHashMap[String, Any]()
    fields.put("type", "audit")
    val authorization = req.headers().get(HttpHeaderNames.AUTHORIZATION)
    fields.put("principal", principal(authorization, bearerToken))
    val remote = Option(ctx.remoteAddress[java.net.InetSocketAddress]())
    fields.put("sourceIp", remote.map(_.getAddress.getHostAddress).orNull)
    Option(req.headers().get("x-forwarded-for"))
      .foreach(v => fields.put("forwardedFor", truncate(v)))
    fields.put("action", action)
    fields.put("resource", truncate(names.map(_._2).mkString("/")))
    names.foreach { case (k, v) => fields.put(k, truncate(v)) }
    fields.put("method", method)
    fields.put("path", truncate(path))
    val requestId = Option(req.headers().get("x-request-id"))
      .filter(id => RequestIdPattern.pattern.matcher(id).matches())
      .getOrElse(ctx.id().text())
    fields.put("requestId", requestId)
    fields
  }

  private def emit(pending: java.util.LinkedHashMap[String, Any], log: RequestLog): Unit = {
    val status = try log.responseHeaders().status().code() catch { case _: Throwable => 0 }
    val event = new java.util.LinkedHashMap[String, Any]()
    // `time` is when the request arrived; the layout's own `time` would be the moment
    // the response completed.
    event.put("time", Instant.ofEpochMilli(log.requestStartTimeMillis()).toString)
    event.putAll(pending)
    event.put("status", status)
    event.put("result", result(status))
    event.put("durationMs", log.totalDurationNanos() / 1000000L)
    event.put("message", s"${pending.get("action")} ${result(status)}")
    logger.info(event)
  }

  private[scality] def result(status: Int): String = status match {
    case s if s >= 200 && s < 400 => "success"
    case 401 | 403 => "denied"
    case 404 => "not_found"
    case s if s >= 400 && s < 500 => "rejected"
    case _ => "error"
  }

  private[scality] def fingerprint(token: String): String = {
    val digest = MessageDigest.getInstance("SHA-256").digest(token.getBytes(UTF_8))
    digest.map(b => f"${b & 0xff}%02x").mkString.take(12)
  }

  private[scality] def principal(authorization: String, bearerToken: Option[String]): String = {
    bearerToken match {
      case None => "unauthenticated"
      case Some(expected) =>
        val presented = Option(authorization)
          .filter(_.regionMatches(true, 0, "Bearer ", 0, 7))
          .map(_.substring(7).trim)
        presented match {
          case None => "anonymous"
          case Some(t) if MessageDigest.isEqual(t.getBytes(UTF_8), expected.getBytes(UTF_8)) =>
            s"recipient:${fingerprint(expected)}"
          case Some(_) => "invalid-token"
        }
    }
  }

  /**
   * The protocol action a request is, and the share / schema / table names its path
   * carries, in order. `unknown` for anything outside the protocol's routes.
   */
  private[scality] def classify(
      method: String,
      path: String,
      endpoint: String): (String, Seq[(String, String)]) = {
    val prefix = endpoint.stripSuffix("/")
    if (!(path == prefix || path.startsWith(prefix + "/"))) return ("unknown", Nil)
    val seg = path.substring(prefix.length).split("/").filter(_.nonEmpty).toList
    seg match {
      case "shares" :: Nil => ("share.list", Nil)
      case "shares" :: s :: Nil => ("share.get", Seq("share" -> s))
      case "shares" :: s :: "schemas" :: Nil => ("schema.list", Seq("share" -> s))
      case "shares" :: s :: "all-tables" :: Nil => ("table.list_all", Seq("share" -> s))
      case "shares" :: s :: "schemas" :: sc :: "tables" :: Nil =>
        ("table.list", Seq("share" -> s, "schema" -> sc))
      case "shares" :: s :: "schemas" :: sc :: "tables" :: t :: rest =>
        val names = Seq("share" -> s, "schema" -> sc, "table" -> t)
        val action = rest match {
          case Nil => "table.version"
          case "version" :: Nil => "table.version"
          case "metadata" :: Nil => "table.metadata"
          case "query" :: Nil => "table.query"
          case "queries" :: _ :: Nil => "table.query_status"
          case "changes" :: Nil => "table.changes"
          case "temporary-table-credentials" :: Nil => "table.credentials"
          case _ => "unknown"
        }
        (action, names)
      case _ => ("unknown", Nil)
    }
  }
}
