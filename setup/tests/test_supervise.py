import os
import sys
import tempfile
import types
import unittest
from unittest import mock

# supervise.py imports `render` and `tls` lazily, from inside the methods that
# need them, so this suite can run whether or not those sibling modules exist
# yet in this checkout. When a real one is missing, insert a bare stub so the
# `import render` / `import tls` inside supervise.py succeeds; either way, every
# test below monkeypatches the one or two functions it actually exercises, so
# behaviour here never depends on the real modules' own logic.
for _name in ("render", "tls"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            sys.modules[_name] = types.ModuleType(_name)

import render  # noqa: E402  (module resolved above, real or stub)
import tls  # noqa: E402

if not hasattr(render, "write_config"):
    def _missing_write_config(cfg, token, dest_dir):
        raise NotImplementedError("render.write_config stub; tests patch this")
    render.write_config = _missing_write_config

if not hasattr(tls, "java_tool_options"):
    def _missing_java_tool_options(cfg, config_dir):
        return None
    tls.java_tool_options = _missing_java_tool_options

from supervise import Supervisor  # noqa: E402

CFG = {"platform": "ring", "s3_endpoint": "https://s3.example.com", "bucket": "bkt",
       "access_key": "AK", "secret_key": "SK", "region": "us-east-1"}


def _fake_write_config(cfg, token, dest_dir):
    """Stand-in for render.write_config: writes the two files supervise.py
    backs up and restores, with just enough content for these tests to
    inspect — the bearer token and a line naming the bucket. The exact byte
    format is render.py's contract (tested separately); supervise.py only
    needs the files to exist, be reversible, and be chmod-able."""
    core = os.path.join(dest_dir, "core-site.xml")
    server_yaml = os.path.join(dest_dir, "delta-sharing-server.yaml")
    with open(core, "w") as f:
        f.write(
            "<configuration>\n"
            "  <property><name>fs.s3a.endpoint</name><value>%s</value></property>\n"
            "  <property><name>fs.s3a.access.key</name><value>%s</value></property>\n"
            "  <property><name>fs.s3a.secret.key</name><value>%s</value></property>\n"
            "</configuration>\n"
            % (cfg.get("s3_endpoint", ""), cfg.get("access_key", ""), cfg.get("secret_key", ""))
        )
    os.chmod(core, 0o600)
    with open(server_yaml, "w") as f:
        f.write(
            "version: 1\n"
            "# bucket root: s3a://%s/\n"
            "authorization:\n"
            "  bearerToken: \"%s\"\n"
            % (cfg.get("bucket", ""), token)
        )
    os.chmod(server_yaml, 0o600)


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        write_patcher = mock.patch("render.write_config", side_effect=_fake_write_config)
        self.addCleanup(write_patcher.stop)
        write_patcher.start()
        tls_patcher = mock.patch("tls.java_tool_options", return_value=None)
        self.addCleanup(tls_patcher.stop)
        tls_patcher.start()

    def test_apply_writes_config_and_starts(self):
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            r = s.apply(CFG, "tok1")
            self.assertTrue(r["ok"], r)
            self.assertTrue(s.running())
            self.assertIn('bearerToken: "tok1"',
                          open(os.path.join(d, "delta-sharing-server.yaml")).read())

    def test_failed_start_restores_the_previous_config(self):
        # A typo must not take a working share offline with no way back. The previous
        # files are kept and restored, and the failure is reported.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "good-token")["ok"])
            s.launcher = ["false"]          # next start will fail immediately
            r = s.apply(dict(CFG, bucket="broken"), "bad-token")
            self.assertFalse(r["ok"], r)
            y = open(os.path.join(d, "delta-sharing-server.yaml")).read()
            self.assertIn('bearerToken: "good-token"', y)
            self.assertIn("s3a://bkt/", y)
            self.assertNotIn("broken", y)

    def test_backup_files_survive_a_failed_apply(self):
        # CRITICAL: the rollback point must never be deleted, including on the
        # success path of an EARLIER apply — otherwise a server that starts, binds,
        # and dies later is reported ok with the last-known-good config already gone.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "good-token")["ok"])
            s.launcher = ["false"]
            r = s.apply(dict(CFG, bucket="broken"), "bad-token")
            self.assertFalse(r["ok"], r)
            for name in ("core-site.xml", "delta-sharing-server.yaml"):
                self.assertTrue(
                    os.path.exists(os.path.join(d, name + ".prev")),
                    "%s.prev must survive a failed apply" % name)

    def test_a_failed_apply_recovers_the_previous_server(self):
        # Restore-then-restart: a failed apply must not leave the share offline
        # until somebody happens to re-apply. The previous server comes back up on
        # the launcher that was proven to work with it, not on the (now broken)
        # launcher the failed apply was attempted with.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "good-token")["ok"])
            s.launcher = ["false"]
            r = s.apply(dict(CFG, bucket="broken"), "bad-token")
            self.assertFalse(r["ok"], r)
            y = open(os.path.join(d, "delta-sharing-server.yaml")).read()
            self.assertIn('bearerToken: "good-token"', y)
            self.assertNotIn("broken", y)
            self.assertTrue(s.running())

    def test_credentials_reach_the_child_environment(self):
        # The presigner reads the AWS default chain, not fs.s3a.*; core-site alone is
        # not enough and a presign failure is the symptom.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            env = s.child_env(CFG)
            self.assertEqual(env["AWS_ACCESS_KEY_ID"], "AK")
            self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "SK")
            self.assertEqual(env["AWS_REGION"], "us-east-1")

    def test_child_env_sets_java_tool_options_when_tls_returns_a_value(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("tls.java_tool_options", return_value="-Djavax.net.ssl.trustStore=x"):
            s = Supervisor(d, ["sleep", "60"], {})
            env = s.child_env(CFG)
            self.assertEqual(env["JAVA_TOOL_OPTIONS"], "-Djavax.net.ssl.trustStore=x")

    def test_child_env_removes_pre_existing_java_tool_options_when_tls_returns_none(self):
        # A private-CA apply followed by a non-private-CA apply must not leave the
        # earlier trust-store flag sitting in the environment the JVM inherits.
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"JAVA_TOOL_OPTIONS": "-Dstale=1"}), \
             mock.patch("tls.java_tool_options", return_value=None):
            s = Supervisor(d, ["sleep", "60"], {})
            env = s.child_env(CFG)
            self.assertNotIn("JAVA_TOOL_OPTIONS", env)

    def test_rendered_credential_files_are_0600(self):
        # core-site.xml holds fs.s3a.secret.key, delta-sharing-server.yaml holds
        # the bearer token — this directory's convention for anything holding a
        # credential is 0600, never the caller's umask default.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "tok1")["ok"])
            for name in ("core-site.xml", "delta-sharing-server.yaml"):
                mode = os.stat(os.path.join(d, name)).st_mode & 0o777
                self.assertEqual(oct(mode), oct(0o600), name)

    def test_prev_snapshots_are_also_0600(self):
        # `shutil.copy2` (used by `_backup()`) preserves the SOURCE's mode, and
        # the source is already 0600 by the time this test's first apply()
        # finishes. So applying twice and checking `.prev` afterwards proves
        # nothing on its own: `.prev` comes out 0600 whether or not
        # `_backup()`'s own `os.chmod` exists, because copy2 alone already
        # reproduced it.
        #
        # `.prev` is never deleted (it is the rollback target for the next
        # apply), so what this needs to prove is narrower: a snapshot taken from
        # a file that PREDATES the 0600 convention must not carry that looser
        # mode forward forever. Reproduced by chmod'ing the on-disk files back to
        # 0644 between the two applies, so `_backup()`'s own `os.chmod` is the
        # only thing that can still make `.prev` 0600 here.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "tok1")["ok"])
            for name in ("core-site.xml", "delta-sharing-server.yaml"):
                os.chmod(os.path.join(d, name), 0o644)
            s.launcher = ["false"]
            self.assertFalse(s.apply(dict(CFG, bucket="broken"), "tok2")["ok"])
            for name in ("core-site.xml", "delta-sharing-server.yaml"):
                mode = os.stat(os.path.join(d, name + ".prev")).st_mode & 0o777
                self.assertEqual(oct(mode), oct(0o600), name + ".prev")

    def test_server_log_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "tok1")["ok"])
            mode = os.stat(os.path.join(d, "server.log")).st_mode & 0o777
            self.assertEqual(oct(mode), oct(0o600))

    def test_a_log_echo_of_the_token_is_redacted_before_it_reaches_the_caller(self):
        # The detail this returns is what the operator UI displays. A Delta
        # Sharing server that fails to parse its config may echo the config it
        # failed to parse — including the bearer token — into its own log, which
        # `_log_tail()` reads into that detail. Redaction has to happen HERE,
        # before the caller ever sees it.
        with tempfile.TemporaryDirectory() as d:
            token = "sentinel-token-must-not-leak-abc123"
            launcher = ["bash", "-c",
                       "echo 'bearerToken: \"%s\"'; exit 1" % token]
            s = Supervisor(d, launcher, {})
            self.addCleanup(s.stop)
            r = s.apply(CFG, token)
            self.assertFalse(r["ok"], r)
            self.assertNotIn(token, r["detail"])
            self.assertIn("«redacted»", r["detail"])

    def test_a_log_echo_of_the_secret_key_is_redacted_too(self):
        with tempfile.TemporaryDirectory() as d:
            secret = "SentinelSecretMustNotLeak987"
            launcher = ["bash", "-c",
                       "echo 'fs.s3a.secret.key: %s'; exit 1" % secret]
            s = Supervisor(d, launcher, {})
            self.addCleanup(s.stop)
            r = s.apply(dict(CFG, secret_key=secret), "tok1")
            self.assertFalse(r["ok"], r)
            self.assertNotIn(secret, r["detail"])

    def test_a_render_failure_echoing_a_secret_is_redacted_too(self):
        # apply()'s render-failure branch returns "render failed: %s" % e
        # verbatim — str(e) is render's own failure detail, exactly the channel
        # most likely to start carrying a secret — so it must be redacted before
        # it is ever returned.
        with tempfile.TemporaryDirectory() as d:
            token = "sentinel-render-failure-token-must-not-leak"
            secret = "SentinelRenderFailureSecretMustNotLeak"
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            with mock.patch("render.write_config", side_effect=RuntimeError(
                    "bad config: saw bearerToken=%s secret=%s" % (token, secret))):
                r = s.apply(dict(CFG, secret_key=secret), token)
            self.assertFalse(r["ok"], r)
            self.assertNotIn(token, r["detail"])
            self.assertNotIn(secret, r["detail"])
            self.assertIn("«redacted»", r["detail"])

    def test_a_log_tail_carrying_a_previous_runs_token_is_still_redacted(self):
        # `_launch` opens server.log in APPEND mode (never truncated) and
        # `_log_tail` reads its last bytes, so a child that exits immediately and
        # writes nothing of its own returns the PREVIOUS run's own output —
        # which can carry a previous config's echoed secret. Redacting only the
        # CURRENT apply's token/secret would miss exactly that run.
        with tempfile.TemporaryDirectory() as d:
            first_token = "sentinel-first-run-token-must-not-leak"
            launcher = ["bash", "-c",
                       "echo 'startup token was %s'; sleep 60" % first_token]
            s = Supervisor(d, launcher, {})
            self.addCleanup(s.stop)
            # A genuinely successful first apply — its own stdout, carrying its
            # own token, now sits in server.log, and `_last_good` records it.
            self.assertTrue(s.apply(CFG, first_token)["ok"])

            # The next apply's launcher fails immediately and writes nothing of
            # its own, so `_log_tail()` at the moment of failure returns
            # exactly the FIRST run's output — a different token from either
            # of this second apply's own secrets, so only history-aware
            # redaction can catch it.
            s.launcher = ["false"]
            r = s.apply(dict(CFG, bucket="broken"), "second-run-token")
            self.assertFalse(r["ok"], r)
            self.assertNotIn(first_token, r["detail"])
            self.assertIn("«redacted»", r["detail"])

    def test_redact_helper_strips_every_secret_given(self):
        # Direct test of the primitive `_recover` relies on to protect the
        # PREVIOUS good config's secrets (rather than the failed attempt's) when
        # a log echo happens during recovery — see supervise.py's own note on
        # why that call site passes `good["token"]`/`good["cfg"]`, not the
        # values the caller of apply() was trying to switch to.
        text = "bearerToken: tok123 and secret fs.s3a.secret.key: sk456 end"
        redacted = Supervisor._redact(text, "tok123", "sk456")
        self.assertNotIn("tok123", redacted)
        self.assertNotIn("sk456", redacted)
        self.assertIn("bearerToken:", redacted)  # non-secret text survives

    def test_redact_helper_tolerates_absent_secrets(self):
        # apply()'s first call has a token but the render may have failed
        # before a secret_key was ever known; `cfg.get("secret_key")` can be
        # None or empty, and that must not raise or wildcard-match everything.
        self.assertEqual(Supervisor._redact("no secrets here", None, ""),
                         "no secrets here")

    def test_stop_is_idempotent(self):
        # Exercised against a REAL running child: calling stop() twice with nothing
        # ever started only tests the proc-is-None branch, and could not fail if
        # idempotence after a real stop regressed.
        with tempfile.TemporaryDirectory() as d:
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            self.assertTrue(s.apply(CFG, "tok")["ok"])
            self.assertTrue(s.running())
            s.stop()
            self.assertFalse(s.running())
            s.stop()
            self.assertFalse(s.running())

    def test_resume_launches_against_the_config_already_on_disk(self):
        # resume() is the startup path: this process itself restarted and found
        # a previously-applied config still in place, so it must launch without
        # rendering anything new.
        with tempfile.TemporaryDirectory() as d:
            _fake_write_config(CFG, "tok1", d)
            s = Supervisor(d, ["sleep", "60"], {})
            self.addCleanup(s.stop)
            with mock.patch("render.write_config") as write_config:
                r = s.resume(CFG, "tok1")
                write_config.assert_not_called()
            self.assertTrue(r["ok"], r)
            self.assertTrue(s.running())
            self.assertEqual(s._last_good["cfg"], CFG)
            self.assertEqual(s._last_good["token"], "tok1")

    def test_resume_reports_failure_on_immediate_exit(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_write_config(CFG, "tok1", d)
            s = Supervisor(d, ["false"], {})
            self.addCleanup(s.stop)
            r = s.resume(CFG, "tok1")
            self.assertFalse(r["ok"], r)
            self.assertFalse(s.running())
            self.assertIsNone(s._last_good)


class _FakeChild:
    """Stands in for a Popen that is still running (poll() returns None)."""
    def poll(self):
        return None


class _FlappingProcSupervisor(Supervisor):
    """A Supervisor whose `.proc` attribute answers truthy on the FIRST read
    and None on every read after that — reproducing, deterministically and
    without real thread timing, the exact interleaving a concurrent stop()
    from the apply thread produces between running()'s two loads of
    self.proc."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._reads = 0

    @property
    def proc(self):
        self._reads += 1
        return _FakeChild() if self._reads == 1 else None

    @proc.setter
    def proc(self, value):
        pass  # ignore assignments — the flapping getter stays in control


class _CountingProcSupervisor(Supervisor):
    """A Supervisor that counts how many times `.proc` is read, to assert
    running() reads it exactly once."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.proc_reads = 0
        self._real_proc = None

    @property
    def proc(self):
        self.proc_reads += 1
        return self._real_proc

    @proc.setter
    def proc(self, value):
        self._real_proc = value


class TestRunningToctou(unittest.TestCase):
    def test_running_survives_proc_flipping_to_none_between_the_two_loads(self):
        # `running()` must not load `self.proc` twice. `stop()` sets it to None
        # from the apply thread while a request thread calls this via a status
        # poll; if the assignment lands between two loads, the second load sees
        # None and `.poll()` raises AttributeError on a request thread that
        # never touched the lock — and `apply()` calls `self.stop()` on every
        # apply while a 1s status poll runs throughout it, a window that is
        # genuinely polled rather than idle.
        s = _FlappingProcSupervisor("/tmp", ["sleep", "60"], {})
        # Must not raise "AttributeError: 'NoneType' object has no attribute
        # 'poll'". A single-load implementation reads .proc once (getting the
        # live fake child) and calls .poll() on THAT SAME object; a two-load
        # implementation's second read sees None.
        self.assertTrue(s.running())

    def test_running_loads_proc_exactly_once(self):
        # White-box companion to the above: proves the fix's shape directly (one
        # load) rather than only the crash it prevents.
        s = _CountingProcSupervisor("/tmp", ["sleep", "60"], {})
        s.proc = _FakeChild()
        self.assertTrue(s.running())
        self.assertEqual(s.proc_reads, 1,
            "running() must load self.proc exactly once — a second load "
            "races a concurrent stop()")


if __name__ == "__main__":
    unittest.main()
