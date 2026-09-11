"""The API's behaviour, without a socket.

Everything here drives `App` directly. The supervised child is a real `sleep`
process so the running/stopped distinction is real, but the start grace period is
shortened — the tests exercise the decisions, not the JVM's boot time. Nothing in
this file touches the network: the opener is a fake, the S3 scan is patched, and
the gate suite is replaced with a function that returns whatever a given test
needs. That last substitution is also what lets this suite pass before gates.py
exists.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import persist
import render
import s3
import supervise
import tls
from app import App
from auth import Auth
from state import (DEGRADED, FAILED_START, NEVER_VERIFIED, STOPPED, UNCONFIGURED,
                   VERIFIED)
from supervise import Supervisor

OPENSSL = shutil.which("openssl")

CFG = {
    "platform": "ring",
    "endpoint_mode": "trusted",
    "s3_endpoint": "https://s3.example.com",
    "bucket": "bkt",
    "access_key": "AK",
    "secret_key": "SK",
    "region": "us-east-1",
    "share_public_url": "https://share.example.com",
    "ca_pem_sha256": "",
    "tables": [{"prefix": "data/customers", "share": "scality",
                "schema": "poc", "table": "customers"}],
}


class _Response:
    """The least a response can be and still satisfy every caller here: a status,
    and a body that reads as empty."""

    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def getcode(self):
        return self.status

    def read(self, *args):
        return self._body


def offline_opener(target, timeout=None, context=None):
    """Never touches the network. Answers everything with a bare 200, which is a
    pass for the reachability check and an honest `unknown` for the two that read
    a specific status."""
    return _Response()


def passing(*args):
    return [{"id": "x", "result": "pass", "detail": ""}]


def failing(*args):
    return [{"id": "x", "result": "fail", "detail": "broken"}]


def unknown(*args):
    return [{"id": "x", "result": "unknown", "detail": "could not run"}]


class AppCase(unittest.TestCase):
    """A temporary config directory, a shortened start grace, and a helper that
    builds an App over a real Supervisor."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        grace = mock.patch.object(supervise, "START_GRACE_SECONDS", 0.05)
        grace.start()
        self.addCleanup(grace.stop)

    def make(self, post_checks=passing, launcher=("sleep", "60"), opener=None,
             **kwargs):
        sup = Supervisor(self.dir, list(launcher), {})
        self.addCleanup(sup.stop)
        app = App(sup, Auth(), self.dir,
                  share_url_default=kwargs.pop("share_url_default", ""),
                  opener=opener or offline_opener, **kwargs)
        app._run_post_checks = post_checks
        return app, sup


class TestStatus(AppCase):
    def test_starts_unconfigured(self):
        app, _ = self.make()
        self.assertEqual(app.get_status()["state"], UNCONFIGURED)

    def test_status_never_returns_the_secret(self):
        app, _ = self.make()
        app.put_config(CFG)
        body = json.dumps(app.get_status())
        self.assertNotIn("SK", body)
        self.assertIn("secret_set", body)

    def test_status_carries_the_tables_and_the_public_url(self):
        app, _ = self.make()
        app.put_config(CFG)
        config = app.get_status()["config"]
        self.assertEqual(config["tables"], CFG["tables"])
        self.assertEqual(config["share_public_url"], CFG["share_public_url"])

    def test_status_reports_the_warnings_from_the_last_validate(self):
        app, _ = self.make()
        app.put_config(dict(CFG, share_public_url=""))
        warnings = app.get_status()["warnings"]
        self.assertTrue(any("public share URL" in w for w in warnings), warnings)

    def test_status_reports_the_prechecks_from_the_last_submit(self):
        app, _ = self.make()
        app.put_config(CFG)
        ids = [c["id"] for c in app.get_status()["prechecks"]]
        self.assertEqual(ids, ["rest_endpoint_registered", "endpoint_reachable",
                               "bucket_listable", "share_url_reachable"])

    def test_status_reports_no_ca_when_none_was_uploaded(self):
        app, _ = self.make()
        self.assertEqual(app.get_status()["ca"], {"present": False, "sha256": ""})

    def test_configured_does_not_wait_for_a_successful_apply(self):
        # "Configured" follows from a configuration having been submitted. Tying
        # it to a successful apply would tell an operator who has just filled in
        # the form that nothing is configured yet.
        app, _ = self.make()
        app.put_config(CFG)
        self.assertNotEqual(app.get_status()["state"], UNCONFIGURED)


class TestPutConfig(AppCase):
    def test_rejects_a_plain_http_endpoint_in_a_tls_mode(self):
        app, _ = self.make()
        result = app.put_config(dict(CFG, s3_endpoint="http://s3.example.com"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("https" in p for p in result["problems"]))

    def test_a_rejected_config_runs_no_prechecks(self):
        calls = []

        def opener(target, timeout=None, context=None):
            calls.append(target)
            return _Response()

        app, _ = self.make(opener=opener)
        result = app.put_config(dict(CFG, bucket=""))
        self.assertFalse(result["ok"])
        self.assertEqual(result["prechecks"], [])
        self.assertEqual(calls, [])

    def test_a_rejected_config_still_reports_its_warnings(self):
        app, _ = self.make()
        result = app.put_config(dict(CFG, bucket="", endpoint_mode="http",
                                     s3_endpoint="http://s3.example.com"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("Plain HTTP" in w for w in result["warnings"]),
                        result["warnings"])

    def test_prechecks_use_the_injected_opener(self):
        # The seam has to stay wired, or the suite goes back to firing live
        # requests with nothing here to notice.
        calls = []

        def opener(target, timeout=None, context=None):
            calls.append(getattr(target, "full_url", target))
            return _Response()

        app, _ = self.make(opener=opener)
        app.put_config(CFG)
        self.assertIn("https://s3.example.com/", calls)
        self.assertTrue(any("/bkt" in str(c) for c in calls), calls)

    def test_an_omitted_secret_on_a_resubmit_keeps_the_previous_one(self):
        # The page is never sent the secret, so it has none to send back. Reading
        # an empty field as a deletion would wipe a working credential on every
        # edit of any other field.
        app, _ = self.make()
        app.put_config(CFG)
        resubmit = dict(CFG, bucket="other")
        resubmit.pop("secret_key")
        result = app.put_config(resubmit)
        self.assertTrue(result["ok"], result)
        self.assertEqual(app._cfg["secret_key"], "SK")
        self.assertEqual(app._cfg["bucket"], "other")

    def test_an_empty_secret_on_a_first_submit_is_still_required(self):
        app, _ = self.make()
        result = app.put_config(dict(CFG, secret_key=""))
        self.assertFalse(result["ok"])
        self.assertTrue(any("secret_key" in p for p in result["problems"]))

    def test_the_ca_hash_comes_from_disk_not_from_the_form(self):
        # A page that could name its own CA hash could claim a CA nobody
        # uploaded, and private_ca mode would pass validation with no trust
        # material anywhere.
        app, _ = self.make()
        app.put_config(dict(CFG, ca_pem_sha256="f" * 64))
        self.assertEqual(app._cfg["ca_pem_sha256"], "")


class TestApplyAndVerdict(AppCase):
    def test_apply_then_all_pass_is_verified(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        self.assertTrue(app.post_apply()["ok"])
        self.assertEqual(app.get_status()["state"], VERIFIED)

    def test_a_failing_post_check_is_degraded(self):
        app, _ = self.make(failing)
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(app.get_status()["state"], DEGRADED)

    def test_an_unknown_post_check_does_not_verify(self):
        # A check that could not run is an absent measurement, never a pass and
        # never a finding — and the handover stays refused either way.
        app, _ = self.make(unknown)
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(app.get_status()["state"], NEVER_VERIFIED)
        self.assertEqual(app.get_profile()[0], 409)

    def test_apply_saves_the_configuration_to_disk_without_the_secret(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        saved = persist.load(self.dir)
        self.assertEqual(saved["bucket"], "bkt")
        self.assertEqual(saved["applied_hash"], app._applied_hash)
        self.assertNotIn("secret_key", saved)

    def test_changing_the_configuration_clears_a_pass(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(app.get_status()["state"], VERIFIED)
        app.put_config(dict(CFG, bucket="other"))
        self.assertEqual(app.get_status()["state"], NEVER_VERIFIED)

    def test_rotate_changes_the_token_and_reverifies(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        first = app._token
        self.assertTrue(app.post_rotate()["ok"])
        self.assertNotEqual(app._token, first)
        self.assertEqual(app.get_status()["state"], VERIFIED)

    def test_a_first_ever_failed_apply_is_failed_start(self):
        app, _ = self.make(passing, launcher=("false",))
        app.put_config(CFG)
        result = app.post_apply()
        self.assertFalse(result["ok"], result)
        self.assertEqual(app.get_status()["state"], FAILED_START)

    def test_the_gates_run_against_the_local_server_not_the_public_url(self):
        # A gate that went out through the customer's ingress and came back would
        # be testing the ingress. The checks talk to the supervised child.
        seen = []

        def record(cfg, token, url, ssl_ctx):
            seen.append(url)
            return passing()

        app, _ = self.make(record, local_server_url="http://127.0.0.1:8080")
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(seen, ["http://127.0.0.1:8080"])


class TestApplyLock(AppCase):
    def test_a_second_apply_while_one_is_in_flight_is_refused(self):
        app, sup = self.make(passing)
        app.put_config(CFG)
        app._apply_lock.acquire()
        try:
            result = app.post_apply()
        finally:
            app._apply_lock.release()
        self.assertTrue(result.get("busy"), result)
        self.assertIsNone(app._applied_hash)
        self.assertFalse(sup.running())
        self.assertTrue(app.post_apply()["ok"])

    def test_a_rotate_while_an_apply_is_in_flight_is_also_refused(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app._apply_lock.acquire()
        try:
            result = app.post_rotate()
        finally:
            app._apply_lock.release()
        self.assertTrue(result.get("busy"), result)
        self.assertIsNone(app._token)

    def test_a_verify_while_an_apply_is_in_flight_is_refused(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        app._apply_lock.acquire()
        try:
            result = app.post_verify()
        finally:
            app._apply_lock.release()
        self.assertTrue(result.get("busy"), result)

    def test_a_rotate_hands_over_no_token_while_it_is_in_flight(self):
        # The token is not one of the hashed fields, so the hash comparison in
        # the state model structurally cannot notice a rotation. Without the
        # verdict being cleared before the new token is minted, the profile route
        # would hand over a credential nothing has ever tested for as long as the
        # gates take to run. Asserting only the end state passes against that
        # bug, which is why this one blocks inside the post-checks.
        reached = threading.Event()
        release = threading.Event()

        def blocking(*args):
            reached.set()
            release.wait(timeout=5)
            return passing()

        app, _ = self.make(passing)
        app.put_config(CFG)
        self.assertTrue(app.post_apply()["ok"])
        self.assertEqual(app.get_status()["state"], VERIFIED)

        app._run_post_checks = blocking
        thread = threading.Thread(target=app.post_rotate)
        thread.start()
        try:
            self.assertTrue(reached.wait(timeout=5), "rotate never reached its gates")
            self.assertEqual(app.get_profile()[0], 409)
            self.assertEqual(app.get_status()["state"], NEVER_VERIFIED)
        finally:
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "the rotate thread did not finish")
        self.assertEqual(app.get_status()["state"], VERIFIED)


class TestVerify(AppCase):
    def test_verify_before_anything_is_applied_is_a_conflict(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        result = app.post_verify()
        self.assertTrue(result.get("conflict"), result)

    def test_verify_with_the_server_stopped_is_a_conflict(self):
        app, sup = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        sup.stop()
        self.assertEqual(app.get_status()["state"], STOPPED)
        result = app.post_verify()
        self.assertTrue(result.get("conflict"), result)

    def test_verify_reruns_the_gates_without_applying_anything(self):
        app, sup = self.make(failing)
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(app.get_status()["state"], DEGRADED)

        applies = []
        original = sup.apply
        sup.apply = lambda *a: applies.append(a) or original(*a)
        app._run_post_checks = passing
        result = app.post_verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(applies, [], "verify must not re-apply the configuration")
        self.assertEqual(app.get_status()["state"], VERIFIED)

    def test_verify_refuses_a_configuration_that_has_not_been_applied_yet(self):
        # Checking the running server and stamping the edited configuration's
        # hash on the result would manufacture a pass for something that was
        # never deployed.
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        app.put_config(dict(CFG, bucket="other"))
        result = app.post_verify()
        self.assertTrue(result.get("conflict"), result)
        self.assertIn("changed", result["detail"])


class TestProfile(AppCase):
    def test_profile_is_409_until_verified(self):
        app, _ = self.make(failing)
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(app.get_profile()[0], 409)

    def test_profile_is_200_once_verified(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        code, body = app.get_profile()
        self.assertEqual(code, 200)
        document = json.loads(body)
        self.assertEqual(document["endpoint"],
                         "https://share.example.com/delta-sharing")
        self.assertEqual(document["bearerToken"], app._token)

    def test_an_empty_public_url_falls_back_to_the_seeded_default(self):
        app, _ = self.make(passing, share_url_default="https://seeded.example.com")
        app.put_config(dict(CFG, share_public_url=""))
        app.post_apply()
        _, body = app.get_profile()
        self.assertEqual(json.loads(body)["endpoint"],
                         "https://seeded.example.com/delta-sharing")

    def test_with_no_public_url_anywhere_the_profile_points_at_this_host(self):
        app, _ = self.make(passing, local_server_url="http://127.0.0.1:8080")
        app.put_config(dict(CFG, share_public_url=""))
        app.post_apply()
        _, body = app.get_profile()
        self.assertEqual(json.loads(body)["endpoint"],
                         "http://127.0.0.1:8080/delta-sharing")


class TestBrowse(AppCase):
    FOUND = [{"prefix": "data/customers", "share": "bkt", "schema": "data",
              "table": "customers"},
             {"prefix": "warehouse/orders", "share": "bkt", "schema": "warehouse",
              "table": "orders"}]

    def test_browsing_without_credentials_is_a_conflict(self):
        app, _ = self.make()
        code, body = app.get_browse()
        self.assertEqual(code, 409)
        self.assertIn("error", body)

    def test_storage_details_alone_are_enough_to_browse(self):
        """Picking a table needs the browse, so the browse cannot need a table.

        A submission with the storage half right and no table yet is refused as
        a configuration (the table problem is reported) but kept as the draft
        the browse runs with, and the storage prechecks run on it — the operator
        learns the endpoint and bucket answer before looking for tables."""
        app, _ = self.make()
        out = app.put_config(dict(CFG, tables=[]))
        self.assertFalse(out["ok"])
        self.assertIn("at least one table is required", out["problems"])
        self.assertTrue(out["prechecks"], "storage prechecks ran on the draft")
        self.assertIsNone(app._cfg, "an incomplete submission is not a configuration")
        with mock.patch.object(s3, "discover_delta_tables",
                               return_value=(self.FOUND, False)):
            code, body = app.get_browse()
        self.assertEqual(code, 200)
        self.assertEqual(body["tables"], self.FOUND)

    def test_the_draft_is_shown_back_and_keeps_its_secret(self):
        app, _ = self.make()
        app.put_config(dict(CFG, tables=[]))
        shown = app.get_status()["config"]
        self.assertTrue(shown["draft"])
        self.assertTrue(shown["secret_set"])
        self.assertNotIn("secret_key", shown)
        out = app.put_config(dict(CFG, secret_key=""))
        self.assertTrue(out["ok"])
        self.assertEqual(app._cfg["secret_key"], CFG["secret_key"])
        self.assertFalse(app.get_status()["config"]["draft"])

    def test_a_storage_problem_leaves_nothing_to_browse_with(self):
        app, _ = self.make()
        out = app.put_config(dict(CFG, tables=[], bucket=""))
        self.assertEqual(out["prechecks"], [])
        code, _ = app.get_browse()
        self.assertEqual(code, 409)

    def test_browse_returns_the_discovered_tables(self):
        app, _ = self.make()
        app.put_config(CFG)
        with mock.patch.object(s3, "discover_delta_tables",
                               return_value=(self.FOUND, False)):
            code, body = app.get_browse()
        self.assertEqual(code, 200)
        self.assertEqual(body, {"tables": self.FOUND, "truncated": False})

    def test_browse_reports_a_scan_that_was_cut_short(self):
        app, _ = self.make()
        app.put_config(CFG)
        with mock.patch.object(s3, "discover_delta_tables",
                               return_value=(self.FOUND, True)):
            _, body = app.get_browse()
        self.assertTrue(body["truncated"])

    def test_a_prefix_narrows_the_result(self):
        app, _ = self.make()
        app.put_config(CFG)
        with mock.patch.object(s3, "discover_delta_tables",
                               return_value=(self.FOUND, False)):
            _, body = app.get_browse("warehouse/")
        self.assertEqual([t["prefix"] for t in body["tables"]], ["warehouse/orders"])

    def test_browse_uses_the_current_credentials_and_endpoint(self):
        app, _ = self.make()
        app.put_config(CFG)
        seen = {}

        def capture(client, *args, **kwargs):
            seen["endpoint"] = client.endpoint
            seen["bucket"] = client.bucket
            seen["key"] = client.cfg["access_key"]
            return ([], False)

        with mock.patch.object(s3, "discover_delta_tables", capture):
            app.get_browse()
        self.assertEqual(seen, {"endpoint": "https://s3.example.com",
                                "bucket": "bkt", "key": "AK"})

    def test_a_storage_error_is_a_message_not_a_traceback(self):
        app, _ = self.make()
        app.put_config(CFG)
        error = s3.S3Error(403, "AccessDenied", "Access Denied")
        with mock.patch.object(s3, "discover_delta_tables", side_effect=error):
            code, body = app.get_browse()
        self.assertEqual(code, 502)
        self.assertEqual(body, {"error": str(error)})
        self.assertNotIn("Traceback", body["error"])


@unittest.skipIf(OPENSSL is None, "openssl is not available")
class TestCertificateAuthority(AppCase):
    """The CA routes. The certificate is real — `save_ca_pem` validates it, so a
    stub would prove nothing — while the truststore build is replaced: what
    matters here is that App calls it, with the keytool it was given."""

    @classmethod
    def setUpClass(cls):
        source = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, source)
        path = os.path.join(source, "ca.pem")
        subprocess.run(
            [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=Example Test CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-keyout", os.path.join(source, "ca.key"), "-out", path],
            check=True, capture_output=True)
        with open(path) as handle:
            cls.pem = handle.read()

    def setUp(self):
        super().setUp()
        self.built = []
        patch = mock.patch.object(
            tls, "build_truststore",
            side_effect=lambda config_dir, keytool="keytool":
                self.built.append((config_dir, keytool)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_uploading_a_ca_stores_it_and_builds_the_truststore(self):
        app, _ = self.make(keytool="/usr/bin/keytool")
        code, body = app.put_ca(self.pem)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["sha256"], tls.ca_sha256(self.dir))
        self.assertEqual(self.built, [(self.dir, "/usr/bin/keytool")])

    def test_the_uploaded_ca_shows_up_in_the_status(self):
        app, _ = self.make()
        _, body = app.put_ca(self.pem)
        self.assertEqual(app.get_status()["ca"],
                         {"present": True, "sha256": body["sha256"]})

    def test_uploading_a_ca_updates_the_held_configuration(self):
        app, _ = self.make()
        app.put_config(CFG)
        _, body = app.put_ca(self.pem)
        self.assertEqual(app._cfg["ca_pem_sha256"], body["sha256"])

    def test_uploading_a_ca_invalidates_a_verdict(self):
        # Which CA is trusted is part of what the checks exercised.
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        self.assertEqual(app.get_status()["state"], VERIFIED)
        app.put_ca(self.pem)
        self.assertEqual(app.get_status()["state"], NEVER_VERIFIED)

    def test_a_paste_that_is_not_a_certificate_is_a_plain_400(self):
        app, _ = self.make()
        code, body = app.put_ca("not a certificate")
        self.assertEqual(code, 400)
        self.assertIn("error", body)
        self.assertEqual(self.built, [], "nothing should be built from a bad paste")
        self.assertEqual(tls.ca_sha256(self.dir), "")

    def test_deleting_the_ca_clears_the_hash_and_the_verdict(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.put_ca(self.pem)
        app.post_apply()
        self.assertEqual(app.get_status()["state"], VERIFIED)
        code, body = app.delete_ca()
        self.assertEqual(code, 200)
        self.assertEqual(body["sha256"], "")
        self.assertEqual(app._cfg["ca_pem_sha256"], "")
        self.assertEqual(tls.ca_sha256(self.dir), "")
        self.assertEqual(app.get_status()["state"], NEVER_VERIFIED)

    def test_a_private_ca_configuration_needs_the_upload_first(self):
        app, _ = self.make()
        private = dict(CFG, endpoint_mode="private_ca",
                       s3_endpoint="https://s3.example.test")
        result = app.put_config(private)
        self.assertFalse(result["ok"])
        self.assertTrue(any("CA certificate" in p for p in result["problems"]))
        app.put_ca(self.pem)
        self.assertTrue(app.put_config(private)["ok"])


class TestGateSeam(AppCase):
    """Every other test here replaces `_run_post_checks`, so nothing else would
    notice the default losing its way to the gate suite."""

    def test_the_default_post_checks_call_the_gate_suite(self):
        import gates

        seen = {}

        def record(cfg, token, local_server_url, ssl_ctx, opener=None, client=None):
            seen.update(token=token, url=local_server_url, opener=opener,
                        bucket=cfg["bucket"])
            return passing()

        app, _ = self.make(launcher=("sleep", "60"),
                           local_server_url="http://127.0.0.1:8080")
        del app._run_post_checks  # back to the method on the class
        app.put_config(CFG)
        with mock.patch.object(gates, "run_post_checks", record):
            app.post_apply()
        self.assertEqual(seen["url"], "http://127.0.0.1:8080")
        self.assertEqual(seen["bucket"], "bkt")
        self.assertIs(seen["opener"], offline_opener)
        self.assertEqual(seen["token"], app._token)
        self.assertEqual(app.get_status()["state"], VERIFIED)


class TestRenderedOutput(AppCase):
    def test_an_apply_writes_the_two_files_the_server_reads(self):
        app, _ = self.make(passing)
        app.put_config(CFG)
        app.post_apply()
        for name in (render.CORE_SITE_FILE, render.SERVER_YAML_FILE):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, name)), name)


if __name__ == "__main__":
    unittest.main()
