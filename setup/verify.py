"""The post-apply verification suite: walk the sharing protocol and prove the data path.

This is a port of the shell verifier the lab used, kept in the same order so a
failure points at the same cause. The reasoning behind each gate, in the order
they run:

Access control comes first. The blueprint a recipient platform reviews against
asks that the bearer token be enforced, that authorisation happen *before* any
URL is minted, and that a share be scoped. An unauthenticated query that answers
401 but still puts a presigned URL in the body has failed the second of those
even though it looks like it passed the first, so that one is checked on the
body, not on the status.

The control path — /shares, /schemas, /tables — is the cheap part and is mostly
there to say which of the configured names the server actually loaded.

The data path is where the two real traps live:

  * The presigned-URL host. The sharing server mints the URL through its own
    presigner, and when that presigner is not pointed at the storage endpoint the
    URL comes back addressed to s3.amazonaws.com or to an internal address. The
    recipient then fails on a host it was never told about, and the server-side
    log says nothing. So every file.url's host:port is compared against the
    configured endpoint's.

  * The world-readable bucket. A presigned URL that works proves nothing on its
    own: if the bucket is public, the signature is decoration and the deployment
    is vending nothing. The only gate that can tell the difference is the one
    that fetches the *same* object with the query string stripped and insists on
    a refusal. That is the check a partner review actually cares about, and it is
    the reason this suite fetches each object twice.

A table that is empty is not a failure and must not be reported as one — it is a
measurement that could not be taken, so its data-path gates are `unknown`.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import checks
import render
from checks import FAIL, PASS, UNKNOWN, check

_TIMEOUT = 20

# Deterministic names for the scoping probes. They only have to be names no
# sensible deployment would configure; randomising them would make a failure
# harder to reproduce for no gain.
UNKNOWN_SHARE = "no-such-share-setup-verify"
UNKNOWN_TABLE = "no-such-table-setup-verify"

# The body a client sends to read a table. No predicate, and a small limit: this
# suite wants one file back, not the table.
QUERY_BODY = {"predicateHints": [], "limitHint": 100}

EMPTY_TABLE_DETAIL = ("the query returned no files: the table is empty, so the "
                      "data path could not be exercised")
PUBLIC_OBJECT_DETAIL = ("object is readable without a signature — the bucket is "
                        "public, so the presigned URL is proving nothing")


def _default_opener(req, timeout=_TIMEOUT, context=None):
    return urllib.request.urlopen(req, timeout=timeout, context=context)


def _http(opener, url, ssl_ctx, method="GET", token=None, payload=None):
    """One request, returning (status, body) for any HTTP status.

    A 4xx is an answer here, not an exception: most of these gates are asserting
    on a refusal. Anything that is not an HTTP answer at all — a connection
    error, a TLS failure — propagates, and `gates.run_post_checks` turns it into
    a failed suite rather than a silently short result set.
    """
    headers = {}
    data = None
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = opener(req, timeout=_TIMEOUT, context=ssl_ctx)
    except urllib.error.HTTPError as e:
        # An error with no body is normal, and reading one that is not there must
        # not turn a clean refusal into an exception the caller reads as a crash.
        try:
            body = e.read()
        except Exception:
            body = b""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return int(e.code), body or b""
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return int(status), response.read()


def _status(opener, url, ssl_ctx):
    """The status of a bare anonymous GET, for the unsigned-fetch gate."""
    try:
        response = opener(url, timeout=_TIMEOUT, context=ssl_ctx)
    except urllib.error.HTTPError as e:
        return int(e.code)
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return int(status)


def _json_items(body):
    """The `items` list of a listing response, or [] if the body is not one."""
    try:
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def _names(body):
    return [str(item.get("name", "")) for item in _json_items(body)
            if isinstance(item, dict)]


def parse_ndjson(body):
    """The query response is newline-delimited JSON, one object per line.

    The first line is the protocol, the second the table metadata, and every line
    after that is one file. A line that does not parse is skipped rather than
    aborting the read: a trailing blank line is normal, and a malformed line
    would otherwise hide the files that did parse.
    """
    objects = []
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except ValueError:
            continue
    return objects


def file_urls(body):
    urls = []
    for obj in parse_ndjson(body):
        entry = obj.get("file") if isinstance(obj, dict) else None
        if isinstance(entry, dict) and entry.get("url"):
            urls.append(str(entry["url"]))
    return urls


def _label(entry):
    return "%s.%s.%s" % (entry.get("share"), entry.get("schema"), entry.get("table"))


def _host(url):
    """host:port, which is what a signature covers and what a recipient dials."""
    return urllib.parse.urlsplit(url).netloc


def _shares_in_order(tables):
    """Shares, schemas and tables in the order the configuration first names them."""
    order = []
    grouped = {}
    for entry in tables:
        share = entry.get("share")
        if share not in grouped:
            grouped[share] = []
            order.append(share)
        grouped[share].append(entry)
    return [(share, grouped[share]) for share in order]


def run(cfg, token, local_server_url, ssl_ctx, opener=None, client=None, collected=None):
    """Run every gate against the locally supervised server.

    `local_server_url` is the child this process started, not the public share
    URL: whether the outside world can reach the public URL is a different
    question and a different check. Verifying through the local address means a
    broken reverse proxy cannot make a working deployment look broken, nor the
    other way round.

    `collected` is the list the checks are appended to. Passing one in lets
    `gates.run_post_checks` keep the partial results when a gate raises — the
    difference between reporting a degraded deployment and reporting nothing.
    """
    results = collected if collected is not None else []
    opener = opener or _default_opener
    base = str(local_server_url).rstrip("/") + render.ENDPOINT_PREFIX
    tables = cfg.get("tables") or []
    expected_host = _host(str(cfg.get("s3_endpoint", "")))

    s3_client = {"value": client}

    def get_client():
        if s3_client["value"] is None:
            import s3
            s3_client["value"] = s3.S3Client(cfg, ssl_ctx)
        return s3_client["value"]

    # The storage hostname must be one the storage answers for. When it is not,
    # the failure surfaces much later as an opaque 500 on the first metadata
    # read, so it is worth one anonymous request before anything else.
    results.append(checks.precheck_rest_endpoint(str(cfg.get("s3_endpoint", "")),
                                                 ssl_ctx, opener))

    status, _ = _http(opener, base + "/shares", ssl_ctx)
    if status == 401:
        results.append(check("auth_no_token_401", PASS, "no token is refused"))
    else:
        results.append(check("auth_no_token_401", FAIL,
                             "no token returned HTTP %s, expected 401: the server is "
                             "not enforcing bearer authentication" % status))

    status, _ = _http(opener, base + "/shares", ssl_ctx, token="not-a-real-token")
    if status == 401:
        results.append(check("auth_wrong_token_401", PASS, "a wrong token is refused"))
    else:
        results.append(check("auth_wrong_token_401", FAIL,
                             "a wrong token returned HTTP %s, expected 401: tokens are "
                             "not being validated" % status))

    if tables:
        first = tables[0]
        query_path = "%s/shares/%s/schemas/%s/tables/%s/query" % (
            base, first.get("share"), first.get("schema"), first.get("table"))
        status, body = _http(opener, query_path, ssl_ctx, method="POST", payload=QUERY_BODY)
        leaked = b'"url"' in body
        if status in (401, 403) and not leaked:
            results.append(check("unauth_query_leaks_no_url", PASS,
                                 "an unauthenticated query is refused and mints no URL"))
        elif leaked:
            results.append(check("unauth_query_leaks_no_url", FAIL,
                                 "an unauthenticated query returned a presigned URL "
                                 "(HTTP %s): authorisation is not happening before the "
                                 "URL is minted" % status))
        else:
            results.append(check("unauth_query_leaks_no_url", FAIL,
                                 "an unauthenticated query returned HTTP %s, expected "
                                 "401 or 403" % status))

    for share, entries in _shares_in_order(tables):
        results.append(_listing_check(opener, base, ssl_ctx, token, share, entries))

    for entry in tables:
        _table_checks(results, opener, base, ssl_ctx, token, cfg, entry,
                      expected_host, get_client)

    status, _ = _http(opener, "%s/shares/%s/schemas" % (base, UNKNOWN_SHARE),
                      ssl_ctx, token=token)
    if status in (404, 400):
        results.append(check("unknown_share_404", PASS,
                             "an unknown share does not resolve (HTTP %s)" % status))
    else:
        results.append(check("unknown_share_404", FAIL,
                             "an unknown share returned HTTP %s, expected 404 or 400: "
                             "shares are not scoped" % status))

    if tables:
        first = tables[0]
        status, _ = _http(opener, "%s/shares/%s/schemas/%s/tables/%s/query" % (
            base, first.get("share"), first.get("schema"), UNKNOWN_TABLE),
            ssl_ctx, method="POST", token=token, payload=QUERY_BODY)
        if status in (404, 400):
            results.append(check("unknown_table_404", PASS,
                                 "an unknown table does not resolve (HTTP %s)" % status))
        else:
            results.append(check("unknown_table_404", FAIL,
                                 "an unknown table returned HTTP %s, expected 404 or "
                                 "400" % status))

    return results


def _listing_check(opener, base, ssl_ctx, token, share, entries):
    """Does the server actually serve the names this configuration asked for?

    One check per share, covering the share itself and every schema and table
    configured under it, because an operator reading a result list wants to know
    "is my share there", not to read one line per path segment.
    """
    id = "listing_%s" % share
    missing = []

    status, body = _http(opener, base + "/shares", ssl_ctx, token=token)
    if status != 200:
        return check(id, FAIL, "GET /shares returned HTTP %s" % status)
    if share not in _names(body):
        return check(id, FAIL, "share %s is not listed by the server" % share)

    schemas = []
    for entry in entries:
        if entry.get("schema") not in schemas:
            schemas.append(entry.get("schema"))

    for schema in schemas:
        status, body = _http(opener, "%s/shares/%s/schemas" % (base, share),
                             ssl_ctx, token=token)
        if status != 200:
            return check(id, FAIL, "listing the schemas of %s returned HTTP %s"
                         % (share, status))
        if schema not in _names(body):
            missing.append("schema %s" % schema)
            continue
        status, body = _http(opener, "%s/shares/%s/schemas/%s/tables"
                             % (base, share, schema), ssl_ctx, token=token)
        if status != 200:
            return check(id, FAIL, "listing the tables of %s.%s returned HTTP %s"
                         % (share, schema, status))
        served = _names(body)
        for entry in entries:
            if entry.get("schema") == schema and entry.get("table") not in served:
                missing.append("table %s.%s" % (schema, entry.get("table")))

    if missing:
        return check(id, FAIL, "not served by the server: %s" % ", ".join(missing))
    return check(id, PASS, "share, schemas and tables are all listed")


def _table_checks(results, opener, base, ssl_ctx, token, cfg, entry,
                  expected_host, get_client):
    label = _label(entry)
    url = "%s/shares/%s/schemas/%s/tables/%s/query" % (
        base, entry.get("share"), entry.get("schema"), entry.get("table"))
    status, body = _http(opener, url, ssl_ctx, method="POST", token=token,
                         payload=QUERY_BODY)

    data_path_ids = ["query_url_host_%s" % label, "parquet_par1_%s" % label,
                     "unsigned_fetch_refused_%s" % label, "url_expiry_bounded_%s" % label]

    if status != 200:
        for id in data_path_ids:
            results.append(check(id, FAIL, "the query returned HTTP %s" % status))
        return

    urls = file_urls(body)
    if not urls:
        # An empty table is a configuration an operator may have on purpose. It
        # is not evidence that the data path is broken, and reporting it as a
        # failure would send them looking for a fault that is not there.
        for id in data_path_ids:
            results.append(check(id, UNKNOWN, EMPTY_TABLE_DETAIL))
        return

    mismatched = [u for u in urls if _host(u) != expected_host]
    if mismatched:
        results.append(check(data_path_ids[0], FAIL,
                             "presigned URL host is %s, expected %s: the server's "
                             "presigner is not pointed at the configured storage "
                             "endpoint, so the recipient will dial a host it was never "
                             "given" % (_host(mismatched[0]), expected_host)))
    else:
        results.append(check(data_path_ids[0], PASS,
                             "%d presigned URL(s), all on %s" % (len(urls), expected_host)))

    first = urls[0]

    try:
        payload = get_client().get(first)
    except Exception as e:
        results.append(check(data_path_ids[1], FAIL,
                             "fetching the presigned URL failed: %s" % e))
    else:
        if payload[:4] == b"PAR1":
            results.append(check(data_path_ids[1], PASS,
                                 "the fetched object starts with the Parquet magic bytes"))
        else:
            results.append(check(data_path_ids[1], FAIL,
                                 "the fetched object does not start with PAR1: got %r"
                                 % payload[:4]))

    unsigned = first.split("?", 1)[0]
    try:
        unsigned_status = _status(opener, unsigned, ssl_ctx)
    except Exception as e:
        results.append(check(data_path_ids[2], UNKNOWN,
                             "the unsigned fetch could not be made: %s" % e))
    else:
        if unsigned_status in (403, 401):
            results.append(check(data_path_ids[2], PASS,
                                 "the unsigned fetch is refused (HTTP %s): the object "
                                 "is not public" % unsigned_status))
        elif unsigned_status == 200:
            results.append(check(data_path_ids[2], FAIL, PUBLIC_OBJECT_DETAIL))
        else:
            results.append(check(data_path_ids[2], UNKNOWN,
                                 "the unsigned fetch returned HTTP %s, which is neither "
                                 "a refusal nor a read" % unsigned_status))

    results.append(_expiry_check(data_path_ids[3], first))


def _expiry_check(id, url):
    """A signature, and a lifetime no longer than the one the server was configured for.

    Both halves matter. Without X-Amz-Signature the URL is not presigned at all,
    and without a bounded X-Amz-Expires the credential it vends never stops
    working — which is the same failure as a public bucket, arrived at slowly.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    if not query.get("X-Amz-Signature", [""])[0]:
        return check(id, FAIL, "the URL carries no X-Amz-Signature: it is not presigned")
    raw = query.get("X-Amz-Expires", [""])[0]
    if not raw:
        return check(id, FAIL, "the URL carries no X-Amz-Expires: its lifetime is unbounded")
    try:
        expires = int(raw)
    except ValueError:
        return check(id, FAIL, "X-Amz-Expires is not a number: %r" % raw)
    if expires > render.PRESIGNED_TIMEOUT_SECONDS:
        return check(id, FAIL, "X-Amz-Expires is %ss, above the %ss the server is "
                     "configured for" % (expires, render.PRESIGNED_TIMEOUT_SECONDS))
    return check(id, PASS, "signed, expiring in %ss (at most %ss)"
                 % (expires, render.PRESIGNED_TIMEOUT_SECONDS))
