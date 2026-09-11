# The setup page

A container image that puts a browser page in front of the sharing server:
`ghcr.io/scality/databricks-opensharing-setup`. It renders the two configuration
files the server reads, starts the server, and runs the same gate suite described
in [`docs/scality/README.md`](../docs/scality/README.md#verifying-a-deployment) —
so a deployment is checked before a recipient is handed anything, without typing
YAML or XML by hand. Same tags as the server image (`v<upstream>-scality.<n>` and
`latest`), `linux/amd64` only, built `FROM` the server image so both carry the
same server build.

## Running it

```bash
docker run -d --platform linux/amd64 \
  -p 127.0.0.1:8088:8088 \
  -p 8080:8080 \
  -v opensharing-config:/config \
  -e SHARE_PUBLIC_URL=https://share.example.com \
  ghcr.io/scality/databricks-opensharing-setup:latest
```

`-p 8080:8080` publishes the sharing server that runs inside the same container,
next to the setup page on :8088. Replace it with whatever ingress fronts the
share endpoint in your deployment — a load balancer, a reverse proxy — as long as
it reaches the container's :8080. `SHARE_PUBLIC_URL` seeds the "Public share URL"
field on first configuration; an operator-entered value in the page always wins
over it. `/config` is a named volume so the rendered configuration, the server's
own log and any private CA survive a container replacement.

⚠ **The page binds every interface of the container.** There is no listen-address
option and no auth in front of it beyond the setup token below. The published
port *is* the access control: `-p 127.0.0.1:8088:8088` (as above) keeps the page
on the host's loopback; publishing it on `0.0.0.0` puts it on the network with no
further gate. Put the page behind SSH port-forwarding or a VPN for anything
beyond a single trusted host.

## The setup token

On first start the container prints a line to its log:

```
Setup token: 3f9a2c1e7b6d4508a9c2e1f7b3d6a904c8e1f2a5b7d3c609
```

A new token is generated each time the container starts and is never written to
disk. Read it with `docker logs <container>`, paste it into the page's login
form, and it exchanges for a session cookie (`HttpOnly`, `SameSite=Strict`, valid
12 hours). Whoever can read the container log can already reach `/config` and
its credentials, so the token controls nothing beyond that — it stops a session
being opened by whoever merely reaches the port.

## The four sections of the page

**Endpoint.** The Scality platform (RING or ARTESCA), the S3 endpoint URL, and
which of three ways it serves TLS:

| Mode | What it needs |
| --- | --- |
| Publicly trusted certificate | Nothing further. The shape a Databricks recipient needs, since it fetches presigned URLs from this host. |
| Private or corporate CA | A CA certificate, pasted or uploaded in this section. The page validates it, stores it, and rebuilds the server's Java truststore. Rules out Databricks Serverless as a recipient — it must trust the same CA — but works for a client next to the storage. |
| Plain HTTP | Nothing further; the page renders `fs.s3a.connection.ssl.enabled=false`. Presigned URLs are then plain HTTP too. Lab use only, and the page says so on screen. |

**Credentials.** Access key, secret key and bucket. The secret is never sent back
to the page once stored — the field shows as write-only, and leaving it empty on
a later edit keeps the secret already held rather than clearing it.

**Tables.** *Browse bucket* lists every prefix under the bucket root that holds a
`_delta_log/` directory — a bounded scan, so a very large bucket can report the
listing truncated, in which case a table can be added by hand instead of found
by the scan. The operator picks which discovered tables to share and names each
one's share, schema and table.

**Recipient.** The public share URL a recipient dials, which seeds the `.share`
file's `endpoint`; left empty, the file points at this host's own address. This
section also carries the bearer token's expiry, which is advisory only — nothing
enforces it, and access ends when the token is rotated, not when this date
passes.

Five buttons act on all of the above together:

- **Check** validates the form and runs a set of pre-checks that need no running
  server — the same reachability, rest-endpoint-registration and bucket
  checks described in "What the checks prove", below. It does not start
  anything.
- **Apply & start** renders `core-site.xml` and `delta-sharing-server.yaml`,
  restarts the sharing server against them, and runs the full gate suite
  (steps 0–6 of "Verifying a deployment" in `docs/scality/README.md`; step 7,
  reading the share with the reference client, stays manual). A first apply also
  mints the recipient's bearer token; a later apply against an unchanged
  configuration does not.
- **Verify** re-runs the same gate suite against the server that is already
  running, without rendering or restarting anything — the button to press after
  fixing something on the storage side. It refuses when nothing has been applied
  yet, or when the form has changed since the last apply.
- **Download .share** is enabled only once every gate in the suite has passed.
  Pressing it before that would hand a recipient a profile against a deployment
  nothing has confirmed serves data correctly.
- **Export support bundle** downloads one `.tar.gz` describing this deployment,
  for attaching to a support case. It is enabled as soon as a configuration
  exists — verified or not, since the states it is most wanted in are the ones
  that failed. See "What to send when something fails", below.

## What to send when something fails

Three things, in this order. Together they say what was configured, what the
server did with it, and which check disagreed — which is enough to answer most
failures without a screen-sharing session.

1. **The support bundle.** Press *Export support bundle* on the page. It is a
   `.tar.gz` holding `core-site.xml` and `delta-sharing-server.yaml` as rendered,
   the **whole** of `server.log` rather than the tail the page shows, `setup.json`,
   the version report, the status the page was displaying and the last gate
   suite's results one line per check. `ca.pem` is included when a private CA is
   configured — a certificate is public material.

   **What is masked.** The S3 secret key, the S3 access key and the recipient's
   bearer token are replaced with `«redacted»` everywhere in the archive,
   including inside the server log, and including the credentials of the
   *previous* run — the log is appended across restarts, so a failure that
   happened before the last restart is still in it. The two rendered files keep
   everything else: the endpoint, the region, path-style access, the credentials
   provider, and the shares, schemas, tables and locations. The bundle is built
   inside the container and downloaded by the browser that asked for it; nothing
   is uploaded anywhere, and it reaches Scality only if you attach it yourself.

2. **The output of the step-0 probe** — an anonymous `GET /` against the S3
   endpoint host:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://<s3-endpoint>/
   # 403  → the host is a registered rest-endpoint
   # 400  → it is not (InvalidURI), and nothing else can work until it is
   ```

   The page runs this itself as the rest-endpoint pre-check, but running it from
   your own shell separates "the container cannot reach the endpoint" from "the
   endpoint is not registered", which the page's single line cannot.

3. **The failing check line**, copied from the page as it reads — for example
   `FAIL signature_required — the stripped URL returned 200`. It is also in the
   bundle's `checks.txt`; quoting it in the case body is what says which failure
   the bundle is about.

## Where `/config` lives and what is in it

The named volume holds everything the server and the page need across a
restart:

| File | What it is |
| --- | --- |
| `core-site.xml` | The S3A endpoint, credentials and region the server reads. Mode `0600`. |
| `delta-sharing-server.yaml` | The shares/schemas/tables and the bearer token. Mode `0600`. |
| `setup.json` | Everything the operator entered except the secret key — read back on restart so the form is not empty over a running server. Carries no credential. |
| `ca.pem` | The uploaded CA certificate, present only in private-CA mode. |
| `truststore.jks` | The Java truststore built from `ca.pem`, present only in private-CA mode. |
| `server.log` | The sharing server's own stdout/stderr, appended across restarts. Mode `0600`, since a startup failure can echo the configuration it failed to parse. |

On a container restart the page reads `core-site.xml` and
`delta-sharing-server.yaml` back — not `setup.json` alone — and resumes the
server against whatever is already there. The state immediately after a restart
is **never verified**, even if it was verified before the restart: the gate
suite ran against a process that no longer exists, and its result does not
carry forward. Press Verify to re-establish it.

## Rotating the token

**Rotate token** mints a new bearer token, re-renders the YAML, restarts the
server, and invalidates every `.share` profile handed out before — the old
token stops working the moment the new server is up. It also invalidates the
current verdict, the same as any change that touches what was configured, so
the state reads never-verified until Verify (or another Apply) runs again.

## Metrics

`GET /metrics` on the page's port returns the Prometheus text exposition
(version 0.0.4). It answers **without a session** — a scraper holds no cookie —
and it carries nothing a scrape should not: no S3 secret, no access key, no
bearer token, no share, schema or table name, and not the S3 endpoint hostname.
Those name the customer's storage and its data, and a label value lives in the
monitoring system for as long as the series does. Everything below is a count, a
state or a check outcome.

| Series | Meaning |
| --- | --- |
| `opensharing_setup_info{image,server}` | always 1; the setup image tag and the sharing server version, to join against |
| `opensharing_state{state}` | one series per state (`unconfigured`, `never_verified`, `verified`, `degraded`, `stopped`, `failed_start`), exactly one at 1 |
| `opensharing_server_running` | 1 when the sharing server is up and serving |
| `opensharing_tables_shared` | how many tables are shared; the names are on the page, not here |
| `opensharing_endpoint_mode{mode}` | `trusted`, `private_ca` or `http`; all 0 before anything is configured |
| `opensharing_check{id,table}` | last verdict per check: 1 pass, 0 fail, −1 could not run. A check about one shared table carries `table="<position>"` — its place in the configuration, because the gate suite names those checks after the table itself |
| `opensharing_last_verdict_timestamp_seconds` | when those checks ran; absent until a verdict exists |
| `opensharing_token_expiry_timestamp_seconds` | the date stamped on the recipient token; absent when no token is minted |

`opensharing_check` is the one worth alerting on: a `0` is a gate that failed,
and a `−1` is a check that could not run, which is an absent measurement rather
than a finding — alert on the two differently or a storage blip pages as a
broken share.

⚠ **The `table` label is a position, and the page is where positions become
names.** The gate suite names a per-table check `query_url_host_<share>.<schema>.<table>`,
which is the right name on the page and the wrong one in a monitoring system, so
the metric carries `opensharing_check{id="query_url_host",table="2"}` instead —
the second table in the configuration. A check from a verdict taken before the
table list changed keeps its full id: it belongs to no current position, and a
table no longer configured is a table no longer shared.

**Scraping it.** The endpoint sits on the page port, so `-p 127.0.0.1:8088:8088`
keeps it on the host's loopback along with the page — leave it there and scrape
from a Prometheus on the same host. If a Prometheus elsewhere must reach it,
publish that port on the monitoring network and remember the page comes with it:
the token is the only thing in front of the page, so put both behind the same
network control you would have used for the page alone.

```yaml
scrape_configs:
  - job_name: opensharing
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:8088"]
```

## What the checks prove, and what they do not

Every check — the pre-checks under Check, and the gate suite under Apply/Verify
— runs **from this host**, against the sharing server's own local address, not
through whatever ingress a recipient actually dials. That is deliberate: a
broken reverse proxy in front of the share endpoint must not make a working
deployment look broken, and the other way round. It also means a pass here
does not prove a Databricks recipient can reach anything. A Databricks
Serverless recipient additionally needs its own egress to reach both hostnames
publicly, over publicly-trusted TLS — see "Reaching it from Databricks" in
`docs/scality/README.md`. Nothing in this page tests that; it is asserted, not
verified, by any state this page reports.

The rest-endpoint check is worth naming on its own: an anonymous `GET /` against
the S3 endpoint host returns 403 when the host is registered and 400 when it is
not. A 400 here means the deployment cannot work at all until the host is added
to CloudServer's `restEndpoints` — no amount of correct credentials or table
configuration fixes it.

## Private CA note

A CA certificate that carries no `basicConstraints` or `keyUsage` extension is
refused by current OpenSSL and by the truststore build here — `keytool` reports
`CA cert does not include key usage extension` — even though older tooling
accepted such a certificate. A CA generated for production use normally carries
both; if an upload is refused with that message, regenerate the CA with
`basicConstraints=critical,CA:TRUE` and `keyUsage=critical,keyCertSign,cRLSign`
rather than looking for a fault in the page.

## What is verified versus asserted

Exercised by [`setup/ci/integration.sh`](ci/integration.sh) against Scality
CloudServer, locally, as of 2026-09-11:

| Claim | Status |
| --- | --- |
| Private-CA mode: CA upload, truststore build, rest-endpoint precheck, bucket precheck, browse, apply, full gate suite passing | **Tested** |
| A container restart resumes the server from `/config` and reads `never_verified` until Verify is pressed | **Tested** |
| Plain-HTTP mode: apply and the gate suite passing, `fs.s3a.connection.ssl.enabled=false` rendered | **Tested** |
| Publicly-trusted-certificate mode | **Not covered by the integration script** — exercised only by the two other modes |
| A Databricks Serverless recipient reading a table configured through this page | **Not done** — see "What the checks prove", above |
