"""The support bundle: everything a Scality engineer needs to read a failure,
with every credential masked before the archive is built.

An operator whose deployment will not verify is otherwise asked to hand-copy a
log tail out of a browser, and the two files that hold the answer are the two
that hold the S3 secret key and the bearer token — so they are the two nobody
should be asked to attach. This module produces one gzip tar that carries the
whole of `server.log` rather than the tail the page shows, both rendered files
with their credential values replaced, and the status the page was displaying.

Two rules the rest of this file exists to keep. Every text member goes through
`redact` on the way in, not on the way out: an archive is written once and read
somewhere else, so a secret that reaches the tar is already gone. And a missing
file is skipped rather than fatal — a bundle is asked for when something is
broken, and refusing to produce one because `ca.pem` is absent would withhold
exactly the evidence the request was made for.
"""
import gzip
import io
import json
import os
import re
import tarfile

import persist
import render
import tls
import supervise

MASK = "«redacted»"

# The two credential values in `core-site.xml`. Matched as name/value pairs so a
# value that happens to equal a non-credential value elsewhere in the document is
# untouched, and so the document still parses afterwards.
_XML_CREDENTIAL = re.compile(
    r"(<name>(?:fs\.s3a\.secret\.key|fs\.s3a\.access\.key)</name>\s*<value>)"
    r"(.*?)(</value>)", re.S)

# The bearer token in `delta-sharing-server.yaml`, anchored to the exact line
# `render.server_yaml` emits and `render.parse_server_yaml` reads back.
_YAML_TOKEN = re.compile(r'^(  bearerToken: ").*(")$', re.M)

README = """\
Scality OpenSharing — support bundle
====================================

What this is
------------
A snapshot of one deployment's configuration, state and server log, taken by the
setup page on the host that runs the sharing server. Attach it to a support case.

Nothing was sent anywhere
-------------------------
This archive was built in the setup container and handed to the browser that
asked for it. It was not uploaded, phoned home or transmitted to Scality or to
anyone else. It reaches a support case only if you attach it yourself.

What was redacted
-----------------
Before this archive was written, every occurrence of the S3 secret key, the S3
access key and the recipient's bearer token — the current ones and those of the
previous run the server log may still carry — was replaced with the text
"%(mask)s" in every member below. In particular:

  core-site.xml               fs.s3a.secret.key and fs.s3a.access.key values
  delta-sharing-server.yaml   the bearerToken value
  server.log                  every occurrence of any of the three
  status.json                 never carried the secret key; the access key was
                              removed here as well

What is in it
-------------
  version.json                setup image and sharing server versions
  status.json                 what the page was showing when the bundle was made
  checks.txt                  the last gate suite's result, one line per check
  setup.json                  the saved configuration, as it is on disk
  core-site.xml               the S3A configuration the server reads (masked)
  delta-sharing-server.yaml   the shares, schemas, tables and token (masked)
  server.log                  the sharing server's whole log, not just the tail
  ca.pem                      the uploaded CA certificate, when one is configured
                              (public material — a certificate, not a key)

A member is absent when the file it comes from is absent.
""" % {"mask": MASK}


def _read_text(path):
    """The file's text, or None when it is not there or cannot be read. A bundle
    is requested when something is already wrong; a directory in an unexpected
    state must produce a smaller bundle, never no bundle."""
    try:
        with open(path, "rb") as handle:
            return handle.read().decode(errors="replace")
    except OSError:
        return None


def mask_core_site(text):
    """`core-site.xml` with its two credential values replaced.

    The property names, the document shape and every other value survive, so
    `render.parse_core_site` still reads the file and a reviewer can still see
    the endpoint, the region and the credentials provider — the four settings
    that are each a silent 403.
    """
    return _XML_CREDENTIAL.sub(lambda m: m.group(1) + MASK + m.group(3), text)


def mask_server_yaml(text):
    """`delta-sharing-server.yaml` with the bearer token replaced. The shares,
    schemas, tables and locations are what the file is read for and are kept."""
    return _YAML_TOKEN.sub(lambda m: m.group(1) + MASK + m.group(2), text)


def checks_text(status):
    """The last verdict as one line per check, or a sentence saying there is none.

    An empty file would read as "every check passed and printed nothing", which
    is the opposite of what an absent verdict means.
    """
    verdict = status.get("verdict") or {}
    checks = verdict.get("checks") or []
    if not checks:
        return "no verdict — nothing has been verified against this configuration\n"
    return "".join(
        "%s %s — %s\n" % (str(c.get("result", "")).upper(), c.get("id", ""),
                          c.get("detail", ""))
        for c in checks)


def status_json(status):
    """The status payload minus the S3 access key.

    The secret key is never in a status payload — `App._public_config` drops it —
    so this asserts that rather than removing it: if a change ever puts it back,
    the bundle must fail loudly here instead of shipping it in an archive whose
    own README says it was redacted.
    """
    out = dict(status)
    config = out.get("config")
    if isinstance(config, dict):
        assert "secret_key" not in config, \
            "the status payload must never carry the S3 secret key"
        config = dict(config)
        config.pop("access_key", None)
        out["config"] = config
    return json.dumps(out, indent=2, sort_keys=True) + "\n"


def build(config_dir, status, secrets, redact):
    """The bundle's bytes.

    `secrets` are the credentials known to the caller right now; `redact` is
    `Supervisor._redact_with_history`, which adds whatever the previous run held —
    the log is opened in append mode, so the run most likely to still be sitting
    in it is the one that already ended.

    Every timestamp in the archive is zero. A bundle built twice from an
    unchanged directory is then byte-identical, which is what makes "is this the
    same bundle you sent yesterday" answerable with a checksum.
    """
    secrets = [s for s in secrets if s]

    core_path = os.path.join(config_dir, render.CORE_SITE_FILE)
    yaml_path = os.path.join(config_dir, render.SERVER_YAML_FILE)
    members = [
        ("README.txt", README),
        ("version.json", json.dumps(status.get("version") or {}, indent=2,
                                    sort_keys=True) + "\n"),
        ("status.json", status_json(status)),
        ("checks.txt", checks_text(status)),
        ("setup.json", _read_text(os.path.join(config_dir, persist.SETUP_FILE))),
        (render.CORE_SITE_FILE, _mask_or_none(mask_core_site, _read_text(core_path))),
        (render.SERVER_YAML_FILE, _mask_or_none(mask_server_yaml,
                                                _read_text(yaml_path))),
        ("server.log", _read_text(os.path.join(config_dir, supervise.LOG_NAME))),
        ("ca.pem", _read_text(os.path.join(config_dir, tls.CA_FILE))),
    ]

    raw = io.BytesIO()
    # mtime=0 on the gzip header as well as on every member: the header carries
    # its own timestamp, and leaving it would make every bundle differ.
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name, text in members:
                if text is None:
                    continue
                payload = redact(text, *secrets).encode()
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mtime = 0
                info.mode = 0o600
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def _mask_or_none(mask, text):
    return None if text is None else mask(text)
