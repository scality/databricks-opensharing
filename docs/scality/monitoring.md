# Monitoring: metrics, scrape jobs, alerts and runbooks

The setup image serves Prometheus metrics on its metrics port, **9482**
(`METRICS_PORT`), next to `/healthz` and `/readyz`; the page port serves the same
`/metrics`. The series are listed in [`setup/README.md`](../../setup/README.md#metrics).
The server image alone exposes no metrics, only `/healthz`.

The scrape job is named **`isv-opensharing`** on both products; the alert rules match on
it.

## Scraping

**ARTESCA (MetalK8s).** [`deploy/kubernetes/servicemonitor.yaml`](../../deploy/kubernetes/servicemonitor.yaml)
carries the `metalk8s.scality.com/monitor` label the MetalK8s Prometheus selects on, and
targets the `opensharing-metrics` Service, which publishes the pod even while it is not
ready — an unconfigured or failed deployment is when the metrics matter.

**RING.** A static scrape job on the Prometheus that reaches the node running the
container. The Federation form, for a role's `meta/main.yml`
`prepare-prometheus-configuration` dependency, is not written: there is no role yet.

```yaml
scrape_configs:
  - job_name: isv-opensharing
    metrics_path: /metrics
    static_configs:
      - targets: ["<node>:9482"]
```

## Alert rules

[`monitoring/alerts.yaml`](../../monitoring/alerts.yaml) is the one source, tested by
[`monitoring/alerts.test.yaml`](../../monitoring/alerts.test.yaml) with
`promtool test rules` in CI ([`monitoring.yml`](../../.github/workflows/monitoring.yml)).
[`deploy/kubernetes/prometheusrule.yaml`](../../deploy/kubernetes/prometheusrule.yaml) is
generated from it (`python3 monitoring/render-prometheusrule.py`), and CI fails when the
two differ. Labels follow the RING convention (`severity`, `domain`, `feature`); each
alert's `runbook` annotation points at its section below.

On RING the rule file goes where the customer's custom rules go
(`/srv/scality/pillar/custom/custom_alerts/`), until a product-side placement exists.

| Alert | Fires when | Severity |
| --- | --- | --- |
| `OpenSharingDownCritical` | the metrics scrape fails for 5 minutes | critical |
| `OpenSharingServerNotRunningCritical` | a configured deployment's sharing server is not running for 5 minutes | critical |
| `OpenSharingCheckFailedWarning` | a verification check failed (`opensharing_check == 0`) for 10 minutes | warning |
| `OpenSharingCheckNotRunWarning` | a verification check could not run (`opensharing_check == -1`) for 30 minutes | warning |
| `OpenSharingNeverVerifiedInfo` | a deployment has served for an hour with no verification | info |
| `OpenSharingTokenExpiryWarning` | the recipient token's stated expiry is less than 14 days away | warning |

Not covered: the expiry of the public TLS certificates in front of the share and S3
endpoints. No series here carries it; on ARTESCA, cert-manager's own
`certmanager_certificate_expiration_timestamp_seconds` does when its metrics are scraped.

## Runbooks

### OpenSharingDownCritical

The setup process does not answer on 9482. Check the container or pod is running
(`docker ps`, `kubectl get pods -l app.kubernetes.io/name=opensharing`) and read its log.
A container that restarts in a loop usually names the cause in its first lines — for
example the `/config` ownership warning after an upgrade (see
[`setup/README.md`](../../setup/README.md#upgrading-a-config-volume-written-as-root)).

### OpenSharingServerNotRunningCritical

The configuration exists but the sharing server is down; recipients get no answer. Open
the setup page: the state is `stopped` or `failed_start` and the server log tail says
why. Press **Apply & start** after fixing the cause; a failed apply restores the previous
configuration and restarts it.

### OpenSharingCheckFailedWarning

A gate of the verification suite ran and failed: the `id` label names it, and a per-table
check carries the table's position in `table`. The page shows the same check with the
table's name and the detail. Do not hand out a profile while this fires. The four
causes of a silent 403 are in [README.md](README.md#the-four-things-that-are-each-a-silent-403).

### OpenSharingCheckNotRunWarning

A check could not run — typically the S3 endpoint was unreachable at the time. It is an
absent measurement, not a finding about the share. Check reachability from the node, then
press **Verify**.

### OpenSharingNeverVerifiedInfo

The server has served for an hour on a configuration nothing has verified — normal right
after a container restart, since a verdict does not survive the process that took it.
Press **Verify**.

### OpenSharingTokenExpiryWarning

The expiry date stamped on the recipient token is near. The server does not enforce it:
access ends when the token is rotated. Rotate the token (**Rotate token** on the page),
then send the recipient the new `.share` profile.
