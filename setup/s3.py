"""S3 access with the standard library only: SigV4 signing, path-style requests,
and a breadth-first scan that recognises Delta tables by their `_delta_log/` prefix.

There is no boto3 in the image, so signing is done here. The rules that bite are all
about encoding: the canonical URI is encoded segment by segment, the canonical query
string is sorted and encoded with `/` written as %2F, and the payload hash is the
hash of the empty body because every request this module makes has no body.
"""
import datetime
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
SIGNED_HEADERS = "host;x-amz-content-sha256;x-amz-date"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# A listing page is a few hundred kilobytes at most. The cap is there so a wrong
# endpoint answering with something enormous cannot exhaust memory.
MAX_BODY = 8 * 1024 * 1024
TIMEOUT = 20

DELTA_LOG = "_delta_log/"


class S3Error(Exception):
    def __init__(self, status, code="", message=""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__("HTTP %s%s%s" % (status,
                                          " %s" % code if code else "",
                                          ": %s" % message if message else ""))


def _encode(value):
    """RFC 3986 encoding of a single query component: nothing but the unreserved
    characters survives, so `/` becomes %2F and a space becomes %20."""
    return urllib.parse.quote(str(value), safe="-_.~")


def canonical_uri(path):
    """Encode the path one segment at a time, leaving the separators alone.

    The segment is unquoted first so a path that already carries %XX escapes is not
    encoded twice.
    """
    if not path:
        return "/"
    return "/".join(_encode(urllib.parse.unquote(seg)) for seg in path.split("/"))


def canonical_query(query):
    """Sort and encode the query string. A key with an empty value keeps its `=`."""
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    pairs = sorted((_encode(k), _encode(v)) for k, v in pairs)
    return "&".join("%s=%s" % (k, v) for k, v in pairs)


def canonical_request(method, url, amz_date, payload_sha256):
    """Return (canonical_request, host). The signed headers are always the same three."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    canonical_headers = "host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n" % (
        host, payload_sha256, amz_date)
    creq = "\n".join([
        method.upper(),
        canonical_uri(parts.path),
        canonical_query(parts.query),
        canonical_headers,
        SIGNED_HEADERS,
        payload_sha256,
    ])
    return creq, host


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key, datestamp, region, service=SERVICE):
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def sign_v4(method, url, headers, payload_sha256, access_key, secret_key, region,
            now=None):
    """Return a new headers dict carrying host, x-amz-date, x-amz-content-sha256 and
    Authorization. `url` is path-style: https://host[:port]/bucket[/key]."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    creq, host = canonical_request(method, url, amz_date, payload_sha256)
    scope = "%s/%s/%s/aws4_request" % (datestamp, region, SERVICE)
    to_sign = "\n".join([
        ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(creq.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret_key, datestamp, region),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = dict(headers or {})
    out["host"] = host
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_sha256
    out["Authorization"] = (
        "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (ALGORITHM, access_key, scope, SIGNED_HEADERS, signature))
    return out


def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def _error_from_body(body):
    """Pull Code and Message out of an S3 error document, tolerating a body that is
    not XML at all — a proxy in the way answers with HTML."""
    try:
        root = ET.fromstring(body.decode("utf-8", "replace"))
    except ET.ParseError:
        return "", ""
    if _strip_ns(root.tag) != "Error":
        return "", ""
    code = message = ""
    for child in root:
        name = _strip_ns(child.tag)
        if name == "Code":
            code = (child.text or "").strip()
        elif name == "Message":
            message = (child.text or "").strip()
    return code, message


class S3Client:
    def __init__(self, cfg, ssl_ctx, opener=None):
        self.cfg = cfg
        self.ssl_ctx = ssl_ctx
        self.opener = opener or urllib.request.urlopen
        self.endpoint = str(cfg.get("s3_endpoint", "")).rstrip("/")
        self.bucket = str(cfg.get("bucket", ""))
        self.region = str(cfg.get("region", "") or "us-east-1")

    def _open(self, req):
        try:
            resp = self.opener(req, timeout=TIMEOUT, context=self.ssl_ctx)
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            code, message = _error_from_body(body or b"")
            raise S3Error(e.code, code, message)
        status = getattr(resp, "status", None)
        if status is None:
            status = resp.getcode()
        body = resp.read(MAX_BODY)
        if not 200 <= int(status) < 300:
            code, message = _error_from_body(body or b"")
            raise S3Error(int(status), code, message)
        return int(status), body

    def _signed(self, method, path, query=None):
        url = self.endpoint + path
        if query:
            url += "?" + "&".join(
                "%s=%s" % (_encode(k), _encode(v)) for k, v in sorted(query.items()))
        headers = sign_v4(method, url, {}, EMPTY_SHA256,
                          self.cfg.get("access_key", ""),
                          self.cfg.get("secret_key", ""),
                          self.region)
        req = urllib.request.Request(url, method=method, headers=headers)
        return self._open(req)

    def head_bucket(self):
        self._signed("HEAD", "/" + self.bucket)

    def list_objects_v2(self, prefix="", delimiter="/", continuation=None):
        query = {"list-type": "2"}
        if prefix:
            query["prefix"] = prefix
        if delimiter:
            query["delimiter"] = delimiter
        if continuation:
            query["continuation-token"] = continuation
        _, body = self._signed("GET", "/" + self.bucket, query)
        return self._parse_listing(body)

    @staticmethod
    def _parse_listing(body):
        root = ET.fromstring(body.decode("utf-8", "replace"))
        keys = []
        common = []
        truncated = False
        next_token = None
        for child in root:
            name = _strip_ns(child.tag)
            if name == "Contents":
                for sub in child:
                    if _strip_ns(sub.tag) == "Key" and sub.text:
                        keys.append(sub.text)
            elif name == "CommonPrefixes":
                for sub in child:
                    if _strip_ns(sub.tag) == "Prefix" and sub.text:
                        common.append(sub.text)
            elif name == "IsTruncated":
                truncated = (child.text or "").strip().lower() == "true"
            elif name == "NextContinuationToken":
                next_token = (child.text or "").strip() or None
        return keys, common, (next_token if truncated else None)

    def get(self, url):
        """Fetch a presigned URL. It carries its own signature in the query string,
        so signing it again would invalidate it."""
        req = urllib.request.Request(url, method="GET")
        _, body = self._open(req)
        return body


def sanitize_name(s):
    """Fold an S3 path segment into a share/schema/table name."""
    out = []
    for ch in str(s).lower():
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "_-")) else "_")
    name = "".join(out)
    while "__" in name:
        name = name.replace("__", "_")
    name = name.strip("_")[:64].strip("_")
    return name or "t"


def _list_all(client, prefix):
    """Every page of one prefix listing. Returns (common_prefixes, requests_made)."""
    common = []
    token = None
    made = 0
    while True:
        _, page_common, token = client.list_objects_v2(prefix=prefix, delimiter="/",
                                                       continuation=token)
        made += 1
        common.extend(page_common)
        if not token:
            return common, made


def discover_delta_tables(client, max_requests=300, max_depth=4):
    """Breadth-first scan for Delta tables.

    A prefix is a table when its immediate children include `<prefix>_delta_log/`;
    that prefix is recorded and not descended into. The bucket root counts, so a
    bucket that is itself one table is found. Returns (tables, truncated) — truncated
    is True when the request budget or the depth limit stopped the scan with prefixes
    still unexplored.
    """
    bucket = str(getattr(client, "bucket", "") or client.cfg.get("bucket", ""))
    share = sanitize_name(bucket)
    tables = []
    seen = {}
    queue = [("", 0)]
    requests_made = 0
    truncated = False

    while queue:
        prefix, depth = queue.pop(0)
        if requests_made >= max_requests:
            truncated = True
            break
        common, made = _list_all(client, prefix)
        requests_made += made
        if prefix + DELTA_LOG in common:
            tables.append(_table_entry(bucket, share, prefix, seen))
            continue
        children = sorted(p for p in common if p != prefix + DELTA_LOG)
        if not children:
            continue
        if depth + 1 > max_depth:
            truncated = True
            continue
        for child in children:
            queue.append((child, depth + 1))
    if queue:
        truncated = True
    return tables, truncated


def _table_entry(bucket, share, prefix, seen):
    clean = prefix.rstrip("/")
    segments = [s for s in clean.split("/") if s]
    # At the bucket root there is no segment to name the table after, so the bucket
    # names it.
    table = sanitize_name(segments[-1]) if segments else sanitize_name(bucket)
    schema = sanitize_name(segments[-2]) if len(segments) >= 2 else "default"
    key = (share, schema, table)
    n = seen.get(key, 0) + 1
    seen[key] = n
    if n > 1:
        table = "%s_%d" % (table, n)
    return {"prefix": clean, "share": share, "schema": schema, "table": table}
