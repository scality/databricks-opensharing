# CLAUDE.md — databricks-opensharing (Scality fork)

A fork of [delta-io/delta-sharing](https://github.com/delta-io/delta-sharing), so a Databricks recipient can read Delta and Iceberg tables living on **Scality RING or ARTESCA**, with the server out of the data path. `origin` is `scality/databricks-opensharing`, `upstream` is `delta-io/delta-sharing`.

The user-facing explanation is the README preamble and [`docs/scality/`](docs/scality/) — read those before changing anything; this file is only what an agent needs about the fork itself.

## What may diverge from upstream

Keep the fork thin. Two things are ours and everything else should stay identical to the upstream tag we track:

- **The presigner fix.** Upstream's `S3FileSigner` built its presigning client with an empty `S3ClientCreationParameters`, so `fs.s3a.endpoint` never reached it: metadata reads worked while every presigned URL pointed at `s3.amazonaws.com` and the recipient's fetch returned **403**. That is upstream [#753](https://github.com/delta-io/delta-sharing/issues/753); the fix is carried here and offered upstream as [PR #965](https://github.com/delta-io/delta-sharing/pull/965). ⚠ **This is the load-bearing change** — without it the server is unusable against a non-AWS endpoint, and the failure is a 403 at the recipient rather than an error on the server.
- **The README preamble and `docs/scality/`**, plus the published container image `ghcr.io/scality/databricks-opensharing:latest` (the only image on Docker Hub is years old and predates the S3A endpoint handling this depends on).
- **`setup/` and the second image it builds**, `ghcr.io/scality/databricks-opensharing-setup`. It touches no upstream file — `setup/Dockerfile` builds `FROM` the server image rather than editing it — so a rebase carries `setup/` through unchanged, the same as the docs.

A change that is not one of those belongs upstream, not here.

## Branches and rebasing

Work happens on the release branch tracking the upstream tag (`scality-1.4` today); `main` follows `origin/main`. To move to a newer upstream release:

```bash
git fetch upstream --tags
git log --oneline upstream/main..HEAD      # what is ours — expect the presigner fix + docs
git rebase --onto v<new-tag> v<old-tag> scality-1.4
```

Resolve conflicts in favour of upstream everywhere except the two items above, then verify the presigner change is still present before pushing — a rebase that silently drops it leaves a server that passes its own tests and 403s every recipient.

`setup/ci/integration.sh` must pass before a tag is pushed — it is the first automated
check in this repository that would notice a dropped presigner fix, since it exercises
the actual data path (presigned URL on the right host, `PAR1`, unsigned fetch refused)
rather than the unit tests, which mock the server. It runs in
[`publish-image.yml`](.github/workflows/publish-image.yml) ahead of the tag-push step, so
a red run there blocks the push; run it locally against a candidate build before tagging
if there is any doubt.

## Release

Tag `v<upstream>-scality.<n>`. [`publish-image.yml`](.github/workflows/publish-image.yml)
publishes four refs from that one tag: `ghcr.io/scality/databricks-opensharing:<tag>`,
`:latest`, `ghcr.io/scality/databricks-opensharing-setup:<tag>` and `-setup:latest`. Auth
is `GITHUB_TOKEN`; no long-lived credential is stored.

**Check a new package pulls anonymously after its first tag.** Measured 2026-09-11 on the
first push of `databricks-opensharing-setup`: the package was public immediately, so no
manual visibility step was needed. That is the org's package setting rather than anything
this workflow does, so verify rather than assume — an anonymous token from
`https://ghcr.io/token?scope=repository:scality/<package>:pull` followed by a manifest
GET must answer 200; if it does not, the fix is *Change package visibility → Public* in
the package's own settings.

## Push policy

Owned repo: commit to the working branch and push after each commit. It is a **public** fork, so nothing internal — no customer names, no lab hostnames, no credentials — goes into a commit message, a doc or a test fixture.

## Where it is used

The lab stack that deploys this server is [`isv-labs/scripts/stacks/artesca-plus-delta-sharing/`](../isv-labs/scripts/stacks/artesca-plus-delta-sharing/) — its `CLAUDE.md` carries the stack contract and its `RUNBOOK.md` the live environment, the public-endpoint paths and credential rotation. ⚠ **One server process signs for exactly one S3 endpoint**; that constraint is documented there and is not a configuration detail to work around here.
