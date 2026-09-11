"""The support bundle: what it carries, and what it must not.

The whole point of the archive is that it can be attached to a support case
without the operator reading it first, so the load-bearing assertion in this file
is the negative one — none of the three credentials appears in any member, in any
file, in any form. It is asserted against the bytes of the archive after it is
built, not against the inputs, because that is the artefact that leaves the host.

The log fixture deliberately carries a *previous* run's secret and token as well
as the current ones: `server.log` is opened in append mode and never truncated,
so the run most likely to still be echoed in it is the one that already ended,
and only `Supervisor._redact_with_history` knows those.
"""
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest

import bundle
import persist
import render
import supervise
import tls
from app import App
from auth import Auth
from supervise import Supervisor
from tests.test_app import offline_opener

# Distinctive enough that a substring match cannot pass by accident, and that a
# masked file cannot still contain one of them as a fragment of something else.
SECRET = "s3cr3t-KEY-ZZZQQQ-current"
ACCESS_KEY = "AKIAZZZQQQCURRENT"
TOKEN = "bearer-ZZZQQQ-current-0123456789abcdef"
OLD_SECRET = "s3cr3t-KEY-ZZZQQQ-previous"
OLD_TOKEN = "bearer-ZZZQQQ-previous-0123456789abcdef"

CFG = {
    "platform": "ring",
    "endpoint_mode": "trusted",
    "s3_endpoint": "https://s3.example.com",
    "bucket": "bkt",
    "access_key": ACCESS_KEY,
    "secret_key": SECRET,
    "region": "us-east-1",
    "share_public_url": "https://share.example.com",
    "ca_pem_sha256": "",
    "tables": [{"prefix": "data/customers", "share": "scality",
                "schema": "poc", "table": "customers"}],
}

# What a JVM config-parse failure actually looks like: the configuration echoed
# back, including the credentials, from this run and from the one before it.
LOG = """\
INFO  server - starting
ERROR server - could not parse configuration:
  fs.s3a.access.key=%s
  fs.s3a.secret.key=%s
  bearerToken=%s
INFO  server - restarting after the previous run
  previous fs.s3a.secret.key=%s
  previous bearerToken=%s
""" % (ACCESS_KEY, SECRET, TOKEN, OLD_SECRET, OLD_TOKEN)

CA_PEM = "-----BEGIN CERTIFICATE-----\nnot-a-real-certificate\n-----END CERTIFICATE-----\n"

ALL_SECRETS = (SECRET, ACCESS_KEY, TOKEN, OLD_SECRET, OLD_TOKEN)


def members(payload):
    """{name: text} for every member of the archive."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for info in tar.getmembers():
            out[info.name] = tar.extractfile(info).read().decode()
    return out


class BundleCase(unittest.TestCase):
    """A config directory holding a real deployment, with real credentials in
    every file that holds one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        render.write_config(CFG, TOKEN, self.dir)
        persist.save(self.dir, CFG, "2027-01-01T00:00:00Z", "hash-abc")
        with open(os.path.join(self.dir, supervise.LOG_NAME), "w") as handle:
            handle.write(LOG)
        with open(os.path.join(self.dir, tls.CA_FILE), "w") as handle:
            handle.write(CA_PEM)

        # A real Supervisor, so the redactor under test is the shipped one and
        # `_last_good` carries the previous run exactly as a live one would.
        self.sup = Supervisor(self.dir, ["/bin/true"], {})
        self.sup._last_good = {"cfg": {"secret_key": OLD_SECRET},
                               "launcher": ["/bin/true"], "token": OLD_TOKEN}
        self.app = App(self.sup, Auth(), self.dir, opener=offline_opener)
        self.app._cfg = dict(CFG)
        self.app._token = TOKEN
        self.app._expires = "2027-01-01T00:00:00Z"
        self.app._applied_hash = "hash-abc"
        self.app._verdict = {"hash": "hash-abc", "checks": [
            {"id": "rest_endpoint", "result": "pass", "detail": "403 as expected"},
            {"id": "signature_required", "result": "fail",
             "detail": "the stripped URL returned 200"},
        ]}

    def build(self, redact=None):
        return bundle.build(
            self.dir, self.app.get_status(),
            (SECRET, ACCESS_KEY, TOKEN),
            redact or self.sup._redact_with_history)


class TestContents(BundleCase):
    def test_every_member_is_present(self):
        found = members(self.build())
        for name in ("README.txt", "version.json", "status.json", "checks.txt",
                     "setup.json", render.CORE_SITE_FILE, render.SERVER_YAML_FILE,
                     "server.log", "ca.pem"):
            self.assertIn(name, found)

    def test_the_masked_files_still_parse(self):
        # A redacted file nobody can read back is evidence of nothing. Both
        # parsers are the ones the product itself uses to reconstruct a
        # deployment from disk.
        found = members(self.build())
        properties = render.parse_core_site(found[render.CORE_SITE_FILE])
        self.assertEqual(properties["fs.s3a.endpoint"], CFG["s3_endpoint"])
        self.assertEqual(properties["fs.s3a.endpoint.region"], CFG["region"])
        self.assertEqual(properties["fs.s3a.secret.key"], bundle.MASK)
        self.assertEqual(properties["fs.s3a.access.key"], bundle.MASK)

        document = render.parse_server_yaml(found[render.SERVER_YAML_FILE])
        self.assertEqual(document["bucket"], CFG["bucket"])
        self.assertEqual(document["tables"], CFG["tables"])
        self.assertEqual(document["token"], bundle.MASK)

    def test_the_whole_log_is_carried_not_the_tail(self):
        found = members(self.build())
        self.assertIn("starting", found["server.log"])
        self.assertIn("restarting after the previous run", found["server.log"])
        self.assertEqual(found["server.log"].count("\n"), LOG.count("\n"))

    def test_the_checks_file_carries_one_line_per_check(self):
        found = members(self.build())
        lines = found["checks.txt"].strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("PASS rest_endpoint — "))
        self.assertTrue(lines[1].startswith("FAIL signature_required — "))

    def test_an_absent_verdict_says_so_rather_than_producing_an_empty_file(self):
        self.app._verdict = None
        found = members(self.build())
        self.assertIn("no verdict", found["checks.txt"])

    def test_the_status_carries_the_version_and_drops_the_access_key(self):
        found = members(self.build())
        status = json.loads(found["status.json"])
        self.assertNotIn("access_key", status["config"])
        self.assertNotIn("secret_key", status["config"])
        self.assertEqual(status["config"]["bucket"], CFG["bucket"])
        self.assertEqual(json.loads(found["version.json"]), status["version"])

    def test_a_status_carrying_the_secret_key_refuses_to_be_bundled(self):
        # The bundle's own README says the secret was never in this member. If a
        # change ever puts it back, failing here is the only honest answer.
        status = self.app.get_status()
        status["config"]["secret_key"] = SECRET
        with self.assertRaises(AssertionError):
            bundle.build(self.dir, status, (SECRET,), self.sup._redact_with_history)

    def test_a_missing_file_is_skipped_rather_than_fatal(self):
        os.remove(os.path.join(self.dir, tls.CA_FILE))
        os.remove(os.path.join(self.dir, supervise.LOG_NAME))
        found = members(self.build())
        self.assertNotIn("ca.pem", found)
        self.assertNotIn("server.log", found)
        self.assertIn(render.CORE_SITE_FILE, found)

    def test_the_readme_says_what_was_redacted_and_that_nothing_was_sent(self):
        found = members(self.build())["README.txt"]
        self.assertIn("not uploaded", found)
        self.assertIn(bundle.MASK, found)
        self.assertIn("server.log", found)

    def test_two_builds_of_an_unchanged_directory_are_identical(self):
        # Deterministic timestamps: "is this the same bundle you sent yesterday"
        # is then a checksum comparison rather than a diff of nine files.
        self.assertEqual(self.build(), self.build())


class TestNoCredentialSurvives(BundleCase):
    def test_no_secret_token_or_access_key_appears_in_any_member(self):
        found = members(self.build())
        for name, text in found.items():
            for secret in ALL_SECRETS:
                self.assertNotIn(secret, text, "%s carries a credential" % name)

    def test_none_of_them_appears_in_the_raw_archive_bytes_either(self):
        # Members are read individually above; this reads the artefact that
        # actually leaves the host, headers and all.
        payload = self.build()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            raw = b"".join(tar.extractfile(i).read() for i in tar.getmembers())
        for secret in ALL_SECRETS:
            self.assertNotIn(secret.encode(), raw)

    def test_the_previous_runs_credentials_are_masked_by_history(self):
        # Neither is in `secrets`; only `_last_good` knows them, which is why the
        # Supervisor's redactor is the one passed in rather than a plain replace.
        found = members(self.build())["server.log"]
        self.assertNotIn(OLD_SECRET, found)
        self.assertNotIn(OLD_TOKEN, found)
        self.assertIn(bundle.MASK, found)

    def test_the_per_file_masking_stands_on_its_own(self):
        # Built with a redactor that does nothing, so the two rendered files are
        # protected by `mask_core_site` / `mask_server_yaml` alone. The redact
        # pass is belt and braces; if it were the only thing masking these files,
        # a caller that forgot to pass a secret would ship one.
        found = members(self.build(redact=lambda text, *secrets: text))
        self.assertNotIn(TOKEN, found[render.SERVER_YAML_FILE])
        self.assertNotIn(SECRET, found[render.CORE_SITE_FILE])
        self.assertNotIn(ACCESS_KEY, found[render.CORE_SITE_FILE])


class TestMasking(unittest.TestCase):
    def test_the_yaml_token_line_is_replaced_in_place(self):
        text = render.server_yaml(CFG, TOKEN)
        masked = bundle.mask_server_yaml(text)
        self.assertNotIn(TOKEN, masked)
        self.assertIn('  bearerToken: "%s"' % bundle.MASK, masked)

    def test_the_core_site_credentials_are_replaced_and_nothing_else_is(self):
        text = render.core_site_xml(CFG)
        masked = bundle.mask_core_site(text)
        self.assertNotIn(SECRET, masked)
        self.assertNotIn(ACCESS_KEY, masked)
        self.assertIn(CFG["s3_endpoint"], masked)
        self.assertIn(render.CREDENTIALS_PROVIDER, masked)


if __name__ == "__main__":
    unittest.main()
