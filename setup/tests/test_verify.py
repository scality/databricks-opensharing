"""The verification suite, against a deployment made of dictionaries.

Every test here is a deployment with exactly one thing wrong with it, and asserts
both the gate that notices and the state an operator would be shown. A gate that
can only ever pass is worth nothing, so each trap is exercised in the failing
direction as well as the passing one.
"""
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cases
import fake_share
import render
import state as st
import verify
from checks import FAIL, PASS, UNKNOWN
from fake_share import LOCAL_SERVER_URL, TOKEN, FakeDeployment, FakeS3Client

CFG = cases.BASE
THREE = cases.THREE_TABLES


def run(cfg=CFG, opener=None, client=None, token=TOKEN):
    opener = opener or FakeDeployment(cfg)
    client = client or FakeS3Client()
    return verify.run(cfg, token, LOCAL_SERVER_URL, None, opener=opener, client=client)


def by_id(results):
    return {c["id"]: c for c in results}


def resolve(results, cfg=CFG):
    """The state an operator is shown for this result set."""
    applied = st.config_hash(cfg)
    return st.resolve_state(configured=True, running=True, start_failed=False,
                            verdict={"hash": applied, "checks": results},
                            applied_hash=applied)


class TestHappyDeployment(unittest.TestCase):
    def test_every_check_passes(self):
        results = run()
        failures = [c for c in results if c["result"] != PASS]
        self.assertEqual(failures, [], "unexpected non-pass checks: %r" % failures)

    def test_state_is_verified(self):
        self.assertEqual(resolve(run()), st.VERIFIED)

    def test_the_gate_ids_are_the_documented_ones(self):
        ids = [c["id"] for c in run()]
        self.assertEqual(ids, [
            "rest_endpoint_registered",
            "auth_no_token_401",
            "auth_wrong_token_401",
            "unauth_query_leaks_no_url",
            "listing_scality",
            "query_url_host_scality.poc.customers",
            "parquet_par1_scality.poc.customers",
            "unsigned_fetch_refused_scality.poc.customers",
            "url_expiry_bounded_scality.poc.customers",
            "unknown_share_404",
            "unknown_table_404",
        ])

    def test_every_configured_table_gets_its_own_data_path_gates(self):
        ids = [c["id"] for c in run(THREE, opener=FakeDeployment(THREE))]
        for label in ("scality.poc.customers", "scality.poc.orders",
                      "scality.finance.ledger"):
            self.assertIn("query_url_host_%s" % label, ids)
            self.assertIn("parquet_par1_%s" % label, ids)
            self.assertIn("unsigned_fetch_refused_%s" % label, ids)
            self.assertIn("url_expiry_bounded_%s" % label, ids)

    def test_one_listing_check_per_share(self):
        ids = [c["id"] for c in run(THREE, opener=FakeDeployment(THREE))]
        self.assertEqual([i for i in ids if i.startswith("listing_")], ["listing_scality"])

    def test_the_suite_talks_to_the_local_server_not_the_public_url(self):
        opener = FakeDeployment(CFG)
        run(opener=opener)
        dialled = [url for _, url, _ in opener.requests]
        self.assertTrue(any(u.startswith(LOCAL_SERVER_URL) for u in dialled))
        self.assertFalse([u for u in dialled if u.startswith(CFG["share_public_url"])])

    def test_the_presigned_object_is_fetched_through_the_injected_client(self):
        client = FakeS3Client()
        run(client=client)
        self.assertEqual(len(client.urls), 1)
        self.assertIn("X-Amz-Signature=", client.urls[0])

    def test_the_ssl_context_is_passed_to_every_request(self):
        sentinel = object()
        opener = FakeDeployment(CFG)
        verify.run(CFG, TOKEN, LOCAL_SERVER_URL, sentinel, opener=opener,
                   client=FakeS3Client())
        self.assertTrue(opener.contexts)
        self.assertTrue(all(ctx is sentinel for ctx in opener.contexts))


class TestAccessControl(unittest.TestCase):
    def test_shares_readable_without_a_token_fails(self):
        results = run(opener=FakeDeployment(CFG, shares_open=True))
        self.assertEqual(by_id(results)["auth_no_token_401"]["result"], FAIL)
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_a_token_the_server_does_not_validate_fails(self):
        # A deployment that accepts the probe's deliberately wrong token, which is
        # a server not validating tokens at all.
        opener = FakeDeployment(CFG, token="not-a-real-token")
        results = verify.run(CFG, "not-a-real-token", LOCAL_SERVER_URL, None,
                             opener=opener, client=FakeS3Client())
        wrong = by_id(results)["auth_wrong_token_401"]
        self.assertEqual(wrong["result"], FAIL)
        self.assertIn("not being validated", wrong["detail"])

    def test_an_unauthenticated_query_that_leaks_a_url_fails(self):
        class Leaky(FakeDeployment):
            def _share_api(self, url, method, auth):
                if url.endswith("/query") and auth is None:
                    body = self._query_body(self._entries()[0])
                    return fake_share.FakeResponse(200, body)
                return FakeDeployment._share_api(self, url, method, auth)

        results = run(opener=Leaky(CFG))
        leak = by_id(results)["unauth_query_leaks_no_url"]
        self.assertEqual(leak["result"], FAIL)
        self.assertIn("before the URL is minted", leak["detail"])
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_an_unknown_share_that_resolves_fails(self):
        results = run(opener=FakeDeployment(CFG, unknown_share_status=200))
        self.assertEqual(by_id(results)["unknown_share_404"]["result"], FAIL)
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_an_unknown_table_that_resolves_fails(self):
        results = run(opener=FakeDeployment(CFG, unknown_table_status=200))
        self.assertEqual(by_id(results)["unknown_table_404"]["result"], FAIL)
        self.assertEqual(resolve(results), st.DEGRADED)


class TestListing(unittest.TestCase):
    def test_a_share_the_server_does_not_serve_fails(self):
        cfg = dict(CFG, tables=[dict(CFG["tables"][0], share="other")])
        opener = FakeDeployment(CFG)  # the server still serves the old share
        results = verify.run(cfg, TOKEN, LOCAL_SERVER_URL, None, opener=opener,
                             client=FakeS3Client())
        listing = by_id(results)["listing_other"]
        self.assertEqual(listing["result"], FAIL)
        self.assertIn("not listed", listing["detail"])

    def test_a_table_missing_from_the_listing_fails(self):
        cfg = dict(CFG, tables=CFG["tables"] + [
            {"prefix": "opensharing-poc/orders", "share": "scality",
             "schema": "poc", "table": "orders"}])
        results = verify.run(cfg, TOKEN, LOCAL_SERVER_URL, None,
                             opener=FakeDeployment(CFG), client=FakeS3Client())
        listing = by_id(results)["listing_scality"]
        self.assertEqual(listing["result"], FAIL)
        self.assertIn("poc.orders", listing["detail"])


class TestPresignedUrlHost(unittest.TestCase):
    """The presigner bug: a URL minted for a host the recipient was never given."""

    def test_a_url_on_another_host_fails(self):
        opener = FakeDeployment(CFG, url_host="https://s3.amazonaws.com")
        results = run(opener=opener)
        host = by_id(results)["query_url_host_scality.poc.customers"]
        self.assertEqual(host["result"], FAIL)
        self.assertIn("s3.amazonaws.com", host["detail"])
        self.assertIn("presigner", host["detail"])
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_a_url_on_the_configured_host_with_a_port_passes(self):
        cfg = dict(CFG, s3_endpoint="https://s3.example.com:8443")
        results = run(cfg, opener=FakeDeployment(cfg))
        self.assertEqual(by_id(results)["query_url_host_scality.poc.customers"]["result"],
                         PASS)


class TestParquetBytes(unittest.TestCase):
    def test_a_body_that_is_not_parquet_fails(self):
        results = run(client=FakeS3Client(payload=b"<?xml version=\"1.0\"?>"))
        self.assertEqual(by_id(results)["parquet_par1_scality.poc.customers"]["result"],
                         FAIL)

    def test_a_fetch_that_raises_fails_rather_than_aborting_the_suite(self):
        results = run(client=FakeS3Client(error=RuntimeError("connection refused")))
        self.assertEqual(by_id(results)["parquet_par1_scality.poc.customers"]["result"],
                         FAIL)
        self.assertIn("unknown_table_404", by_id(results))


class TestUnsignedFetch(unittest.TestCase):
    """The world-readable bucket: the only gate that can tell a vended credential
    from a public object."""

    def test_an_object_readable_without_a_signature_fails(self):
        results = run(opener=FakeDeployment(CFG, unsigned_status=200))
        refused = by_id(results)["unsigned_fetch_refused_scality.poc.customers"]
        self.assertEqual(refused["result"], FAIL)
        self.assertIn("bucket is public", refused["detail"])
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_a_401_is_also_a_refusal(self):
        results = run(opener=FakeDeployment(CFG, unsigned_status=401))
        self.assertEqual(
            by_id(results)["unsigned_fetch_refused_scality.poc.customers"]["result"], PASS)

    def test_any_other_status_is_unknown_not_a_pass(self):
        results = run(opener=FakeDeployment(CFG, unsigned_status=500))
        self.assertEqual(
            by_id(results)["unsigned_fetch_refused_scality.poc.customers"]["result"],
            UNKNOWN)

    def test_the_query_string_is_stripped_before_the_unsigned_fetch(self):
        opener = FakeDeployment(CFG)
        run(opener=opener)
        object_gets = [url for _, url, _ in opener.requests
                       if url.endswith(".parquet")]
        self.assertEqual(len(object_gets), 1)
        self.assertNotIn("?", object_gets[0])


class TestExpiryBound(unittest.TestCase):
    def test_an_expiry_above_the_configured_bound_fails(self):
        opener = FakeDeployment(CFG, expires=render.PRESIGNED_TIMEOUT_SECONDS + 1)
        results = run(opener=opener)
        expiry = by_id(results)["url_expiry_bounded_scality.poc.customers"]
        self.assertEqual(expiry["result"], FAIL)
        self.assertIn(str(render.PRESIGNED_TIMEOUT_SECONDS), expiry["detail"])
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_a_missing_expiry_fails(self):
        results = run(opener=FakeDeployment(CFG, expires=None))
        expiry = by_id(results)["url_expiry_bounded_scality.poc.customers"]
        self.assertEqual(expiry["result"], FAIL)
        self.assertIn("unbounded", expiry["detail"])

    def test_a_missing_signature_fails(self):
        results = run(opener=FakeDeployment(CFG, signature=""))
        expiry = by_id(results)["url_expiry_bounded_scality.poc.customers"]
        self.assertEqual(expiry["result"], FAIL)
        self.assertIn("not presigned", expiry["detail"])
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_the_bound_is_inclusive(self):
        opener = FakeDeployment(CFG, expires=render.PRESIGNED_TIMEOUT_SECONDS)
        self.assertEqual(
            by_id(run(opener=opener))["url_expiry_bounded_scality.poc.customers"]["result"],
            PASS)


class TestEmptyTable(unittest.TestCase):
    """An empty table is a measurement that could not be taken, not a finding."""

    def test_the_data_path_gates_are_unknown(self):
        opener = FakeDeployment(CFG, empty_tables=["scality.poc.customers"])
        results = by_id(run(opener=opener))
        for id in ("query_url_host_scality.poc.customers",
                   "parquet_par1_scality.poc.customers",
                   "unsigned_fetch_refused_scality.poc.customers",
                   "url_expiry_bounded_scality.poc.customers"):
            self.assertEqual(results[id]["result"], UNKNOWN, id)
            self.assertIn("empty", results[id]["detail"])

    def test_nothing_is_reported_as_a_failure(self):
        opener = FakeDeployment(CFG, empty_tables=["scality.poc.customers"])
        results = run(opener=opener)
        self.assertEqual([c for c in results if c["result"] == FAIL], [])

    def test_the_state_is_never_verified_not_verified(self):
        opener = FakeDeployment(CFG, empty_tables=["scality.poc.customers"])
        self.assertEqual(resolve(run(opener=opener)), st.NEVER_VERIFIED)

    def test_no_object_is_fetched(self):
        client = FakeS3Client()
        run(opener=FakeDeployment(CFG, empty_tables=["scality.poc.customers"]),
            client=client)
        self.assertEqual(client.urls, [])

    def test_only_the_empty_table_loses_its_gates(self):
        opener = FakeDeployment(THREE, empty_tables=["scality.poc.orders"])
        results = by_id(run(THREE, opener=opener))
        self.assertEqual(results["query_url_host_scality.poc.orders"]["result"], UNKNOWN)
        self.assertEqual(results["query_url_host_scality.poc.customers"]["result"], PASS)


class TestRestEndpointProbe(unittest.TestCase):
    def test_an_unregistered_host_fails(self):
        results = run(opener=FakeDeployment(CFG, rest_endpoint_status=400))
        probe = by_id(results)["rest_endpoint_registered"]
        self.assertEqual(probe["result"], FAIL)
        self.assertIn("restEndpoints", probe["detail"])
        self.assertEqual(resolve(results), st.DEGRADED)

    def test_the_probe_addresses_the_storage_not_the_share_server(self):
        opener = FakeDeployment(CFG)
        run(opener=opener)
        self.assertEqual(opener.requests[0][1], "https://s3.example.com/")


class TestNdjson(unittest.TestCase):
    def test_file_lines_are_read_and_other_lines_ignored(self):
        body = (b'{"protocol":{"minReaderVersion":1}}\n'
                b'{"metaData":{"id":"x"}}\n'
                b'{"file":{"url":"https://h/a","id":"1"}}\n'
                b'{"file":{"url":"https://h/b","id":"2"}}\n')
        self.assertEqual(verify.file_urls(body), ["https://h/a", "https://h/b"])

    def test_a_malformed_line_does_not_hide_the_files_that_parsed(self):
        body = b'not json\n{"file":{"url":"https://h/a"}}\n\n'
        self.assertEqual(verify.file_urls(body), ["https://h/a"])


class TestQueryFailure(unittest.TestCase):
    def test_a_query_that_answers_an_error_fails_the_data_path_gates(self):
        class Broken(FakeDeployment):
            def _share_api(self, url, method, auth):
                if url.endswith("/query") and auth is not None and "no-such" not in url:
                    raise urllib.error.HTTPError(url, 500, "", {}, None)
                return FakeDeployment._share_api(self, url, method, auth)

        results = by_id(run(opener=Broken(CFG)))
        self.assertEqual(results["query_url_host_scality.poc.customers"]["result"], FAIL)
        self.assertIn("500", results["query_url_host_scality.poc.customers"]["detail"])


if __name__ == "__main__":
    unittest.main()
