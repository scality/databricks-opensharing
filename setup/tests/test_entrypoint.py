"""The HTTP layer: the session guard, the status codes, the static files, and the
boot that adopts a deployment already on disk.

The handler tests drive a real server over a loopback socket on an ephemeral
port, with `http.client` rather than urllib — it returns a status code on a 4xx
instead of raising, which is the whole point of most of these assertions. Unit
tests of `is_public` and `session_from_cookie` alone would stay green with the
enforcement line deleted from a verb handler, so they are kept as what they are
and the wire tests are what actually prove the guard fires.
"""
import http.client
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
from http.server import HTTPServer

import entrypoint
import render
from app import App
from auth import Auth
from entrypoint import STATE_COPY, boot, make_handler, session_from_cookie
from state import (DEGRADED, FAILED_START, NEVER_VERIFIED, STOPPED, UNCONFIGURED,
                   VERIFIED)
from supervise import Supervisor
from tests.test_app import CFG, offline_opener, passing

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef"


class FakeSupervisor:
    """A supervisor that never spawns anything. `boot` only asks it to resume and
    to say whether something is running, so those are the only two answers a test
    needs to control."""

    def __init__(self, ok=True):
        self.ok = ok
        self.resumed = []

    def resume(self, cfg, token):
        self.resumed.append((cfg, token))
        return {"ok": self.ok,
                "detail": "server started" if self.ok else "server exited immediately"}

    def running(self):
        return self.ok

    def apply(self, cfg, token):
        raise AssertionError("nothing in this file should apply a configuration")

    def stop(self):
        self.ok = False

    # The shipped redactor, not a stand-in: the bundle route hands this method to
    # bundle.build, and a fake that returned its input unchanged would let a
    # route test pass over an archive carrying credentials.
    _last_good = None
    _redact = staticmethod(Supervisor._redact)
    _redact_with_history = Supervisor._redact_with_history


class TestStateCopy(unittest.TestCase):
    def test_every_state_has_copy(self):
        for name in (UNCONFIGURED, NEVER_VERIFIED, VERIFIED, DEGRADED, STOPPED,
                     FAILED_START):
            self.assertIn(name, STATE_COPY)
            self.assertTrue(STATE_COPY[name].strip())

    def test_never_verified_and_degraded_do_not_read_alike(self):
        # One is an absent measurement, the other is a finding. Rendering them
        # alike is how a profile from a broken share gets handed over.
        self.assertNotEqual(STATE_COPY[NEVER_VERIFIED], STATE_COPY[DEGRADED])

    def test_degraded_copy_says_a_check_failed(self):
        self.assertIn("failed", STATE_COPY[DEGRADED].lower())

    def test_never_verified_copy_does_not_claim_a_failure(self):
        self.assertNotIn("failed", STATE_COPY[NEVER_VERIFIED].lower())

    def test_never_verified_copy_names_the_way_out(self):
        # The state is reached by doing nothing wrong, so the sentence has to say
        # which button ends it rather than leaving the operator to guess.
        self.assertIn("verify", STATE_COPY[NEVER_VERIFIED].lower())


class TestSessionHelpers(unittest.TestCase):
    def setUp(self):
        self.auth = Auth()

    def test_api_paths_are_protected(self):
        for path in ("/api/status", "/api/config", "/api/ca", "/api/browse",
                     "/api/apply", "/api/verify", "/api/token/rotate",
                     "/api/profile"):
            self.assertTrue(path.startswith(entrypoint.PROTECTED_PREFIX), path)

    def test_login_is_the_only_unauthenticated_api_path(self):
        self.assertTrue(entrypoint.is_public("/api/login"))
        for path in ("/api/status", "/api/apply", "/api/profile", "/api/browse",
                     "/api/ca", "/api/verify"):
            self.assertFalse(entrypoint.is_public(path), path)

    def test_a_session_cookie_is_parsed_out_of_a_crowded_header(self):
        sid = self.auth.login(self.auth.bootstrap)
        header = "other=1; %s=%s; last=2" % (entrypoint.COOKIE_NAME, sid)
        self.assertEqual(session_from_cookie(header), sid)
        self.assertTrue(self.auth.valid(session_from_cookie(header)))

    def test_absent_or_malformed_cookies_yield_no_session(self):
        for header in (None, "", "novalue", "wrong=abc", "%s=" % entrypoint.COOKIE_NAME):
            self.assertIsNone(session_from_cookie(header), header)

    def test_a_forged_session_is_rejected(self):
        header = "%s=forged" % entrypoint.COOKIE_NAME
        self.assertFalse(self.auth.valid(session_from_cookie(header)))


class TestBoot(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def make(self, sup):
        return App(sup, Auth(), self.dir, opener=offline_opener)

    def test_an_empty_directory_boots_unconfigured(self):
        sup = FakeSupervisor(ok=False)
        app = self.make(sup)
        self.assertIsNone(boot(app, sup, self.dir))
        self.assertEqual(app.get_status()["state"], UNCONFIGURED)

    def test_a_configuration_on_disk_is_resumed_and_never_verified(self):
        # The configuration comes back; the verdict does not. It was taken by a
        # process that is gone, against a server that has just been restarted.
        render.write_config(CFG, TOKEN, self.dir)
        sup = FakeSupervisor(ok=True)
        app = self.make(sup)
        found = boot(app, sup, self.dir)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["bucket"], "bkt")
        self.assertEqual(found["tables"], 1)
        self.assertEqual(len(sup.resumed), 1)
        self.assertEqual(app._token, TOKEN)
        self.assertIsNone(app._verdict)
        self.assertEqual(app.get_status()["state"], NEVER_VERIFIED)

    def test_the_resumed_configuration_carries_its_tables_and_secret(self):
        render.write_config(CFG, TOKEN, self.dir)
        sup = FakeSupervisor(ok=True)
        app = self.make(sup)
        boot(app, sup, self.dir)
        self.assertEqual(app._cfg["tables"], CFG["tables"])
        # The secret is read back out of core-site.xml — the one file that has to
        # hold it — so a resumed deployment can be re-applied without retyping.
        self.assertEqual(app._cfg["secret_key"], "SK")

    def test_a_summary_of_a_resumed_deployment_names_no_secret(self):
        render.write_config(CFG, TOKEN, self.dir)
        sup = FakeSupervisor(ok=True)
        app = self.make(sup)
        found = boot(app, sup, self.dir)
        self.assertNotIn("SK", json.dumps(found))
        self.assertNotIn(TOKEN, json.dumps(found))

    def test_a_server_that_will_not_resume_is_a_failed_start(self):
        render.write_config(CFG, TOKEN, self.dir)
        sup = FakeSupervisor(ok=False)
        app = self.make(sup)
        found = boot(app, sup, self.dir)
        self.assertFalse(found["ok"])
        self.assertEqual(app.get_status()["state"], FAILED_START)

    def test_a_configuration_that_cannot_be_read_is_a_failed_start(self):
        # Present but unparseable. Reading that as "nothing configured" would put
        # an empty form on screen over a directory that plainly holds a
        # deployment, which is the one answer that would be a lie.
        render.write_config(CFG, TOKEN, self.dir)
        with open(os.path.join(self.dir, render.SERVER_YAML_FILE), "w") as handle:
            handle.write("this is not the document we wrote\n")
        sup = FakeSupervisor(ok=False)
        app = self.make(sup)
        found = boot(app, sup, self.dir)
        self.assertFalse(found["ok"])
        self.assertEqual(sup.resumed, [], "nothing should be resumed from a "
                                          "configuration that could not be read")
        self.assertEqual(app.get_status()["state"], FAILED_START)


class HandlerCase(unittest.TestCase):
    """A real server on an ephemeral port, over a real socket."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.static = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.static)
        self.auth = Auth()
        self.sup = FakeSupervisor(ok=False)
        self.app = App(self.sup, self.auth, self.dir, opener=offline_opener)
        self.app._run_post_checks = passing
        self.srv = HTTPServer(("127.0.0.1", 0),
                              make_handler(self.app, self.auth, static_dir=self.static))
        self.port = self.srv.server_address[1]
        # A short poll interval: `shutdown()` blocks until the serve loop notices,
        # and the default half-second would be paid once per test in this file.
        self.thread = threading.Thread(
            target=lambda: self.srv.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.msg, response.read()
        finally:
            conn.close()

    def login(self, token):
        status, msg, body = self.request(
            "POST", "/api/login", body=json.dumps({"token": token}),
            headers={"Content-Type": "application/json"})
        return status, msg, body

    def cookie(self):
        _, msg, _ = self.login(self.auth.bootstrap)
        return {"Cookie": msg.get("Set-Cookie").split(";", 1)[0],
                "Content-Type": "application/json"}

    def configure(self, headers):
        status, _, body = self.request("PUT", "/api/config", body=json.dumps(CFG),
                                       headers=headers)
        self.assertEqual(status, 200, body)


class TestSessionEnforcement(HandlerCase):
    def test_every_api_route_refuses_a_request_with_no_session(self):
        for method, path in (("GET", "/api/status"), ("GET", "/api/profile"),
                             ("GET", "/api/browse"), ("PUT", "/api/config"),
                             ("PUT", "/api/ca"), ("DELETE", "/api/ca"),
                             ("POST", "/api/apply"), ("POST", "/api/verify"),
                             ("POST", "/api/token/rotate"),
                             ("GET", "/api/support-bundle")):
            status, _, _ = self.request(method, path, body="{}",
                                        headers={"Content-Type": "application/json"})
            self.assertEqual(status, 401, "%s %s" % (method, path))

    def test_a_wrong_token_is_refused_in_exactly_the_same_words(self):
        # A different message would tell a guesser their token was the wrong
        # shape rather than the wrong value.
        wrong_status, _, wrong_body = self.login("not-the-setup-token")
        none_status, _, none_body = self.request("GET", "/api/status")
        self.assertEqual(wrong_status, 401)
        self.assertEqual(none_status, 401)
        self.assertEqual(wrong_body, none_body)

    def test_the_setup_token_buys_a_locked_down_cookie(self):
        status, msg, _ = self.login(self.auth.bootstrap)
        self.assertEqual(status, 204)
        cookie = msg.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_a_valid_session_reaches_the_status(self):
        status, _, _ = self.request("GET", "/api/status", headers=self.cookie())
        self.assertEqual(status, 200)


class TestRoutes(HandlerCase):
    def test_status_carries_a_sentence_for_its_state(self):
        _, _, body = self.request("GET", "/api/status", headers=self.cookie())
        payload = json.loads(body)
        self.assertEqual(payload["state"], UNCONFIGURED)
        self.assertEqual(payload["copy"], STATE_COPY[UNCONFIGURED])
        for key in ("warnings", "prechecks", "ca", "config_hash", "token_expires"):
            self.assertIn(key, payload)

    def test_an_unverified_profile_is_409(self):
        status, _, _ = self.request("GET", "/api/profile", headers=self.cookie())
        self.assertEqual(status, 409)

    def test_browsing_before_any_credentials_is_409(self):
        status, _, body = self.request("GET", "/api/browse", headers=self.cookie())
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(body))

    def test_a_browse_prefix_reaches_the_app(self):
        headers = self.cookie()
        self.configure(headers)
        seen = []
        self.app.get_browse = lambda prefix="": seen.append(prefix) or (200, {
            "tables": [], "truncated": False})
        status, _, _ = self.request("GET", "/api/browse?prefix=warehouse%2F",
                                    headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["warehouse/"])

    def test_configuring_reports_problems_without_applying(self):
        headers = self.cookie()
        status, _, body = self.request(
            "PUT", "/api/config", body=json.dumps(dict(CFG, bucket="")),
            headers=headers)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["problems"])
        self.assertIn("warnings", payload)

    def test_an_unparseable_certificate_is_400(self):
        headers = self.cookie()
        status, _, body = self.request("PUT", "/api/ca",
                                       body=json.dumps({"pem": "nope"}),
                                       headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_deleting_a_certificate_that_is_not_there_is_200(self):
        headers = self.cookie()
        status, _, body = self.request("DELETE", "/api/ca", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sha256"], "")

    def test_verifying_with_nothing_applied_is_409(self):
        headers = self.cookie()
        self.configure(headers)
        status, _, body = self.request("POST", "/api/verify", headers=headers)
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(body)["conflict"])

    def test_an_apply_while_one_is_in_flight_is_409_over_the_wire(self):
        headers = self.cookie()
        self.configure(headers)
        self.app._apply_lock.acquire()
        try:
            status, _, body = self.request("POST", "/api/apply", headers=headers)
        finally:
            self.app._apply_lock.release()
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(body)["busy"])

    def test_a_rotate_while_an_apply_is_in_flight_is_409_over_the_wire(self):
        headers = self.cookie()
        self.configure(headers)
        self.app._apply_lock.acquire()
        try:
            status, _, body = self.request("POST", "/api/token/rotate",
                                           headers=headers)
        finally:
            self.app._apply_lock.release()
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(body)["busy"])

    def test_an_apply_with_nothing_in_flight_is_not_409(self):
        # The lock guard must not fire when nothing is running — this reaches the
        # ordinary "no configuration submitted" answer.
        headers = self.cookie()
        status, _, _ = self.request("POST", "/api/apply", headers=headers)
        self.assertEqual(status, 200)

    def test_a_support_bundle_before_any_configuration_is_409(self):
        status, _, body = self.request("GET", "/api/support-bundle",
                                       headers=self.cookie())
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(body))

    def test_a_support_bundle_is_a_named_gzip_attachment(self):
        headers = self.cookie()
        self.configure(headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/support-bundle", headers=headers)
            response = conn.getresponse()
            payload = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "application/gzip")
            disposition = response.getheader("Content-Disposition")
        finally:
            conn.close()
        self.assertTrue(disposition.startswith("attachment; filename="), disposition)
        self.assertIn("opensharing-support-", disposition)
        self.assertTrue(disposition.rstrip('"').endswith(".tar.gz"), disposition)
        # Really an archive, not a JSON error with the wrong header on it.
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertIn("README.txt", names)
        self.assertIn("status.json", names)

    def test_an_unknown_path_is_404(self):
        headers = self.cookie()
        for method, path in (("GET", "/api/nothing"), ("PUT", "/api/nothing"),
                             ("POST", "/api/nothing"), ("DELETE", "/api/nothing")):
            status, _, _ = self.request(method, path, body="{}", headers=headers)
            self.assertEqual(status, 404, "%s %s" % (method, path))


class TestStaticFiles(HandlerCase):
    def write(self, name, text):
        with open(os.path.join(self.static, name), "w") as handle:
            handle.write(text)

    def test_a_real_file_is_served_with_its_content_type(self):
        self.write("app.js", "export const x = 1;\n")
        status, msg, body = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", msg.get("Content-Type"))
        self.assertIn(b"export const x", body)

    def test_a_cache_busting_query_string_still_finds_the_file(self):
        self.write("app.js", "ok\n")
        status, _, _ = self.request("GET", "/static/app.js?v=2")
        self.assertEqual(status, 200)

    def test_the_root_serves_the_page(self):
        self.write("index.html", "<!doctype html><title>setup</title>")
        status, msg, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", msg.get("Content-Type"))
        self.assertIn(b"setup", body)

    def test_a_traversal_cannot_escape_the_static_directory(self):
        status, _, _ = self.request("GET", "/static/../entrypoint.py")
        self.assertEqual(status, 404)

    def test_a_traversal_that_survives_normalisation_is_still_refused(self):
        # http.client does not rewrite this one, so it arrives at the handler as
        # written and the basename rule is what has to refuse it.
        status, _, _ = self.request("GET", "/static/%2e%2e/entrypoint.py")
        self.assertEqual(status, 404)

    def test_a_missing_file_is_404_rather_than_a_crash(self):
        status, _, _ = self.request("GET", "/static/not-there.js")
        self.assertEqual(status, 404)

    def test_static_files_need_no_session(self):
        # The page has to load before anyone can type the token into it.
        self.write("app.js", "ok\n")
        status, _, _ = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)


class TestMetricsRoute(HandlerCase):
    """The scrape endpoint. A scraper holds no cookie, so this is the one route
    besides the login and the static files that answers without a session."""

    def test_metrics_needs_no_session(self):
        status, msg, body = self.request("GET", "/metrics")
        self.assertEqual(status, 200, body)
        self.assertEqual(msg.get("Content-Type"),
                         "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn(b"opensharing_setup_info", body)

    def test_metrics_reports_the_state_the_status_route_reports(self):
        headers = self.cookie()
        _, _, raw = self.request("GET", "/api/status", headers=headers)
        state = json.loads(raw)["state"]
        _, _, body = self.request("GET", "/metrics")
        self.assertIn(b'opensharing_state{state="%s"} 1' % state.encode(), body)

    def test_a_configured_deployment_leaks_no_secret_through_the_wire(self):
        # The whole path, not just the renderer: a future handler that decided to
        # append something of its own would be caught here and nowhere else.
        self.configure(self.cookie())
        _, _, body = self.request("GET", "/metrics")
        for forbidden in (CFG["secret_key"], CFG["access_key"],
                          CFG["tables"][0]["table"], CFG["s3_endpoint"]):
            self.assertNotIn(forbidden.encode(), body)


if __name__ == "__main__":
    unittest.main()
