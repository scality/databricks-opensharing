"""What survives a restart, and how a running deployment is read back off disk.

Two paths. The normal one is `setup.json`, written on every apply: everything the
operator entered except the S3 secret key, which is never copied out of the file
that has to hold it. The other is `reconstruct`, which rebuilds a working
configuration from the two files the server itself reads — so a deployment whose
`setup.json` was lost, or which was configured before this tool existed, comes
back with its tables, its bucket and its bearer token intact instead of
presenting an empty form over a running server.
"""
import json
import os

import render
import state
import tls

SETUP_FILE = "setup.json"

# The one field that is never written here. It lives in core-site.xml, which the
# server has to be able to read, and copying it into a second file would be a
# second thing to protect and a second thing to forget.
SECRET_FIELD = "secret_key"


def save(config_dir, cfg, token_expires, applied_hash):
    payload = {k: v for k, v in cfg.items() if k != SECRET_FIELD}
    payload["token_expires"] = token_expires
    payload["applied_hash"] = applied_hash
    path = os.path.join(config_dir, SETUP_FILE)
    previous = os.umask(0o077)
    try:
        with open(path, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(path, 0o600)
    finally:
        os.umask(previous)


def load(config_dir):
    """The saved setup, or None when there is none that can be read.

    A file that will not parse is treated as absent: the two files the server
    reads are the authority on what is actually deployed, and `reconstruct` can
    rebuild the rest from them.
    """
    path = os.path.join(config_dir, SETUP_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _derive_endpoint_mode(config_dir, endpoint, ssl_enabled):
    if endpoint.startswith("http://"):
        return "http"
    if not ssl_enabled:
        return "http"
    if os.path.exists(os.path.join(config_dir, tls.CA_FILE)):
        return "private_ca"
    return "trusted"


def reconstruct(config_dir):
    """(cfg, token, token_expires, applied_hash) from what is on disk, or None.

    Both server files must be present. Either one alone describes a deployment
    that could not be serving, and guessing the missing half would put a
    configuration on screen that was never applied.
    """
    core_path = os.path.join(config_dir, render.CORE_SITE_FILE)
    yaml_path = os.path.join(config_dir, render.SERVER_YAML_FILE)
    if not (os.path.exists(core_path) and os.path.exists(yaml_path)):
        return None

    with open(core_path) as handle:
        properties = render.parse_core_site(handle.read())
    with open(yaml_path) as handle:
        document = render.parse_server_yaml(handle.read())

    saved = load(config_dir) or {}
    endpoint = properties.get("fs.s3a.endpoint", "")
    ssl_enabled = properties.get("fs.s3a.connection.ssl.enabled", "true").lower() != "false"

    cfg = {
        "platform": saved.get("platform") or "ring",
        "endpoint_mode": saved.get("endpoint_mode")
                         or _derive_endpoint_mode(config_dir, endpoint, ssl_enabled),
        "s3_endpoint": endpoint,
        "bucket": document["bucket"],
        "access_key": properties.get("fs.s3a.access.key", ""),
        "secret_key": properties.get("fs.s3a.secret.key", ""),
        "region": properties.get("fs.s3a.endpoint.region", ""),
        "share_public_url": saved.get("share_public_url", ""),
        "ca_pem_sha256": tls.ca_sha256(config_dir),
        "tables": document["tables"],
    }
    # The hash is recomputed from what is actually on disk rather than taken from
    # setup.json. If the two disagree — a file edited by hand between restarts —
    # the stored verdict stops matching, which reads as "not verified" rather
    # than as a pass for a configuration nobody checked.
    return cfg, document["token"], saved.get("token_expires", ""), state.config_hash(cfg)
