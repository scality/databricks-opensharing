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

Four buttons act on all of the above together:

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
