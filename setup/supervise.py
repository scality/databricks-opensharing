"""The Delta Sharing server as a supervised child, with a reversible apply.

The server cannot boot unconfigured (Authorization.checkConfig requires a
bearerToken), so this process owns writing the config and starting it.
"""
import os
import shutil
import signal
import subprocess
import time

FILES = ("core-site.xml", "delta-sharing-server.yaml", "truststore.jks")
START_GRACE_SECONDS = 3
LOG_TAIL_BYTES = 2000
LOG_NAME = "server.log"


class Supervisor:
    def __init__(self, config_dir, launcher, env, keytool="keytool"):
        self.config_dir = config_dir
        self.launcher = list(launcher)
        self.base_env = dict(env)
        self.keytool = keytool
        self.proc = None
        self.log = None  # the open file handle the current child's stdout/stderr go to
        # The (cfg, launcher) that were last proven to start successfully — what a
        # failed apply falls back to, since self.launcher/self.cfg at the moment of
        # failure may themselves be the broken half of the change being applied.
        self._last_good = None

    def child_env(self, cfg):
        """The presigner resolves credentials through the AWS default chain, so these
        must be in the process environment as well as in core-site.xml."""
        import tls
        env = dict(os.environ)
        env.update(self.base_env)
        env["AWS_ACCESS_KEY_ID"] = cfg["access_key"]
        env["AWS_SECRET_ACCESS_KEY"] = cfg["secret_key"]
        env["AWS_REGION"] = cfg["region"]
        options = tls.java_tool_options(cfg, self.config_dir)
        if options:
            env["JAVA_TOOL_OPTIONS"] = options
        else:
            env.pop("JAVA_TOOL_OPTIONS", None)
        return env

    def running(self):
        # `self.proc` must be loaded exactly once here. `stop()` sets it to None
        # from the apply thread while a request thread calls this via a status
        # poll; if that assignment lands between two separate loads of
        # `self.proc`, the second load sees None and `.poll()` raises
        # AttributeError on a request thread that never touched the lock. A
        # single local load closes the window.
        proc = self.proc
        return proc is not None and proc.poll() is None

    def stop(self):
        if self.running():
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # leaves a zombie only if the kill itself did not land
        self.proc = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def _backup(self):
        """Snapshot the config on disk into `<name>.prev`, overwriting any earlier
        snapshot. Never deleted: a `.prev` is the rollback target for whatever apply
        is about to run, and it must survive that apply whether it succeeds or fails,
        or a later failed apply has nothing left to restore."""
        saved = {}
        for name in FILES:
            path = os.path.join(self.config_dir, name)
            if os.path.exists(path):
                prev = path + ".prev"
                shutil.copy2(path, prev)
                # Belt and suspenders: copy2 preserves the source's mode, so this
                # is a no-op once the file's own 0600 has ever applied — but it
                # also holds for a `.prev` snapshotted from a file that predates
                # that fix, rather than quietly carrying an old 0644 forward
                # forever.
                os.chmod(prev, 0o600)
                saved[name] = prev
        return saved

    def _restore(self, saved):
        for name, prev in saved.items():
            shutil.copy2(prev, os.path.join(self.config_dir, name))

    def _launch(self, launcher, cfg):
        """Start `launcher` against whatever config is currently on disk, replacing
        any child this Supervisor is tracking. Blocks for the start grace period and
        returns the new Popen."""
        if self.log is not None:
            self.log.close()
        log_path = os.path.join(self.config_dir, LOG_NAME)
        self.log = open(log_path, "ab")
        # The JVM's own log can echo the parsed config on a startup failure (see
        # _redact below), so it gets the same 0600 as the credential files.
        os.chmod(log_path, 0o600)
        proc = subprocess.Popen(launcher, cwd=self.config_dir, env=self.child_env(cfg),
                                stdout=self.log, stderr=self.log)
        time.sleep(START_GRACE_SECONDS)
        return proc

    def _log_tail(self):
        """The end of the child's own log — real logs, not a captured pipe nobody
        drains. A JVM server logs steadily; reading it after the fact (rather than
        holding the pipe open and never reading it) is what stops the ~64KB pipe
        buffer from filling and the child blocking on write mid-boot."""
        path = os.path.join(self.config_dir, LOG_NAME)
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - LOG_TAIL_BYTES))
                data = f.read()
        except OSError:
            data = b""
        return data.decode(errors="replace").strip()

    @staticmethod
    def _redact(text, *secrets):
        """`text` is about to leave this process in an API response the operator
        UI is expected to display. It can carry the JVM's own log, which on a
        config-parse failure may echo the config it failed to parse — bearer
        token, S3 secret key. Redact BEFORE it is ever returned, not after: once
        it has left this function it may already be on the wire."""
        for s in secrets:
            if s:
                text = text.replace(s, "«redacted»")
        return text

    def _redact_with_history(self, text, *secrets):
        """`_launch` opens server.log in **append** mode and `_log_tail` reads the
        last bytes of that file — never truncated, since a real boot failure needs
        the log around it to diagnose. A child that exits immediately writes
        nothing of its own, so the tail returned is the *previous* run's output,
        which can carry a previous config echo. Redacting only the secrets of the
        run just attempted (`secrets`) therefore misses exactly the run most
        likely to still be sitting in the file. Every known secret — the ones
        passed in, plus whatever `_last_good` holds — is redacted on the way
        out."""
        known = list(secrets)
        if self._last_good:
            known.append(self._last_good.get("token"))
            known.append(self._last_good["cfg"].get("secret_key"))
        return self._redact(text, *known)

    def apply(self, cfg, token):
        import render
        saved = self._backup()
        try:
            render.write_config(cfg, token, self.config_dir)
        except Exception as e:
            # The old server was never touched — a broken new config never risked it.
            self._restore(saved)
            # This message carries `e`'s str(), which can echo a render failure's
            # own detail — the channel most likely to start carrying a secret —
            # so it is redacted the same as any other detail leaving apply().
            return {"ok": False, "detail": self._redact_with_history(
                "render failed: %s" % e, token, cfg.get("secret_key"))}

        # Only now, with the new config safely on disk, replace the running server.
        self.stop()
        self.proc = self._launch(self.launcher, cfg)
        if self.running():
            self._last_good = {"cfg": cfg, "launcher": list(self.launcher),
                               "token": token}
            return {"ok": True, "detail": "server started"}

        tail = self._redact_with_history(
            self._log_tail() or "server exited immediately",
            token, cfg.get("secret_key"))
        self._restore(saved)
        return self._recover(tail, token, cfg.get("secret_key"))

    def resume(self, cfg, token):
        """Launch against the config already on disk, without rendering anything
        new — the startup path used when this process itself restarts and finds a
        previously-applied config still in place."""
        self.proc = self._launch(self.launcher, cfg)
        if self.running():
            self._last_good = {"cfg": cfg, "launcher": list(self.launcher),
                               "token": token}
            return {"ok": True, "detail": "server started"}

        tail = self._redact_with_history(
            self._log_tail() or "server exited immediately",
            token, cfg.get("secret_key"))
        return {"ok": False, "detail": tail}

    def _recover(self, failure_detail, *other_secrets):
        """A failed apply must not leave the share down: bring the previous server
        back up on the config we just restored, using the launcher that was proven
        to work with it — not `self.launcher`, which may itself be the broken half
        of the change that just failed.

        `other_secrets` are the secrets of the apply that JUST failed (its own
        `failure_detail` is already redacted against them) — `_redact_with_history`
        alone would only add `_last_good`'s, and a log tail read here can still
        carry the just-failed run's own echo from the same append-mode file."""
        if self._last_good is None:
            return {"ok": False, "detail": failure_detail}
        good = self._last_good
        self.proc = self._launch(good["launcher"], good["cfg"])
        if self.running():
            detail = "%s; restored the previous config and the previous server is running again" % failure_detail
        else:
            tail = self._redact_with_history(
                self._log_tail() or "server exited immediately",
                good.get("token"), good["cfg"].get("secret_key"), *other_secrets)
            detail = "%s; restored the previous config but it did not restart: %s" % (
                failure_detail, tail)
        return {"ok": False, "detail": detail}
