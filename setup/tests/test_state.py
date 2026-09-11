import unittest

import cases
from state import (config_hash, resolve_state, HASHED_FIELDS, UNCONFIGURED,
                   NEVER_VERIFIED, VERIFIED, DEGRADED, STOPPED, FAILED_START)

CFG = cases.BASE

# A hash of the known configuration in tests/cases.py. Pinned so a change to the
# hashed field list, to the serialisation, or to what counts as an empty default
# shows up here — every one of those would silently keep a stale verdict valid or
# invalidate every verdict at once.
GOLDEN_HASH = "fe5104fc1006d6e1e0f9c5c1fda398e96f9d2c07051aabb2d2c8f7dba6e68fa6"


class TestConfigHash(unittest.TestCase):
    def test_hash_matches_the_golden(self):
        self.assertEqual(config_hash(CFG), GOLDEN_HASH)

    def test_hash_is_order_independent(self):
        self.assertEqual(config_hash(CFG), config_hash(dict(reversed(list(CFG.items())))))

    def test_every_hashed_field_moves_the_hash(self):
        for field in HASHED_FIELDS:
            with self.subTest(field=field):
                if field == "tables":
                    other = dict(CFG, tables=CFG["tables"] + [
                        {"prefix": "p/q", "share": "s", "schema": "c", "table": "t"}])
                else:
                    other = dict(CFG, **{field: str(CFG.get(field, "")) + "-x"})
                self.assertNotEqual(config_hash(CFG), config_hash(other))

    def test_a_change_inside_a_table_entry_moves_the_hash(self):
        # The verdict includes a per-table query check, so renaming a shared
        # table invalidates it just as changing the bucket does.
        other = dict(CFG, tables=[dict(CFG["tables"][0], table="renamed")])
        self.assertNotEqual(config_hash(CFG), config_hash(other))

    def test_table_order_is_significant(self):
        # Order is what the YAML renders in, so the rendered file differs and the
        # hash must too.
        reordered = dict(cases.THREE_TABLES,
                         tables=list(reversed(cases.THREE_TABLES["tables"])))
        self.assertNotEqual(config_hash(cases.THREE_TABLES), config_hash(reordered))

    def test_key_order_inside_a_table_entry_does_not_matter(self):
        entry = CFG["tables"][0]
        flipped = dict(CFG, tables=[dict(reversed(list(entry.items())))])
        self.assertEqual(config_hash(CFG), config_hash(flipped))

    def test_a_missing_field_is_not_an_error(self):
        # A configuration read back from an older deployment may not carry every
        # field; hashing must still produce an answer rather than raise on the
        # status path.
        self.assertTrue(config_hash({}))


class TestResolveState(unittest.TestCase):
    def test_unconfigured(self):
        self.assertEqual(resolve_state(configured=False, running=False,
                                       start_failed=False, verdict=None,
                                       applied_hash=None), UNCONFIGURED)

    def test_configured_but_stopped(self):
        self.assertEqual(resolve_state(configured=True, running=False,
                                       start_failed=False, verdict=None,
                                       applied_hash="h"), STOPPED)

    def test_running_with_no_verdict_is_never_verified(self):
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=None,
                                       applied_hash="h"), NEVER_VERIFIED)

    def test_an_empty_check_list_is_never_verified(self):
        v = {"hash": "h", "checks": []}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), NEVER_VERIFIED)

    def test_running_all_pass_is_verified(self):
        v = {"hash": "h", "checks": [{"id": "a", "result": "pass"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), VERIFIED)

    def test_a_failing_check_is_degraded(self):
        v = {"hash": "h", "checks": [{"id": "a", "result": "pass"},
                                     {"id": "b", "result": "fail"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), DEGRADED)

    def test_an_unknown_check_never_reaches_verified(self):
        # Handing a recipient a profile is gated on VERIFIED, and "we could not
        # look" is not "we looked and it passed".
        v = {"hash": "h", "checks": [{"id": "a", "result": "unknown"}]}
        self.assertNotEqual(resolve_state(configured=True, running=True,
                                          start_failed=False, verdict=v,
                                          applied_hash="h"), VERIFIED)

    def test_an_unknown_check_is_never_verified_not_degraded(self):
        # An absent measurement and a finding must never read alike: nothing
        # failed here, nothing could be checked.
        v = {"hash": "h", "checks": [{"id": "a", "result": "unknown"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), NEVER_VERIFIED)

    def test_a_fail_outranks_an_unknown(self):
        v = {"hash": "h", "checks": [{"id": "a", "result": "fail"},
                                     {"id": "b", "result": "unknown"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), DEGRADED)

    def test_an_unrecognised_result_never_reaches_verified(self):
        # This function reads plain dictionaries that have been through JSON. The
        # one place whose whole job is to fail closed must not have a fail-open
        # default resting on a validator somewhere else.
        v = {"hash": "h", "checks": [{"id": "a", "result": "PASS"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), NEVER_VERIFIED)

    def test_an_unrecognised_result_alongside_a_real_pass_is_not_verified(self):
        v = {"hash": "h", "checks": [{"id": "a", "result": "pass"},
                                     {"id": "b", "result": "bogus"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), NEVER_VERIFIED)

    def test_a_check_with_no_result_at_all_is_not_verified(self):
        v = {"hash": "h", "checks": [{"id": "a"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="h"), NEVER_VERIFIED)

    def test_stale_verdict_reads_as_never_verified(self):
        # The configuration changed after the verdict was taken. Yesterday's pass
        # must not stay on screen.
        v = {"hash": "old", "checks": [{"id": "a", "result": "pass"}]}
        self.assertEqual(resolve_state(configured=True, running=True,
                                       start_failed=False, verdict=v,
                                       applied_hash="new"), NEVER_VERIFIED)

    def test_start_failure_outranks_a_stale_pass(self):
        v = {"hash": "h", "checks": [{"id": "a", "result": "pass"}]}
        self.assertEqual(resolve_state(configured=True, running=False,
                                       start_failed=True, verdict=v,
                                       applied_hash="h"), FAILED_START)

    def test_a_first_ever_failed_apply_is_failed_start_not_unconfigured(self):
        # A first apply that never succeeds has no prior success to derive
        # "configured" from, and reporting "not configured yet" would hide the
        # one message that explains what happened.
        self.assertEqual(resolve_state(configured=False, running=False,
                                       start_failed=True, verdict=None,
                                       applied_hash=None), FAILED_START)


if __name__ == "__main__":
    unittest.main()
