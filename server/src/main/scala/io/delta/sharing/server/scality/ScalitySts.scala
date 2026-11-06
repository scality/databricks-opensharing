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

import java.net.URI
import java.util.Locale

import com.amazonaws.auth.{AWSCredentialsProvider, DefaultAWSCredentialsProviderChain}
import com.amazonaws.client.builder.AwsClientBuilder.EndpointConfiguration
import com.amazonaws.services.securitytoken.{AWSSecurityTokenService, AWSSecurityTokenServiceClientBuilder}
import com.amazonaws.services.securitytoken.model.AssumeRoleRequest
import org.apache.hadoop.conf.Configuration

import io.delta.sharing.server.DeltaSharingIllegalArgumentException
import io.delta.sharing.server.config.TableConfig
import io.delta.sharing.server.credential.StorageScheme
import io.delta.sharing.server.credential.aws.AwsPolicyGenerator
import io.delta.sharing.server.model.{
  AwsTempCredentials,
  Credentials,
  DeltaResponseMetadata,
  DeltaResponseSingleAction,
  Metadata,
  SingleAction,
  TemporaryCredentials
}
import io.delta.sharing.server.protocol.Table

/**
 * Scality fork: temporary credentials issued by a Scality (RING or ARTESCA) STS.
 *
 * Read from the Hadoop configuration (`core-site.xml`), like the S3A endpoint the presigner
 * uses. Absent `delta.sharing.scality.sts.endpoint`, nothing here is active: no table
 * advertises the `dir` access mode and the temporary-credential route keeps upstream's
 * behaviour.
 *
 * Scoping comes from the role, never from a session policy: RING STS rejects `PolicyArns`
 * (400) and accepts but ignores an inline `Policy`. Each share (or schema, or table) maps to
 * its own read-only role limited to that data. `PolicyArns` is never sent; an inline
 * `Policy` is sent only when `delta.sharing.scality.sts.session.policy` is `true`.
 */
case class ScalityStsSettings(
    endpoint: String,
    region: String,
    roles: Map[String, String],
    sendSessionPolicy: Boolean,
    credentialsProvider: AWSCredentialsProvider) {

  /**
   * The role ARN for a table: the most specific key wins:
   * `role.<share>.<schema>.<table>`, then `role.<share>.<schema>`, then `role.<share>`.
   * The server resolves names case-insensitively, so `roles` holds lower-cased keys
   * (`ScalitySts.fromHadoop`) and the lookup lower-cases its inputs: a request spelled
   * `Share/Schema/Table` must find the table's own role, never fall back to the share's.
   */
  def roleFor(share: String, schema: String, table: String): Option[String] = {
    val (sh, sc, t) = (ScalitySts.norm(share), ScalitySts.norm(schema), ScalitySts.norm(table))
    Seq(s"$sh.$sc.$t", s"$sh.$sc", sh).flatMap(roles.get).headOption
  }

  /** The access modes to advertise for a table, or None to advertise nothing (upstream). */
  def accessModesFor(share: String, schema: String, tableConfig: TableConfig): Option[Seq[String]] =
    if (tableConfig.historyShared && roleFor(share, schema, tableConfig.getName).isDefined) {
      Some(ScalitySts.AccessModesUrlAndDir)
    } else {
      None
    }
}

object ScalitySts {
  val Prefix = "delta.sharing.scality.sts."
  val EndpointKey: String = Prefix + "endpoint"
  val RegionKey: String = Prefix + "region"
  val RolePrefix: String = Prefix + "role."
  val SessionPolicyKey: String = Prefix + "session.policy"

  /** RING STS refuses a longer session; AWS also caps role chaining at one hour. */
  val MaxDurationSeconds = 3600

  val AccessModesUrlAndDir: Seq[String] = Seq("url", "dir")

  /**
   * Settings from the Hadoop configuration, or None when no STS endpoint is configured.
   * The vendor's own IAM user signs with the default AWS credential chain, the same
   * `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` the presigner already reads.
   */
  def fromHadoop(
      conf: Configuration,
      credentialsProvider: => AWSCredentialsProvider =
        DefaultAWSCredentialsProviderChain.getInstance()): Option[ScalityStsSettings] = {
    val raw = Option(conf.getTrimmed(EndpointKey)).getOrElse("")
    if (raw.isEmpty) {
      None
    } else {
      val endpoint = if (raw.contains("://")) {
        raw
      } else {
        // Same rule as S3A for a scheme-less fs.s3a.endpoint.
        val tls = conf.getBoolean("fs.s3a.connection.ssl.enabled", true)
        (if (tls) "https://" else "http://") + raw
      }
      val region = Option(conf.getTrimmed(RegionKey)).filter(_.nonEmpty)
        .orElse(Option(conf.getTrimmed("fs.s3a.endpoint.region")).filter(_.nonEmpty))
        .getOrElse("us-east-1")
      import scala.collection.JavaConverters._
      val roles = conf.getPropsWithPrefix(RolePrefix).asScala.toMap
        .map { case (k, v) => (k.trim, Option(v).map(_.trim).getOrElse("")) }
        .filter { case (k, v) => k.nonEmpty && v.nonEmpty }
      Some(ScalityStsSettings(
        endpoint = endpoint,
        region = region,
        roles = normaliseRoles(roles),
        sendSessionPolicy = conf.getBoolean(SessionPolicyKey, false),
        credentialsProvider = credentialsProvider))
    }
  }

  def norm(name: String): String = name.toLowerCase(Locale.ROOT)

  /**
   * Role keys lower-cased. Two keys that differ only in case would make the role depend
   * on how a request spells the name, so they refuse to load.
   */
  def normaliseRoles(raw: Map[String, String]): Map[String, String] = {
    val grouped = raw.toSeq.groupBy { case (k, _) => norm(k) }
    val clashes = grouped.filter(_._2.size > 1).values.map(_.map(_._1).sorted.mkString(", "))
    if (clashes.nonEmpty) {
      throw new IllegalArgumentException(
        s"$RolePrefix keys differ only in case: ${clashes.toSeq.sorted.mkString("; ")}")
    }
    grouped.map { case (k, kvs) => k -> kvs.head._2 }
  }

  /** The DurationSeconds sent: the server's validity, clamped to [1, 3600]. */
  def durationSeconds(validitySeconds: Long): Int =
    math.max(1L, math.min(validitySeconds, MaxDurationSeconds.toLong)).toInt

  /** RoleSessionName allows [\w+=,.@-], 2 to 64 characters. */
  def sessionName(share: String, table: String): String = {
    val cleaned = s"delta-sharing-$share-$table".replaceAll("[^\\w+=,.@-]", "-")
    cleaned.take(64)
  }

  def buildClient(settings: ScalityStsSettings): AWSSecurityTokenService =
    AWSSecurityTokenServiceClientBuilder.standard()
      .withEndpointConfiguration(new EndpointConfiguration(settings.endpoint, settings.region))
      .withCredentials(settings.credentialsProvider)
      .build()

  /** Adds `location` and `accessModes` to a listed table when the table supports `dir`. */
  def decorateTable(table: Table, location: String, modes: Option[Seq[String]]): Table =
    modes.fold(table)(m => table.copy(location = Some(location), accessModes = m))

  /** Adds `location` and `accessModes` to the metadata line of a metadata response. */
  def decorateMetadataActions(
      actions: Seq[Object],
      location: String,
      modes: Option[Seq[String]]): Seq[Object] = modes match {
    case None => actions
    case Some(m) =>
      actions.map {
        case s: SingleAction if s.metaData != null =>
          s.copy(metaData = s.metaData.copy(location = location, accessModes = m))
        case d: DeltaResponseSingleAction if d.metaData != null =>
          d.copy(metaData = d.metaData.copy(location = location, accessModes = m))
        case other => other
      }
  }
}

/** Issues AssumeRole against the configured Scality STS, one role per share. */
class ScalityStsCredentialVendor(
    settings: ScalityStsSettings,
    client: AWSSecurityTokenService) {

  def this(settings: ScalityStsSettings) = this(settings, ScalitySts.buildClient(settings))

  /** The AssumeRole request for a table. Never carries PolicyArns. */
  def assumeRoleRequest(
      share: String,
      schema: String,
      table: String,
      location: URI,
      validitySeconds: Long): AssumeRoleRequest = {
    if (StorageScheme.fromUri(location) != StorageScheme.S3) {
      throw new DeltaSharingIllegalArgumentException(
        s"Temporary table credentials from a Scality STS cover s3a:// locations only: $location")
    }
    val roleArn = settings.roleFor(share, schema, table).getOrElse {
      throw new DeltaSharingIllegalArgumentException(
        s"Temporary table credentials are not configured for table: $share.$schema.$table")
    }
    val request = new AssumeRoleRequest()
      .withRoleArn(roleArn)
      .withRoleSessionName(ScalitySts.sessionName(share, table))
      .withDurationSeconds(ScalitySts.durationSeconds(validitySeconds))
    if (settings.sendSessionPolicy) {
      // Opt-in only. RING STS accepts an inline Policy and ignores it: the role is the scope.
      val bucket = location.getHost
      val rawPath = Option(location.getPath).getOrElse("").stripPrefix("/")
      val resourcePrefix = if (rawPath.isEmpty) "" else rawPath.stripSuffix("/") + "/*"
      request.withPolicy(AwsPolicyGenerator.sessionPolicy(bucket, resourcePrefix))
    }
    request
  }

  def vend(
      share: String,
      schema: String,
      table: String,
      location: URI,
      validitySeconds: Long): TemporaryCredentials = {
    val request = assumeRoleRequest(share, schema, table, location, validitySeconds)
    val issuedAt = System.currentTimeMillis()
    val c = client.assumeRole(request).getCredentials
    val expirationTime = Option(c.getExpiration).map(_.getTime)
      .getOrElse(issuedAt + request.getDurationSeconds.longValue() * 1000L)
    TemporaryCredentials(Credentials(
      location = location.toString,
      awsTempCredentials = AwsTempCredentials(
        accessKeyId = c.getAccessKeyId,
        secretAccessKey = c.getSecretAccessKey,
        sessionToken = c.getSessionToken),
      expirationTime = expirationTime))
  }
}
