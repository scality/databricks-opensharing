# CLAUDE.md — databricks-opensharing (Scality fork)

A fork of [delta-io/delta-sharing](https://github.com/delta-io/delta-sharing), so a Databricks recipient can read Delta and Iceberg tables living on **Scality RING or ARTESCA**, with the server out of the data path. `origin` is `scality/databricks-opensharing`, `upstream` is `delta-io/delta-sharing`.

The user-facing explanation is the README preamble and [`docs/scality/`](docs/scality/) — read those before changing anything; this file is only what an agent needs about the fork itself.

## Working with Claude Code: the main session orchestrates, subagents do the work

Keep the session the human types in lean. It holds requests, decisions and short
conclusions; reading, searching, implementing, testing and reviewing happen in
subagents (Agent tool), which return a summary rather than file dumps.

- **Delegate by default** whenever a task means reading several files, sweeping the
  codebase, implementing, running long gates, or reviewing. Work directly only for a
  single-fact lookup or a one-line edit where the file and change are already known.
- **Pick the model by complexity — Sonnet is the floor, never Haiku:**
  - **Sonnet** — mechanical work whose target is already stated: searches, enumeration,
    edits the tests pin, running gates, doc updates.
  - **Opus** — judgement: design, debugging, reviews, anything that moves a figure or
    classifies.
  - **Fable** — sparingly, it is the most expensive: only the hardest calls, where
    Opus is genuinely not enough — adversarial verification of a change that matters,
    synthesis across many results, security-sensitive or schema/production-affecting
    changes. One Fable pass at the end beats Fable on every step.
- **A subagent does the work itself and never spawns another subagent.** Delegation is
  the orchestrating session's job; one level deep is the whole design. If a subagent
  judges the task needs a stronger model, it says so and stops — it does not launch
  one. Measured 2026-09-13: two Sonnet agents each spent roughly 100k tokens deciding
  to launch an Opus child and returned a plan instead of a result, so the cheap tier
  was billed for nothing, the expensive one ran anyway, and the grandchild was
  invisible to the orchestrator — no completion notification, and two of them writing
  into one shared worktree. Say it in the brief ("do this yourself, do not use the
  Agent tool"), because an agent handed a hard task reaches for that reflex on its own.
- **Brief each subagent fully** — it starts with no context: goal, paths, constraints,
  what "done" means, and what to report back. Ask for the conclusion and evidence, not
  transcripts. Run independent agents in parallel.
- **Relay only what matters** to the human: outcome, decisions needed, verification
  evidence. A subagent's "tests pass" is a claim until the evidence is seen.

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
