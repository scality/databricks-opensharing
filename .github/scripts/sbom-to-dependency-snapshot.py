#!/usr/bin/env python3
"""Turn a CycloneDX SBOM of the server image into a GitHub dependency-graph snapshot.

    python3 .github/scripts/sbom-to-dependency-snapshot.py <sbom.cdx.json> > snapshot.json
    gh api repos/<owner>/<repo>/dependency-graph/snapshots --input snapshot.json

Dependabot has no sbt ecosystem, so it cannot read build.sbt. What it can do is raise
alerts on packages submitted to the dependency graph. This submits the Maven packages
the SBOM found in the built image — the JVM dependencies sbt actually resolved and
packaged, transitive ones included — so Dependabot alerts cover them.

Environment (all set by GitHub Actions): GITHUB_SHA, GITHUB_REF, GITHUB_RUN_ID,
GITHUB_WORKFLOW, GITHUB_JOB. Stdlib only.
"""
import datetime
import json
import os
import sys


def maven_packages(sbom):
    """{purl: {"package_url": purl}} for every Maven component, de-duplicated."""
    found = {}
    stack = list(sbom.get("components", []))
    while stack:
        comp = stack.pop()
        stack.extend(comp.get("components", []) or [])
        purl = comp.get("purl") or ""
        if purl.startswith("pkg:maven/"):
            # The image SBOM does not say which packages build.sbt names directly, so
            # every one is submitted as indirect rather than guessing.
            found[purl] = {"package_url": purl, "relationship": "indirect", "scope": "runtime"}
    return found


def snapshot(sbom, env):
    return {
        "version": 0,
        "sha": env["GITHUB_SHA"],
        "ref": env["GITHUB_REF"],
        "job": {"correlator": "%s-%s" % (env.get("GITHUB_WORKFLOW", "publish"),
                                         env.get("GITHUB_JOB", "dependency-graph")),
                "id": env.get("GITHUB_RUN_ID", "0")},
        "detector": {"name": "syft-cyclonedx", "version": "1",
                     "url": "https://github.com/anchore/syft"},
        "scanned": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifests": {
            "server-image": {
                "name": "server-image",
                "file": {"source_location": "build.sbt"},
                "resolved": maven_packages(sbom),
            }
        },
    }


def main(argv):
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0]) as handle:
        sbom = json.load(handle)
    doc = snapshot(sbom, os.environ)
    if not doc["manifests"]["server-image"]["resolved"]:
        print("no Maven component in %s — refusing to submit an empty snapshot" % argv[0],
              file=sys.stderr)
        return 1
    json.dump(doc, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
