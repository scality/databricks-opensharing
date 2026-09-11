import unittest
import urllib.error

import cases
import render
from checks import (PASS, FAIL, UNKNOWN, check, validate_inputs, precheck_endpoint,
                    precheck_rest_endpoint, precheck_bucket, precheck_share_url,
                    HTTP_WARNING, NO_SHARE_URL_WARNING)

CFG = cases.BASE


def _raises(exc):
    def opener(*args, **kwargs):
        raise exc
    return opener


def _status(code):
    """An opener answering with a real HTTP status rather than an error."""
    class Response:
        status = code
    return lambda *args, **kwargs: Response()


def _http_error(code):
    return _raises(urllib.error.HTTPError("u", code, "", {}, None))


class FakeS3Error(Exception):
    """Shaped like s3.S3Error without importing it: the checks read .status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def head_bucket(self):
        self.calls += 1
        if self.error:
            raise self.error


class TestCheckShape(unittest.TestCase):
    def test_check_carries_id_result_detail(self):
        self.assertEqual(check("x", PASS, "d"), {"id": "x", "result": PASS, "detail": "d"})

    def test_result_must_be_one_of_three(self):
        with self.assertRaises(ValueError):
            check("x", "green")


class TestValidateInputs(unittest.TestCase):
    def test_a_valid_config_has_no_problems(self):
        problems, _ = validate_inputs(CFG)
        self.assertEqual(problems, [])

    def test_a_valid_config_has_no_warnings(self):
        _, warnings = validate_inputs(CFG)
        self.assertEqual(warnings, [])

    def test_each_required_field_is_named_when_missing(self):
        for field in ("platform", "endpoint_mode", "s3_endpoint", "bucket",
                      "access_key", "secret_key", "region"):
            with self.subTest(field=field):
                problems, _ = validate_inputs(dict(CFG, **{field: ""}))
                self.assertTrue(any(field in p for p in problems), problems)

    def test_whitespace_is_not_a_value(self):
        problems, _ = validate_inputs(dict(CFG, bucket="   "))
        self.assertTrue(any("bucket" in p for p in problems), problems)

    def test_unknown_platform_and_mode_are_refused(self):
        problems, _ = validate_inputs(dict(CFG, platform="minio"))
        self.assertTrue(any("platform" in p for p in problems), problems)
        problems, _ = validate_inputs(dict(CFG, endpoint_mode="tls"))
        self.assertTrue(any("endpoint_mode" in p for p in problems), problems)

    def test_https_modes_require_an_https_endpoint(self):
        for mode in ("trusted", "private_ca"):
            with self.subTest(mode=mode):
                cfg = dict(CFG, endpoint_mode=mode, s3_endpoint="http://s3.example.com",
                           ca_pem_sha256="a" * 64)
                problems, _ = validate_inputs(cfg)
                self.assertTrue(any("https" in p for p in problems), problems)

    def test_http_mode_requires_an_http_endpoint(self):
        problems, _ = validate_inputs(dict(CFG, endpoint_mode="http"))
        self.assertTrue(any("http://" in p for p in problems), problems)

    def test_private_ca_without_a_certificate_is_refused(self):
        # The mode says the JVM and this tool must trust a CA nobody uploaded.
        # Applying it would fail on the first metadata read, twenty minutes
        # before the recipient sees anything.
        cfg = dict(CFG, endpoint_mode="private_ca", ca_pem_sha256="")
        problems, _ = validate_inputs(cfg)
        self.assertTrue(any("CA" in p for p in problems), problems)

    def test_a_share_url_that_is_not_a_url_is_refused(self):
        problems, _ = validate_inputs(dict(CFG, share_public_url="share.example.com"))
        self.assertTrue(any("share_public_url" in p for p in problems), problems)

    def test_at_least_one_table_is_required(self):
        problems, _ = validate_inputs(dict(CFG, tables=[]))
        self.assertTrue(any("table" in p for p in problems), problems)

    def test_prefixes_that_would_not_resolve_are_refused(self):
        for prefix in ("", "/opensharing-poc/customers", "a/../../etc",
                       "s3://bucket/customers"):
            with self.subTest(prefix=prefix):
                cfg = dict(CFG, tables=[dict(CFG["tables"][0], prefix=prefix)])
                problems, _ = validate_inputs(cfg)
                self.assertTrue(problems, "accepted prefix %r" % prefix)

    def test_names_outside_the_allowed_character_set_are_refused(self):
        for field in ("share", "schema", "table"):
            for value in ("", "a b", "a/b", "x" * 65, 'a"b'):
                with self.subTest(field=field, value=value):
                    cfg = dict(CFG, tables=[dict(CFG["tables"][0], **{field: value})])
                    problems, _ = validate_inputs(cfg)
                    self.assertTrue(problems, "accepted %s=%r" % (field, value))

    def test_a_name_that_would_break_the_yaml_quoting_cannot_get_through(self):
        # The renderer double-quotes names without escaping, so a quote in a name
        # would produce a file the server cannot parse. The character set is what
        # stops that, and this is the test that says so.
        cfg = dict(CFG, tables=[dict(CFG["tables"][0], table='c"; evil')])
        problems, _ = validate_inputs(cfg)
        self.assertTrue(problems)

    def test_duplicate_share_schema_table_is_refused(self):
        entry = CFG["tables"][0]
        cfg = dict(CFG, tables=[entry, dict(entry, prefix="other/place")])
        problems, _ = validate_inputs(cfg)
        self.assertTrue(any("duplicate" in p for p in problems), problems)

    def test_two_tables_differing_only_by_schema_are_fine(self):
        entry = CFG["tables"][0]
        cfg = dict(CFG, tables=[entry, dict(entry, schema="other", prefix="other/place")])
        problems, _ = validate_inputs(cfg)
        self.assertEqual(problems, [])

    def test_http_mode_warns_about_the_recipient(self):
        _, warnings = validate_inputs(dict(CFG, endpoint_mode="http",
                                           s3_endpoint="http://s3.example.test"))
        self.assertIn(HTTP_WARNING, warnings)

    def test_no_share_url_warns_but_does_not_block(self):
        problems, warnings = validate_inputs(dict(CFG, share_public_url=""))
        self.assertEqual(problems, [])
        self.assertIn(NO_SHARE_URL_WARNING, warnings)


class TestPrecheckEndpoint(unittest.TestCase):
    def test_a_reachable_endpoint_passes(self):
        result = precheck_endpoint("https://s3.example.com", None,
                                   opener=lambda *a, **k: None)
        self.assertEqual(result["result"], PASS)

    def test_an_http_error_status_still_proves_reachability(self):
        # A 403 means DNS, TLS and routing all worked, which is what this check
        # asks. Whether the credentials are right is the next check.
        result = precheck_endpoint("https://s3.example.com", None, opener=_http_error(403))
        self.assertEqual(result["result"], PASS)
        self.assertIn("403", result["detail"])

    def test_a_tls_failure_fails_with_the_reason(self):
        opener = _raises(urllib.error.URLError("certificate verify failed"))
        result = precheck_endpoint("https://s3.example.com", None, opener=opener)
        self.assertEqual(result["result"], FAIL)
        self.assertIn("certificate verify failed", result["detail"])

    def test_the_ssl_context_is_passed_through(self):
        # The private-CA context is the whole reason this check can distinguish
        # an untrusted CA from an unreachable host.
        seen = {}

        def opener(url, timeout=None, context=None):
            seen["context"] = context
        sentinel = object()
        precheck_endpoint("https://s3.example.com", sentinel, opener=opener)
        self.assertIs(seen["context"], sentinel)


class TestPrecheckRestEndpoint(unittest.TestCase):
    def test_403_means_registered(self):
        result = precheck_rest_endpoint("https://s3.example.com/some/key", None,
                                        opener=_http_error(403))
        self.assertEqual(result["result"], PASS)

    def test_400_means_not_registered_and_says_what_to_do(self):
        result = precheck_rest_endpoint("https://s3.example.com", None,
                                        opener=_http_error(400))
        self.assertEqual(result["result"], FAIL)
        self.assertIn("restEndpoints", result["detail"])

    def test_any_other_status_is_unknown_not_a_verdict(self):
        for code in (200, 404, 500):
            with self.subTest(code=code):
                result = precheck_rest_endpoint("https://s3.example.com", None,
                                                opener=_status(code))
                self.assertEqual(result["result"], UNKNOWN)
                self.assertIn(str(code), result["detail"])

    def test_an_exception_is_unknown(self):
        # Whether the host is a registered rest-endpoint is unanswered when the
        # request never completed; the reachability check is what reports that.
        result = precheck_rest_endpoint("https://s3.example.com", None,
                                        opener=_raises(urllib.error.URLError("no route")))
        self.assertEqual(result["result"], UNKNOWN)

    def test_the_request_goes_to_the_host_root(self):
        seen = {}

        def opener(url, timeout=None, context=None):
            seen["url"] = url
            raise urllib.error.HTTPError("u", 403, "", {}, None)
        precheck_rest_endpoint("https://s3.example.com:8443/bucket/key?x=1", None,
                               opener=opener)
        self.assertEqual(seen["url"], "https://s3.example.com:8443/")


class TestPrecheckBucket(unittest.TestCase):
    def test_a_readable_bucket_passes(self):
        client = FakeClient()
        result = precheck_bucket(CFG, None, client=client)
        self.assertEqual(result["result"], PASS)
        self.assertEqual(client.calls, 1)

    def test_403_is_a_credentials_failure(self):
        result = precheck_bucket(CFG, None,
                                 client=FakeClient(FakeS3Error(403, "AccessDenied")))
        self.assertEqual(result["result"], FAIL)
        self.assertIn("credentials", result["detail"])

    def test_404_is_a_missing_bucket(self):
        result = precheck_bucket(CFG, None,
                                 client=FakeClient(FakeS3Error(404, "NoSuchBucket")))
        self.assertEqual(result["result"], FAIL)
        self.assertIn("not found", result["detail"])

    def test_anything_else_is_unknown_not_a_failure(self):
        # A timeout or a proxy error says nothing about the credentials, and
        # reporting a failure would send the operator to re-check keys that are
        # fine.
        for error in (FakeS3Error(500, "InternalError"), OSError("connection reset")):
            with self.subTest(error=error):
                result = precheck_bucket(CFG, None, client=FakeClient(error))
                self.assertEqual(result["result"], UNKNOWN)


class TestPrecheckShareUrl(unittest.TestCase):
    def test_an_unset_url_is_unknown(self):
        result = precheck_share_url("", None, opener=_status(200))
        self.assertEqual(result["result"], UNKNOWN)
        self.assertIn("not set", result["detail"])

    def test_401_is_the_good_answer(self):
        result = precheck_share_url("https://share.example.com", None,
                                    opener=_http_error(401))
        self.assertEqual(result["result"], PASS)

    def test_anything_else_is_unknown_never_a_failure(self):
        # This check runs from inside the deployment and cannot answer whether a
        # recipient out on the internet reaches the URL, so it is informational
        # and must never contribute a finding to the verdict.
        for opener in (_status(200), _http_error(404),
                       _raises(urllib.error.URLError("name resolution failed"))):
            with self.subTest(opener=opener):
                result = precheck_share_url("https://share.example.com", None,
                                            opener=opener)
                self.assertEqual(result["result"], UNKNOWN)

    def test_it_asks_for_the_shares_listing_under_the_contract_prefix(self):
        seen = {}

        def opener(url, timeout=None, context=None):
            seen["url"] = url
            raise urllib.error.HTTPError("u", 401, "", {}, None)
        precheck_share_url("https://share.example.com/", None, opener=opener)
        self.assertEqual(seen["url"],
                         "https://share.example.com" + render.ENDPOINT_PREFIX + "/shares")


if __name__ == "__main__":
    unittest.main()
