"""Running the verification suite so that a suite which died still says so.

The failure this module exists to prevent: a gate raises halfway through — the
storage stops answering, a URL will not resolve — the exception escapes, the
caller stores whatever short list of checks it has, and every one of them is a
pass. The state model then reads an all-pass result set and reports the
deployment verified, on the strength of the three gates that ran before the one
that blew up.

So the suite always ends with a check of its own. `suite_completed` passes only
when `verify.run` returned normally, and fails with the exception text otherwise
— carrying the partial results alongside, because which gates did run is exactly
what an operator needs. A failed check makes the state degraded, which is the
honest answer: something is wrong and the measurement is incomplete.
"""
import verify
from checks import FAIL, PASS, check

SUITE_ID = "suite_completed"


def run_post_checks(cfg, token, local_server_url, ssl_ctx, opener=None, client=None):
    """Every check `verify.run` produced, plus one saying whether it finished."""
    results = []
    # Set before the try so the `finally` has something to append even on a
    # control flow no `except` clause here covers.
    completed = check(SUITE_ID, FAIL, "the verification suite did not finish")
    try:
        verify.run(cfg, token, local_server_url, ssl_ctx, opener=opener,
                   client=client, collected=results)
        completed = check(SUITE_ID, PASS, "the verification suite ran to the end")
    except Exception as e:
        completed = check(SUITE_ID, FAIL, "%s: %s" % (type(e).__name__, e))
    finally:
        results.append(completed)
    return results
