"""Rendering is the one thing a customer's server actually reads.

The golden files under tests/golden are the point: a change to either rendered
file is a change to what the server parses, so it has to show up as a diff a
reviewer sees rather than as a test that adjusts itself. To refresh them
deliberately, render each case in tests/cases.py and write the two files back.
"""
import json
import os
import re
import stat
import tempfile
import unittest

import cases
import render

GOLDEN = os.path.join(os.path.dirname(__file__), "golden")


def _golden(name):
    with open(os.path.join(GOLDEN, name)) as handle:
        return handle.read()


class TestGoldens(unittest.TestCase):
    def test_rendered_files_match_the_goldens(self):
        for name, cfg in cases.CASES:
            with self.subTest(case=name):
                self.assertEqual(render.core_site_xml(cfg),
                                 _golden("%s.core-site.xml" % name))
                self.assertEqual(render.server_yaml(cfg, cases.TOKEN),
                                 _golden("%s.server.yaml" % name))

    def test_rendering_is_deterministic(self):
        for name, cfg in cases.CASES:
            with self.subTest(case=name):
                self.assertEqual(render.server_yaml(cfg, cases.TOKEN),
                                 render.server_yaml(cfg, cases.TOKEN))


class TestCoreSite(unittest.TestCase):
    def test_every_property_the_server_needs_is_present(self):
        properties = render.parse_core_site(render.core_site_xml(cases.BASE))
        self.assertEqual(properties["fs.s3a.endpoint"], "https://s3.example.com")
        self.assertEqual(properties["fs.s3a.path.style.access"], "true")
        self.assertEqual(properties["fs.s3a.access.key"], cases.BASE["access_key"])
        self.assertEqual(properties["fs.s3a.secret.key"], cases.BASE["secret_key"])
        self.assertEqual(properties["fs.s3a.endpoint.region"], "us-east-1")
        self.assertEqual(properties["fs.s3a.paging.maximum"], "1000")
        self.assertEqual(properties["fs.s3a.aws.credentials.provider"],
                         "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider")

    def test_ssl_is_disabled_only_in_http_mode(self):
        # Plain HTTP needs the flag as well as an http:// endpoint; leaving it at
        # true against an http endpoint fails on the first metadata read.
        for mode, expected in (("trusted", "true"), ("private_ca", "true"),
                               ("http", "false")):
            with self.subTest(mode=mode):
                properties = render.parse_core_site(
                    render.core_site_xml(dict(cases.BASE, endpoint_mode=mode)))
                self.assertEqual(properties["fs.s3a.connection.ssl.enabled"], expected)

    def test_values_are_xml_escaped(self):
        cfg = dict(cases.BASE, secret_key='a&b<c>"d"')
        text = render.core_site_xml(cfg)
        self.assertNotIn("a&b<c>", text)
        self.assertEqual(render.parse_core_site(text)["fs.s3a.secret.key"], 'a&b<c>"d"')

    def test_a_document_type_declaration_is_refused(self):
        # Only stdlib parsers are available, so entity declarations are refused
        # before the text reaches one rather than trusted to be harmless.
        with self.assertRaises(ValueError):
            render.parse_core_site(
                '<!DOCTYPE configuration [<!ENTITY x "y">]>\n<configuration/>')


class TestServerYaml(unittest.TestCase):
    def test_tables_group_by_share_and_schema_in_first_seen_order(self):
        text = render.server_yaml(cases.THREE_TABLES, "tok")
        self.assertLess(text.index('- name: "poc"'), text.index('- name: "finance"'))
        self.assertLess(text.index('- name: "customers"'), text.index('- name: "orders"'))

    def test_table_id_is_stable_and_location_derived(self):
        self.assertEqual(render.table_id("b", "p/q"), render.table_id("b", "p/q"))
        self.assertNotEqual(render.table_id("b", "p/q"), render.table_id("b", "p/r"))
        self.assertNotEqual(render.table_id("b", "p/q"), render.table_id("c", "p/q"))

    def test_server_contract_values_are_the_module_constants(self):
        text = render.server_yaml(cases.BASE, "tok")
        self.assertIn("port: %d" % render.SERVER_PORT, text)
        self.assertIn('endpoint: "%s"' % render.ENDPOINT_PREFIX, text)
        self.assertIn("preSignedUrlTimeoutSeconds: %d" % render.PRESIGNED_TIMEOUT_SECONDS,
                      text)
        self.assertIn('bearerToken: "tok"', text)


class TestFixedPoint(unittest.TestCase):
    def test_parse_reproduces_tables_bucket_and_token(self):
        for name, cfg in cases.CASES:
            with self.subTest(case=name):
                parsed = render.parse_server_yaml(render.server_yaml(cfg, cases.TOKEN))
                self.assertEqual(parsed["tables"], cfg["tables"])
                self.assertEqual(parsed["bucket"], cfg["bucket"])
                self.assertEqual(parsed["token"], cases.TOKEN)

    def test_anything_other_than_what_we_emit_is_refused(self):
        # The parser reads back a file this tool wrote. Anything else is a file
        # somebody edited by hand, and reconstructing a configuration from a
        # guess is worse than saying the file cannot be read.
        text = render.server_yaml(cases.BASE, cases.TOKEN)
        for bad in (text.replace("historyShared: true", "historyShared: false"),
                    text + "extra: 1\n",
                    text.replace('  bearerToken: "%s"' % cases.TOKEN, ""),
                    text.replace('            location: "s3a://delta-share/'
                                 'opensharing-poc/customers"', "")):
            with self.subTest(bad=bad[-40:]):
                with self.assertRaises(ValueError):
                    render.parse_server_yaml(bad)

    def test_tables_in_more_than_one_bucket_are_refused(self):
        text = render.server_yaml(cases.THREE_TABLES, cases.TOKEN)
        text = text.replace("s3a://delta-share/warehouse", "s3a://other-bucket/warehouse")
        with self.assertRaises(ValueError):
            render.parse_server_yaml(text)


class TestWriteConfig(unittest.TestCase):
    def test_both_files_are_written_private(self):
        with tempfile.TemporaryDirectory() as d:
            render.write_config(cases.BASE, cases.TOKEN, d)
            for name in (render.CORE_SITE_FILE, render.SERVER_YAML_FILE):
                path = os.path.join(d, name)
                self.assertTrue(os.path.exists(path))
                mode = stat.S_IMODE(os.stat(path).st_mode)
                self.assertEqual(mode, 0o600, "%s is %o" % (name, mode))

    def test_umask_is_restored_afterwards(self):
        before = os.umask(0o022)
        os.umask(before)
        with tempfile.TemporaryDirectory() as d:
            render.write_config(cases.BASE, cases.TOKEN, d)
        after = os.umask(0o022)
        os.umask(after)
        self.assertEqual(before, after)

    def test_the_written_files_are_the_rendered_ones(self):
        with tempfile.TemporaryDirectory() as d:
            render.write_config(cases.BASE, cases.TOKEN, d)
            with open(os.path.join(d, render.SERVER_YAML_FILE)) as handle:
                self.assertEqual(handle.read(),
                                 render.server_yaml(cases.BASE, cases.TOKEN))


class TestTokenAndProfile(unittest.TestCase):
    def test_token_is_48_hex_and_not_repeated(self):
        first, second = render.new_token(), render.new_token()
        self.assertRegex(first, r"^[0-9a-f]{48}$")
        self.assertNotEqual(first, second)

    def test_expiry_is_iso_utc(self):
        self.assertRegex(render.token_expiry(90),
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_share_endpoint_uses_the_contract_prefix_without_doubling_the_slash(self):
        self.assertEqual(render.share_endpoint("https://share.example.com"),
                         "https://share.example.com" + render.ENDPOINT_PREFIX)
        self.assertEqual(render.share_endpoint("https://share.example.com/"),
                         "https://share.example.com" + render.ENDPOINT_PREFIX)

    def test_profile_carries_the_endpoint_and_token(self):
        doc = json.loads(render.profile_json("https://share.example.com/delta-sharing",
                                             "tok", "2099-01-01T00:00:00Z"))
        self.assertEqual(doc["shareCredentialsVersion"], 1)
        self.assertEqual(doc["endpoint"], "https://share.example.com/delta-sharing")
        self.assertEqual(doc["bearerToken"], "tok")
        self.assertEqual(doc["expirationTime"], "2099-01-01T00:00:00Z")

    def test_profile_omits_expiry_when_absent(self):
        # An empty string would read as "expires at the epoch", which is a worse
        # claim than saying nothing.
        doc = json.loads(render.profile_json("https://h/delta-sharing", "t", ""))
        self.assertNotIn("expirationTime", doc)


class TestPurity(unittest.TestCase):
    def test_render_never_shells_out(self):
        # Rendering runs on the apply path with the S3 secret and the bearer
        # token in hand. A subprocess here would put one of them a careless
        # refactor away from argv, which any local user can read through ps and
        # /proc/<pid>/cmdline.
        with open(render.__file__) as handle:
            source = handle.read()
        self.assertFalse(re.search(r"^\s*import subprocess", source, re.M), source[:0])
        self.assertNotIn("subprocess", source)


if __name__ == "__main__":
    unittest.main()
