#!/usr/bin/env python3
"""Render monitoring/alerts.yaml as the ARTESCA PrometheusRule manifest.

    python3 monitoring/render-prometheusrule.py          # write deploy/kubernetes/prometheusrule.yaml
    python3 monitoring/render-prometheusrule.py --check  # exit 1 if anything is stale

alerts.yaml is the one source; the manifest is its `groups` wrapped in a PrometheusRule
with the label the MetalK8s Prometheus selects on. `--check` also compares the
`spec.alerts` of isv-integration.yaml with alerts.yaml (name, expression, `for`,
labels, summary). Needs PyYAML.
"""
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "alerts.yaml")
TARGET = os.path.join(HERE, "..", "deploy", "kubernetes", "prometheusrule.yaml")
DESCRIPTOR = os.path.join(HERE, "..", "isv-integration.yaml")

HEADER = """# GENERATED from monitoring/alerts.yaml by monitoring/render-prometheusrule.py — edit
# the source and re-run; CI fails when this file is stale. Loaded by the MetalK8s
# Prometheus, which selects PrometheusRules carrying `metalk8s.scality.com/monitor`.
"""


def render():
    with open(SOURCE) as handle:
        groups = yaml.safe_load(handle)["groups"]
    manifest = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": "opensharing",
            "labels": {"app.kubernetes.io/name": "opensharing",
                       "metalk8s.scality.com/monitor": ""},
        },
        "spec": {"groups": groups},
    }
    return HEADER + yaml.safe_dump(manifest, sort_keys=False, width=100)


def _alert_key(rule):
    return (rule["alert"], " ".join(str(rule["expr"]).split()), str(rule.get("for", "")),
            tuple(sorted((rule.get("labels") or {}).items())),
            (rule.get("annotations") or {}).get("summary", ""))


def descriptor_drift():
    """Alerts that differ between isv-integration.yaml and alerts.yaml, as sentences."""
    with open(SOURCE) as handle:
        source = {r["alert"]: _alert_key(r)
                  for g in yaml.safe_load(handle)["groups"] for r in g["rules"]}
    with open(DESCRIPTOR) as handle:
        declared = {r["alert"]: _alert_key(r)
                    for r in (yaml.safe_load(handle)["spec"].get("alerts") or [])}
    problems = ["%s is in alerts.yaml but not in isv-integration.yaml" % a
                for a in sorted(set(source) - set(declared))]
    problems += ["%s is in isv-integration.yaml but not in alerts.yaml" % a
                 for a in sorted(set(declared) - set(source))]
    problems += ["%s differs between the two files" % a
                 for a in sorted(set(source) & set(declared)) if source[a] != declared[a]]
    return problems


def main(argv):
    text = render()
    if argv[1:] == ["--check"]:
        try:
            with open(TARGET) as handle:
                current = handle.read()
        except OSError:
            current = ""
        failed = False
        if current != text:
            print("%s is stale: run monitoring/render-prometheusrule.py" % TARGET,
                  file=sys.stderr)
            failed = True
        for problem in descriptor_drift():
            print(problem, file=sys.stderr)
            failed = True
        if failed:
            return 1
        print("prometheusrule.yaml and isv-integration.yaml match alerts.yaml")
        return 0
    with open(TARGET, "w") as handle:
        handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
