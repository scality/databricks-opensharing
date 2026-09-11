"""The API behind the setup page.

Everything the page can do is a method here, and every method returns plain data
rather than an HTTP response — the transport lives in entrypoint.py. That split
is what lets the whole surface be tested without a socket, and it is also why an
operator console other than this page could drive the same deployment.

Two invariants run through the file. A verdict belongs to one configuration and
one token: anything that changes either clears it before the change lands, so a
concurrent reader can see "not verified yet" but never a pass that was taken
against something else. And the S3 secret key never leaves this process — not in
a status payload, not in a saved setup file.
"""
import datetime
import threading
import urllib.request

import bundle
import checks
import persist
import render
import s3
import state as st
import tls
import version

TOKEN_TTL_DAYS = 90


class App:
    def __init__(self, supervisor, auth, config_dir, share_url_default="",
                 opener=None, keytool="keytool",
                 local_server_url="http://127.0.0.1:8080"):
        self.sup = supervisor
        self.auth = auth
        self.config_dir = config_dir
        # What a recipient dials when the operator has not entered a public URL.
        # The environment can seed it; an entered value always wins.
        self.share_url_default = share_url_default
        # The checks this tool runs itself talk to the supervised child directly,
        # not through whatever ingress a recipient uses. A gate that went out and
        # came back would be testing the customer's load balancer.
        self.local_server_url = local_server_url
        self.keytool = keytool
        # Resolved here rather than left as None: the pre-checks' own default is
        # the real network opener and is not None-tolerant, so a None threaded
        # through would turn every pre-check into a failure.
        self.opener = opener or urllib.request.urlopen

        self._cfg = None
        # The storage half of a submission whose table selection is still
        # missing or wrong: enough to browse the bucket with, not enough to apply.
        self._draft = None
        self._token = None
        self._expires = ""
        self._applied_hash = None
        self._verdict = None
        self._start_failed = False
        self._warnings = []
        self._prechecks = []
        # apply, rotate and verify all mutate one Supervisor and run for several
        # seconds inline. A non-reentrant lock, taken without blocking, makes a
        # second caller fail fast with `busy` rather than queue behind the first
        # (which reads as a hang) or interleave inside the Supervisor.
        self._apply_lock = threading.Lock()

    # ── reads ────────────────────────────────────────────────────────────────
    def get_status(self):
        ca_hash = tls.ca_sha256(self.config_dir)
        return {
            "state": st.resolve_state(
                # Configured means a configuration was submitted, not that an
                # apply ever succeeded. Deriving it from a success would report
                # "not configured yet" on the one occasion the operator most
                # needs to be told the server would not start.
                configured=self._cfg is not None,
                running=self.sup.running(),
                start_failed=self._start_failed,
                verdict=self._verdict,
                applied_hash=self._applied_hash),
            "config": self._public_config(),
            "version": version.report(),
            "verdict": self._verdict,
            # Dates the verdict rather than the configuration: a pass from an
            # hour ago and one from last month read alike without it.
            "verdict_at": (self._verdict or {}).get("verdict_at", ""),
            "config_hash": self._applied_hash,
            "token_expires": self._expires,
            "warnings": list(self._warnings),
            "prechecks": list(self._prechecks),
            "ca": {"present": bool(ca_hash), "sha256": ca_hash},
        }

    def _public_config(self):
        """The configuration as the page may see it: everything but the secret,
        plus a flag saying whether one is held. The page renders the flag so the
        operator can re-submit the form without retyping the key."""
        source = self._cfg or self._draft
        if not source:
            return None
        out = {k: v for k, v in source.items() if k != "secret_key"}
        out["secret_set"] = bool(source.get("secret_key"))
        # A draft is what the operator typed before picking a table; the page
        # shows it back so a reload mid-setup does not empty the form.
        out["draft"] = self._cfg is None
        return out

    def get_profile(self):
        """(status, body) for the recipient's `.share` document."""
        if self.get_status()["state"] != st.VERIFIED:
            return (409, '{"error":"not verified — run Verify before handing over '
                         'a profile"}')
        endpoint = render.share_endpoint(self._share_url())
        return (200, render.profile_json(endpoint, self._token, self._expires))

    def _share_url(self):
        """What the profile points a recipient at.

        The entered public URL first; then whatever the deployment was started
        with; and only then this host's own address, which at least produces a
        profile that works from inside the same network rather than one that
        points nowhere.
        """
        entered = str((self._cfg or {}).get("share_public_url", "") or "").strip()
        return entered or str(self.share_url_default or "").strip() or self.local_server_url

    def get_browse(self, prefix=""):
        """(status, body) for a scan of the bucket for Delta tables.

        Needs credentials, so it needs a submitted configuration — an empty form
        has nothing to browse with. Storage errors come back as their own
        message: a traceback in the page would tell the operator nothing and
        could carry a URL with a signature in it.
        """
        source = self._draft or self._cfg
        if not source:
            return (409, {"error": "enter the storage details and press Check "
                                   "before browsing"})
        ssl_ctx = tls.ssl_context(source, self.config_dir)
        client = s3.S3Client(source, ssl_ctx, self.opener)
        try:
            tables, truncated = s3.discover_delta_tables(client)
        except s3.S3Error as e:
            return (502, {"error": str(e)})
        except Exception as e:
            return (502, {"error": str(e)})
        wanted = str(prefix or "").strip().lstrip("/")
        if wanted:
            tables = [t for t in tables if t["prefix"].startswith(wanted)]
        return (200, {"tables": tables, "truncated": truncated})

    def get_support_bundle(self):
        """(status, filename, bytes) for the redacted archive a support case gets.

        Needs a configuration for the same reason browse does: there is nothing
        to describe before one exists, and an archive of an empty directory would
        be mistaken for evidence that nothing is wrong.

        The redactor is the Supervisor's, not `bundle`'s own: it knows the
        previous run's secrets as well as this one's, and `server.log` is
        appended across restarts, so the run most likely to still be echoed in it
        is the one that already ended.
        """
        if not self._cfg:
            return (409, "", '{"error":"nothing is configured yet — press Check '
                             'and Apply before exporting a bundle"}')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        payload = bundle.build(
            self.config_dir, self.get_status(),
            (self._cfg.get("secret_key"), self._cfg.get("access_key"), self._token),
            self.sup._redact_with_history)
        return (200, "opensharing-support-%s.tar.gz" % stamp, payload)

    # ── writes ───────────────────────────────────────────────────────────────
    def put_config(self, cfg):
        """Validate and hold a submitted configuration. Nothing is applied here.

        Two fields the form does not own. `ca_pem_sha256` is whatever CA is
        actually on disk, so the page cannot claim a CA it never uploaded. And an
        empty secret on a re-submit means "unchanged" — the page is never sent
        the secret, so it has none to send back, and reading an empty string as a
        deletion would wipe a working credential on every edit.
        """
        incoming = dict(cfg or {})
        incoming["ca_pem_sha256"] = tls.ca_sha256(self.config_dir)
        previous = self._cfg or self._draft
        if not str(incoming.get("secret_key", "") or "").strip() and previous:
            incoming["secret_key"] = previous.get("secret_key", "")

        storage_problems, warnings = checks.validate_storage(incoming)
        table_problems = checks.validate_tables(incoming)
        problems = storage_problems + table_problems
        self._warnings = warnings
        if storage_problems:
            self._draft = None
            self._prechecks = []
            return {"ok": False, "problems": problems, "warnings": warnings,
                    "prechecks": []}

        # Pre-checks cost a request each and only make sense against inputs that
        # are at least well-formed, so they run once the storage half holds — with
        # or without a table selection, because the operator browses for tables
        # with exactly these details and wants to know they work first.
        ssl_ctx = tls.ssl_context(incoming, self.config_dir)
        endpoint = incoming["s3_endpoint"]
        found = [
            checks.precheck_rest_endpoint(endpoint, ssl_ctx, opener=self.opener),
            checks.precheck_endpoint(endpoint, ssl_ctx, opener=self.opener),
            checks.precheck_bucket(incoming, ssl_ctx,
                                   client=s3.S3Client(incoming, ssl_ctx, self.opener)),
            checks.precheck_share_url(incoming.get("share_public_url", ""), ssl_ctx,
                                      opener=self.opener),
        ]
        self._prechecks = found
        if table_problems:
            self._draft = incoming
            return {"ok": False, "problems": problems, "warnings": warnings,
                    "prechecks": found}
        self._draft = None
        self._cfg = incoming
        # A new configuration invalidates any verdict taken against the old one.
        self._verdict = None
        return {"ok": True, "problems": [], "warnings": warnings,
                "prechecks": found}

    def put_ca(self, pem):
        """(status, body) for an uploaded CA certificate.

        The certificate is validated before anything is written, so a paste that
        is not a certificate leaves the CA that may currently be working in
        place. The truststore is rebuilt in the same call: a CA the JVM does not
        trust is a CA that only half exists.
        """
        try:
            digest = tls.save_ca_pem(pem or "", self.config_dir)
        except Exception as e:
            return (400, {"error": "that does not parse as a PEM certificate: %s" % e})
        try:
            tls.build_truststore(self.config_dir, self.keytool)
        except Exception as e:
            return (500, {"error": "the certificate was stored but the Java "
                                   "truststore could not be built: %s" % e})
        if self._cfg is not None:
            self._cfg["ca_pem_sha256"] = digest
        # Which CA is trusted is part of what the checks exercised.
        self._verdict = None
        return (200, {"ok": True, "sha256": digest})

    def delete_ca(self):
        tls.remove_ca(self.config_dir)
        if self._cfg is not None:
            self._cfg["ca_pem_sha256"] = ""
        self._verdict = None
        return (200, {"ok": True, "sha256": ""})

    def post_apply(self):
        if not self._cfg:
            return {"ok": False, "detail": "no configuration submitted"}
        return self._locked_apply(mint_token=not self._token)

    def post_rotate(self):
        if not self._cfg:
            return {"ok": False, "detail": "no configuration to rotate against"}
        return self._locked_apply(mint_token=True)

    def post_verify(self):
        """Re-run the gates against the server that is already running.

        Nothing is rendered, restarted or minted — this is the button an operator
        presses after fixing something on the storage side. It refuses when there
        is nothing running to check, and when the form has moved on from what was
        applied: checking the old server and stamping the new configuration's
        hash on the result would manufacture a pass nobody earned.
        """
        if not self._cfg or self._applied_hash is None:
            return {"conflict": True,
                    "detail": "nothing has been applied yet — press Apply first"}
        if not self.sup.running():
            return {"conflict": True,
                    "detail": "the sharing server is not running — press Apply first"}
        if st.config_hash(self._cfg) != self._applied_hash:
            return {"conflict": True,
                    "detail": "the configuration has changed since it was applied "
                              "— press Apply before verifying"}
        if not self._apply_lock.acquire(blocking=False):
            return self._busy()
        try:
            self._verdict = None
            self._verify()
            return {"ok": True, "detail": "checks complete"}
        finally:
            self._apply_lock.release()

    @staticmethod
    def _busy():
        return {"ok": False, "busy": True,
                "detail": "an apply is already in progress — wait for it to "
                          "finish and try again"}

    def _locked_apply(self, mint_token):
        # Non-blocking: a caller arriving while another apply is in flight gets
        # an immediate answer rather than being run later against whatever
        # `_cfg`/`_token` happen to hold by the time it is dispatched.
        if not self._apply_lock.acquire(blocking=False):
            return self._busy()
        try:
            # Cleared before the token is touched. The token is not one of the
            # hashed fields, so the hash comparison in state.py structurally
            # cannot notice a rotation: without this line a reader would see a
            # pass — and /api/profile would hand over a credential — for the
            # whole window between minting a new token and the gates finishing.
            self._verdict = None
            if mint_token:
                self._token = render.new_token()
                self._expires = render.token_expiry(TOKEN_TTL_DAYS)
            result = self.sup.apply(self._cfg, self._token)
            self._start_failed = not result["ok"]
            if not result["ok"]:
                return result
            self._applied_hash = st.config_hash(self._cfg)
            # Written before the gates run: what is on disk is now what the
            # server is serving, and a process killed mid-verify should come back
            # to the configuration that is actually live.
            persist.save(self.config_dir, self._cfg, self._expires,
                         self._applied_hash)
            self._verify()
            return result
        finally:
            self._apply_lock.release()

    def _verify(self):
        ssl_ctx = tls.ssl_context(self._cfg, self.config_dir)
        found = self._run_post_checks(self._cfg, self._token,
                                      self.local_server_url, ssl_ctx)
        self._verdict = {"hash": self._applied_hash, "checks": found,
                         "verdict_at": render.now_utc_iso()}

    def _run_post_checks(self, cfg, token, local_server_url, ssl_ctx):
        """The gate suite. Imported here rather than at module scope so this
        module is importable — and the rest of it testable — on its own, and so a
        test can replace this method on an instance."""
        import gates
        return gates.run_post_checks(cfg, token, local_server_url, ssl_ctx,
                                     opener=self.opener)
