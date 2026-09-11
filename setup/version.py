"""What is running: the setup image's own version and the sharing server's.

The initiative behind embedding ISV work products in Scality platforms asks
each one to report its version, so a problem after deployment is visible rather
than reported by the customer. This is that report for the setup image.
"""
import os
import re

SERVER_LIB_DIR = "/opt/delta-sharing-server/lib"
_SERVER_JAR = re.compile(r"^io\.delta\.delta-sharing-server-(.+)\.jar$")


def setup_version():
    """The tag the image was built from, baked in at build time; "dev" otherwise."""
    return os.environ.get("SETUP_VERSION", "") or "dev"


def server_version(lib_dir=SERVER_LIB_DIR):
    """The upstream server version, read from the jar the launcher runs."""
    try:
        names = os.listdir(lib_dir)
    except OSError:
        return ""
    for name in sorted(names):
        found = _SERVER_JAR.match(name)
        if found:
            return found.group(1)
    return ""


def report():
    return {"setup": setup_version(), "server": server_version()}
