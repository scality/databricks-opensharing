import datetime
import hashlib
import hmac
import io
import os
import unittest
import urllib.error
import urllib.parse

import s3

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "s3")

CFG = {"platform": "ring", "endpoint_mode": "trusted",
       "s3_endpoint": "https://s3.example.com:8443", "bucket": "delta-share",
       "access_key": "AKIAIOSFODNN7EXAMPLE",
       "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
       "region": "us-east-1"}

FROZEN = datetime.datetime(2026, 1, 15, 12, 30, 45, tzinfo=datetime.timezone.utc)


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._buf = io.BytesIO(body)

    def getcode(self):
        return self.status

    def read(self, n=None):
        return self._buf.read() if n is None else self._buf.read(n)


class FakeOpener:
    """Answers a listing request from a fixture chosen by (prefix, continuation token).

    Every call is recorded so a test can assert which prefixes were listed and how
    many requests the scan cost.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        prefix = query.get("prefix", [""])[0]
        token = query.get("continuation-token", [None])[0]
        self.calls.append((req.get_method(), prefix, token))
        try:
            name = self.routes[(prefix, token)]
        except KeyError:
            raise AssertionError("unexpected listing: prefix=%r token=%r" % (prefix, token))
        return FakeResponse(fixture(name))


def client_for(routes, cfg=None):
    opener = FakeOpener(routes)
    return s3.S3Client(cfg or CFG, None, opener=opener), opener


class TestCanonicalisation(unittest.TestCase):
    def test_query_is_sorted_and_slash_is_escaped(self):
        q = "prefix=raw data/&list-type=2&delimiter=/"
        self.assertEqual(s3.canonical_query(q),
                         "delimiter=%2F&list-type=2&prefix=raw%20data%2F")

    def test_a_key_with_an_empty_value_keeps_its_equals_sign(self):
        self.assertEqual(s3.canonical_query("uploads=&list-type=2"),
                         "list-type=2&uploads=")

    def test_path_is_encoded_segment_by_segment(self):
        self.assertEqual(s3.canonical_uri("/delta-share/raw data/part 1.parquet"),
                         "/delta-share/raw%20data/part%201.parquet")

    def test_an_already_encoded_path_is_not_encoded_twice(self):
        self.assertEqual(s3.canonical_uri("/delta-share/raw%20data"),
                         "/delta-share/raw%20data")


class TestSigV4(unittest.TestCase):
    """Known-answer test. The expectation is derived here from the AWS reference
    algorithm rather than read back out of s3.py, so a change to either side shows."""

    URL = ("https://s3.example.com:8443/delta-share"
           "?list-type=2&prefix=raw%20data%2F&delimiter=%2F")

    def expected_canonical_request(self):
        return "\n".join([
            "GET",
            "/delta-share",
            "delimiter=%2F&list-type=2&prefix=raw%20data%2F",
            "host:s3.example.com:8443",
            "x-amz-content-sha256:" + hashlib.sha256(b"").hexdigest(),
            "x-amz-date:20260115T123045Z",
            "",
            "host;x-amz-content-sha256;x-amz-date",
            hashlib.sha256(b"").hexdigest(),
        ])

    def expected_signature(self):
        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        creq = self.expected_canonical_request()
        scope = "20260115/us-east-1/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", "20260115T123045Z", scope,
                             hashlib.sha256(creq.encode("utf-8")).hexdigest()])
        k = sign(("AWS4" + CFG["secret_key"]).encode("utf-8"), "20260115")
        k = sign(k, "us-east-1")
        k = sign(k, "s3")
        k = sign(k, "aws4_request")
        return hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    def test_canonical_request_matches_the_reference_form(self):
        creq, host = s3.canonical_request("GET", self.URL, "20260115T123045Z",
                                          hashlib.sha256(b"").hexdigest())
        self.assertEqual(host, "s3.example.com:8443")
        self.assertEqual(creq, self.expected_canonical_request())

    def test_authorization_header_carries_the_expected_signature(self):
        headers = s3.sign_v4("GET", self.URL, {}, hashlib.sha256(b"").hexdigest(),
                             CFG["access_key"], CFG["secret_key"], CFG["region"],
                             now=FROZEN)
        self.assertEqual(headers["x-amz-date"], "20260115T123045Z")
        self.assertEqual(headers["host"], "s3.example.com:8443")
        self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=%s/20260115/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=%s"
            % (CFG["access_key"], self.expected_signature()))

    def test_the_signature_is_a_real_hex_digest(self):
        # Guards against an expectation that is itself empty or malformed.
        sig = self.expected_signature()
        self.assertEqual(len(sig), 64)
        int(sig, 16)

    def test_a_different_region_gives_a_different_signature(self):
        a = s3.sign_v4("GET", self.URL, {}, hashlib.sha256(b"").hexdigest(),
                       CFG["access_key"], CFG["secret_key"], "us-east-1", now=FROZEN)
        b = s3.sign_v4("GET", self.URL, {}, hashlib.sha256(b"").hexdigest(),
                       CFG["access_key"], CFG["secret_key"], "eu-west-1", now=FROZEN)
        self.assertNotEqual(a["Authorization"], b["Authorization"])


class TestRequestShape(unittest.TestCase):
    def test_head_bucket_uses_head_on_the_bucket_path(self):
        seen = {}

        def opener(req, timeout=None, context=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return FakeResponse(b"")

        s3.S3Client(CFG, None, opener=opener).head_bucket()
        self.assertEqual(seen["method"], "HEAD")
        self.assertEqual(seen["url"], "https://s3.example.com:8443/delta-share")
        self.assertIn("authorization", seen["headers"])
        self.assertEqual(seen["headers"]["x-amz-content-sha256"],
                         hashlib.sha256(b"").hexdigest())

    def test_listing_url_is_path_style_with_list_type_2(self):
        client, opener = client_for({("", None): "flat_root.xml"})
        client.list_objects_v2()
        self.assertEqual(opener.calls, [("GET", "", None)])

    def test_listing_reports_common_prefixes_and_keys(self):
        client, _ = client_for({("customers/", None): "flat_customers.xml"})
        keys, common, token = client.list_objects_v2(prefix="customers/")
        self.assertEqual(keys, ["customers/part-00000.snappy.parquet"])
        self.assertEqual(common, ["customers/_delta_log/"])
        self.assertIsNone(token)

    def test_a_truncated_page_reports_its_continuation_token(self):
        client, _ = client_for({("", None): "paged_root_p1.xml"})
        _, common, token = client.list_objects_v2()
        self.assertEqual(common, ["alpha/"])
        self.assertEqual(token, "tok-page-2")


class TestErrors(unittest.TestCase):
    @staticmethod
    def raising(status, body):
        def opener(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, status, "err", {},
                                         io.BytesIO(body))
        return opener

    def test_head_bucket_403_raises_with_the_status(self):
        client = s3.S3Client(CFG, None,
                             opener=self.raising(403, fixture("error_access_denied.xml")))
        with self.assertRaises(s3.S3Error) as ctx:
            client.head_bucket()
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "AccessDenied")

    def test_head_bucket_404_raises_with_the_status(self):
        client = s3.S3Client(CFG, None,
                             opener=self.raising(404, fixture("error_no_such_bucket.xml")))
        with self.assertRaises(s3.S3Error) as ctx:
            client.head_bucket()
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "NoSuchBucket")

    def test_a_non_xml_error_body_still_reports_the_status(self):
        client = s3.S3Client(CFG, None, opener=self.raising(502, b"<html>bad gateway"))
        with self.assertRaises(s3.S3Error) as ctx:
            client.head_bucket()
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "")


class TestGet(unittest.TestCase):
    URL = ("https://s3.example.com:8443/delta-share/customers/part-00000.parquet"
           "?X-Amz-Signature=deadbeef&X-Amz-Expires=3600")

    def test_a_200_returns_the_body_bytes(self):
        seen = {}

        def opener(req, timeout=None, context=None):
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return FakeResponse(b"PAR1and then some")

        body = s3.S3Client(CFG, None, opener=opener).get(self.URL)
        self.assertEqual(body, b"PAR1and then some")
        # A presigned URL carries its own signature; signing it again would break it.
        self.assertNotIn("authorization", seen["headers"])

    def test_a_403_raises_with_the_code_from_the_body(self):
        def opener(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(fixture("error_access_denied.xml")))

        with self.assertRaises(s3.S3Error) as ctx:
            s3.S3Client(CFG, None, opener=opener).get(self.URL)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "AccessDenied")
        self.assertEqual(ctx.exception.message, "Access Denied")

    def test_a_non_2xx_response_that_is_not_raised_still_becomes_an_error(self):
        # A fake or a proxy may hand back a 403 response object instead of raising.
        client = s3.S3Client(CFG, None, opener=lambda req, timeout=None, context=None:
                             FakeResponse(fixture("error_access_denied.xml"), status=403))
        with self.assertRaises(s3.S3Error) as ctx:
            client.get(self.URL)
        self.assertEqual(ctx.exception.status, 403)


FLAT = {("", None): "flat_root.xml",
        ("customers/", None): "flat_customers.xml",
        ("orders/", None): "flat_orders.xml"}

NESTED = {("", None): "nested_root.xml",
          ("archive/", None): "nested_archive.xml",
          ("archive/2026/", None): "nested_archive_2026.xml",
          ("archive/2026/orders/", None): "nested_archive_2026_orders.xml",
          ("finance/", None): "nested_finance.xml",
          ("finance/customers/", None): "nested_finance_customers.xml"}

PAGED = {("", None): "paged_root_p1.xml",
         ("", "tok-page-2"): "paged_root_p2.xml",
         ("alpha/", None): "paged_alpha.xml",
         ("beta/", None): "paged_beta.xml"}


class TestDiscover(unittest.TestCase):
    def test_a_flat_bucket_yields_one_table_per_prefix(self):
        client, _ = client_for(FLAT)
        tables, truncated = s3.discover_delta_tables(client)
        self.assertFalse(truncated)
        self.assertEqual(tables, [
            {"prefix": "customers", "share": "delta-share", "schema": "default",
             "table": "customers"},
            {"prefix": "orders", "share": "delta-share", "schema": "default",
             "table": "orders"},
        ])

    def test_a_nested_layout_finds_tables_at_depth_two_and_three(self):
        client, opener = client_for(NESTED)
        tables, truncated = s3.discover_delta_tables(client)
        self.assertFalse(truncated)
        self.assertEqual(
            sorted(t["prefix"] for t in tables),
            ["archive/2026/orders", "finance/customers"])
        by_prefix = {t["prefix"]: t for t in tables}
        self.assertEqual(by_prefix["finance/customers"]["schema"], "finance")
        self.assertEqual(by_prefix["finance/customers"]["table"], "customers")
        self.assertEqual(by_prefix["archive/2026/orders"]["schema"], "2026")
        self.assertEqual(by_prefix["archive/2026/orders"]["table"], "orders")

    def test_a_table_prefix_is_not_descended_into(self):
        # finance/customers/ has a partition directory beside _delta_log/; listing it
        # would be wasted requests and would invent a table per partition.
        client, opener = client_for(NESTED)
        s3.discover_delta_tables(client)
        listed = [prefix for _, prefix, _ in opener.calls]
        self.assertNotIn("finance/customers/date=2026-01-01/", listed)

    def test_a_paginated_listing_is_followed_to_the_last_page(self):
        client, opener = client_for(PAGED)
        tables, truncated = s3.discover_delta_tables(client)
        self.assertFalse(truncated)
        self.assertEqual([t["prefix"] for t in tables], ["alpha", "beta"])
        self.assertIn(("GET", "", "tok-page-2"), opener.calls)

    def test_a_table_at_the_bucket_root_is_found(self):
        client, _ = client_for({("", None): "root_table.xml"})
        tables, truncated = s3.discover_delta_tables(client)
        self.assertFalse(truncated)
        self.assertEqual(tables, [
            {"prefix": "", "share": "delta-share", "schema": "default",
             "table": "delta-share"},
        ])

    def test_duplicate_names_are_suffixed(self):
        # Two different prefixes whose last two segments fold to the same name.
        routes = {("", None): "dup_root.xml",
                  ("eu/", None): "dup_eu.xml",
                  ("us/", None): "dup_us.xml",
                  ("eu/sales/", None): "dup_eu_sales.xml",
                  ("us/sales/", None): "dup_us_sales.xml",
                  ("eu/sales/orders/", None): "dup_eu_orders.xml",
                  ("us/sales/orders/", None): "dup_us_orders.xml"}
        client, _ = client_for(routes)
        tables, _ = s3.discover_delta_tables(client)
        self.assertEqual([(t["schema"], t["table"]) for t in tables],
                         [("sales", "orders"), ("sales", "orders_2")])

    def test_the_request_budget_reports_truncation(self):
        client, opener = client_for(NESTED)
        tables, truncated = s3.discover_delta_tables(client, max_requests=1)
        self.assertTrue(truncated)
        self.assertEqual(tables, [])
        self.assertEqual(len(opener.calls), 1)

    def test_the_depth_limit_reports_truncation(self):
        client, _ = client_for(NESTED)
        tables, truncated = s3.discover_delta_tables(client, max_depth=2)
        self.assertTrue(truncated)
        # The depth-2 table is still reported; the depth-3 one is out of reach.
        self.assertEqual([t["prefix"] for t in tables], ["finance/customers"])

    def test_a_scan_within_its_limits_is_not_truncated(self):
        client, _ = client_for(NESTED)
        _, truncated = s3.discover_delta_tables(client, max_requests=300, max_depth=4)
        self.assertFalse(truncated)


class TestSanitize(unittest.TestCase):
    def test_spaces_and_slashes_fold_to_underscores(self):
        self.assertEqual(s3.sanitize_name("raw data/2026-01"), "raw_data_2026-01")

    def test_empty_becomes_t(self):
        self.assertEqual(s3.sanitize_name(""), "t")

    def test_only_separators_becomes_t(self):
        self.assertEqual(s3.sanitize_name("///"), "t")

    def test_case_is_folded_down(self):
        self.assertEqual(s3.sanitize_name("Customers"), "customers")

    def test_runs_collapse_and_edges_are_stripped(self):
        self.assertEqual(s3.sanitize_name("  a  b  "), "a_b")

    def test_hyphen_and_underscore_survive(self):
        self.assertEqual(s3.sanitize_name("a-b_c"), "a-b_c")

    def test_names_are_capped_at_64_characters(self):
        self.assertEqual(len(s3.sanitize_name("x" * 200)), 64)

    def test_non_ascii_is_replaced(self):
        self.assertEqual(s3.sanitize_name("café"), "caf")


if __name__ == "__main__":
    unittest.main()
