"""Trusting the storage endpoint's certificate, from Python and from the JVM.

Three endpoint modes, three answers. A publicly-trusted certificate needs
nothing. Plain HTTP has no TLS at all. A private or corporate CA needs the CA in
two places: a Python SSL context for the checks this tool runs itself, and a Java
truststore for the sharing server's JVM, which otherwise refuses the certificate
on its first metadata read and surfaces it to the recipient as an empty 500 some
twenty minutes later.
"""
import hashlib
import os
import ssl
import subprocess

CA_FILE = "ca.pem"
TRUSTSTORE = "truststore.jks"

# The truststore holds public certificates only — there is nothing secret in it,
# and JSSE requires some password, so this is the conventional one. It is still
# kept off the command line: see build_truststore.
STOREPASS = "changeit"

TRUSTSTORE_ALIAS = "storage-ca"


def ssl_context(cfg, config_dir):
    """The context every request this tool makes should use, or None for HTTP."""
    if cfg.get("endpoint_mode") == "http":
        return None
    context = ssl.create_default_context()
    if cfg.get("endpoint_mode") == "private_ca":
        context.load_verify_locations(cafile=os.path.join(config_dir, CA_FILE))
    return context


def save_ca_pem(pem_text, config_dir):
    """Validate an uploaded CA certificate, store it, and return its sha256.

    Validation happens against a throwaway context before anything is written, so
    a paste that is not a certificate is refused while the previous CA — which
    may be the one currently working — is still in place.
    """
    probe = ssl.create_default_context()
    probe.load_verify_locations(cadata=pem_text)
    path = os.path.join(config_dir, CA_FILE)
    data = pem_text.encode()
    with open(path, "w") as handle:
        handle.write(pem_text)
    os.chmod(path, 0o644)
    return hashlib.sha256(data).hexdigest()


def remove_ca(config_dir):
    """Drop the CA and anything derived from it.

    The truststore goes with it: a truststore built from a CA that is no longer
    configured is a trust decision nobody made.
    """
    for name in (CA_FILE, TRUSTSTORE):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            os.remove(path)


def build_truststore(config_dir, keytool="keytool"):
    """Import the CA into a Java truststore the server's JVM can read.

    Two things this gets right that a hand-run keytool usually does not. A stale
    store is removed first — keytool appends, so importing into an existing store
    leaves the previous CA trusted alongside the new one, and a rotation would
    silently keep trusting the old issuer. And the password travels through the
    environment (`-storepass:env`) rather than as an argv element: argv is
    readable by any local user through `ps` and /proc/<pid>/cmdline, and this
    tool is meant to run on a host beside a customer's storage.
    """
    store = os.path.join(config_dir, TRUSTSTORE)
    if os.path.exists(store):
        os.remove(store)
    env = dict(os.environ)
    env["STOREPASS"] = STOREPASS
    argv = [keytool, "-importcert", "-noprompt",
            "-alias", TRUSTSTORE_ALIAS,
            "-file", CA_FILE,
            "-keystore", TRUSTSTORE,
            "-storepass:env", "STOREPASS"]
    result = subprocess.run(argv, capture_output=True, text=True, env=env,
                            cwd=config_dir, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "keytool failed")
    os.chmod(store, 0o644)


def java_tool_options(cfg, config_dir):
    """What the server's JVM needs in its environment, or None.

    JAVA_TOOL_OPTIONS reaches the server process through its launcher, so
    nothing in the image has to change. One truststore covers both clients in
    that process: the Hadoop S3A filesystem that reads the Delta log, and the AWS
    SDK client that signs the URLs.
    """
    if cfg.get("endpoint_mode") != "private_ca":
        return None
    return ("-Djavax.net.ssl.trustStore=%s -Djavax.net.ssl.trustStorePassword=%s"
            % (os.path.join(config_dir, TRUSTSTORE), STOREPASS))


def ca_sha256(config_dir):
    """The hash of the CA on disk, or "" when there is none."""
    path = os.path.join(config_dir, CA_FILE)
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()
