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

import java.util.function.{Function => JFunction}

import com.linecorp.armeria.common.{HttpMethod, HttpRequest, HttpResponse, HttpStatus, MediaType}
import com.linecorp.armeria.server.{HttpService, ServiceRequestContext, SimpleDecoratingHttpService}

/**
 * Scality fork: an unauthenticated liveness endpoint, `GET /healthz`.
 *
 * The share protocol answers 401 to any request without the bearer token, so an
 * orchestrator could only probe the TCP port. `/healthz` answers 200 as soon as the server
 * serves requests and says nothing else: no share, table, version or configuration. It is
 * the one path exempted from the bearer-token check, matched exactly.
 */
object HealthCheck {
  val Path = "/healthz"

  val service: HttpService = new HttpService {
    override def serve(ctx: ServiceRequestContext, req: HttpRequest): HttpResponse = {
      if (req.method() == HttpMethod.GET || req.method() == HttpMethod.HEAD) {
        HttpResponse.of(HttpStatus.OK, MediaType.PLAIN_TEXT_UTF_8, "ok\n")
      } else {
        HttpResponse.of(HttpStatus.METHOD_NOT_ALLOWED)
      }
    }
  }

  def isHealthPath(path: String): Boolean = path == Path

  /**
   * Wrap an authorization decorator so that it applies to every path except [[Path]].
   * Any other path (including an unknown one) still goes through `auth`.
   */
  def exemptFrom(
      auth: JFunction[_ >: HttpService, _ <: HttpService]): JFunction[HttpService, HttpService] = {
    new JFunction[HttpService, HttpService] {
      override def apply(delegate: HttpService): HttpService = {
        val authorized: HttpService = auth.apply(delegate)
        new SimpleDecoratingHttpService(delegate) {
          override def serve(ctx: ServiceRequestContext, req: HttpRequest): HttpResponse = {
            if (isHealthPath(ctx.path())) {
              delegate.serve(ctx, req)
            } else {
              authorized.serve(ctx, req)
            }
          }
        }
      }
    }
  }
}
