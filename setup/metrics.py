"""The Prometheus exposition of what the setup page already knows.

Pure and deterministic: `render` takes the same dictionary `/api/status` returns
and produces text. No I/O, no clock of its own — a scrape and a status poll made
at the same moment describe the same deployment.

What is deliberately absent matters more here than what is present. A label value
travels into a monitoring system the customer may share with people who are not
entitled to the deployment's secrets, and it is kept there for as long as the
series is. So no series and no label carries the S3 secret key, the access key,
the bearer token, or any share, schema or table name. The S3 hostname is left out
for the same reason — it names the customer's storage — and a HELP line says
where to look for it instead. Everything exposed is a count, a state, or a check
outcome.

`image` and `server` sit on the info gauge alone, the usual Prometheus shape: a
join against it carries the versions to every other series without repeating a
string on each one, and without a restart under a new tag doubling every series.
"""

# Every state the tool can be in, in a fixed order so the output is stable. This
# list is deliberately its own: a state that exists in state.py and is missing
# here would silently expose no series at all for the deployment sitting in it,
# and the test that compares the two is what catches that.
STATES = ("unconfigured", "never_verified", "verified", "degraded", "stopped",
          "failed_start")

# The states in which no sharing server is serving anything.
NOT_RUNNING = ("unconfigured", "stopped", "failed_start")

MODES = ("trusted", "private_ca", "http")

RESULT_VALUES = {"pass": 1, "fail": 0, "unknown": -1}


def escape(value):
    """A label value, escaped per the text exposition format."""
    return (str(value).replace("\\", "\\\\")
                      .replace('"', '\\"')
                      .replace("\n", "\\n"))


def _labels(pairs):
    return "{%s}" % ",".join('%s="%s"' % (k, escape(v)) for k, v in pairs)


def _num(value):
    """A value the format accepts. Integers stay integral so a timestamp does not
    acquire a decimal point it never had."""
    if isinstance(value, float) and value.is_integer():
        return "%d" % int(value)
    if isinstance(value, int):
        return "%d" % value
    return repr(float(value))


def _epoch(iso):
    """Seconds since the epoch for an ISO-8601 UTC instant, or None.

    Returns None rather than raising for anything unparseable: a malformed
    timestamp should cost one gauge, not the whole scrape.
    """
    import calendar
    import datetime

    text = str(iso or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return calendar.timegm(parsed.timetuple())
    return None


def render(status, now=None):
    """The exposition text for one status dictionary.

    `now` is accepted so a caller can pin the moment a future series is measured
    against; nothing rendered today derives from it, which is what keeps two
    scrapes of an unchanged deployment byte-identical.
    """
    status = status or {}
    version = status.get("version") or {}
    config = status.get("config") or None
    verdict = status.get("verdict") or None
    out = []

    out.append("# HELP opensharing_setup_info Versions of the setup image and the "
               "sharing server it supervises.")
    out.append("# TYPE opensharing_setup_info gauge")
    out.append("opensharing_setup_info%s 1"
               % _labels([("image", version.get("setup", "")),
                          ("server", version.get("server", ""))]))

    state = status.get("state", "")
    out.append("# HELP opensharing_state The setup tool's state; exactly one "
               "series is 1.")
    out.append("# TYPE opensharing_state gauge")
    for name in STATES:
        out.append("opensharing_state%s %d"
                   % (_labels([("state", name)]), 1 if name == state else 0))

    out.append("# HELP opensharing_server_running 1 when the sharing server is "
               "up and serving.")
    out.append("# TYPE opensharing_server_running gauge")
    out.append("opensharing_server_running %d"
               % (0 if state in NOT_RUNNING or not state else 1))

    tables = (config or {}).get("tables") or []
    out.append("# HELP opensharing_tables_shared How many tables are shared. The "
               "names are not exposed; read them on the setup page.")
    out.append("# TYPE opensharing_tables_shared gauge")
    out.append("opensharing_tables_shared %d" % len(tables))

    mode = (config or {}).get("endpoint_mode", "")
    out.append("# HELP opensharing_endpoint_mode How the S3 endpoint is reached. "
               "The endpoint hostname is not exposed; read it on the setup page.")
    out.append("# TYPE opensharing_endpoint_mode gauge")
    for name in MODES:
        out.append("opensharing_endpoint_mode%s %d"
                   % (_labels([("mode", name)]),
                      1 if config and name == mode else 0))

    checks = (verdict or {}).get("checks") or []
    if checks:
        out.append("# HELP opensharing_check Last verdict per check: 1 pass, "
                   "0 fail, -1 could not run. A check about one shared table "
                   "carries that table's position in the configuration rather "
                   "than its name; the setup page maps position to name.")
        out.append("# TYPE opensharing_check gauge")
        for one in checks:
            value = RESULT_VALUES.get(one.get("result"), -1)
            id, position = split_table(str(one.get("id", "")), config)
            labels = [("id", id)]
            if position:
                labels.append(("table", str(position)))
            out.append("opensharing_check%s %d" % (_labels(labels), value))

    at = _epoch(status.get("verdict_at"))
    if at is not None:
        out.append("# HELP opensharing_last_verdict_timestamp_seconds When the "
                   "checks behind opensharing_check last ran.")
        out.append("# TYPE opensharing_last_verdict_timestamp_seconds gauge")
        out.append("opensharing_last_verdict_timestamp_seconds %s" % _num(at))

    expiry = _epoch(status.get("token_expires"))
    if expiry is not None:
        out.append("# HELP opensharing_token_expiry_timestamp_seconds The date "
                   "stamped on the recipient token. Nothing enforces it.")
        out.append("# TYPE opensharing_token_expiry_timestamp_seconds gauge")
        out.append("opensharing_token_expiry_timestamp_seconds %s" % _num(expiry))

    return "\n".join(out) + "\n"


def qualified_names(config):
    """"<share>.<schema>.<table>" per configured table, in configuration order.

    The gate suite names a per-table check after the table it is about, and that
    name is the customer's vocabulary. It must not travel into a monitoring
    system, which is often shared more widely than the setup page, so metrics
    replace it with the table's position here.
    """
    names = []
    for entry in (config or {}).get("tables") or []:
        names.append("%s.%s.%s" % (entry.get("share"), entry.get("schema"),
                                   entry.get("table")))
    return names


def split_table(check_id, config):
    """(id without the table it names, 1-based position) — (id, 0) when it names none.

    Matched as an exact suffix against the configured tables rather than parsed:
    a share, a schema and a table may each contain the separators, so any parse
    of the shape "<base>_<a>.<b>.<c>" is a guess, and a wrong guess here either
    leaks a name or mangles an id.
    """
    for position, name in enumerate(qualified_names(config), start=1):
        suffix = "_" + name
        if check_id.endswith(suffix):
            return check_id[: -len(suffix)], position
    for position, share in enumerate(_shares_in_order(config), start=1):
        suffix = "_" + share
        if check_id.endswith(suffix):
            return check_id[: -len(suffix)], position
    return check_id, 0


def _shares_in_order(config):
    """Share names, first-seen order — a listing check is named after its share."""
    seen = []
    for entry in (config or {}).get("tables") or []:
        share = entry.get("share")
        if share is not None and share not in seen:
            seen.append(share)
    return seen
