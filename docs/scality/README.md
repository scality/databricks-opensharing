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
this documentation is about, `v1.4.1-scality.1`, pulled anonymously from GHCR onto the
node — rather than against a locally built one.

| Claim | Status |
| --- | --- |
| Presigned URLs resolve to the Scality endpoint; the Parquet fetch returns 200 | **Tested** — ARTESCA 4.3 and Scality RING 9.5.2 |
| Bearer-token authentication enforced — no token and a wrong token both 401 | **Tested** |
| No presigned URL is minted before authorisation | **Tested** — an unauthenticated query returns no URL at all, so nothing leaks even in the error path |
| Presigned URLs are time-bounded, and the signature is load-bearing | **Tested** — the same object fetched with the query string stripped returns 403 |
| A recipient cannot resolve beyond its own share | **Tested** — unknown share and unknown table both 404 |
| An Iceberg table served alongside a Delta one, via Apache XTable | **Tested** — ARTESCA 4.3 |
| Access is auditable | **Tested** — ARTESCA 4.3, at the reverse proxy in front of the server, which records grants, refusals and out-of-scope requests alike. **The server itself writes no access log** — see "Where the audit trail is" below before relying on this |
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
  <!-- Plain HTTP endpoint only: -->
  <!-- <property><name>fs.s3a.connection.ssl.enabled</name><value>false</value></property> -->
</configuration>
```

Both files carry credentials. Mount them read-only, keep them out of image layers, and
remove them when you tear a deployment down.

## The published image

`ghcr.io/scality/databricks-opensharing` — tagged `v<upstream>-scality.<n>` plus `latest`,
built by [`publish-image.yml`](../../.github/workflows/publish-image.yml) from the source in
this repository. Public: it pulls anonymously, no token needed.

**`linux/amd64` only.** The build runs on GitHub's amd64 runners and publishes a single
architecture, so an ARM host runs it under emulation (Docker prints a platform-mismatch
warning). Fine for the x86 servers these deployments target; build locally with
`docker build` if you need a native ARM image.

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
| Kubernetes | alongside ARTESCA on MetalK8s | Both files as a `Secret` mounted at `/config`; expose through the cluster ingress. |
| Host service | a RING supervisor or any host with a JDK 17 | For hosts with no container runtime: extract the distribution from the image and run `bin/delta-sharing-server` under systemd. |

The server co-deploys next to the storage, so it is **not tied to a RING or ARTESCA
release** — a customer does not wait for a version to get it.

## Reaching it from Databricks

A Databricks Serverless recipient needs to reach both the share endpoint **and** the S3
host that the presigned URLs point at, and to trust both certificates.

- **Publicly-trusted TLS on both.** A recipient rejects a self-signed chain. Let's Encrypt
  via cert-manager works; so does any public CA. A lab CA means injecting it recipient-side,
  which is not a production path.
- **Egress.** Databricks Serverless leaves from published NCC stable egress IP ranges;
  allowlist them on the provider side. **NCC private endpoints do not extend to
  on-premises networks**, so an on-premises provider must expose a publicly reachable,
  publicly-trusted endpoint rather than plan for PrivateLink.
- **On ARTESCA**, register the S3 host as an `isBuiltIn` CloudServer rest-endpoint.
  Otherwise the zenko-operator stands up a competing ingress whose internal CA certificate
  wins nginx's oldest-ingress-wins selection, and the recipient fails with a PKIX error
  even though the public certificate exists.
- If the endpoint is an IP address rather than a hostname, the certificate's Subject
  Alternative Name must include that IP.

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
tells you which of the four traps above you hit:

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
