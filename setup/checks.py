"""Three-valued checks, and the pre-checks that need no running server.

`unknown` is a first-class result. A check that could not run must not report
pass (a false all-clear) or fail (which sends the operator chasing a
configuration error that is not there), so it says so, and the state model treats
it as not verified rather than as a finding.
"""
import re
import urllib.error
import urllib.parse
import urllib.request

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
_RESULTS = (PASS, FAIL, UNKNOWN)

MODES = ("trusted", "private_ca", "http")
PLATFORMS = ("ring", "artesca")
REQUIRED = ("platform", "endpoint_mode", "s3_endpoint", "bucket", "access_key",
            "secret_key", "region")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

HTTP_WARNING = ("Plain HTTP: presigned URLs will be plain HTTP; a Databricks "
                "recipient will reject them. Lab use only.")
NO_SHARE_URL_WARNING = ("No public share URL: the .share file will point at this "
                        "host's own address.")

_TIMEOUT = 10


def check(id, result, detail=""):
    if result not in _RESULTS:
        raise ValueError("result must be one of %r, got %r" % (_RESULTS, result))
    return {"id": id, "result": result, "detail": detail}


def _default_opener(req, timeout=_TIMEOUT, context=None):
    return urllib.request.urlopen(req, timeout=timeout, context=context)


def validate_inputs(cfg):
    """(problems, warnings) for a submitted configuration.

    A problem blocks an apply. A warning is something the operator should read
    and may still be the right choice — plain HTTP in a lab, or a deployment
    whose recipients dial this host directly.
    """
    problems, warnings = validate_storage(cfg)
    problems.extend(validate_tables(cfg))
    return problems, warnings


def validate_storage(cfg):
    """The half of validate_inputs that does not involve the table selection.

    Browsing the bucket needs exactly this half to hold and nothing more: the
    operator has to be able to list tables before any table is chosen.
    """
    problems = []
    warnings = []

    for field in REQUIRED:
        if not str(cfg.get(field, "")).strip():
            problems.append("%s is required" % field)

    platform = cfg.get("platform")
    if platform and platform not in PLATFORMS:
        problems.append("platform must be one of %s" % ", ".join(PLATFORMS))

    mode = cfg.get("endpoint_mode")
    if mode and mode not in MODES:
        problems.append("endpoint_mode must be one of %s" % ", ".join(MODES))

    endpoint = str(cfg.get("s3_endpoint", "")).strip()
    if endpoint and mode in ("trusted", "private_ca") and not endpoint.startswith("https://"):
        problems.append("s3_endpoint must start with https:// for endpoint mode %s" % mode)
    if endpoint and mode == "http" and not endpoint.startswith("http://"):
        problems.append("s3_endpoint must start with http:// for endpoint mode http")

    if mode == "private_ca" and not str(cfg.get("ca_pem_sha256", "")).strip():
        problems.append("a CA certificate must be uploaded for endpoint mode private_ca")

    share_url = str(cfg.get("share_public_url", "")).strip()
    if share_url and not (share_url.startswith("http://") or share_url.startswith("https://")):
        problems.append("share_public_url must start with http:// or https://")

    if mode == "http":
        warnings.append(HTTP_WARNING)
    if not share_url:
        warnings.append(NO_SHARE_URL_WARNING)
    return problems, warnings


def validate_tables(cfg):
    """Problems with the table selection alone."""
    problems = []
    tables = cfg.get("tables") or []
    if not tables:
        problems.append("at least one table is required")
    seen = set()
    for index, entry in enumerate(tables):
        where = "table %d" % (index + 1)
        prefix = str(entry.get("prefix", "")).strip()
        if not prefix:
            problems.append("%s: prefix is required" % where)
        else:
            if prefix.startswith("/"):
                problems.append("%s: prefix must not start with /" % where)
            if ".." in prefix:
                problems.append("%s: prefix must not contain .." % where)
            if prefix.startswith("s3://"):
                problems.append("%s: prefix is a path inside the bucket, not an s3:// URL" % where)
        for field in ("share", "schema", "table"):
            value = str(entry.get(field, ""))
            if not NAME_RE.match(value):
                problems.append("%s: %s must be 1-64 characters of letters, digits, "
                                "underscore or hyphen" % (where, field))
        key = (entry.get("share"), entry.get("schema"), entry.get("table"))
        if key in seen:
            problems.append("%s: duplicate share/schema/table %s.%s.%s"
                            % ((where,) + tuple(str(k) for k in key)))
        seen.add(key)

    return problems


def precheck_endpoint(url, ssl_ctx, opener=None):
    """Reachable, and TLS-trusted from here.

    An HTTP error status still proves the host resolved, the certificate verified
    and the request was routed — which is what this check asks. Whether the
    credentials are right is a different check.
    """
    opener = opener or _default_opener
    try:
        opener(url, timeout=_TIMEOUT, context=ssl_ctx)
        return check("endpoint_reachable", PASS, "resolved and TLS verified")
    except urllib.error.HTTPError as e:
        return check("endpoint_reachable", PASS, "reachable (HTTP %s)" % e.code)
    except Exception as e:
        return check("endpoint_reachable", FAIL, str(e))


def precheck_rest_endpoint(url, ssl_ctx, opener=None):
    """Is the endpoint hostname a registered rest-endpoint on the storage?

    A presigned URL's signature covers the Host header, so the storage must
    accept requests addressed to exactly this hostname. An unregistered host
    fails on the first metadata read, long before a URL is minted, and the
    recipient sees only an empty 500 — so it is worth one anonymous request here.
    A 403 means the host resolved into a bucket namespace and was refused for
    lack of credentials, which is the registered case.
    """
    opener = opener or _default_opener
    parts = urllib.parse.urlsplit(url)
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    try:
        response = opener(root, timeout=_TIMEOUT, context=ssl_ctx)
        status = getattr(response, "status", None) or getattr(response, "code", None)
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        return check("rest_endpoint_registered", UNKNOWN, str(e))
    if status == 403:
        return check("rest_endpoint_registered", PASS, "registered rest-endpoint")
    if status == 400:
        return check("rest_endpoint_registered", FAIL,
                     "not a registered rest-endpoint (InvalidURI): add the host to "
                     "CloudServer restEndpoints")
    return check("rest_endpoint_registered", UNKNOWN,
                 "unexpected status %s from an anonymous request" % status)


def precheck_bucket(cfg, ssl_ctx, client=None):
    """Do the credentials reach the bucket?

    The S3 client is imported here rather than at module scope so this module
    stays importable — and testable — on its own.
    """
    if client is None:
        try:
            import s3
        except ImportError as e:
            return check("bucket_listable", UNKNOWN, "no S3 client available: %s" % e)
        client = s3.S3Client(cfg, ssl_ctx)
    try:
        client.head_bucket()
        return check("bucket_listable", PASS, "credentials reach the bucket")
    except Exception as e:
        status = getattr(e, "status", None)
        if status == 403:
            return check("bucket_listable", FAIL, "credentials refused: %s" % e)
        if status == 404:
            return check("bucket_listable", FAIL, "bucket not found: %s" % e)
        return check("bucket_listable", UNKNOWN, str(e))


def precheck_share_url(url, ssl_ctx, opener=None):
    """Can a recipient reach the share endpoint at the public URL?

    Informational only, and deliberately not part of the verdict: this runs from
    inside the deployment, and whether a recipient out on the internet can reach
    the URL is not something this host can answer. A 401 is the good answer — the
    share endpoint is there and is asking for a bearer token.
    """
    import render

    if not str(url or "").strip():
        return check("share_url_reachable", UNKNOWN, "not set")
    opener = opener or _default_opener
    target = render.share_endpoint(url) + "/shares"
    try:
        response = opener(target, timeout=_TIMEOUT, context=ssl_ctx)
        status = getattr(response, "status", None) or getattr(response, "code", None)
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        return check("share_url_reachable", UNKNOWN, str(e))
    if status == 401:
        return check("share_url_reachable", PASS,
                     "share endpoint answered and asked for a bearer token")
    return check("share_url_reachable", UNKNOWN, "unexpected status %s" % status)
