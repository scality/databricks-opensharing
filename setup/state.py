"""The states the setup tool can be in, and when a verdict stops counting.

Pure: no I/O, no subprocess. Everything here is decided from values the caller
already has, so it is testable without a server or a storage backend.
"""
import hashlib
import json

UNCONFIGURED = "unconfigured"
NEVER_VERIFIED = "never_verified"
VERIFIED = "verified"
DEGRADED = "degraded"
STOPPED = "stopped"
FAILED_START = "failed_start"

# The inputs a verdict is valid for. Changing any of them invalidates it, so the
# list has to cover everything the checks actually exercised — including which
# tables were shared and which CA was trusted.
HASHED_FIELDS = ("platform", "endpoint_mode", "s3_endpoint", "bucket", "access_key",
                 "secret_key", "region", "share_public_url", "ca_pem_sha256", "tables")

_EMPTY = {"tables": []}


def config_hash(cfg):
    """Stable hash over the applied inputs.

    Key order must not matter and every field must move it: a hash blind to one
    field would leave a pass on screen after that field changed. `sort_keys`
    reaches inside the table dictionaries too, while the list order — which is
    the order the YAML renders in — is preserved and therefore significant.
    """
    payload = json.dumps({k: cfg.get(k, _EMPTY.get(k, "")) for k in HASHED_FIELDS},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def resolve_state(*, configured, running, start_failed, verdict, applied_hash):
    # start_failed is checked first: a first-ever apply that never reaches a
    # success has nothing to set `configured` from but the config that was
    # submitted, and a caller deriving `configured` from a prior success would
    # read this as UNCONFIGURED — "not configured yet, fill in the fields" — on
    # the one occasion the operator most needs to be told the server would not
    # start.
    if start_failed:
        return FAILED_START
    if not configured:
        return UNCONFIGURED
    if not running:
        return STOPPED
    # A verdict taken against a different config is no verdict at all.
    if not verdict or verdict.get("hash") != applied_hash:
        return NEVER_VERIFIED
    results = [c.get("result") for c in verdict.get("checks", [])]
    if not results:
        return NEVER_VERIFIED
    # An "unknown" must never read as a finding. A check that could not run is an
    # absent measurement, not evidence that something is broken. A genuine "fail"
    # still outranks it: real evidence of brokenness is a finding even alongside
    # checks that could not run.
    if any(r == "fail" for r in results):
        return DEGRADED
    # This function's entire job is to fail closed, so its own tail must not
    # default to VERIFIED. Only an all-"pass" result set is verified; anything
    # else — "unknown", or a value no producer in this codebase can emit —
    # is not. The check constructor validates its own callers, but this function
    # reads plain dictionaries that have been through JSON, and the one place
    # whose job is to fail closed must not rest on a validator elsewhere.
    if all(r == "pass" for r in results):
        return VERIFIED
    return NEVER_VERIFIED
