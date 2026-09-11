"""The two files the sharing server reads, and the recipient credential.

Pure rendering: given a configuration dictionary and a bearer token, produce the
exact bytes of `core-site.xml` and `delta-sharing-server.yaml`, plus the `.share`
profile a recipient is handed. Nothing here shells out, opens a socket or
reads anything outside the destination directory it is given, which is what makes
the output testable byte for byte against goldens.

Both rendered files carry credentials — the S3 secret key in the XML, the bearer
token in the YAML — so they are created under a restrictive umask rather than
created at the caller's umask and chmod-ed a moment later.
"""
import datetime
import json
import os
import re
import secrets
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

# The server serves the share protocol under this path, listens on this port and
# signs URLs valid for this long. One definition each: the YAML the server reads
# and any URL we build for a recipient must agree, or the recipient sees a 404 it
# cannot diagnose.
ENDPOINT_PREFIX = "/delta-sharing"
SERVER_PORT = 8080
PRESIGNED_TIMEOUT_SECONDS = 3600

CORE_SITE_FILE = "core-site.xml"
SERVER_YAML_FILE = "delta-sharing-server.yaml"

# Credentials provider: the S3A filesystem must take the keys written below rather
# than walking the AWS default chain.
CREDENTIALS_PROVIDER = "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider"


def core_site_xml(cfg):
    """`core-site.xml` as the server reads it from its classpath `conf/` directory.

    Four properties here are each a silent 403 when wrong: the endpoint (the
    presigner bakes it into every URL), path-style access (Scality addresses
    buckets as endpoint/bucket/key), the signing region, and the credentials
    provider.
    """
    ssl_enabled = "false" if cfg.get("endpoint_mode") == "http" else "true"
    props = [
        ("fs.s3a.endpoint", str(cfg.get("s3_endpoint", ""))),
        ("fs.s3a.path.style.access", "true"),
        ("fs.s3a.access.key", str(cfg.get("access_key", ""))),
        ("fs.s3a.secret.key", str(cfg.get("secret_key", ""))),
        ("fs.s3a.connection.ssl.enabled", ssl_enabled),
        ("fs.s3a.endpoint.region", str(cfg.get("region", ""))),
        ("fs.s3a.paging.maximum", "1000"),
        ("fs.s3a.aws.credentials.provider", CREDENTIALS_PROVIDER),
    ]
    body = "".join(
        "  <property>\n"
        "    <name>%s</name>\n"
        "    <value>%s</value>\n"
        "  </property>\n" % (_xml_escape(name), _xml_escape(value))
        for name, value in props
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>\n'
        "<configuration>\n"
        "%s"
        "</configuration>\n" % body
    )


def table_id(bucket, prefix):
    """A stable identifier for a table, derived from where it lives.

    The protocol wants an id per table and the recipient caches against it, so it
    must not change between renders of the same configuration. A UUID5 over the
    s3a location gives that without storing a counter anywhere.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "s3a://%s/%s" % (bucket, prefix)))


def _group_tables(tables):
    """shares -> schemas -> tables, in the order each name is first seen.

    Order is part of the output contract: the rendered YAML has to be identical
    for identical input so an unchanged configuration produces an unchanged file
    and the supervisor can tell a real change from a re-render.
    """
    shares = []
    index = {}
    for entry in tables:
        share = str(entry["share"])
        schema = str(entry["schema"])
        if share not in index:
            index[share] = {}
            shares.append((share, index[share]))
        schemas = index[share]
        if schema not in schemas:
            schemas[schema] = []
        schemas[schema].append(entry)
    return [(share, list(schemas.items())) for share, schemas in shares]


def server_yaml(cfg, token):
    """`delta-sharing-server.yaml`, byte-for-byte deterministic.

    Emitted by hand rather than through a YAML library: the file is small, the
    shape is fixed, and `parse_server_yaml` below reads back exactly what this
    writes. Every scalar is double-quoted except the integers and booleans.
    """
    bucket = str(cfg.get("bucket", ""))
    lines = ["version: 1", "", "shares:"]
    for share, schemas in _group_tables(cfg.get("tables", [])):
        lines.append('  - name: "%s"' % share)
        lines.append("    schemas:")
        for schema, entries in schemas:
            lines.append('      - name: "%s"' % schema)
            lines.append("        tables:")
            for entry in entries:
                prefix = str(entry["prefix"])
                lines.append('          - name: "%s"' % entry["table"])
                lines.append('            location: "s3a://%s/%s"' % (bucket, prefix))
                lines.append('            id: "%s"' % table_id(bucket, prefix))
                lines.append("            historyShared: true")
    lines += [
        "",
        'host: "0.0.0.0"',
        "port: %d" % SERVER_PORT,
        'endpoint: "%s"' % ENDPOINT_PREFIX,
        "preSignedUrlTimeoutSeconds: %d" % PRESIGNED_TIMEOUT_SECONDS,
        "",
        "authorization:",
        '  bearerToken: "%s"' % token,
        "",
    ]
    return "\n".join(lines)


def write_config(cfg, token, dest_dir):
    """Write both files into `dest_dir`, never world-readable even momentarily."""
    previous = os.umask(0o077)
    try:
        for name, text in ((CORE_SITE_FILE, core_site_xml(cfg)),
                           (SERVER_YAML_FILE, server_yaml(cfg, token))):
            path = os.path.join(dest_dir, name)
            with open(path, "w") as handle:
                handle.write(text)
            os.chmod(path, 0o600)
    finally:
        os.umask(previous)


def new_token():
    """A fresh recipient credential: 24 bytes, 48 hex characters."""
    return secrets.token_hex(24)


def now_utc_iso():
    """The current instant, in the same ISO-8601 UTC shape as `token_expiry`.

    One format for every timestamp this tool emits, so a reader never has to work
    out which of two shapes a field is in.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_expiry(days):
    """An ISO-8601 UTC instant `days` from now.

    Nothing enforces this. The server compares one bearer token and has no expiry
    field; the reference client parses the value and sends the token regardless.
    It is emitted because the protocol asks for it and because it dates the
    credential the recipient holds — never describe it to a recipient as
    enforcement. Rotating the token is what ends a token's life.
    """
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=int(days))
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def share_endpoint(share_url):
    return "%s%s" % (str(share_url).rstrip("/"), ENDPOINT_PREFIX)


def profile_json(endpoint, token, expires):
    """The `.share` document a recipient loads.

    `expirationTime` is omitted when unknown: an empty string would read as
    "expires at the epoch", which is a worse claim than saying nothing.
    """
    doc = {"shareCredentialsVersion": 1, "endpoint": endpoint, "bearerToken": token}
    if expires:
        doc["expirationTime"] = expires
    return json.dumps(doc, indent=2)


def parse_core_site(text):
    """{property name: value} from a rendered `core-site.xml`.

    Only stdlib parsers are available here, so a document type declaration is
    refused outright rather than handed to expat: entity expansion is the one
    way a hand-edited file in the config directory could turn a parse into
    something expensive or into a file read.
    """
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("core-site.xml must not declare a document type or entities")
    root = ET.fromstring(text)
    values = {}
    for prop in root.findall("property"):
        name = prop.findtext("name")
        if name is None:
            continue
        values[name.strip()] = (prop.findtext("value") or "").strip()
    return values


_SHARE_RE = re.compile(r'^  - name: "(?P<v>[^"]*)"$')
_SCHEMA_RE = re.compile(r'^      - name: "(?P<v>[^"]*)"$')
_TABLE_RE = re.compile(r'^          - name: "(?P<v>[^"]*)"$')
_LOCATION_RE = re.compile(r'^            location: "s3a://(?P<bucket>[^/"]+)/(?P<prefix>[^"]*)"$')
_ID_RE = re.compile(r'^            id: "[^"]*"$')
_TOKEN_RE = re.compile(r'^  bearerToken: "(?P<v>[^"]*)"$')


def parse_server_yaml(text):
    """Read back exactly the format `server_yaml` emits, and nothing else.

    Not a YAML parser. This reads a file we wrote ourselves, so anything that is
    not the shape we emit is a file somebody edited by hand or a different
    document altogether — in either case reconstructing a configuration from it
    would be a guess, so it raises instead.
    """
    tables = []
    bucket = None
    token = None
    share = schema = table = None
    pending_location = False

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line == "" or line in ("version: 1", "shares:", "authorization:",
                                  'host: "0.0.0.0"', "port: %d" % SERVER_PORT,
                                  'endpoint: "%s"' % ENDPOINT_PREFIX,
                                  "preSignedUrlTimeoutSeconds: %d" % PRESIGNED_TIMEOUT_SECONDS,
                                  "    schemas:", "        tables:",
                                  "            historyShared: true"):
            continue
        match = _SHARE_RE.match(line)
        if match:
            share, schema, table = match.group("v"), None, None
            continue
        match = _SCHEMA_RE.match(line)
        if match:
            if share is None:
                raise ValueError("schema outside a share: %r" % line)
            schema, table = match.group("v"), None
            continue
        match = _TABLE_RE.match(line)
        if match:
            if share is None or schema is None:
                raise ValueError("table outside a share and schema: %r" % line)
            table = match.group("v")
            pending_location = True
            continue
        match = _LOCATION_RE.match(line)
        if match:
            if not pending_location:
                raise ValueError("location without a table: %r" % line)
            if bucket is not None and bucket != match.group("bucket"):
                raise ValueError("tables span more than one bucket")
            bucket = match.group("bucket")
            tables.append({"prefix": match.group("prefix"), "share": share,
                           "schema": schema, "table": table})
            pending_location = False
            continue
        if _ID_RE.match(line):
            continue
        match = _TOKEN_RE.match(line)
        if match:
            token = match.group("v")
            continue
        raise ValueError("unrecognised line: %r" % line)

    if pending_location:
        raise ValueError("a table has no location")
    if not tables:
        raise ValueError("no tables")
    if token is None:
        raise ValueError("no bearer token")
    return {"tables": tables, "bucket": bucket, "token": token}
