"""What survives a restart, and what is read back off disk.

The reconstruction path matters more than it looks: without it a restart shows an
empty form over a server that is still serving a share, and the obvious next move
— fill the form in again — rewrites the bearer token every recipient holds.
"""
import json
import os
import shutil
import stat
import tempfile
import unittest

import cases
import persist
import render
import state


class PersistCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def _apply(self, cfg=cases.BASE, token=cases.TOKEN, expires="2099-01-01T00:00:00Z",
               save=True):
        render.write_config(cfg, token, self.dir)
        if save:
            persist.save(self.dir, cfg, expires, state.config_hash(cfg))
        return cfg, token, expires


class TestSaveAndLoad(PersistCase):
    def test_the_secret_key_is_never_copied_into_setup_json(self):
        # It has to live in core-site.xml, which the server reads. A second copy
        # is a second file to protect and a second file to forget.
        persist.save(self.dir, cases.BASE, "", "h")
        with open(os.path.join(self.dir, persist.SETUP_FILE)) as handle:
            text = handle.read()
        self.assertNotIn(cases.BASE["secret_key"], text)
        self.assertNotIn("secret_key", json.loads(text))

    def test_setup_json_is_private(self):
        persist.save(self.dir, cases.BASE, "", "h")
        path = os.path.join(self.dir, persist.SETUP_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_it_carries_the_expiry_and_the_applied_hash(self):
        persist.save(self.dir, cases.BASE, "2099-01-01T00:00:00Z", "hash-value")
        saved = persist.load(self.dir)
        self.assertEqual(saved["token_expires"], "2099-01-01T00:00:00Z")
        self.assertEqual(saved["applied_hash"], "hash-value")
        self.assertEqual(saved["tables"], cases.BASE["tables"])

    def test_load_returns_none_when_there_is_nothing_saved(self):
        self.assertIsNone(persist.load(self.dir))

    def test_a_corrupt_file_reads_as_absent_rather_than_raising(self):
        # The two files the server reads are the authority on what is deployed;
        # reconstruct rebuilds the rest from them. Raising here would take the
        # status page down over a file that is only a convenience.
        with open(os.path.join(self.dir, persist.SETUP_FILE), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(persist.load(self.dir))

    def test_a_json_document_that_is_not_an_object_reads_as_absent(self):
        with open(os.path.join(self.dir, persist.SETUP_FILE), "w") as handle:
            handle.write("[1, 2]")
        self.assertIsNone(persist.load(self.dir))

    def test_saving_twice_is_stable(self):
        persist.save(self.dir, cases.BASE, "e", "h")
        with open(os.path.join(self.dir, persist.SETUP_FILE)) as handle:
            first = handle.read()
        persist.save(self.dir, cases.BASE, "e", "h")
        with open(os.path.join(self.dir, persist.SETUP_FILE)) as handle:
            self.assertEqual(handle.read(), first)


class TestReconstruct(PersistCase):
    def test_nothing_on_disk_reconstructs_to_nothing(self):
        self.assertIsNone(persist.reconstruct(self.dir))

    def test_either_server_file_missing_reconstructs_to_nothing(self):
        # One file alone describes a deployment that could not be serving.
        for missing in (render.CORE_SITE_FILE, render.SERVER_YAML_FILE):
            with self.subTest(missing=missing):
                self._apply()
                os.remove(os.path.join(self.dir, missing))
                self.assertIsNone(persist.reconstruct(self.dir))
                shutil.rmtree(self.dir)
                os.mkdir(self.dir)

    def test_a_full_round_trip_returns_the_configuration_that_was_applied(self):
        cfg, token, expires = self._apply(cases.THREE_TABLES)
        restored, restored_token, restored_expires, applied_hash = \
            persist.reconstruct(self.dir)
        self.assertEqual(restored, cfg)
        self.assertEqual(restored_token, token)
        self.assertEqual(restored_expires, expires)
        self.assertEqual(applied_hash, state.config_hash(cfg))

    def test_the_secret_comes_back_from_core_site(self):
        self._apply()
        restored, _, _, _ = persist.reconstruct(self.dir)
        self.assertEqual(restored["secret_key"], cases.BASE["secret_key"])

    def test_the_bearer_token_comes_back_from_the_yaml(self):
        # This is the one that matters: rewriting the token on a restart would
        # break every recipient's profile.
        self._apply(token="a" * 48)
        _, token, _, _ = persist.reconstruct(self.dir)
        self.assertEqual(token, "a" * 48)

    def test_without_setup_json_a_trusted_https_deployment_is_derived(self):
        self._apply(save=False)
        cfg, _, expires, _ = persist.reconstruct(self.dir)
        self.assertEqual(cfg["platform"], "ring")
        self.assertEqual(cfg["endpoint_mode"], "trusted")
        self.assertEqual(cfg["share_public_url"], "")
        self.assertEqual(cfg["ca_pem_sha256"], "")
        self.assertEqual(expires, "")
        self.assertEqual(cfg["bucket"], cases.BASE["bucket"])
        self.assertEqual(cfg["tables"], cases.BASE["tables"])

    def test_without_setup_json_a_ca_on_disk_means_private_ca(self):
        self._apply(save=False)
        with open(os.path.join(self.dir, "ca.pem"), "w") as handle:
            handle.write("-----BEGIN CERTIFICATE-----\n")
        cfg, _, _, _ = persist.reconstruct(self.dir)
        self.assertEqual(cfg["endpoint_mode"], "private_ca")
        self.assertNotEqual(cfg["ca_pem_sha256"], "")

    def test_without_setup_json_an_http_endpoint_means_http_mode(self):
        self._apply(cases.HTTP_MODE, save=False)
        cfg, _, _, _ = persist.reconstruct(self.dir)
        self.assertEqual(cfg["endpoint_mode"], "http")

    def test_the_applied_hash_is_recomputed_from_disk_not_trusted(self):
        # If the files were edited between restarts, the stored verdict stops
        # matching — which reads as "not verified" rather than as a pass for a
        # configuration nobody checked.
        self._apply()
        persist.save(self.dir, cases.BASE, "", "a-hash-from-a-different-config")
        _, _, _, applied_hash = persist.reconstruct(self.dir)
        self.assertEqual(applied_hash, state.config_hash(cases.BASE))

    def test_a_hand_edited_yaml_changes_the_reconstructed_hash(self):
        cfg, _, _ = self._apply()
        path = os.path.join(self.dir, render.SERVER_YAML_FILE)
        with open(path) as handle:
            text = handle.read()
        with open(path, "w") as handle:
            handle.write(text.replace('name: "customers"', 'name: "renamed"'))
        _, _, _, applied_hash = persist.reconstruct(self.dir)
        self.assertNotEqual(applied_hash, state.config_hash(cfg))

    def test_an_unreadable_yaml_is_refused_rather_than_guessed(self):
        self._apply()
        path = os.path.join(self.dir, render.SERVER_YAML_FILE)
        with open(path, "a") as handle:
            handle.write("something: else\n")
        with self.assertRaises(ValueError):
            persist.reconstruct(self.dir)


if __name__ == "__main__":
    unittest.main()
