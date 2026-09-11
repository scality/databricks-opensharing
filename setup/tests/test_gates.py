"""The wrapper that stops a half-run suite from reading as a clean one.

The failure being pinned: a gate raises, the exception escapes, and the caller
stores the handful of checks that ran before it — all of them passes. Without
`suite_completed` the state model reads that as an all-pass set and reports the
deployment verified. So these tests assert the state, not only the check.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cases
import gates
import state as st
import verify
from checks import FAIL, PASS
from fake_share import LOCAL_SERVER_URL, TOKEN, FakeDeployment, FakeS3Client

CFG = cases.BASE


def run(cfg=CFG, opener=None, client=None):
    return gates.run_post_checks(cfg, TOKEN, LOCAL_SERVER_URL, None,
                                 opener=opener or FakeDeployment(cfg),
                                 client=client or FakeS3Client())


def resolve(results, cfg=CFG):
    applied = st.config_hash(cfg)
    return st.resolve_state(configured=True, running=True, start_failed=False,
                            verdict={"hash": applied, "checks": results},
                            applied_hash=applied)


class TestASuiteThatFinishes(unittest.TestCase):
    def test_the_last_check_says_the_suite_completed(self):
        results = run()
        self.assertEqual(results[-1]["id"], "suite_completed")
        self.assertEqual(results[-1]["result"], PASS)

    def test_the_gate_results_are_carried_through_unchanged(self):
        direct = verify.run(CFG, TOKEN, LOCAL_SERVER_URL, None,
                            opener=FakeDeployment(CFG), client=FakeS3Client())
        # The readiness check comes first and the suite marker last; between
        # them the gates are exactly what verify.run produced.
        self.assertEqual(run()[1:-1], direct)

    def test_a_clean_deployment_is_verified(self):
        self.assertEqual(resolve(run()), st.VERIFIED)

    def test_a_failing_gate_still_completes_the_suite(self):
        results = run(opener=FakeDeployment(CFG, unsigned_status=200))
        self.assertEqual(results[-1]["result"], PASS)
        self.assertEqual(resolve(results), st.DEGRADED)


class TestASuiteThatRaises(unittest.TestCase):
    """A query that dies mid-suite, which is the case this module exists for."""

    def opener(self):
        return FakeDeployment(CFG, raise_on_query=["scality.poc.customers"])

    def test_the_exception_does_not_escape(self):
        results = run(opener=self.opener())
        self.assertTrue(results)

    def test_the_suite_check_fails_and_carries_the_exception_text(self):
        results = run(opener=self.opener())
        self.assertEqual(results[-1]["id"], "suite_completed")
        self.assertEqual(results[-1]["result"], FAIL)
        self.assertIn("connection reset", results[-1]["detail"])

    def test_the_checks_that_did_run_are_kept(self):
        results = run(opener=self.opener())
        ids = [c["id"] for c in results]
        self.assertIn("auth_no_token_401", ids)
        self.assertIn("listing_scality", ids)
        # It died on the first table's query, so the data-path gates never ran.
        self.assertNotIn("query_url_host_scality.poc.customers", ids)

    def test_the_partial_run_is_degraded_and_never_verified(self):
        results = run(opener=self.opener())
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_without_the_suite_check_the_partial_run_would_read_as_verified(self):
        # The mutation this guard is for: drop `suite_completed` and the same
        # partial result set is all passes, which resolves to VERIFIED.
        partial = [c for c in run(opener=self.opener()) if c["id"] != "suite_completed"]
        self.assertTrue(all(c["result"] == PASS for c in partial))
        self.assertEqual(resolve(partial), st.VERIFIED)

    def test_a_deployment_that_never_answers_still_reports(self):
        class DeadOnArrival(FakeDeployment):
            def __call__(self, req, timeout=None, context=None):
                raise OSError("storage is unreachable")

        # A server that never listens: the readiness wait gives up (a fake
        # clock, so no real 90 seconds pass) and the suite is marked as not run.
        with mock.patch.object(gates, "wait_until_listening",
                               lambda url, opener=None: gates.check(
                                   gates.READY_ID, FAIL, "never answered")):
            results = run(opener=DeadOnArrival(CFG))
        self.assertEqual([c["id"] for c in results], ["server_listening", "suite_completed"])
        self.assertEqual(results[-1]["result"], FAIL)
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_the_readiness_wait_gives_up_on_a_fake_clock(self):
        ticks = iter(range(0, 200, 10))
        calls = []

        def dead(req, timeout=None, context=None):
            calls.append(req.full_url)
            raise OSError("connection refused")

        result = gates.wait_until_listening(LOCAL_SERVER_URL, dead, timeout=30,
                                            sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(result["result"], FAIL)
        self.assertIn("connection refused", result["detail"])
        self.assertTrue(all(u.endswith("/delta-sharing/shares") for u in calls))

    def test_a_401_counts_as_listening(self):
        import urllib.error

        def refuses(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        result = gates.wait_until_listening(LOCAL_SERVER_URL, refuses, timeout=1,
                                            sleep=lambda s: None)
        self.assertEqual(result["result"], PASS)

    def test_a_storage_that_dies_mid_suite_still_reports(self):
        class DiesOnStorage(FakeDeployment):
            def __call__(self, req, timeout=None, context=None):
                if "/delta-sharing/" in req.full_url:
                    return super().__call__(req, timeout=timeout, context=context)
                raise OSError("storage is unreachable")

        results = run(opener=DiesOnStorage(CFG))
        self.assertEqual(results[-1]["id"], "suite_completed")
        self.assertNotEqual(resolve(results), st.VERIFIED)


if __name__ == "__main__":
    unittest.main()
