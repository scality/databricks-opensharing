"""Bootstrap-token auth: the k3s/Jupyter pattern.

A token is generated at start and printed to the container log once, then exchanged
for a session id. Whoever can read the log is already privileged, and nothing is
stored on disk to leak or rotate.

Sessions carry a creation timestamp (monotonic, so a wall-clock adjustment cannot
extend or shorten one) and expire after SESSION_TTL_SECONDS. This is the sole
access control in front of a page that holds live S3 credentials and can mint a
share credential, so a session must be revocable rather than living for the
process's whole lifetime.

The server is a `ThreadingHTTPServer` — a single-threaded server would leave every
other request (even a status poll) blocked while apply/rotate run for several
seconds inline — so every method here runs from whichever request thread called
it. `_sessions` is therefore guarded by a lock: without one, `_prune()`'s
iteration and a concurrent `login()`/`logout()`'s mutation of the same dict can
race — CPython raises `RuntimeError: dictionary changed size during iteration`
when that happens, turning an ordinary second browser tab into a 500.
"""
import hmac
import secrets
import threading
import time

SESSION_TTL_SECONDS = 12 * 3600


class Auth:
    def __init__(self, bootstrap=None, ttl=SESSION_TTL_SECONDS):
        self.bootstrap = bootstrap or secrets.token_hex(24)
        self._ttl = ttl
        self._sessions = {}  # session id -> creation time (time.monotonic())
        self._lock = threading.Lock()

    def login(self, token):
        if not token or not hmac.compare_digest(str(token), self.bootstrap):
            return None
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = time.monotonic()
        return sid

    def valid(self, session_id):
        self._prune()
        with self._lock:
            return bool(session_id) and session_id in self._sessions

    def logout(self, session_id):
        """Invalidate one session. Returns whether it existed."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def logout_all(self):
        """Invalidate every session — what a credential rotation should call."""
        with self._lock:
            self._sessions.clear()

    def _prune(self):
        now = time.monotonic()
        with self._lock:
            expired = [
                sid
                for sid, created in self._sessions.items()
                if now - created > self._ttl
            ]
            for sid in expired:
                del self._sessions[sid]
