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
built one. The server rows were measured on `v1.4.1-scality.1`; the setup image exists
from `v1.4.1-scality.2` and its rows are in the setup section below.

| Claim | Status |
| --- | --- |
| Presigned URLs resolve to the Scality endpoint; the Parquet fetch returns 200 | **Tested** — ARTESCA 4.3 and Scality RING 9.5.2 |
| Bearer-token authentication enforced — no token and a wrong token both 401 | **Tested** |
| No presigned URL is minted before authorisation | **Tested** — an unauthenticated query returns no URL at all, so nothing leaks even in the error path |
| Presigned URLs are time-bounded, and the signature is load-bearing | **Tested** — the same object fetched with the query string stripped returns 403 |
| A recipient cannot resolve beyond its own share | **Tested** — unknown share and unknown table both 404 |
| An Iceberg table served alongside a Delta one, via Apache XTable | **Tested** — ARTESCA 4.3 |
| Access is auditable | **Tested** — ARTESCA 4.3, at the reverse proxy in front of the server, which records grants, refusals and out-of-scope requests alike. **The server itself writes no access log** — see "Where the audit trail is" below before relying on this |
| The **reference client** reads the share end to end | **Tested** — the Linux Foundation `delta-sharing` client v1.4.2, against Scality RING and ARTESCA: profile → REST → presigned URL → Parquet → DataFrame |
| End-to-end `SELECT` from a Databricks Serverless warehouse | **Not done** |

The signature check is the one not to skip. Every other check can pass while the bucket is
simply world-readable, in which case the presigned URL proves nothing — so the suite
fetches the same object with the signature removed and requires a 403.

## Where the audit trail is

Plan for this before a deployment needs to answer "who read what, and who was refused",
because the server is not the place to look.

**The server writes no access log.** Its stdout carries startup banners, Delta-kernel
internals and stack traces. A request bearing a wrong bearer token is rejected with a 401
and leaves no entry — measured on ARTESCA 4.3, where two authorised requests plus one
rejected request produced 170 log lines, all of them kernel checkpoint output.

**Put the audit trail at the reverse proxy**, which every request crosses: the share
protocol and the presigned-object fetches both do, so one log covers both. An nginx access
log in the `upstreaminfo` format records client IP, timestamp, method and path, status,
byte counts, upstream and a request id — enough to reconstruct the authorisation decisions,
including the refusals. Verified on ARTESCA 4.3: authorised queries as 200, wrong-token
requests as 401, unknown share or table as 404, signed object fetches as 200/206, and a
fetch with the signature stripped as 403.

Two things that waste time when reading it. The container's `/var/log/nginx/access.log` is
usually a symlink to `/dev/stdout`, so read it from the container's log stream rather than
by exec-ing a `grep` at that path, which blocks on the pipe. And `upstreaminfo` carries no
`Host` field, so filtering by hostname matches nothing — filter on the upstream name or the
request path.

If the object store's own access log is wanted as a second layer, enable it explicitly:
Scality CloudServer ships its `ServerAccessLogger` disabled.

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
port: 8080
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
  -v "$PWD/config:/config" ghcr.io/scality/databricks-opensharing:latest \
  -importcert -noprompt -alias storage-ca -file /config/ca.pem \
  -keystore /config/truststore.jks -storepass changeit

# 2. Point the JVM at it. JAVA_TOOL_OPTIONS reaches the server process through the
#    launcher, so nothing in the image changes.
docker run -d --platform linux/amd64 -p 8080:8080 \
  -v "$PWD/config:/config:ro" --env-file aws.env \
  -e JAVA_TOOL_OPTIONS="-Djavax.net.ssl.trustStore=/config/truststore.jks -Djavax.net.ssl.trustStorePassword=changeit" \
  ghcr.io/scality/databricks-opensharing:latest \
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

`ghcr.io/scality/databricks-opensharing` — tagged `v<upstream>-scality.<n>` plus `latest`,
built by [`publish-image.yml`](../../.github/workflows/publish-image.yml) from the source in
this repository. Public: it pulls anonymously, no token needed.

**`linux/amd64` only.** The build runs on GitHub's amd64 runners and publishes a single
architecture, so an ARM host runs it under emulation (Docker prints a platform-mismatch
warning). Fine for the x86 servers these deployments target; build locally with
`docker build` if you need a native ARM image.

## The setup image

`ghcr.io/scality/databricks-opensharing-setup` — same tags as the server image, same
`linux/amd64`-only build, built `FROM` it by
[`setup/Dockerfile`](../../setup/Dockerfile) so both images published under one version
carry the same server build. Published by the same tag-driven workflow as the server
image, from `v1.4.1-scality.2`; the image reports its own tag and the server version at
start and on the page.

It puts a browser page on :8080's neighbour, :8088, in front of the two files above. The
page automates the endpoint/credentials/tables/recipient decisions this document walks
through by hand, then applies the rendered configuration, starts the server, and runs
the gate suite below (steps 0–6 of "Verifying a deployment"; step 7, the reference
client, stays manual). It does not add anything the manual recipes above do not already
cover — it is the same three TLS modes, the same rest-endpoint precheck, the same
signature check — packaged so an operator fills in a form instead of hand-editing XML
and YAML. Details, the run command, the setup token, and what its checks do and do not
prove: [`setup/README.md`](../../setup/README.md).

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
| Kubernetes | alongside ARTESCA on MetalK8s | Both files as a `Secret` mounted at `/config`; expose through the cluster ingress. |
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
5. **A reverse proxy with an access log in front of the share server** — the audit trail
   (above) lives there, not in the server.

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
