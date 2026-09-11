import os
import tempfile
import unittest
from unittest import mock

import version


class TestVersion(unittest.TestCase):
    def test_the_setup_version_is_the_baked_tag_or_dev(self):
        with mock.patch.dict(os.environ, {"SETUP_VERSION": "v1.4.1-scality.3"}):
            self.assertEqual(version.setup_version(), "v1.4.1-scality.3")
        with mock.patch.dict(os.environ, {"SETUP_VERSION": ""}):
            self.assertEqual(version.setup_version(), "dev")

    def test_the_server_version_comes_from_the_jar_name(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("hadoop-aws-3.3.4.jar", "io.delta.delta-sharing-server-1.4.1.jar"):
                open(os.path.join(d, name), "w").close()
            self.assertEqual(version.server_version(d), "1.4.1")
        self.assertEqual(version.server_version("/no/such/dir"), "")

    def test_report_carries_both(self):
        with mock.patch.dict(os.environ, {"SETUP_VERSION": "x"}):
            self.assertEqual(set(version.report()), {"setup", "server"})


if __name__ == "__main__":
    unittest.main()
