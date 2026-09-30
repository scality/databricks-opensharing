# Running the sharing server against Scality storage

Everything specific to serving Delta and Iceberg tables from **Scality RING** or
**Scality ARTESCA**. Upstream's [protocol spec](../../PROTOCOL.md) and connector
documentation apply unchanged.

## What is verified, and what is not

Read this row by row before you rely on it. Four of the five requirements in the
Databricks software-defined-storage blueprint are covered by an automated gate suite; the
fifth is covered by captured evidence rather than a gate, and the Databricks-side query is
not covered at all.

Everything marked tested below was last exercised against the **published image** — the one
this documentation is about, pulled anonymously from GHCR — rather than against a locally
built one. The server rows were measured on `v1.4.1-scality.1`. Every tag from
`v1.4.1-scality.2` also publishes the setup image, and CI runs its integration test against
the server built from that same tag; its rows are in the setup section below. The current
release is `v1.4.1-scality.4`. What this branch adds on top of it — non-root images, ports
9480-9482, health endpoints, JSON logs and audit events, alert rules, SBOMs — is in the
next release; those claims were verified on images built locally from the branch, and say
so.

| Claim | Status |
| --- | --- |
| Presigned URLs resolve to the Scality endpoint; the Parquet fetch returns 200 | **Tested** — ARTESCA 4.3 and Scality RING 9.5.2 |
| Bearer-token authentication enforced — no token and a wrong token both 401 | **Tested** |
| No presigned URL is minted before authorisation | **Tested** — an unauthenticated query returns no URL at all, so nothing leaks even in the error path |
| Presigned URLs are time-bounded, and the signature is load-bearing | **Tested** — the same object fetched with the query string stripped returns 403 |
| A recipient cannot resolve beyond its own share | **Tested** — unknown share and unknown table both 404 |
| An Iceberg table served alongside a Delta one, via Apache XTable | **Tested** — ARTESCA 4.3 |
| Access is auditable | **Tested** — the server writes one JSON audit event per protocol request, refusals included (locally, on the image built from this branch, and in `setup/ci/integration.sh`); the reverse proxy in front of it records the same on ARTESCA 4.3. The object fetches reach only the S3 endpoint — see "Where the audit trail is" below |
| The **reference client** reads the share end to end | **Tested** — the Linux Foundation `delta-sharing` client v1.4.2, against Scality RING and ARTESCA: profile → REST → presigned URL → Parquet → DataFrame |
| End-to-end `SELECT` from a Databricks Serverless warehouse | **Not done** |

The signature check is the one not to skip. Every other check can pass while the bucket is
simply world-readable, in which case the presigned URL proves nothing — so the suite
fetches the same object with the signature removed and requires a 403.

## What this release line does not yet do

Measured against the engineering guidelines for an ISV integration inside RING and
ARTESCA, and against the descriptor [`isv-integration.yaml`](../../isv-integration.yaml).
`isvh validate isv-integration.yaml` reports the first three items; the rest are outside
what the validator checks.

| Guideline point | Not yet | What exists |
| --- | --- | --- |
| TLS | The server serves plain HTTP; no certificate is provisioned or renewed by the integration, and no alert watches a certificate's expiry. | TLS terminated by an ingress or reverse proxy; the three S3-side TLS modes above. |
| S3 credentials | An operator types an S3 key; nothing restricts it to read and presign on the shared bucket, and nothing rotates or revokes it. | The key stays in `/config` (0600) and is masked in every log line and bundle leaving the container. |
| Handler provisioning | No product tool installs, upgrades or removes the integration. | `docker run`, the reference manifests in [`deploy/kubernetes/`](../../deploy/kubernetes/), a host service. |
| Released version referenced from product repositories | No RING, Federation or ARTESCA pin; no Solution ISO or offline bundle. | GitHub Releases with image digests; tags published once. |
| SBOM and CVE audit | No upload to a vulnerability tracker (Dependency-Track); 16 fixable Critical findings in upstream's dependency tree are waived, not fixed. | SBOMs on every Release; the grype gate; Trivy per tag and weekly. |
| Logging and SIEM | No rsyslog or LEEF output and no product log path; the audit events are JSON on stdout for a collector to ship. | One JSON audit event per protocol request, refusals included. |
| OIDC | The setup page logs in with a per-start token, not the product identity provider; it is meant for loopback or port-forward access only. | The recipient bearer token for the data plane. |
| Metrics and dashboards | No Grafana dashboard; the sharing server has no request metrics of its own (rate, errors, latency). | Setup-image metrics on 9482, `ServiceMonitor`, tested alert rules and `PrometheusRule`. |
| Sizing | CPU and memory figures are lab values, not a measurement. | Requests and limits declared in the reference manifests. |
| Product documentation | No page in the RING or ARTESCA documentation. | This page, [`setup/README.md`](../../setup/README.md), [`monitoring.md`](monitoring.md), and draft port rows in [`port-doc-rows.rst`](port-doc-rows.rst). |
| Testing | No run on a product nightly and no N → N+1 upgrade test; the Databricks Serverless `SELECT` is not done. | Unit suites, the CloudServer integration test in CI, the reference-client check by hand. |

## Where the audit trail is

**The server writes one audit event per request to the share protocol**, refusals
included, as a JSON line on stdout. Every other log line is JSON too, one object per line
(`time`, `level`, `logger`, `thread`, `message`, and `exception` for a stack trace), so a
log collector needs no parser.

An audit event is on the logger `io.delta.sharing.audit`:

```json
{"time":"2026-09-30T22:54:07.097Z","level":"INFO","logger":"io.delta.sharing.audit","thread":"armeria-common-worker-nio-2-4","type":"audit","principal":"recipient:57c69531b610","sourceIp":"192.168.215.1","action":"table.query","resource":"s/c/t","share":"s","schema":"c","table":"t","method":"POST","path":"/delta-sharing/shares/s/schemas/c/tables/t/query","requestId":"a34c615453433301","status":404,"result":"not_found","durationMs":34,"message":"table.query not_found"}
```

| Field | Meaning |
| --- | --- |
| `time` | when the request arrived (ISO-8601, UTC) |
| `principal` | `recipient:<first 12 hex of SHA-256 of the token>` for the configured bearer token; `invalid-token`, `anonymous` (no token) or `unauthenticated` (no authorization configured). The token itself is never logged. |
| `sourceIp`, `forwardedFor` | the peer address; the `X-Forwarded-For` header as received, when present (untrusted: whoever connects sets it) |
| `action` | `share.list`, `share.get`, `schema.list`, `table.list`, `table.list-all`, `table.version`, `table.metadata`, `table.query`, `table.query-status`, `table.changes`, `table.credentials`, or `unknown` |
| `resource`, `share`, `schema`, `table` | the names the path carries |
| `status`, `result` | the HTTP status, and `success`, `denied` (401/403), `not_found`, `rejected` (other 4xx) or `error` |
| `requestId` | the incoming `X-Request-Id` when it is well-formed (an ingress-nginx access log carries the same id), the server's own id otherwise |

`GET /healthz` is not audited. Verified locally on the image built from this branch
(2026-10-01): an unauthenticated, an authorised and a table-query request produced three
events with `denied`, `success` and `not_found`, every one of 56 log lines parsed as JSON,
and the token appeared in none of them; `setup/ci/integration.sh` asserts the same through
the setup image, where the server's output reaches the container log with every known
secret masked.

**What the server does not record: the object fetches.** A recipient reads Parquet from
the presigned URLs directly on the S3 endpoint, so those requests never reach the
server. The `table.query` event records which files were signed for whom; the fetches are
in the S3 endpoint's own access log, or at a reverse proxy in front of it.

**A reverse proxy in front of the server is a second layer, not a requirement.** An nginx
access log in the `upstreaminfo` format records client IP, timestamp, method and path,
status, byte counts, upstream and a request id. Verified on ARTESCA 4.3: authorised
queries as 200, wrong-token requests as 401, unknown share or table as 404, signed object
fetches as 200/206, and a fetch with the signature stripped as 403. Two things that waste
time when reading it: the container's `/var/log/nginx/access.log` is usually a symlink to
`/dev/stdout`, so read it from the container's log stream rather than by exec-ing a `grep`
at that path, which blocks on the pipe; and `upstreaminfo` carries no `Host` field, so
filter on the upstream name or the request path.

If the object store's own access log is wanted, enable it explicitly: Scality CloudServer
ships its `ServerAccessLogger` disabled.

**Levels and format.** The packaged `conf/log4j.properties` sends everything at `INFO` to
stdout through `io.delta.sharing.server.scality.JsonLayout`. To change it, mount another
file and point the JVM at it:
`JAVA_TOOL_OPTIONS=-Dlog4j.configuration=file:/config/log4j.properties`.

For a support case rather than an audit, the server-side record is the setup page's
**support bundle** — the rendered configuration, the whole server log and the last check
results in one archive, with the S3 keys and the bearer token masked before it is written
(see "What to send when something fails" in [`setup/README.md`](../../setup/README.md)).

## The four things that are each a silent 403

Every one of these fails the *data* path while the *control* path keeps working, which is
what makes them hard to spot: shares, schemas and tables all list correctly, and only the
recipient's fetch of the Parquet fails.

1. **The presigner needs the endpoint.** Fixed in this fork — see the README. Without the
   fix no configuration helps.
2. **The presigner resolves credentials through the AWS default chain**, not the
   `fs.s3a.*` keys. The server process therefore needs `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY` and `AWS_REGION` **in its environment**, in addition to the keys
   in `core-site.xml`. Setting only one of the two is the most common failure.
3. **Path-style addressing.** Scality requires `endpoint/bucket/key`, not
   `bucket.endpoint/key`. Set `fs.s3a.path.style.access=true`; a virtual-hosted URL against
   a path-style-only endpoint fails as `NoSuchBucket` or a DNS error at fetch time.
4. **The signing region.** Set `fs.s3a.endpoint.region` (for example `us-east-1`); the SDK
   derives the SigV4 signing scope from it. A `SignatureDoesNotMatch` 403 on the presigned
   URL points here.

## The two configuration files

The server takes its YAML by `--config`. It reads `core-site.xml` as a **classpath
resource** from the distribution's `conf/` directory, *not* from `HADOOP_CONF_DIR` — the
launcher puts `<dist>/../conf` on the classpath. The image symlinks
`conf/core-site.xml` to `/config/core-site.xml` so a single mounted `/config` supplies
both files.

The setup page (below) renders exactly these two files from what an operator enters —
nothing more, nothing less.

`config/delta-sharing-server.yaml`:

```yaml
version: 1
shares:
  - name: "scality"
    schemas:
      - name: "poc"
        tables:
          - name: "customers"
            location: "s3a://<bucket>/opensharing-poc/customers"
            id: "00000000-0000-0000-0000-0000000000c0"
            historyShared: true
host: "0.0.0.0"
port: 9480
endpoint: "/delta-sharing"
preSignedUrlTimeoutSeconds: 3600
authorization:
  bearerToken: "<a long random string>"
```

`config/core-site.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property><name>fs.s3a.endpoint</name><value>https://s3.example.com</value></property>
  <property><name>fs.s3a.path.style.access</name><value>true</value></property>
  <property><name>fs.s3a.access.key</name><value>...</value></property>
  <property><name>fs.s3a.secret.key</name><value>...</value></property>
  <property><name>fs.s3a.endpoint.region</name><value>us-east-1</value></property>
  <property>
    <name>fs.s3a.aws.credentials.provider</name>
    <value>org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider</value>
  </property>
</configuration>
```

Both files carry credentials. Mount them read-only, keep them out of image layers, and
remove them when you tear a deployment down.

## How the server reaches the S3 endpoint

Two things about the endpoint must hold before any of the configuration above matters,
and each fails differently from the silent 403s.

### The hostname must be a registered rest-endpoint

A presigned URL's SigV4 signature covers the `Host` header, so the storage has to accept
requests addressed to the exact hostname in `fs.s3a.endpoint`. On both RING (S3
Connector) and ARTESCA that means the hostname is in CloudServer's `restEndpoints`; on
ARTESCA it is registered as an `isBuiltIn` rest-endpoint through the operator. An
unregistered host fails on the **first metadata read**, before any URL is minted, and the
S3 side answers `400 InvalidURI`.

Tell the two apart with one anonymous request:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://<fs.s3a.endpoint host>/
# 403  → registered (AccessDenied: the host resolved to a bucket namespace)
# 400  → not registered (InvalidURI)
```

### TLS: three cases

| The S3 endpoint serves | What to configure | What the setup page does |
| --- | --- | --- |
| HTTPS with a **publicly-trusted** certificate | Nothing beyond `fs.s3a.endpoint`. This is the shape a Databricks recipient needs in the end, since it fetches the presigned URLs from this host. | Nothing further to enter; the page renders `fs.s3a.endpoint` as given. |
| HTTPS with a **private or corporate CA** | The server's JVM must trust that CA — see below. The recipient must trust it too, which rules the case out for Databricks Serverless but not for a co-located client. | Accepts the CA (pasted or uploaded), builds the truststore, and sets `JAVA_TOOL_OPTIONS` on the child process — the two manual steps below, done in one form field. |
| **Plain HTTP** | `fs.s3a.endpoint` as `http://…` **and** `<property><name>fs.s3a.connection.ssl.enabled</name><value>false</value></property>` in `core-site.xml`. Lab-only: the presigned URLs are then plain HTTP as well. | Renders `fs.s3a.connection.ssl.enabled=false` and shows a standing on-screen warning; no other manual step. |

A private CA is the case that is neither documented upstream nor a silent 403: the S3A
client refuses the certificate on the first metadata read, and the failure surfaces
server-side as a `PKIX path building failed … unable to find valid certification path`
error. The fix is a truststore the JVM reads, mounted with the rest of `/config`:

```bash
# 1. Import the CA into a truststore, using the JDK inside the image so the versions match.
docker run --rm --platform linux/amd64 --entrypoint keytool \
  -v "$PWD/config:/config" ghcr.io/scality/databricks-opensharing:<release tag> \
  -importcert -noprompt -alias storage-ca -file /config/ca.pem \
  -keystore /config/truststore.jks -storepass changeit

# 2. Point the JVM at it. JAVA_TOOL_OPTIONS reaches the server process through the
#    launcher, so nothing in the image changes.
docker run -d --platform linux/amd64 -p 9480:9480 \
  -v "$PWD/config:/config:ro" --env-file aws.env \
  -e JAVA_TOOL_OPTIONS="-Djavax.net.ssl.trustStore=/config/truststore.jks -Djavax.net.ssl.trustStorePassword=changeit" \
  ghcr.io/scality/databricks-opensharing:<release tag> \
  --config /config/delta-sharing-server.yaml
```

One truststore covers both clients in the process — the Hadoop S3A filesystem that reads
the Delta log and the AWS SDK client that signs the URLs. `javax.net.ssl.trustStoreType`
does not need setting: JDK 17 sniffs the store type keytool produced. The certificate must
name the host in `fs.s3a.endpoint`, so if that is an IP address the Subject Alternative
Name must include the IP.

**How the two endpoint failures look, and why they are easy to confuse.** Both give the
recipient the same answer to `POST …/query` — `500 {"errorCode":"INTERNAL_ERROR","message":""}`
with an empty message — while `GET /shares` keeps working. Only the server log and the
anonymous probe above tell them apart:

| Cause | Recipient sees | Server log says | How long it takes |
| --- | --- | --- | --- |
| CA not trusted | `500 INTERNAL_ERROR`, empty message | `SSLHandshakeException: (certificate_unknown) PKIX path building failed … unable to find valid certification path to requested target` | **~20 minutes** — the S3A and Delta-kernel retry loops wrap one handshake failure, so the recipient's own timeout usually fires first and reads as a hang |
| Host not a registered rest-endpoint | `500 INTERNAL_ERROR`, empty message | `AWSBadRequestException: getFileStatus … Status Code: 400` — the `InvalidURI` body is swallowed | ~3 s; a 400 is not retried |

A reverse-proxy access log in front of the S3 endpoint records **nothing** for the
untrusted-CA case, because the handshake never completes: an absence, not a refusal.

Measured 2026-09-10 on the published image (`v1.4.1-scality.1`) against Scality
CloudServer — the S3 service inside both RING and ARTESCA — behind an nginx TLS front with
a self-signed CA, run locally; the three-state comparison (pass / no truststore /
unregistered host) was done on the same setup. Not yet repeated against a RING release
carrying its own certificate.

## The published image

`ghcr.io/scality/databricks-opensharing` — tagged `v<upstream>-scality.<n>`, built by
[`publish-image.yml`](../../.github/workflows/publish-image.yml) from the source in this
repository. Public: it pulls anonymously, no token needed.

**Pin a release, and preferably its digest.** Each release tag has a
[GitHub Release](https://github.com/scality/databricks-opensharing/releases) whose notes give
the registry digest of both images:

```bash
docker pull ghcr.io/scality/databricks-opensharing@sha256:<digest from the release notes>
```

- A release tag is published once. The workflow refuses a tag that either image already
  carries, or that already has a Release, before it pushes anything — so a tag keeps
  pointing at the bytes that were validated under it.
- No floating tag is moved. `latest` still exists on both packages, frozen at
  `v1.4.1-scality.4`, the last release that moved it; a deployment that pulls `latest`
  keeps getting that build and nothing newer. Name a release tag instead.

**`linux/amd64` only.** The build runs on GitHub's amd64 runners and publishes a single
architecture, so an ARM host runs it under emulation (Docker prints a platform-mismatch
warning). Fine for the x86 servers these deployments target; build locally with
`docker build` if you need a native ARM image.

## Supply chain: SBOM and CVE scanning

**Every Release carries four SBOM assets under fixed names**, generated by syft from the
published digests:

| Asset | Image | Format |
| --- | --- | --- |
| `databricks-opensharing.sbom.cdx.json` | server | CycloneDX JSON |
| `databricks-opensharing.sbom.spdx.json` | server | SPDX JSON |
| `databricks-opensharing-setup.sbom.cdx.json` | setup | CycloneDX JSON |
| `databricks-opensharing-setup.sbom.spdx.json` | setup | SPDX JSON |

```bash
gh release download <release tag> -R scality/databricks-opensharing -p 'databricks-opensharing.sbom.cdx.json'
```

**The CVE gate.** Every build — pull request, branch push and tag — scans both images
with grype before anything is pushed:

- **Fails the build:** a Critical finding with a fix available that is not waived in
  [`.grype.yaml`](../../.grype.yaml).
- **Reported, does not fail:** every other finding, counted per severity in the job
  summary ([`.github/scripts/cve-gate.py`](../../.github/scripts/cve-gate.py)).
- **Waivers** pin the package version, so a dependency change re-surfaces the finding. The
  waived findings all come from upstream delta-sharing's own dependency tree
  (`jackson-databind` 2.6.7.3, pinned in `build.sbt` because a newer
  `jackson-module-scala` breaks `delta-standalone`; Spark 2.4.7; Avro; Netty through
  Armeria 1.6.0; ZooKeeper through Hadoop 3.3.4).

Trivy, through the organisation's reusable workflow, scans each published tag and, weekly,
the newest release ([`security.yml`](../../.github/workflows/security.yml)); it reports to
the repository's Security tab and does not gate. Dependabot proposes new base-image
digests (both Dockerfiles pin `tag@sha256:…`) and action versions. sbt is not a
Dependabot ecosystem: the Maven packages found in the built server image are submitted to
the dependency graph on each push to `scality-1.4`, which is what Dependabot alerts read.

Baseline, measured 2026-10-01 with grype 0.119.0 on the published `v1.4.1-scality.4`
images (`linux/amd64`), before any change on this line:

| Image | Critical | High | Medium | Low |
| --- | --- | --- | --- | --- |
| server | 20 (16 with a fix) | 145 (140) | 269 (165) | 29 (22) |
| setup | 20 (16 with a fix) | 145 (140) | 345 (165) | 29 (22) |

The 16 fixable Criticals are the waived set. The 4 without a fix are `log4j` 1.2.17
(three) and `jackson-mapper-asl` 1.9.13.

The same scan of this branch, built locally (native `linux/arm64`, 2026-10-01), counting
findings outside the waivers:

| Image | Critical | High | Medium | Low |
| --- | --- | --- | --- | --- |
| server | 1 (0 with a fix) | 136 (133) | 226 (122) | 28 (21) |
| setup | 1 (0 with a fix) | 136 (133) | 302 (122) | 28 (21) |

The gate passes with the 16 waivers. The three `log4j` 1.2.17 Criticals are gone with
the logging backend (reload4j 1.2.25 replaces it); the remaining one is
`jackson-mapper-asl` 1.9.13, with no fix. The drop in High and Medium is the
digest-pinned, newer `eclipse-temurin:17-jre` base.

## The setup image

`ghcr.io/scality/databricks-opensharing-setup` — same tags as the server image, same
`linux/amd64`-only build, built `FROM` it by
[`setup/Dockerfile`](../../setup/Dockerfile) so both images published under one version
carry the same server build. Published by the same tag-driven workflow as the server
image, from `v1.4.1-scality.2`; `/metrics` and the support bundle from
`v1.4.1-scality.4`. The image reports its own tag and the server version at start and on
the page.

It puts a browser page on :9481, next to the server on :9480, in front of the two files
above. The
page automates the endpoint/credentials/tables/recipient decisions this document walks
through by hand, then applies the rendered configuration, starts the server, and runs
the gate suite below (steps 0–6 of "Verifying a deployment"; step 7, the reference
client, stays manual). It does not add anything the manual recipes above do not already
cover — it is the same three TLS modes, the same rest-endpoint precheck, the same
signature check — packaged so an operator fills in a form instead of hand-editing XML
and YAML. Details, the run command, the setup token, and what its checks do and do not
prove: [`setup/README.md`](../../setup/README.md).

It also exposes `GET /metrics` on the page port in Prometheus text format — state, table
count, endpoint mode and the last verdict per check, carrying no secret, token or table
name — so the deployment's health reaches a monitoring system; see
[the Metrics section](../../setup/README.md#metrics).

## Runtime: user, health endpoints, Kubernetes

**Both images run as uid/gid 1000**, set numerically (`USER 1000:1000`) so Kubernetes
`runAsNonRoot: true` can verify it. The server image writes nothing outside `/tmp`; the
setup image writes only `/config`, which it creates owned by uid 1000.

**`GET /healthz` on the server port answers `200 ok` with no token.** It is the one path
the bearer-token check exempts, matched exactly; it says nothing about shares, tables or
configuration, and every other path, unknown ones included, still answers 401 without the
token. Probe it instead of the TCP port:

```yaml
livenessProbe:
  httpGet: {path: /healthz, port: 9480}   # the server's `port:`
readinessProbe:
  httpGet: {path: /healthz, port: 9480}
```

The setup image adds `/healthz` (the setup process answers) and `/readyz` (200 only while
the sharing server is up and answers its own `/healthz`; 503 with the state otherwise) on
the page port — see [`setup/README.md`](../../setup/README.md#health-endpoints).

**Reference Kubernetes manifests** for the setup image are in
[`deploy/kubernetes/`](../../deploy/kubernetes/): non-root security context with every
capability dropped, HTTP probes, a PersistentVolumeClaim for `/config`, and a Service for
the share protocol only. Resource figures there are lab values, not a measurement.

## One server process serves one S3 endpoint

Worth knowing before designing a deployment that fronts more than one store.

Hadoop supports per-bucket S3A configuration (`fs.s3a.bucket.<name>.endpoint`) and the
filesystem honours it for metadata reads. **The presigner does not** — it reads the
global `fs.s3a.endpoint`, so every presigned URL a process emits names one host. A
config with two tables on two different endpoints therefore lists both correctly, reads
both correctly, and hands the recipient unreachable URLs for one of them.

To serve two stores, run two processes: two configurations, two ports, two hostnames.
That is a limitation of this fork rather than of the protocol, and a fix — honouring
per-bucket configuration in the presigner — would be a welcome contribution.

## Deployment profiles

| Profile | Where it runs | Notes |
| --- | --- | --- |
| Container | anywhere with a container runtime | The `docker run` in the README. Simplest, and what the published image is for. |
| Container with the setup page | anywhere with a container runtime | the setup image; renders and verifies the two files below instead of hand-editing them |
| Kubernetes | alongside ARTESCA on MetalK8s | The server image with both files as a `Secret` mounted at `/config`, or the setup image with the reference manifests in [`deploy/kubernetes/`](../../deploy/kubernetes/); expose the share protocol through the cluster ingress. |
| Host service | a RING supervisor or any host with a JDK 17 | For hosts with no container runtime: extract the distribution from the image and run `bin/delta-sharing-server` under systemd. |

The server co-deploys next to the storage, so it is **not tied to a RING or ARTESCA
release** — a customer does not wait for a version to get it.

## Reaching it from Databricks

A Databricks Serverless recipient needs to reach both the share endpoint **and** the S3
host that the presigned URLs point at, and to trust both certificates. Everything in the
sections above can pass from a client next to the storage while none of the following is
in place, so treat this as the pre-flight the network owner signs off before a
Databricks-side test is scheduled:

1. **Two public DNS names**, one for the share server and one for the S3 endpoint the
   presigned URLs will carry. The S3 name must be the value of `fs.s3a.endpoint`, and it
   must be a registered rest-endpoint (above).
2. **Publicly-trusted TLS on both.** A recipient rejects a self-signed or corporate chain.
   Let's Encrypt via cert-manager works; so does any public CA. A private CA means
   injecting it recipient-side, which is not a production path. If the endpoint is an IP
   address rather than a hostname, the certificate's Subject Alternative Name must include
   that IP.
3. **Inbound reachability from Databricks Serverless**, which leaves from published NCC
   stable egress IP ranges; allowlist them on the provider side for both hostnames.
   **NCC private endpoints do not extend to on-premises networks**, so an on-premises
   provider exposes a publicly reachable endpoint rather than planning for PrivateLink.
4. **On ARTESCA**, register the S3 host as an `isBuiltIn` CloudServer rest-endpoint.
   Otherwise the zenko-operator stands up a competing ingress whose internal CA certificate
   wins nginx's oldest-ingress-wins selection, and the recipient fails with a PKIX error
   even though the public certificate exists.
5. **A log collector reading the server's stdout** — the audit events (above) are there.
   A reverse proxy with an access log in front of the share server is a second layer.

## Writing a table to share

The server vends tables that already exist in the bucket; it writes nothing. For a first
test, [delta-rs](https://delta-io.github.io/delta-rs/) writes a Delta table straight onto
Scality storage from any laptop, with no Spark:

```bash
python3 -m venv .venv && .venv/bin/pip install deltalake pyarrow
```

```python
import pyarrow as pa
from deltalake import write_deltalake

table = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"], "amount": [10, 20, 30]})
write_deltalake(
    "s3://<bucket>/opensharing-poc/customers",
    table,
    mode="overwrite",
    storage_options={
        "AWS_ENDPOINT_URL": "https://s3.example.com",
        "AWS_ACCESS_KEY_ID": "...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_REGION": "us-east-1",
        "AWS_VIRTUAL_HOSTED_STYLE_REQUEST": "false",   # path style, as for the server
        # Private CA only:
        # "AWS_CA_BUNDLE": "/path/to/ca.pem",
    },
)
```

The `location` in `delta-sharing-server.yaml` is the same path with an `s3a://` scheme.
Two things about CloudServer, the S3 service in RING and ARTESCA, seen while writing
with it:

- delta-rs commits with a conditional `PUT` (`If-None-Match: *`). CloudServer accepts the
  write but does not enforce the precondition, so the commit succeeds and protects nothing
  against a concurrent writer. Fine for a single writer; do not rely on it for more.
- If you create the bucket with boto3 or a recent AWS CLI and every `PutObject` fails
  `503 ServiceUnavailable`, the cause is the SDK's default trailing-checksum upload. Set
  `request_checksum_calculation = "when_required"` (boto3 `Config`, or the same key in
  `~/.aws/config`). delta-rs is not affected.

## Serving Iceberg tables

The reference server reads Delta only. [Apache XTable](https://xtable.apache.org/)
(incubating) reads an Iceberg table's metadata and writes an equivalent Delta `_delta_log`
alongside the **same Parquet files** — no data is copied — and the server then vends it as
an ordinary Delta table. This mirrors Databricks' own requirement that foreign Iceberg
tables carry Delta Uniform metadata when the recipient is not an Iceberg-REST client.

Two things to know before trying it:

- **The conversion is a batch step, not a proxy.** Re-run it after each Iceberg write.
- **The source table must use the Hadoop-catalog on-disk layout** —
  `metadata/version-hint.text` plus `metadata/v<N>.metadata.json`, with `s3a://` paths
  inside. A pyiceberg `SqlCatalog` table keeps the current-metadata pointer in its SQL
  catalog row instead, so XTable's `HadoopTables.load` fails with `NoSuchTableException`.
  Create the table with Spark using a Hadoop-type Iceberg catalog.

## Verifying a deployment

Walk the protocol surface and assert the data path, in this order. A failure at any step
tells you which of the four traps above you hit. The setup page runs steps 0–6 itself, as
its Apply/Verify gate suite; step 7, reading the share with the reference client, stays
manual there too.

0. Anonymous `GET /` on the S3 endpoint host — **403** means the host is a registered
   rest-endpoint, **400** means it is not (above). Do this first: the failure it catches
   shows up at step 4 as an opaque `500 INTERNAL_ERROR`.
1. `GET /shares` with no token, and with a wrong token — both must return **401**.
2. `POST /shares/<s>/schemas/<sc>/tables/<t>/query` unauthenticated — must return **no**
   `url` field at all.
3. `GET /shares`, `…/schemas`, `…/tables` with the bearer token — the configured share,
   schema and table appear.
4. `POST …/query` with the token — each returned `file.url` host is the **Scality**
   endpoint, not `s3.amazonaws.com`.
5. `curl` one of those URLs — **200**, and the body starts with `PAR1`.
6. `curl` the same URL with the query string stripped — **403**. If it returns 200 the
   bucket is public and the signature was proving nothing.
7. Read the table with the **reference client**, which is the step curl cannot stand in
   for — it proves the profile format is accepted by the official library, that the
   client's own presigned-URL handling works, and that the bytes decode into a table:

   ```python
   import delta_sharing
   client = delta_sharing.SharingClient("recipient.share")
   print([s.name for s in client.list_shares()])
   # list_all_tables() takes NO argument in the 1.x client; passing the share raises
   # TypeError, which reads like a protocol failure and is not one.
   tables = client.list_all_tables()
   df = delta_sharing.load_as_pandas(f"recipient.share#{tables[0].share}.{tables[0].schema}.{tables[0].name}")
   print(len(df), list(df.columns))
   ```

   The client must trust the S3 endpoint's certificate too. With a private CA, point
   `SSL_CERT_FILE` at a bundle holding **both** the CA and the system roots (concatenate
   `certifi.where()` with the CA; a bare CA file breaks `pip` in the same environment).
   An untrusted CA on the client side surfaces as `FileNotFoundError: https://<s3 host>/…`
   on the presigned URL, not as a TLS error. The `delta-kernel-rust-sharing-wrapper`
   dependency has no linux/arm64 wheel, so run the client on x86-64.

⚠ **A pass at step 7 is not a Databricks-side validation.** A Databricks Serverless
recipient additionally needs its egress to reach both hostnames, Unity Catalog to
import the provider from the credential file, and `CREATE CATALOG … USING SHARE` to
resolve — none of which this exercises. Note also that Unity Catalog has **no
recipient-side `CREATE PROVIDER` SQL**: the credential file goes in through Catalog
Explorer's *Import provider*, or `POST /api/2.1/unity-catalog/providers` with
`authentication_type: TOKEN` and the profile JSON in `recipient_profile_str`.
