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
import time
import urllib.error
import urllib.request

import render
import verify
from checks import FAIL, PASS, UNKNOWN, check

SUITE_ID = "suite_completed"
READY_ID = "server_listening"
READY_TIMEOUT_SECONDS = 90


def wait_until_listening(local_server_url, opener=None, timeout=READY_TIMEOUT_SECONDS,
                         sleep=time.sleep, clock=time.monotonic):
    """Block until the sharing server answers HTTP at all, or the timeout passes.

    The supervisor's start grace period says the process is alive, not that the
    JVM has bound its port: on a slow host the gap is tens of seconds, and a gate
    that runs inside it reads "connection refused" as a failed deployment. Any
    HTTP status counts as listening — the first request here carries no token,
    so the expected answer is a 401.
    """
    opener = opener or urllib.request.urlopen
    url = local_server_url.rstrip("/") + render.ENDPOINT_PREFIX + "/shares"
    deadline = clock() + timeout
    last = None
    while True:
        try:
            opener(urllib.request.Request(url), timeout=5, context=None)
            return check(READY_ID, PASS, "the sharing server answers on %s" % local_server_url)
        except urllib.error.HTTPError:
            return check(READY_ID, PASS, "the sharing server answers on %s" % local_server_url)
        except Exception as e:
            last = e
        if clock() >= deadline:
            return check(READY_ID, FAIL, "the sharing server did not answer within %ds: %s"
                         % (timeout, last))
        sleep(1)


def run_post_checks(cfg, token, local_server_url, ssl_ctx, opener=None, client=None):
    """Every check `verify.run` produced, plus one saying whether it finished."""
    results = [wait_until_listening(local_server_url, opener)]
    if results[0]["result"] != PASS:
        results.append(check(SUITE_ID, FAIL, "the verification suite did not run: "
                                             "the server never answered"))
        return results
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
