# Temporary credentials from a Scality STS

By default the server serves every table through **pre-signed URLs** signed with one static
S3 key, and advertises no `accessModes`, so a recipient reads by URL. This page describes
the optional second mode: the server hands the recipient **temporary credentials issued by
the RING or ARTESCA STS** (`AssumeRole`), scoped by one role per share, and the recipient
reads the table directory directly.

Nothing here is active unless `delta.sharing.scality.sts.endpoint` is set. Without it the
server's responses and its temporary-credential route are upstream's, unchanged.

Status: **unit-tested only**, against an in-process fake STS. Not yet run against a RING or
ARTESCA STS, and not yet run with a Databricks recipient — see
[the manual test procedure](#manual-live-test-procedure).

## How a recipient is told

The Delta Sharing protocol carries the choice in an `accessModes` array (`url`, `dir` or
both) on the table entries of the list-tables APIs and on the table metadata line
([`PROTOCOL.md`](../../PROTOCOL.md) lines 66-105, "Access Modes"; the field in the list
responses at lines 836 and 856, in the Metadata object at line 3118, in Metadata in Delta
format at line 3607). A server that omits it is URL-only (line 105). `dir` means the
server implements `POST …/tables/{table}/temporary-table-credentials` (line 2630), and
`location` must then be present (line 852).

With an STS endpoint configured, the server adds `"location"` and
`"accessModes": ["url","dir"]` to a table in `GET /shares/{share}/schemas/{schema}/tables`,
`GET /shares/{share}/all-tables` and `GET …/tables/{table}/metadata` when, and only when:

- a role is configured for that table (below), and
- the table is `historyShared: true` — the credential route refuses any other table.

`url` stays in the list, so a URL-only client keeps working. Which mode a recipient picks
when it is offered both is the recipient's choice (PROTOCOL.md line 101); the reference
client in this repository calls the credential route
(`client/src/main/scala/io/delta/sharing/client/DeltaSharingClient.scala` line 1165) but
does not read `accessModes`. **Whether a Databricks recipient switches to `dir` is not
known** until the live test below runs.

## What RING STS accepts

Measured on lab RING STS (2026-11-04), read in the Vault source and confirmed by RING
engineering (2026-11-06):

| | |
| --- | --- |
| Caller | `AssumeRole` works for an **IAM user**. The account root cannot assume a role. |
| `DurationSeconds` | **3600 at most.** The server clamps to 3600 whatever it is asked. |
| `PolicyArns` | **Rejected — 400, the AssumeRole fails.** The server never sends it. |
| Inline `Policy` | **Accepted and ignored.** It narrows nothing. |
| Inline role policies (`PutRolePolicy`) | 501. A role gets its permissions from **attached managed policies**. |

So the scope of a credential is the scope of the role, and nothing else. **Create one
read-only role per share**, its attached managed policy limited to the share's bucket and
prefix(es). A role shared by two shares gives each recipient the other's data.

Upstream's AWS vendor builds an inline session policy per table
(`server/src/main/scala/io/delta/sharing/server/credential/aws/AwsCredentialVendor.scala`)
and calls AWS STS with no endpoint override. Neither works here: the policy is ignored,
and the endpoint is AWS. The Scality path
([`ScalitySts.scala`](../../server/src/main/scala/io/delta/sharing/server/scality/ScalitySts.scala))
replaces that vendor whenever an STS endpoint is configured.

## Configuration

Everything is in `core-site.xml`, next to the S3A keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `delta.sharing.scality.sts.endpoint` | unset — feature off | STS endpoint URL. Without a scheme, `https://` unless `fs.s3a.connection.ssl.enabled` is `false` (the S3A rule). |
| `delta.sharing.scality.sts.region` | `fs.s3a.endpoint.region`, then `us-east-1` | SigV4 signing region. |
| `delta.sharing.scality.sts.role.<share>` | — | Role ARN for every table of the share. |
| `delta.sharing.scality.sts.role.<share>.<schema>` | — | Overrides the share's role for one schema. |
| `delta.sharing.scality.sts.role.<share>.<schema>.<table>` | — | Overrides it for one table. The most specific key wins. |
| `delta.sharing.scality.sts.session.policy` | `false` | Also send upstream's inline per-table session policy. RING ignores it; keep it off unless the STS is known to enforce one. |

Session length is the existing YAML key `temporaryCredentialValiditySeconds` (default
3600), sent as `DurationSeconds` clamped to 3600. The recipient is told the expiry STS
returns.

The server's own identity — the IAM user that calls `AssumeRole` — comes from the default
AWS credential chain: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in the process
environment, the same variables the presigner already needs. It must be an IAM user, not
the account root, and its policy must allow `sts:AssumeRole` on the roles above. Each
role's trust policy must name that user.

TLS to the STS endpoint uses the JVM trust store, as the S3 client does; a private CA is
added the same way ([TLS: three cases](README.md#tls-three-cases)). Path-style addressing
does not apply to STS.

Example — two shares, one role each:

```xml
<property><name>delta.sharing.scality.sts.endpoint</name><value>https://sts.example.com</value></property>
<property><name>delta.sharing.scality.sts.region</name><value>us-east-1</value></property>
<property>
  <name>delta.sharing.scality.sts.role.sales</name>
  <value>arn:aws:iam::123456789012:role/delta-share-sales-read</value>
</property>
<property>
  <name>delta.sharing.scality.sts.role.finance</name>
  <value>arn:aws:iam::123456789012:role/delta-share-finance-read</value>
</property>
```

A share, schema or table name containing a dot makes these keys ambiguous; avoid dots in
names you map.

## Manual live test procedure

Not yet run. It needs a separately approved lab slot; it creates an IAM user, two roles
and their policies, and removes them at the end. Replace every `<…>`.

1. **Identities on the Scality account.** Create IAM user `<ds-vendor>` with an access key
   and a managed policy allowing `sts:AssumeRole` on `arn:aws:iam::<account>:role/ds-*`.
   Create roles `ds-share-a-read` and `ds-share-b-read`, each with a trust policy naming
   `<ds-vendor>` and one attached managed policy: `s3:GetObject` on
   `arn:aws:s3:::<bucket>/<prefix-a>/*` and `s3:ListBucket` on `arn:aws:s3:::<bucket>`
   conditioned on `s3:prefix` `<prefix-a>/*` (respectively `<prefix-b>`).
2. **Server.** Two shares, `share_a` and `share_b`, one `historyShared: true` table each,
   under `<prefix-a>` and `<prefix-b>`, plus a third share `share_c` with no role mapped.
   `core-site.xml` as in the example, mapping `share_a` and `share_b` to their roles;
   `temporaryCredentialValiditySeconds: 7200`. Run with `AWS_ACCESS_KEY_ID` /
   `AWS_SECRET_ACCESS_KEY` of `<ds-vendor>`.
3. **Advertisement.** `GET /shares/share_a/all-tables` lists the table with `location` and
   `"accessModes":["url","dir"]`; `GET /shares/share_c/all-tables` has neither. The
   `…/metadata` line of the `share_a` table carries both fields.
4. **Vend.** `POST /shares/share_a/schemas/<schema>/tables/<table>/temporary-table-credentials`
   with body `{}` returns `awsTempCredentials` and an `expirationTime` about one hour out
   (not two: the clamp). The STS access log shows `AssumeRole` on `ds-share-a-read`, with
   no `PolicyArns` parameter. The same call on the `share_c` table returns 400.
5. **Scope.** With those three values as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
   `AWS_SESSION_TOKEN`, against `fs.s3a.endpoint`: listing and reading under
   `<prefix-a>` succeed; reading under `<prefix-b>` is 403; a `PutObject` under
   `<prefix-a>` is 403.
6. **Expiry.** After `expirationTime`, the same read is 403.
7. **Default unchanged.** Remove `delta.sharing.scality.sts.endpoint`, restart: no
   `accessModes` anywhere, the presigned-URL query still returns 200 Parquet fetches
   (`setup/ci/integration.sh` covers this path).
8. **Databricks.** From a Databricks recipient, `SELECT` the `share_a` table. The server's
   audit events show which route it used: `temporary-table-credentials` means `dir`,
   `…/query` means URL. Record which.
9. **Clean up.** Delete the access key, the user, both roles and their policies.

Record the RING or ARTESCA version, the date and each step's result in the verified-claims
table of [`README.md`](README.md) before relying on this mode.
