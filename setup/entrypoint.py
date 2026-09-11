"""Container entrypoint: serve the page and the API, print the setup token once.

The HTTP layer and nothing else. Every decision about what a request means lives
in app.py; this file turns paths into calls, enforces the session, and decides
which status code a result deserves.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import metrics
import persist
from app import App
from auth import Auth
from state import (DEGRADED, FAILED_START, NEVER_VERIFIED, STOPPED, UNCONFIGURED,
                   VERIFIED)
from supervise import Supervisor

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
BIND = os.environ.get("SETUP_BIND", "0.0.0.0")
PORT = int(os.environ.get("SETUP_PORT", "8088"))
SHARE_URL = os.environ.get("SHARE_PUBLIC_URL", "")
LAUNCHER = ["/opt/delta-sharing-server/bin/delta-sharing-server",
            "--config", os.path.join(CONFIG_DIR, "delta-sharing-server.yaml")]

COOKIE_NAME = "opensharing_session"
PROTECTED_PREFIX = "/api/"
# The paths reachable without a session: the login exchange, which is how a
# session is obtained, and the metrics exposition, which a scraper reaches with no
# way to hold a cookie. Everything metrics.py renders is a count, a state or a
# check outcome — no secret, no token, no table name — so the port that publishes
# the page is the only access control it needs, exactly as for the page itself.
PUBLIC_API = ("/api/login", "/metrics")

CONTENT_TYPES = {".js": "text/javascript", ".css": "text/css",
                 ".html": "text/html", ".svg": "image/svg+xml"}


def is_public(path):
    """True for a path that needs no session. Only the login exchange qualifies."""
    return path in PUBLIC_API


def session_from_cookie(header):
    """Pull our session id out of a Cookie header, or None.

    Tolerates a header carrying several cookies, and returns None for absent,
    empty or malformed input rather than raising — an unparseable cookie is
    simply not a session.
    """
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value
    return None


# One sentence per state. never_verified and degraded must not read alike: one is
# an absent measurement, the other is a finding, and rendering them the same way
# is how a profile from a broken share gets handed over.
STATE_COPY = {
    UNCONFIGURED: "Not configured yet. Fill in the fields and press Check.",
    NEVER_VERIFIED: "Serving, but no check has been run against this "
                    "configuration — press Verify.",
    DEGRADED: "Serving, and at least one check failed — do not hand over a profile.",
    VERIFIED: "Serving, and every check passed.",
    STOPPED: "Configured, but the sharing server is not running.",
    FAILED_START: "The sharing server would not start. The previous configuration "
                  "has been restored.",
}


def boot(app, sup, config_dir):
    """Adopt a deployment that is already on disk, or report that there is none.

    Returns None when the directory holds no applied configuration, and otherwise
    a summary of what was found. A configuration that is present but unreadable
    is not treated as absence: an empty form over a directory that plainly holds
    a deployment is the one answer that would be a lie, so it becomes a failed
    start, which is the state whose copy says so.
    """
    try:
        restored = persist.reconstruct(config_dir)
    except Exception as e:
        app._start_failed = True
        return {"ok": False, "bucket": "", "tables": 0,
                "detail": "a configuration is present but could not be read: %s" % e}
    if not restored:
        return None

    cfg, token, expires, applied_hash = restored
    result = sup.resume(cfg, token)
    app._cfg = cfg
    app._token = token
    app._expires = expires
    app._applied_hash = applied_hash
    # Whatever was checked last ran in a process that is gone. The configuration
    # comes back; the verdict does not.
    app._verdict = None
    app._start_failed = not result["ok"]
    return {"ok": result["ok"], "detail": result["detail"],
            "bucket": cfg.get("bucket", ""), "tables": len(cfg.get("tables") or [])}


def make_handler(app, auth, static_dir=STATIC_DIR):
    class Handler(BaseHTTPRequestHandler):
        # ── session ──────────────────────────────────────────────────────────
        def _authed(self):
            return auth.valid(session_from_cookie(self.headers.get("Cookie")))

        def _needs_session(self, path):
            return path.startswith(PROTECTED_PREFIX) and not is_public(path)

        def _refuse(self):
            self._send(401, '{"error":"no valid session — POST the setup token to '
                            '/api/login"}')

        def _login(self):
            token = (self._body() or {}).get("token", "")
            sid = auth.login(token)
            if not sid:
                # Deliberately the same wording as the no-session refusal: a
                # different message would tell a guesser their token was the
                # wrong shape rather than the wrong value.
                return self._refuse()
            self.send_response(204)
            # HttpOnly so page script cannot read it; SameSite=Strict because
            # nothing cross-site should ever drive this surface. No Secure flag:
            # what reaches this port is decided by how the operator published it,
            # and TLS is the ingress's job.
            self.send_header("Set-Cookie",
                             "%s=%s; HttpOnly; SameSite=Strict; Path=/"
                             % (COOKIE_NAME, sid))
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ── plumbing ─────────────────────────────────────────────────────────
        def _send(self, code, body, ctype="application/json"):
            raw = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _download(self, payload, filename, ctype):
            """A response the browser saves rather than renders. The filename is
            server-side because it carries the timestamp the bundle was taken at,
            which the page has no reason to invent for itself."""
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_pair(self, pair):
            code, body = pair
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
            return self._send(code, body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _not_found(self):
            self._send(404, '{"error":"not found"}')

        def _result(self, result):
            """An apply-shaped result. A refusal that is about the server's
            current state — an apply already running, or nothing to verify — is a
            409: the request was understood and the state says no."""
            code = 409 if (result.get("busy") or result.get("conflict")) else 200
            return self._send(code, json.dumps(result))

        def _static(self, path):
            # Served by basename only, so a traversal cannot escape the
            # directory. The query string is stripped first, or a cache-busting
            # "app.js?v=2" would never match a real file.
            name = os.path.basename(path.split("?", 1)[0])
            full = os.path.join(static_dir, name)
            if not name or not os.path.isfile(full):
                return self._not_found()
            _, ext = os.path.splitext(name)
            ctype = CONTENT_TYPES.get(ext, "text/plain")
            with open(full, "rb") as handle:
                data = handle.read()
            return self._send(200, data, "%s; charset=utf-8" % ctype)

        # ── verbs ────────────────────────────────────────────────────────────
        def do_GET(self):
            if self._needs_session(self.path) and not self._authed():
                return self._refuse()
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith("/static/"):
                return self._static(self.path)
            if path == "/":
                return self._static("/static/index.html")
            if path == "/api/status":
                status = app.get_status()
                status["copy"] = STATE_COPY[status["state"]]
                return self._send(200, json.dumps(status))
            if path == "/metrics":
                return self._send(200, metrics.render(app.get_status()),
                                  "text/plain; version=0.0.4; charset=utf-8")
            if path == "/api/profile":
                return self._send_pair(app.get_profile())
            if path == "/api/support-bundle":
                code, filename, payload = app.get_support_bundle()
                if code != 200:
                    return self._send(code, payload)
                return self._download(payload, filename, "application/gzip")
            if path == "/api/browse":
                prefix = (parse_qs(parts.query).get("prefix") or [""])[0]
                return self._send_pair(app.get_browse(prefix))
            self._not_found()

        def do_PUT(self):
            if self._needs_session(self.path) and not self._authed():
                return self._refuse()
            if self.path == "/api/config":
                return self._send(200, json.dumps(app.put_config(self._body())))
            if self.path == "/api/ca":
                return self._send_pair(app.put_ca((self._body() or {}).get("pem", "")))
            self._not_found()

        def do_DELETE(self):
            if self._needs_session(self.path) and not self._authed():
                return self._refuse()
            if self.path == "/api/ca":
                return self._send_pair(app.delete_ca())
            self._not_found()

        def do_POST(self):
            if self.path == "/api/login":
                return self._login()
            if self._needs_session(self.path) and not self._authed():
                return self._refuse()
            if self.path == "/api/apply":
                return self._result(app.post_apply())
            if self.path == "/api/verify":
                return self._result(app.post_verify())
            if self.path == "/api/token/rotate":
                return self._result(app.post_rotate())
            self._not_found()

        def log_message(self, fmt, *args):
            """Nothing is logged. A request line here can carry a browse prefix
            and a body can carry an S3 secret key; the container log is also
            where the setup token is printed, so it is read by people."""

    return Handler


def main():
    auth = Auth()
    sup = Supervisor(CONFIG_DIR, LAUNCHER, {})
    app = App(sup, auth, CONFIG_DIR, share_url_default=SHARE_URL)

    found = boot(app, sup, CONFIG_DIR)
    if found is None:
        print("No configuration on disk: starting unconfigured.", flush=True)
    elif found["ok"]:
        print("Resumed the configuration in %s: bucket %s, %d table(s) shared."
              % (CONFIG_DIR, found["bucket"], found["tables"]), flush=True)
    else:
        print("Found a configuration in %s but the sharing server did not start: %s"
              % (CONFIG_DIR, found["detail"]), flush=True)

    import version
    print("Setup image %s, sharing server %s" % (version.setup_version(), version.server_version() or "unknown"), flush=True)
    print("Setup token: %s" % auth.bootstrap, flush=True)
    print("Listening on http://%s:%d" % (BIND, PORT), flush=True)
    # Threaded: apply, rotate and verify run inline for several seconds, and a
    # single-threaded server would block every other request — including a status
    # poll — for the whole duration. App's own lock is what stops two of them
    # from interleaving now that they can arrive concurrently.
    ThreadingHTTPServer((BIND, PORT), make_handler(app, auth)).serve_forever()


if __name__ == "__main__":
    main()
