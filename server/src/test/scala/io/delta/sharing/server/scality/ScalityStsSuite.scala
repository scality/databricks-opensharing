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

import java.net.{InetSocketAddress, URI, URLDecoder}
import java.nio.charset.StandardCharsets.UTF_8

import scala.collection.mutable.ArrayBuffer
import scala.io.Source

import com.amazonaws.auth.{AWSStaticCredentialsProvider, BasicAWSCredentials}
import com.linecorp.armeria.common.{AggregatedHttpRequest, HttpMethod, HttpRequest}
import com.sun.net.httpserver.{HttpExchange, HttpHandler, HttpServer}
import org.apache.hadoop.conf.Configuration
import org.scalatest.{BeforeAndAfterAll, BeforeAndAfterEach, FunSuite}

import io.delta.sharing.server.{DeltaSharingIllegalArgumentException, DeltaSharingService}
import io.delta.sharing.server.common.JsonUtils
import io.delta.sharing.server.common.actions.DeltaFormat
import io.delta.sharing.server.config.{SchemaConfig, ServerConfig, ShareConfig, TableConfig}
import io.delta.sharing.server.model.{
  DeltaMetadataCopy,
  DeltaResponseMetadata,
  Metadata,
  Protocol,
  TemporaryCredentials
}

/** One recorded STS call: the form parameters and the Authorization header. */
case class StsCall(params: Map[String, String], authorization: String)

/** An in-process STS answering AssumeRole; nothing leaves the loopback interface. */
class FakeSts {
  val calls = ArrayBuffer[StsCall]()
  private val server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0)
  server.createContext("/", new HttpHandler {
    override def handle(ex: HttpExchange): Unit = {
      val body = Source.fromInputStream(ex.getRequestBody, UTF_8.name).mkString
      val params = body.split("&").filter(_.nonEmpty).map { kv =>
        val i = kv.indexOf('=')
        val (k, v) = if (i < 0) (kv, "") else (kv.substring(0, i), kv.substring(i + 1))
        URLDecoder.decode(k, UTF_8.name) -> URLDecoder.decode(v, UTF_8.name)
      }.toMap
      calls.synchronized {
        calls += StsCall(params, Option(ex.getRequestHeaders.getFirst("Authorization")).orNull)
      }
      val xml =
        """<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
          |<AssumeRoleResult><Credentials>
          |<AccessKeyId>FAKEACCESSKEY</AccessKeyId>
          |<SecretAccessKey>fake-secret</SecretAccessKey>
          |<SessionToken>fake-token</SessionToken>
          |<Expiration>2030-01-01T00:00:00Z</Expiration>
          |</Credentials>
          |<AssumedRoleUser><AssumedRoleId>X:s</AssumedRoleId><Arn>arn:x</Arn></AssumedRoleUser>
          |</AssumeRoleResult>
          |<ResponseMetadata><RequestId>r</RequestId></ResponseMetadata>
          |</AssumeRoleResponse>""".stripMargin.getBytes(UTF_8)
      ex.getResponseHeaders.set("Content-Type", "text/xml")
      ex.sendResponseHeaders(200, xml.length)
      ex.getResponseBody.write(xml)
      ex.close()
    }
  })
  server.start()

  def endpoint: String = s"http://127.0.0.1:${server.getAddress.getPort}"
  def stop(): Unit = server.stop(0)
}

class ScalityStsSuite extends FunSuite with BeforeAndAfterAll with BeforeAndAfterEach {
  private var sts: FakeSts = _

  private val dummyCreds =
    new AWSStaticCredentialsProvider(new BasicAWSCredentials("TESTKEY", "test-secret"))

  private val shareRole = "arn:aws:iam::000000000000:role/share-a-read"
  private val tableRole = "arn:aws:iam::000000000000:role/share-a-t2-read"

  override def beforeAll(): Unit = sts = new FakeSts
  override def afterAll(): Unit = sts.stop()
  override def beforeEach(): Unit = sts.calls.synchronized(sts.calls.clear())

  private def conf(pairs: (String, String)*): Configuration = {
    val c = new Configuration(false)
    pairs.foreach { case (k, v) => c.set(k, v) }
    c
  }

  private def settings(extra: (String, String)*): ScalityStsSettings =
    ScalitySts.fromHadoop(conf(Seq(
      ScalitySts.EndpointKey -> sts.endpoint,
      ScalitySts.RegionKey -> "test-region-1",
      ScalitySts.RolePrefix + "share_a" -> shareRole,
      ScalitySts.RolePrefix + "share_a.s1.t2" -> tableRole) ++ extra: _*), dummyCreds).get

  private def table(name: String, historyShared: Boolean = true): TableConfig =
    TableConfig(name, s"s3a://bucket-a/data/$name", historyShared = historyShared)

  private def serverConfig(): ServerConfig = {
    val sc = new ServerConfig()
    sc.setVersion(1)
    sc.setTemporaryCredentialValiditySeconds(7200)
    sc.setShares(java.util.Arrays.asList(
      ShareConfig("share_a", java.util.Arrays.asList(
        SchemaConfig("s1", java.util.Arrays.asList(table("t1"), table("t2"))))),
      ShareConfig("share_b", java.util.Arrays.asList(
        SchemaConfig("s1", java.util.Arrays.asList(table("t3"))))),
      ShareConfig("share_c", java.util.Arrays.asList(
        SchemaConfig("s1", java.util.Arrays.asList(table("t4", historyShared = false)))))))
    sc.checkConfig()
    sc
  }

  // configuration

  test("no endpoint configured: nothing is active") {
    assert(ScalitySts.fromHadoop(conf(), dummyCreds).isEmpty)
    assert(ScalitySts.fromHadoop(
      conf(ScalitySts.RolePrefix + "share_a" -> shareRole), dummyCreds).isEmpty)
  }

  test("a scheme-less endpoint follows fs.s3a.connection.ssl.enabled, region falls back") {
    val tls = ScalitySts.fromHadoop(conf(ScalitySts.EndpointKey -> "sts.example:443"), dummyCreds)
    assert(tls.get.endpoint == "https://sts.example:443")
    assert(tls.get.region == "us-east-1")
    val plain = ScalitySts.fromHadoop(conf(
      ScalitySts.EndpointKey -> "sts.example:80",
      "fs.s3a.connection.ssl.enabled" -> "false",
      "fs.s3a.endpoint.region" -> "s3-region"), dummyCreds)
    assert(plain.get.endpoint == "http://sts.example:80")
    assert(plain.get.region == "s3-region")
    assert(!plain.get.sendSessionPolicy)
  }

  test("role selection: the most specific key wins") {
    val s = settings()
    assert(s.roleFor("share_a", "s1", "t1").contains(shareRole))
    assert(s.roleFor("share_a", "s1", "t2").contains(tableRole))
    assert(s.roleFor("share_b", "s1", "t3").isEmpty)
    val withSchema = settings(ScalitySts.RolePrefix + "share_a.s1" -> "arn:schema")
    assert(withSchema.roleFor("share_a", "s1", "t1").contains("arn:schema"))
    assert(withSchema.roleFor("share_a", "s1", "t2").contains(tableRole))
  }

  test("duration is clamped to 3600 seconds") {
    assert(ScalitySts.durationSeconds(7200) == 3600)
    assert(ScalitySts.durationSeconds(3600) == 3600)
    assert(ScalitySts.durationSeconds(900) == 900)
    assert(ScalitySts.durationSeconds(0) == 1)
  }

  // the AssumeRole request

  test("AssumeRole never carries PolicyArns, and no Policy unless opted in") {
    val loc = new URI("s3a://bucket-a/data/t1")
    val req = new ScalityStsCredentialVendor(settings())
      .assumeRoleRequest("share_a", "s1", "t1", loc, 7200)
    assert(req.getRoleArn == shareRole)
    assert(req.getDurationSeconds == 3600)
    assert(req.getPolicyArns == null || req.getPolicyArns.isEmpty)
    assert(req.getPolicy == null)

    val optIn = new ScalityStsCredentialVendor(settings(ScalitySts.SessionPolicyKey -> "true"))
      .assumeRoleRequest("share_a", "s1", "t1", loc, 600)
    assert(optIn.getPolicyArns == null || optIn.getPolicyArns.isEmpty)
    assert(optIn.getPolicy.contains("arn:aws:s3:::bucket-a/data/t1/*"))
  }

  test("a table with no role is refused, and so is a non-S3 location") {
    val vendor = new ScalityStsCredentialVendor(settings())
    intercept[DeltaSharingIllegalArgumentException] {
      vendor.assumeRoleRequest("share_b", "s1", "t3", new URI("s3a://bucket-a/data/t3"), 3600)
    }
    intercept[DeltaSharingIllegalArgumentException] {
      vendor.assumeRoleRequest("share_a", "s1", "t1", new URI("abfss://c@a.dfs/x"), 3600)
    }
  }

  test("the STS client calls the configured endpoint and signs for the configured region") {
    val vendor = new ScalityStsCredentialVendor(settings())
    val result = vendor.vend("share_a", "s1", "t2", new URI("s3a://bucket-a/data/t2"), 7200)
    assert(sts.calls.size == 1)
    val call = sts.calls.head
    assert(call.params("Action") == "AssumeRole")
    assert(call.params("RoleArn") == tableRole)
    assert(call.params("DurationSeconds") == "3600")
    assert(!call.params.keys.exists(_.startsWith("PolicyArns")))
    assert(!call.params.contains("Policy"))
    assert(call.authorization.contains("/test-region-1/sts/aws4_request"))
    assert(call.authorization.contains("Credential=TESTKEY/"))
    val creds = result.credentials
    assert(creds.location == "s3a://bucket-a/data/t2")
    assert(creds.awsTempCredentials.accessKeyId == "FAKEACCESSKEY")
    assert(creds.awsTempCredentials.sessionToken == "fake-token")
    assert(creds.expirationTime == 1893456000000L) // 2030-01-01T00:00:00Z from STS
  }

  // access modes

  test("accessModes only for a history-shared table with a role") {
    val s = settings()
    assert(s.accessModesFor("share_a", "s1", table("t1")).contains(Seq("url", "dir")))
    assert(s.accessModesFor("share_b", "s1", table("t3")).isEmpty)
    assert(s.accessModesFor("share_a", "s1", table("t1", historyShared = false)).isEmpty)
  }

  test("metadata lines carry location and accessModes only when advertised") {
    val parquet = Seq[Object](Protocol(1).wrap, Metadata(id = "m").wrap)
    val plain = ScalitySts.decorateMetadataActions(parquet, "s3a://b/t", None)
    assert(plain eq parquet)
    assert(!JsonUtils.toJson(plain(1)).contains("accessModes"))
    assert(!JsonUtils.toJson(plain(1)).contains("location"))

    val dir = ScalitySts.decorateMetadataActions(parquet, "s3a://b/t", Some(Seq("url", "dir")))
    val json = JsonUtils.toJson(dir(1))
    assert(json.contains("\"accessModes\":[\"url\",\"dir\"]"))
    assert(json.contains("\"location\":\"s3a://b/t\""))

    val delta = Seq[Object](DeltaResponseMetadata(deltaMetadata =
      DeltaMetadataCopy("m", null, null, DeltaFormat(), "{}", Nil, Map.empty, None)).wrap)
    val deltaJson = JsonUtils.toJson(
      ScalitySts.decorateMetadataActions(delta, "s3a://b/t", Some(Seq("url", "dir"))).head)
    assert(deltaJson.contains("\"accessModes\":[\"url\",\"dir\"]"))
    assert(!JsonUtils.toJson(delta.head).contains("accessModes"))
  }

  // the service, end to end

  private def tablesJson(service: DeltaSharingService, share: String): String =
    scalapb.json4s.JsonFormat.toJsonString(service.listAllTables(share, 500, null))

  test("default service: list responses carry no accessModes") {
    val service = new DeltaSharingService(serverConfig(), None)
    val json = tablesJson(service, "share_a")
    assert(json.contains("\"t1\""))
    assert(!json.contains("accessModes"))
    assert(!json.contains("location"))
  }

  test("configured service: accessModes on the tables a role covers, nowhere else") {
    val service = new DeltaSharingService(serverConfig(), Some(settings()))
    val a = tablesJson(service, "share_a")
    assert(a.contains("\"accessModes\":[\"url\",\"dir\"]"))
    assert(a.contains("\"location\":\"s3a://bucket-a/data/t1\""))
    assert(!tablesJson(service, "share_b").contains("accessModes"))
    assert(!scalapb.json4s.JsonFormat.toJsonString(
      service.listTables("share_c", "s1", 500, null)).contains("accessModes"))
  }

  test("configured service: the credential route assumes the share's role at the fake STS") {
    val service = new DeltaSharingService(serverConfig(), Some(settings()))
    val response = service.generateTemporaryTableCredential(
      HttpRequest.of(HttpMethod.POST, "/"), "share_a", "s1", "t1",
      AggregatedHttpRequest.of(HttpMethod.POST, "/"))
    val body = response.aggregate().join().contentUtf8()
    val result = JsonUtils.fromJson[TemporaryCredentials](body)
    assert(result.credentials.awsTempCredentials.accessKeyId == "FAKEACCESSKEY")
    assert(sts.calls.size == 1)
    assert(sts.calls.head.params("RoleArn") == shareRole)
    assert(sts.calls.head.params("DurationSeconds") == "3600")
    assert(!sts.calls.head.params.keys.exists(_.startsWith("PolicyArns")))
  }
}
