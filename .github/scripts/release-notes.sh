#!/usr/bin/env bash
# Print the GitHub Release notes for one published tag, as Markdown, on stdout.
#
# Inputs (environment):
#   VERSION        the release tag, e.g. v1.4.1-scality.5
#   SERVER_DIGEST  sha256:… of ghcr.io/<repo>:<VERSION>, as the registry reported it
#   SETUP_DIGEST   sha256:… of ghcr.io/<repo>-setup:<VERSION>
#   REPOSITORY     owner/name; defaults to GITHUB_REPOSITORY, then scality/databricks-opensharing
#
# Run locally from a checkout that has the tags:
#   VERSION=v1.4.1-scality.4 SERVER_DIGEST=sha256:… SETUP_DIGEST=sha256:… bash .github/scripts/release-notes.sh
set -euo pipefail

: "${VERSION:?VERSION is required}"
: "${SERVER_DIGEST:?SERVER_DIGEST is required}"
: "${SETUP_DIGEST:?SETUP_DIGEST is required}"
REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:-scality/databricks-opensharing}}"
IMAGE="ghcr.io/${REPOSITORY}"
SETUP_IMAGE="ghcr.io/${REPOSITORY}-setup"

case "$SERVER_DIGEST$SETUP_DIGEST" in
  sha256:*sha256:*) ;;
  *) echo "SERVER_DIGEST and SETUP_DIGEST must both be sha256:<hex>" >&2; exit 1 ;;
esac

# The previous release tag on this line, if any: the change list runs from there.
previous="$(git describe --tags --abbrev=0 --match 'v*-scality.*' "${VERSION}^" 2>/dev/null || true)"

cat <<EOF
## Images

| Image | Tag | Digest |
| --- | --- | --- |
| \`${IMAGE}\` | \`${VERSION}\` | \`${SERVER_DIGEST}\` |
| \`${SETUP_IMAGE}\` | \`${VERSION}\` | \`${SETUP_DIGEST}\` |

Pin the digest, not only the tag:

\`\`\`bash
docker pull ${IMAGE}@${SERVER_DIGEST}
docker pull ${SETUP_IMAGE}@${SETUP_DIGEST}
\`\`\`

\`linux/amd64\` only. This release moves no floating tag: \`latest\` is not updated.

EOF

if [[ -n "$previous" ]]; then
  echo "## Changes since ${previous}"
  echo
  git log --no-merges --format='- %s (%h)' "${previous}..${VERSION}"
else
  echo "## Changes"
  echo
  echo "First release on this line."
fi

cat <<EOF

## Documentation

- What is verified and what is not: [docs/scality/README.md](https://github.com/${REPOSITORY}/blob/${VERSION}/docs/scality/README.md)
- The setup image: [setup/README.md](https://github.com/${REPOSITORY}/blob/${VERSION}/setup/README.md)
EOF
