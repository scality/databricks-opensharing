import sys
import threading
import unittest
from auth import Auth


class TestAuth(unittest.TestCase):
    def test_generates_a_bootstrap_token(self):
        self.assertRegex(Auth().bootstrap, r"^[0-9a-f]{32,}$")

    def test_two_instances_differ(self):
        self.assertNotEqual(Auth().bootstrap, Auth().bootstrap)

    def test_correct_token_yields_a_session(self):
        a = Auth()
        self.assertIsNotNone(a.login(a.bootstrap))

    def test_wrong_token_yields_nothing(self):
        a = Auth()
        self.assertIsNone(a.login("wrong"))

    def test_session_validates_then_an_unknown_one_does_not(self):
        a = Auth()
        sid = a.login(a.bootstrap)
        self.assertTrue(a.valid(sid))
        self.assertFalse(a.valid("nope"))
        self.assertFalse(a.valid(None))

    def test_comparison_is_length_independent(self):
        # compare_digest, not ==, so a wrong token of a different length does not
        # return faster than a near-match.
        a = Auth()
        self.assertIsNone(a.login(a.bootstrap[:-1]))
        self.assertIsNone(a.login(a.bootstrap + "x"))

    def test_session_expires_after_ttl(self):
        # ttl=-1, not 0: `now - created > ttl` compares against elapsed time, which
        # can be exactly 0.0 on a fast clock read, so a ttl of 0 is not guaranteed to
        # have already expired the instant login() returns. A negative ttl is always
        # less than any non-negative elapsed time, so expiry here is deterministic.
        a = Auth(ttl=-1)
        sid = a.login(a.bootstrap)
        self.assertFalse(a.valid(sid))

    def test_session_valid_immediately_with_default_ttl(self):
        a = Auth()
        sid = a.login(a.bootstrap)
        self.assertTrue(a.valid(sid))

    def test_logout_invalidates_a_session(self):
        a = Auth()
        sid = a.login(a.bootstrap)
        self.assertTrue(a.logout(sid))
        self.assertFalse(a.valid(sid))

    def test_logout_unknown_session_returns_false(self):
        a = Auth()
        self.assertFalse(a.logout("nope"))

    def test_logout_all_invalidates_every_session(self):
        a = Auth()
        sid1 = a.login(a.bootstrap)
        sid2 = a.login(a.bootstrap)
        a.logout_all()
        self.assertFalse(a.valid(sid1))
        self.assertFalse(a.valid(sid2))

    def test_expired_sessions_are_pruned_from_the_store(self):
        a = Auth(ttl=-1)
        sid = a.login(a.bootstrap)
        self.assertEqual(len(a._sessions), 1)
        a.valid(sid)  # triggers the prune pass
        self.assertEqual(len(a._sessions), 0)

    def test_concurrent_login_and_prune_do_not_corrupt_the_store(self):
        # The server is a ThreadingHTTPServer, so every method here runs from
        # whichever request thread called it. `_prune()` iterates
        # `_sessions.items()`; a concurrent `login()` growing the SAME dict
        # mid-iteration raises `RuntimeError: dictionary changed size during
        # iteration` in CPython — an ordinary second browser tab turning into a
        # 500. Sessions are left unexpired (default ttl) and never removed, so the
        # dict being iterated grows into the hundreds while pruners are hammering
        # it — an unguarded `_sessions` fails this reliably.
        #
        # Every `t.join(timeout=15)` below is followed by an explicit
        # `is_alive()` assertion, so a timeout on a pathologically slow runner is
        # a reported failure rather than silent success (nothing ran long enough
        # to raise, so `errors` would still be `[]` and the test would pass
        # having proved nothing). The adders are also bounded (a fixed iteration
        # count, same as the pruners) rather than running flat-out until `stop`
        # is set — unbounded, `_prune()`'s O(n) scan runs against a same-lock
        # dict that grows without limit for the whole 15s window, which is
        # wasted CPU rather than exercising anything new: the race is in a
        # single `login()` interleaving with a single `_prune()`, not in how
        # many sessions accumulate.
        a = Auth()
        errors = []
        stop = threading.Event()
        ADD_ITERATIONS = 20000
        # Force far more frequent thread switches than the 5ms default so the
        # threads actually interleave inside the test's short runtime, rather
        # than each running to completion in a single scheduling slice.
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.00001)
        try:
            def add_sessions():
                for _ in range(ADD_ITERATIONS):
                    if stop.is_set():
                        break
                    a.login(a.bootstrap)

            def prune_repeatedly():
                try:
                    for _ in range(3000):
                        a.valid("irrelevant-session-id")  # triggers _prune()
                except Exception as e:  # noqa: BLE001 — recording, not narrowing
                    errors.append(e)

            adders = [threading.Thread(target=add_sessions) for _ in range(4)]
            for t in adders:
                t.start()
            pruners = [threading.Thread(target=prune_repeatedly) for _ in range(4)]
            for t in pruners:
                t.start()
            for t in pruners:
                t.join(timeout=15)
            for t in pruners:
                self.assertFalse(t.is_alive(), "a pruner thread did not finish "
                                 "within 15s — the run proves nothing if it "
                                 "timed out mid-iteration")
            stop.set()
            for t in adders:
                t.join(timeout=15)
            for t in adders:
                self.assertFalse(t.is_alive(), "an adder thread did not finish "
                                 "within 15s after stop was set")
        finally:
            sys.setswitchinterval(old_interval)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
