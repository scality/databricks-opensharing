#!/usr/bin/env bash
# End-to-end test of the setup image against a Scality CloudServer — the S3 service in
# RING and ARTESCA — first behind a self-signed CA, then over plain HTTP.
#
# What it proves, in order: the page accepts a private CA and builds the truststore, the
# JVM reads it, the S3 host is a registered rest-endpoint (the 403 probe), the bucket is
# browsed and the Delta table discovered, the server starts on the rendered config, every
# gate passes (presigned URL on the right host, PAR1, unsigned fetch refused), the .share
# file is handed out, a container restart resumes the server from /config and reads
# never_verified until Verify is pressed, and the same flow passes in http mode.
#
# Needs: docker, openssl, curl, python3. Usage: setup/ci/integration.sh <setup image>
set -euo pipefail

IMAGE="${1:?setup image tag}"
PLATFORM="${PLATFORM:---platform=linux/amd64}"
NET="dsci-$$"
WORK="$(mktemp -d)"
ACCESS_KEY="cisetupaccesskey"
SECRET_KEY="cisetupsecretkey0123456789"
HOST_PORT="${HOST_PORT:-18088}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE="$HERE/../tests/fixtures/delta/customers"

log() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
  set +e
  for c in setup cloudserver tls helper; do docker rm -f "dsci-$c-$$" >/dev/null 2>&1; done
  docker volume rm "dsci-config-$$" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── certificates ─────────────────────────────────────────────────────────────
log "self-signed CA and a leaf for s3.local.test"
mkdir -p "$WORK/tls"
# A CA without basicConstraints and keyUsage is refused by current OpenSSL ("CA cert
# does not include key usage extension"); a real corporate CA carries both.
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=Integration test CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$WORK/tls/ca.key" -out "$WORK/tls/ca.pem" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj "/CN=s3.local.test" \
  -keyout "$WORK/tls/server.key" -out "$WORK/tls/server.csr" >/dev/null 2>&1
printf 'subjectAltName=DNS:s3.local.test\n' > "$WORK/tls/san.ext"
openssl x509 -req -in "$WORK/tls/server.csr" -CA "$WORK/tls/ca.pem" -CAkey "$WORK/tls/ca.key" \
  -CAcreateserial -days 2 -extfile "$WORK/tls/san.ext" -out "$WORK/tls/server.crt" >/dev/null 2>&1
chmod 644 "$WORK/tls/"*

# ── storage ──────────────────────────────────────────────────────────────────
log "CloudServer with s3.local.test registered as a rest-endpoint, nginx TLS in front"
docker network create "$NET" >/dev/null
docker run -d --name "dsci-cloudserver-$$" $PLATFORM --network "$NET" --network-alias cloudserver \
  -e SCALITY_ACCESS_KEY_ID="$ACCESS_KEY" -e SCALITY_SECRET_ACCESS_KEY="$SECRET_KEY" \
  -e ENDPOINT=s3.local.test -e REMOTE_MANAGEMENT_DISABLE=1 -e S3BACKEND=mem \
  zenko/cloudserver:latest >/dev/null
docker run -d --name "dsci-tls-$$" --network "$NET" --network-alias s3.local.test \
  -v "$WORK/tls:/tls:ro" -v "$HERE/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine >/dev/null

# A helper container on the same network does the S3 writes and the assertions that
# must originate from inside the network (the page is only published to the host).
docker run -d --name "dsci-helper-$$" --network "$NET" -v "$WORK/tls:/tls:ro" \
  -v "$FIXTURE:/fixture:ro" python:3.12-slim sleep 3600 >/dev/null

log "waiting for CloudServer"
for i in $(seq 1 60); do
  code="$(docker exec "dsci-helper-$$" python3 -c '
import urllib.request, ssl
ctx = ssl.create_default_context(cafile="/tls/ca.pem")
try:
    urllib.request.urlopen("https://s3.local.test/", context=ctx, timeout=3)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)' 2>/dev/null || echo 0)"
  [ "$code" = "403" ] && break
  sleep 2
done
[ "$code" = "403" ] || fail "storage never answered 403 on the anonymous probe (got $code)"
echo "anonymous GET / -> 403: registered rest-endpoint"

# ── bucket + table (stdlib SigV4 from the helper, so no SDK checksum surprises) ──
log "bucket delta-share, Delta table uploaded from the committed fixture"
docker cp "$HERE/../s3.py" "dsci-helper-$$:/tmp/s3.py"
docker exec -i -e AK="$ACCESS_KEY" -e SK="$SECRET_KEY" "dsci-helper-$$" python3 - <<'PY'
import os, ssl, sys, urllib.request, hashlib
sys.path.insert(0, "/tmp")
import s3
cfg = {"s3_endpoint": "https://s3.local.test", "bucket": "delta-share",
       "access_key": os.environ["AK"], "secret_key": os.environ["SK"], "region": "us-east-1"}
ctx = ssl.create_default_context(cafile="/tls/ca.pem")
def put(path, body):
    url = "https://s3.local.test/delta-share" + path
    h = s3.sign_v4("PUT", url, {"content-type": "application/octet-stream"},
                   hashlib.sha256(body).hexdigest(), cfg["access_key"], cfg["secret_key"], cfg["region"])
    req = urllib.request.Request(url, data=body, method="PUT", headers=h)
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        assert r.status in (200, 201), r.status
put("", b"")
for root, _, files in os.walk("/fixture"):
    for f in files:
        p = os.path.join(root, f)
        key = "/opensharing-poc/customers/" + os.path.relpath(p, "/fixture")
        put(key, open(p, "rb").read())
        print("uploaded", key)
PY

# ── the setup image ──────────────────────────────────────────────────────────
run_setup() {
  docker rm -f "dsci-setup-$$" >/dev/null 2>&1 || true
  docker run -d --name "dsci-setup-$$" $PLATFORM --network "$NET" \
    -p "127.0.0.1:${HOST_PORT}:8088" -v "dsci-config-$$:/config" "$IMAGE" >/dev/null
}
wait_page() {
  for i in $(seq 1 40); do
    curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/" 2>/dev/null | grep -q 200 && return 0
    sleep 1
  done
  fail "the page never answered 200 on 127.0.0.1:${HOST_PORT}"
}
api() {  # api METHOD PATH [JSON]
  local m="$1" p="$2" d="${3:-}"
  if [ -n "$d" ]; then
    curl -sS -b "$WORK/cookies" -c "$WORK/cookies" -X "$m" -H 'Content-Type: application/json' \
      --data-binary "$d" "http://127.0.0.1:${HOST_PORT}$p"
  else
    curl -sS -b "$WORK/cookies" -c "$WORK/cookies" -X "$m" "http://127.0.0.1:${HOST_PORT}$p"
  fi
}
login() {
  local token
  token="$(docker logs "dsci-setup-$$" 2>&1 | sed -n 's/^Setup token: //p' | tail -1)"
  [ -n "$token" ] || fail "no setup token in the container log"
  rm -f "$WORK/cookies"
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' -c "$WORK/cookies" -X POST \
    -H 'Content-Type: application/json' --data-binary "{\"token\":\"$token\"}" \
    "http://127.0.0.1:${HOST_PORT}/api/login")"
  [ "$code" = "204" ] || fail "login returned $code"
}
state() { api GET /api/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'; }
wait_state() {  # wait_state <state> <seconds>
  local want="$1" n="$2" s=""
  for i in $(seq 1 "$n"); do s="$(state)"; [ "$s" = "$want" ] && return 0; sleep 2; done
  echo "last status:"; api GET /api/status | python3 -m json.tool | head -60
  fail "state is '$s', wanted '$want'"
}

log "setup page: login, CA upload, browse, configure (private CA), apply"
run_setup; wait_page; login
pem="$(python3 -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read()))' "$WORK/tls/ca.pem")"
api PUT /api/ca "{\"pem\": $pem}" | grep -q '"ok": *true' || fail "CA upload refused"

cfg_base="\"platform\":\"ring\",\"access_key\":\"$ACCESS_KEY\",\"secret_key\":\"$SECRET_KEY\",\"region\":\"us-east-1\",\"bucket\":\"delta-share\",\"share_public_url\":\"\""
# A first PUT with credentials but no tables is expected to report a problem — it is what
# lets /api/browse run with those credentials.
api PUT /api/config "{${cfg_base},\"endpoint_mode\":\"private_ca\",\"s3_endpoint\":\"https://s3.local.test\",\"tables\":[]}" >/dev/null
browse="$(api GET '/api/browse?prefix=')"
echo "$browse" | grep -q '"prefix": *"opensharing-poc/customers"' || { echo "$browse"; fail "browse did not discover the fixture table"; }
echo "browse discovered opensharing-poc/customers"

tables='[{"prefix":"opensharing-poc/customers","share":"scality","schema":"poc","table":"customers"}]'
out="$(api PUT /api/config "{${cfg_base},\"endpoint_mode\":\"private_ca\",\"s3_endpoint\":\"https://s3.local.test\",\"tables\":$tables}")"
echo "$out" | grep -q '"ok": *true' || { echo "$out"; fail "config refused"; }
echo "$out" | grep -q '"rest_endpoint_registered", *"result": *"pass"' || { echo "$out"; fail "rest-endpoint precheck did not pass"; }
echo "$out" | grep -q '"bucket_listable", *"result": *"pass"' || { echo "$out"; fail "bucket precheck did not pass"; }

api POST /api/apply >"$WORK/apply.json" || true
wait_state verified 120
echo "verified after apply"
api GET /api/profile | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["bearerToken"] and d["endpoint"].endswith("/delta-sharing"), d; print("profile:", d["endpoint"])'

log "restart: the server resumes from /config and the state is never_verified"
docker restart "dsci-setup-$$" >/dev/null; wait_page; login
wait_state never_verified 60
api GET /api/status | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["config"]["bucket"]=="delta-share" and len(d["config"]["tables"])==1, d["config"]; print("config resumed:", d["config"]["bucket"], d["config"]["tables"])'
api POST /api/verify >/dev/null || true
wait_state verified 120
echo "verified after restart + verify"

# ── plain http, straight at CloudServer ──────────────────────────────────────
log "http mode against cloudserver:8000"
docker volume rm -f "dsci-config-$$" >/dev/null 2>&1 || true
docker rm -f "dsci-setup-$$" >/dev/null 2>&1 || true
docker volume create "dsci-config-$$" >/dev/null
# CloudServer registered s3.local.test only; give that name to the plain-HTTP path too.
docker network disconnect "$NET" "dsci-tls-$$"
docker network disconnect "$NET" "dsci-cloudserver-$$"
docker network connect --alias s3.local.test --alias cloudserver "$NET" "dsci-cloudserver-$$"
run_setup; wait_page; login
out="$(api PUT /api/config "{${cfg_base},\"endpoint_mode\":\"http\",\"s3_endpoint\":\"http://s3.local.test:8000\",\"tables\":$tables}")"
echo "$out" | grep -q '"ok": *true' || { echo "$out"; fail "http config refused"; }
echo "$out" | grep -q '"rest_endpoint_registered", *"result": *"pass"' || { echo "$out"; fail "http rest-endpoint precheck did not pass"; }
echo "$out" | grep -q '"bucket_listable", *"result": *"pass"' || { echo "$out"; fail "http bucket precheck did not pass"; }
echo "$out" | grep -q 'Plain HTTP' || { echo "$out"; fail "http mode did not warn"; }
api POST /api/apply >/dev/null || true
wait_state verified 120
docker exec "dsci-setup-$$" grep -q '<name>fs.s3a.connection.ssl.enabled</name><value>false</value>' /config/core-site.xml \
  || docker exec "dsci-setup-$$" sh -c 'grep -A1 ssl.enabled /config/core-site.xml' | grep -q false \
  || fail "ssl.enabled=false not rendered in http mode"
echo "verified in http mode"

log "ALL PASSED"
