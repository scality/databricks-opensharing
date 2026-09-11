"""Trust material: the Python side, and the truststore the JVM reads.

keytool is faked with a script on PATH that records its argv — the assertion is
about how it is invoked, not about what a JDK does with the arguments. The CA
itself is real: a throwaway one generated with openssl in the test's own
temporary directory, so `load_verify_locations` is exercised rather than
stubbed. Tests needing it skip when openssl is absent.
"""
import os
import shutil
import stat
import subprocess
import ssl
import tempfile
import unittest

import tls

OPENSSL = shutil.which("openssl")

FAKE_KEYTOOL = """#!/bin/sh
printf '%s\\n' "$@" > '{record}'
env > '{envfile}'
if [ -e truststore.jks ]; then echo yes > '{preexisting}'; else echo no > '{preexisting}'; fi
echo fake-truststore > truststore.jks
"""

FAILING_KEYTOOL = """#!/bin/sh
echo 'keytool error: java.lang.Exception: Input not an X.509 certificate' >&2
exit 1
"""


def make_ca(directory, name="ca.pem"):
    """A throwaway self-signed CA certificate, written to `directory`."""
    path = os.path.join(directory, name)
    key = os.path.join(directory, "ca.key")
    subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=Example Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
         "-keyout", key, "-out", path],
        check=True, capture_output=True)
    with open(path) as handle:
        return handle.read()


@unittest.skipIf(OPENSSL is None, "openssl is not available")
class TestCaMaterial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One generated CA for the whole class: key generation is the slowest
        # thing in this file and none of these tests need a distinct issuer.
        cls.source = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.source)
        cls.pem = make_ca(cls.source)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_saving_a_ca_returns_its_hash_and_writes_it_readable(self):
        import hashlib
        digest = tls.save_ca_pem(self.pem, self.dir)
        self.assertEqual(digest, hashlib.sha256(self.pem.encode()).hexdigest())
        path = os.path.join(self.dir, tls.CA_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        with open(path) as handle:
            self.assertEqual(handle.read(), self.pem)

    def test_a_paste_that_is_not_a_certificate_is_refused(self):
        with self.assertRaises(Exception):
            tls.save_ca_pem("-----BEGIN CERTIFICATE-----\nnot base64\n"
                            "-----END CERTIFICATE-----\n", self.dir)

    def test_a_rejected_upload_leaves_the_working_ca_in_place(self):
        # The certificate currently on disk may be the one the running server
        # trusts. Validating before writing is what keeps a bad paste from
        # breaking a deployment that was working.
        tls.save_ca_pem(self.pem, self.dir)
        try:
            tls.save_ca_pem("garbage", self.dir)
        except Exception:
            pass
        with open(os.path.join(self.dir, tls.CA_FILE)) as handle:
            self.assertEqual(handle.read(), self.pem)

    def test_ca_sha256_reads_what_is_on_disk(self):
        self.assertEqual(tls.ca_sha256(self.dir), "")
        digest = tls.save_ca_pem(self.pem, self.dir)
        self.assertEqual(tls.ca_sha256(self.dir), digest)

    def test_removing_the_ca_removes_the_truststore_with_it(self):
        # A truststore built from a CA that is no longer configured is a trust
        # decision nobody made.
        tls.save_ca_pem(self.pem, self.dir)
        store = os.path.join(self.dir, tls.TRUSTSTORE)
        with open(store, "w") as handle:
            handle.write("store")
        tls.remove_ca(self.dir)
        self.assertFalse(os.path.exists(os.path.join(self.dir, tls.CA_FILE)))
        self.assertFalse(os.path.exists(store))

    def test_removing_a_ca_that_is_not_there_is_not_an_error(self):
        tls.remove_ca(self.dir)

    def test_a_private_ca_context_trusts_the_uploaded_certificate(self):
        tls.save_ca_pem(self.pem, self.dir)
        context = tls.ssl_context({"endpoint_mode": "private_ca"}, self.dir)
        self.assertIsInstance(context, ssl.SSLContext)
        subjects = [c["subject"] for c in context.get_ca_certs()]
        self.assertTrue(any("Example Test CA" in str(s) for s in subjects), subjects)

    def test_a_private_ca_context_with_no_certificate_raises(self):
        # Silently falling back to the default trust store would produce a
        # context that cannot reach the endpoint, reported as an unexplained
        # handshake failure much later.
        with self.assertRaises(Exception):
            tls.ssl_context({"endpoint_mode": "private_ca"}, self.dir)


class TestSslContextModes(unittest.TestCase):
    def test_http_has_no_context_at_all(self):
        self.assertIsNone(tls.ssl_context({"endpoint_mode": "http"}, "/nonexistent"))

    def test_trusted_uses_the_default_verification(self):
        context = tls.ssl_context({"endpoint_mode": "trusted"}, "/nonexistent")
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class TestJavaToolOptions(unittest.TestCase):
    def test_only_private_ca_needs_the_jvm_pointed_at_a_truststore(self):
        for mode in ("trusted", "http"):
            with self.subTest(mode=mode):
                self.assertIsNone(tls.java_tool_options({"endpoint_mode": mode}, "/config"))

    def test_private_ca_names_the_store_and_its_password(self):
        options = tls.java_tool_options({"endpoint_mode": "private_ca"}, "/config")
        self.assertIn("-Djavax.net.ssl.trustStore=/config/truststore.jks", options)
        self.assertIn("-Djavax.net.ssl.trustStorePassword=changeit", options)


class TestBuildTruststore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin)
        self.record = os.path.join(self.bin, "argv")
        self.envfile = os.path.join(self.bin, "env")
        self.preexisting = os.path.join(self.bin, "preexisting")
        with open(os.path.join(self.dir, tls.CA_FILE), "w") as handle:
            handle.write("-----BEGIN CERTIFICATE-----\n")
        self._install("keytool", FAKE_KEYTOOL.format(
            record=self.record, envfile=self.envfile, preexisting=self.preexisting))
        previous = os.environ["PATH"]
        os.environ["PATH"] = self.bin + os.pathsep + previous
        self.addCleanup(os.environ.__setitem__, "PATH", previous)

    def _install(self, name, text):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, 0o755)
        return path

    def _argv(self):
        with open(self.record) as handle:
            return [line for line in handle.read().split("\n") if line != ""]

    def test_it_imports_the_ca_under_the_expected_alias(self):
        tls.build_truststore(self.dir)
        argv = self._argv()
        self.assertIn("-importcert", argv)
        self.assertIn("-noprompt", argv)
        self.assertEqual(argv[argv.index("-alias") + 1], "storage-ca")
        self.assertEqual(argv[argv.index("-file") + 1], tls.CA_FILE)
        self.assertEqual(argv[argv.index("-keystore") + 1], tls.TRUSTSTORE)

    def test_the_password_never_appears_on_the_command_line(self):
        # argv is readable by any local user through ps and /proc/<pid>/cmdline,
        # and this tool runs on a host beside a customer's storage. keytool's
        # -storepass:env reads the value from the environment, which is readable
        # only by the same user or root.
        tls.build_truststore(self.dir)
        argv = self._argv()
        self.assertNotIn(tls.STOREPASS, argv)
        self.assertNotIn(tls.STOREPASS, " ".join(argv))
        self.assertIn("-storepass:env", argv)
        self.assertEqual(argv[argv.index("-storepass:env") + 1], "STOREPASS")
        with open(self.envfile) as handle:
            self.assertIn("STOREPASS=%s" % tls.STOREPASS, handle.read().split("\n"))

    def test_a_stale_store_is_removed_before_the_import(self):
        # keytool appends. Importing into an existing store would leave the
        # previous CA trusted alongside the new one, so a rotation would
        # silently keep trusting the old issuer.
        with open(os.path.join(self.dir, tls.TRUSTSTORE), "w") as handle:
            handle.write("previous store")
        tls.build_truststore(self.dir)
        with open(self.preexisting) as handle:
            self.assertEqual(handle.read().strip(), "no")

    def test_the_store_is_left_readable_by_the_server(self):
        tls.build_truststore(self.dir)
        store = os.path.join(self.dir, tls.TRUSTSTORE)
        self.assertTrue(os.path.exists(store))
        self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o644)

    def test_it_runs_in_the_config_directory(self):
        # The file and keystore arguments are relative, so the working directory
        # is what makes them resolve.
        tls.build_truststore(self.dir)
        self.assertTrue(os.path.exists(os.path.join(self.dir, tls.TRUSTSTORE)))

    def test_a_keytool_failure_surfaces_its_stderr(self):
        path = self._install("failing-keytool", FAILING_KEYTOOL)
        with self.assertRaises(RuntimeError) as caught:
            tls.build_truststore(self.dir, keytool=path)
        self.assertIn("not an X.509 certificate", str(caught.exception))

    def test_a_failure_leaves_no_truststore_behind(self):
        # A store from a previous CA must not survive a failed rebuild and be
        # mistaken for the new one.
        with open(os.path.join(self.dir, tls.TRUSTSTORE), "w") as handle:
            handle.write("previous store")
        path = self._install("failing-keytool", FAILING_KEYTOOL)
        with self.assertRaises(RuntimeError):
            tls.build_truststore(self.dir, keytool=path)
        self.assertFalse(os.path.exists(os.path.join(self.dir, tls.TRUSTSTORE)))


if __name__ == "__main__":
    unittest.main()
