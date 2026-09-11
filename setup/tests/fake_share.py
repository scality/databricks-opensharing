"""A sharing deployment made of dictionaries, so the gates can be tested offline.

`FakeDeployment` answers the requests `verify.run` makes — the anonymous probe on
the storage host, the listing and query endpoints of the sharing server, and the
unsigned fetch of a presigned object — from the same configuration the suite is
given. Every trap the gates exist to catch is a flag on the constructor, so a
test says what is wrong with the deployment rather than hand-building a response.
"""
import io
import json
import urllib.error
import urllib.parse

import render

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef"
LOCAL_SERVER_URL = "http://127.0.0.1:8080"
PARQUET = b"PAR1" + b"\x00" * 32 + b"PAR1"


class FakeResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self, *args):
        return self._body


def _error(url, status, body=b""):
    # No file object when there is no body: an unread HTTPError holding one warns
    # about an unclosed resource, which buries the test output in noise.
    return urllib.error.HTTPError(url, status, "", {}, io.BytesIO(body) if body else None)


def presigned_url(cfg, entry, expires=render.PRESIGNED_TIMEOUT_SECONDS,
                  signature="1f2e3d4c", host=None, part=0):
    """A URL shaped like the one the sharing server's presigner mints."""
    query = [
        "X-Amz-Algorithm=AWS4-HMAC-SHA256",
        "X-Amz-Credential=AKIAEXAMPLE%2F20260101%2Fus-east-1%2Fs3%2Faws4_request",
        "X-Amz-Date=20260101T000000Z",
        "X-Amz-SignedHeaders=host",
    ]
    if expires is not None:
        query.append("X-Amz-Expires=%s" % expires)
    if signature:
        query.append("X-Amz-Signature=%s" % signature)
    endpoint = host or cfg["s3_endpoint"]
    return "%s/%s/%s/part-%05d.snappy.parquet?%s" % (
        endpoint.rstrip("/"), cfg["bucket"], entry["prefix"], part, "&".join(query))


class FakeDeployment:
    """An opener: `opener(req, timeout=..., context=...)`.

    Flags, each of which is one of the failures a gate is there to notice:
      rest_endpoint_status   the anonymous probe on the storage host
      shares_open            GET /shares answers 200 with no token
      unsigned_status        the status of a fetch with the query string stripped
      unknown_share_status   what an unknown share name resolves to
      empty_tables           labels whose query returns a protocol and no files
      url_host               a host the presigner wrongly mints URLs for
      expires / signature    the lifetime and signature carried in the URL
      raise_on_query         labels whose query raises instead of answering
    """

    def __init__(self, cfg, token=TOKEN, rest_endpoint_status=403, shares_open=False,
                 unsigned_status=403, unknown_share_status=404, unknown_table_status=404,
                 empty_tables=(), url_host=None,
                 expires=render.PRESIGNED_TIMEOUT_SECONDS, signature="1f2e3d4c",
                 raise_on_query=()):
        self.cfg = cfg
        self.token = token
        self.rest_endpoint_status = rest_endpoint_status
        self.shares_open = shares_open
        self.unsigned_status = unsigned_status
        self.unknown_share_status = unknown_share_status
        self.unknown_table_status = unknown_table_status
        self.empty_tables = set(empty_tables)
        self.url_host = url_host
        self.expires = expires
        self.signature = signature
        self.raise_on_query = set(raise_on_query)
        self.contexts = []
        self.requests = []
        self.base = LOCAL_SERVER_URL + render.ENDPOINT_PREFIX
        self.storage_root = urllib.parse.urlunsplit(
            urllib.parse.urlsplit(cfg["s3_endpoint"])[:2] + ("/", "", ""))

    # -- the tables this deployment serves ------------------------------------

    def _entries(self):
        return self.cfg.get("tables") or []

    def _find(self, share, schema, table):
        for entry in self._entries():
            if (entry["share"], entry["schema"], entry["table"]) == (share, schema, table):
                return entry
        return None

    def urls_for(self, entry):
        label = "%s.%s.%s" % (entry["share"], entry["schema"], entry["table"])
        if label in self.empty_tables:
            return []
        return [presigned_url(self.cfg, entry, expires=self.expires,
                              signature=self.signature, host=self.url_host)]

    def _query_body(self, entry):
        lines = [json.dumps({"protocol": {"minReaderVersion": 1}}),
                 json.dumps({"metaData": {"id": "abc", "format": {"provider": "parquet"}}})]
        for index, url in enumerate(self.urls_for(entry)):
            lines.append(json.dumps({"file": {"url": url, "id": "f%d" % index,
                                              "expirationTimestamp": 1893456000000}}))
        return ("\n".join(lines) + "\n").encode()

    # -- the opener -----------------------------------------------------------

    def __call__(self, req, timeout=None, context=None):
        self.contexts.append(context)
        if isinstance(req, str):
            url, method, auth = req, "GET", None
        else:
            url, method = req.full_url, req.get_method()
            auth = req.get_header("Authorization")
        self.requests.append((method, url, auth))

        if url == self.storage_root:
            return self._answer(url, self.rest_endpoint_status, b"")
        if url.startswith(self.base):
            return self._share_api(url, method, auth)
        # Anything else is an object on the storage. Without a query string it is
        # the unsigned fetch, which is the one the gate is asking about.
        if "?" not in url:
            return self._answer(url, self.unsigned_status, b"")
        return self._answer(url, 200, PARQUET)

    def _share_api(self, url, method, auth):
        path = urllib.parse.urlsplit(url[len(self.base):]).path
        authenticated = auth == "Bearer %s" % self.token
        if not authenticated and not (self.shares_open and path == "/shares"):
            return self._answer(url, 401, b'{"errorCode":"UNAUTHORIZED"}')

        parts = [p for p in path.split("/") if p]
        if parts == ["shares"]:
            shares = []
            for entry in self._entries():
                if entry["share"] not in shares:
                    shares.append(entry["share"])
            return self._answer(url, 200, self._items(shares))

        if len(parts) == 3 and parts[0] == "shares" and parts[2] == "schemas":
            share = parts[1]
            schemas = [e["schema"] for e in self._entries() if e["share"] == share]
            if not schemas:
                return self._answer(url, self.unknown_share_status, b"")
            return self._answer(url, 200, self._items(sorted(set(schemas))))

        if len(parts) == 5 and parts[2] == "schemas" and parts[4] == "tables":
            share, schema = parts[1], parts[3]
            tables = [e["table"] for e in self._entries()
                      if e["share"] == share and e["schema"] == schema]
            if not tables:
                return self._answer(url, 404, b"")
            return self._answer(url, 200, self._items(tables))

        if len(parts) == 7 and parts[6] == "query" and method == "POST":
            share, schema, table = parts[1], parts[3], parts[5]
            label = "%s.%s.%s" % (share, schema, table)
            if label in self.raise_on_query:
                raise urllib.error.URLError("connection reset while querying %s" % label)
            entry = self._find(share, schema, table)
            if entry is None:
                return self._answer(url, self.unknown_table_status, b"")
            return self._answer(url, 200, self._query_body(entry))

        return self._answer(url, 404, b"")

    @staticmethod
    def _items(names):
        return json.dumps({"items": [{"name": n} for n in names]}).encode()

    @staticmethod
    def _answer(url, status, body):
        if 200 <= status < 300:
            return FakeResponse(status, body)
        raise _error(url, status, body)


class FakeS3Client:
    """Stands in for `s3.S3Client`: the gates only ever call `.get(url)`."""

    def __init__(self, payload=PARQUET, error=None):
        self.payload = payload
        self.error = error
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.payload
