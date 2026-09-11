"""The Prometheus exposition: its shape, and what it must never carry.

Two of these tests are the point of the file. The golden outputs pin the exact
text, so a series that changes name or loses a label is a diff rather than a
silently broken dashboard. And the leak test renders a status carrying a real
secret, a real token, an access key and a table name, and asserts that none of
the four reaches the output — a label value travels into a monitoring system the
customer may share, and it stays there for as long as the series does.
"""
import re
import unittest

import metrics
import state as st

SECRET = "s3cr3t-key-do-not-leak"
TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef"
ACCESS_KEY = "AKIALEAKTHISNEVER"
TABLE = "customers_pii"

VERIFIED_STATUS = {
    "state": st.VERIFIED,
    "config": {
        "platform": "ring",
        "endpoint_mode": "private_ca",
        "s3_endpoint": "https://s3.storage.example.com",
        "bucket": "bkt",
        "access_key": ACCESS_KEY,
        "region": "us-east-1",
        "secret_set": True,
        "draft": False,
        "tables": [{"prefix": "data/one", "share": "scality", "schema": "poc",
                    "table": "one"},
                   {"prefix": "data/two", "share": "scality", "schema": "poc",
                    "table": "two"}],
    },
    "version": {"setup": "v1.4.1-scality.2", "server": "1.4.1"},
    "verdict": {"hash": "abc", "verdict_at": "2026-09-11T10:00:00Z",
                "checks": [{"id": "server_listening", "result": "pass", "detail": ""},
                           {"id": "signature", "result": "unknown", "detail": ""},
                           {"id": "suite_completed", "result": "pass", "detail": ""}]},
    "verdict_at": "2026-09-11T10:00:00Z",
    "token_expires": "2026-12-10T10:00:00Z",
    "warnings": [],
    "prechecks": [],
    "ca": {"present": True, "sha256": "ff"},
}

UNCONFIGURED_STATUS = {
    "state": st.UNCONFIGURED,
    "config": None,
    "version": {"setup": "dev", "server": ""},
    "verdict": None,
    "verdict_at": "",
    "token_expires": "",
    "warnings": [],
    "prechecks": [],
    "ca": {"present": False, "sha256": ""},
}

VERIFIED_GOLDEN = """\
# HELP opensharing_setup_info Versions of the setup image and the sharing server it supervises.
# TYPE opensharing_setup_info gauge
opensharing_setup_info{image="v1.4.1-scality.2",server="1.4.1"} 1
# HELP opensharing_state The setup tool's state; exactly one series is 1.
# TYPE opensharing_state gauge
opensharing_state{state="unconfigured"} 0
opensharing_state{state="never_verified"} 0
opensharing_state{state="verified"} 1
opensharing_state{state="degraded"} 0
opensharing_state{state="stopped"} 0
opensharing_state{state="failed_start"} 0
# HELP opensharing_server_running 1 when the sharing server is up and serving.
# TYPE opensharing_server_running gauge
opensharing_server_running 1
# HELP opensharing_tables_shared How many tables are shared. The names are not exposed; read them on the setup page.
# TYPE opensharing_tables_shared gauge
opensharing_tables_shared 2
# HELP opensharing_endpoint_mode How the S3 endpoint is reached. The endpoint hostname is not exposed; read it on the setup page.
# TYPE opensharing_endpoint_mode gauge
opensharing_endpoint_mode{mode="trusted"} 0
opensharing_endpoint_mode{mode="private_ca"} 1
opensharing_endpoint_mode{mode="http"} 0
# HELP opensharing_check Last verdict per check: 1 pass, 0 fail, -1 could not run. A check about one shared table carries that table's position in the configuration rather than its name; the setup page maps position to name.
# TYPE opensharing_check gauge
opensharing_check{id="server_listening"} 1
opensharing_check{id="signature"} -1
opensharing_check{id="suite_completed"} 1
# HELP opensharing_last_verdict_timestamp_seconds When the checks behind opensharing_check last ran.
# TYPE opensharing_last_verdict_timestamp_seconds gauge
opensharing_last_verdict_timestamp_seconds 1789120800
# HELP opensharing_token_expiry_timestamp_seconds The date stamped on the recipient token. Nothing enforces it.
# TYPE opensharing_token_expiry_timestamp_seconds gauge
opensharing_token_expiry_timestamp_seconds 1796896800
"""

UNCONFIGURED_GOLDEN = """\
# HELP opensharing_setup_info Versions of the setup image and the sharing server it supervises.
# TYPE opensharing_setup_info gauge
opensharing_setup_info{image="dev",server=""} 1
# HELP opensharing_state The setup tool's state; exactly one series is 1.
# TYPE opensharing_state gauge
opensharing_state{state="unconfigured"} 1
opensharing_state{state="never_verified"} 0
opensharing_state{state="verified"} 0
opensharing_state{state="degraded"} 0
opensharing_state{state="stopped"} 0
opensharing_state{state="failed_start"} 0
# HELP opensharing_server_running 1 when the sharing server is up and serving.
# TYPE opensharing_server_running gauge
opensharing_server_running 0
# HELP opensharing_tables_shared How many tables are shared. The names are not exposed; read them on the setup page.
# TYPE opensharing_tables_shared gauge
opensharing_tables_shared 0
# HELP opensharing_endpoint_mode How the S3 endpoint is reached. The endpoint hostname is not exposed; read it on the setup page.
# TYPE opensharing_endpoint_mode gauge
opensharing_endpoint_mode{mode="trusted"} 0
opensharing_endpoint_mode{mode="private_ca"} 0
opensharing_endpoint_mode{mode="http"} 0
"""

LINE = re.compile(r"^[a-z_]+(\{[^}]*\})? -?[0-9.e+]+$")


class TestGolden(unittest.TestCase):
    def test_a_verified_deployment(self):
        self.assertEqual(metrics.render(VERIFIED_STATUS), VERIFIED_GOLDEN)

    def test_an_unconfigured_deployment(self):
        # No verdict and no token, so neither timestamp gauge appears at all.
        # Emitting them as 0 would put the deployment at the epoch on a graph.
        self.assertEqual(metrics.render(UNCONFIGURED_STATUS), UNCONFIGURED_GOLDEN)

    def test_rendering_twice_gives_the_same_bytes(self):
        self.assertEqual(metrics.render(VERIFIED_STATUS),
                         metrics.render(VERIFIED_STATUS))

    def test_an_empty_status_still_renders(self):
        # A scrape that arrives before anything is known must not 500.
        out = metrics.render({})
        self.assertIn("opensharing_server_running 0", out)


class TestFormat(unittest.TestCase):
    def _lines(self, text):
        return [ln for ln in text.strip().split("\n") if not ln.startswith("#")]

    def test_every_sample_line_matches_the_exposition_shape(self):
        for status in (VERIFIED_STATUS, UNCONFIGURED_STATUS, {}):
            for line in self._lines(metrics.render(status)):
                self.assertRegex(line, LINE, line)

    def test_every_metric_is_typed(self):
        text = metrics.render(VERIFIED_STATUS)
        typed = {ln.split()[2] for ln in text.split("\n") if ln.startswith("# TYPE")}
        for line in self._lines(text):
            name = line.split("{")[0].split(" ")[0]
            self.assertIn(name, typed, line)

    def test_exactly_one_state_series_is_one(self):
        for name in metrics.STATES:
            text = metrics.render({"state": name})
            ones = [ln for ln in text.split("\n")
                    if ln.startswith("opensharing_state{") and ln.endswith(" 1")]
            self.assertEqual(len(ones), 1, name)

    def test_the_state_list_matches_the_state_module(self):
        # A state that exists in state.py and not here would expose no series at
        # all for the deployment sitting in it.
        self.assertEqual(sorted(metrics.STATES),
                         sorted([st.UNCONFIGURED, st.NEVER_VERIFIED, st.VERIFIED,
                                 st.DEGRADED, st.STOPPED, st.FAILED_START]))

    def test_running_is_zero_in_every_state_with_no_server(self):
        for name in metrics.STATES:
            text = metrics.render({"state": name})
            expected = 0 if name in ("unconfigured", "stopped", "failed_start") else 1
            self.assertIn("opensharing_server_running %d" % expected, text, name)

    def test_a_label_value_is_escaped(self):
        text = metrics.render({"version": {"setup": 'a"b\\c\nd', "server": ""}})
        self.assertIn(r'image="a\"b\\c\nd"', text)

    def test_a_failed_check_is_zero_and_an_unknown_is_minus_one(self):
        text = metrics.render({"verdict": {"checks": [
            {"id": "a", "result": "fail"}, {"id": "b", "result": "unknown"}]}})
        self.assertIn('opensharing_check{id="a"} 0', text)
        self.assertIn('opensharing_check{id="b"} -1', text)

    def test_an_unparseable_timestamp_costs_one_gauge_not_the_scrape(self):
        text = metrics.render(dict(VERIFIED_STATUS, token_expires="whenever"))
        self.assertNotIn("opensharing_token_expiry_timestamp_seconds", text)
        self.assertIn("opensharing_last_verdict_timestamp_seconds", text)


class TestNoLeak(unittest.TestCase):
    """The output goes to a monitoring system the customer may share, and stays
    there. Nothing that identifies the storage, the credential or the data may
    appear in it."""

    def status(self):
        status = {
            "state": st.VERIFIED,
            "config": dict(VERIFIED_STATUS["config"],
                           access_key=ACCESS_KEY,
                           secret_key=SECRET,
                           s3_endpoint="https://s3.storage.example.com",
                           tables=[{"prefix": "data/%s" % TABLE, "share": "scality",
                                    "schema": "poc", "table": TABLE}]),
            "version": {"setup": "v1.4.1-scality.2", "server": "1.4.1"},
            # The gate suite names a per-table check after the table, and a
            # listing check after the share: the ids the real suite emits, which
            # is where the first version of this leaked.
            "verdict": {"hash": "abc",
                        "checks": [{"id": "server_listening", "result": "pass",
                                    "detail": "token %s reached %s" % (TOKEN, TABLE)},
                                   {"id": "listing_scality", "result": "pass",
                                    "detail": "listed"},
                                   {"id": "query_url_host_scality.poc.%s" % TABLE,
                                    "result": "pass", "detail": "1 URL"},
                                   {"id": "unsigned_fetch_refused_scality.poc.%s" % TABLE,
                                    "result": "fail", "detail": "public"}]},
            "verdict_at": "2026-09-11T10:00:00Z",
            "token_expires": "2026-12-10T10:00:00Z",
            "token": TOKEN,
        }
        return status

    def test_no_secret_token_key_or_table_name_is_rendered(self):
        text = metrics.render(self.status())
        for forbidden in (SECRET, TOKEN, ACCESS_KEY, TABLE):
            self.assertNotIn(forbidden, text)

    def test_the_storage_hostname_is_not_rendered(self):
        text = metrics.render(self.status())
        self.assertNotIn("s3.storage.example.com", text)
        # and the operator is told where to find it instead.
        self.assertIn("setup page", text)

    def test_the_table_count_is_still_reported(self):
        # The leak test must not be satisfiable by rendering nothing.
        self.assertIn("opensharing_tables_shared 1", metrics.render(self.status()))

    def test_a_per_table_check_is_still_reported_by_position(self):
        """Dropping the per-table checks would satisfy the leak test and lose the
        one measurement an operator watches: which gate is failing."""
        text = metrics.render(self.status())
        self.assertIn('opensharing_check{id="unsigned_fetch_refused",table="1"} 0', text)
        self.assertIn('opensharing_check{id="query_url_host",table="1"} 1', text)
        self.assertIn('opensharing_check{id="listing",table="1"} 1', text)
        self.assertIn('opensharing_check{id="server_listening"} 1', text)

    def test_a_check_naming_a_table_that_is_not_configured_keeps_its_id(self):
        """A stale verdict from a previous configuration must not be mangled into
        a different check's name — it keeps its own id, table names included, so
        the operator sees the mismatch. It cannot leak: a table no longer in the
        configuration is also no longer being shared."""
        status = self.status()
        status["config"]["tables"] = [{"prefix": "data/other", "share": "scality",
                                       "schema": "poc", "table": "other"}]
        text = metrics.render(status)
        self.assertIn('id="query_url_host_scality.poc.%s"' % TABLE, text)


if __name__ == "__main__":
    unittest.main()
