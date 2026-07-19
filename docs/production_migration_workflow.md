# Production migration build workflow

The `Exordos element` GitHub Actions workflow keeps its existing default path
for pull requests, pushes, tags, and ordinary manual runs: it builds one element
version and publishes it with the configured repository credentials. That
steady-state path uses `exordos/exordos.yaml`, which builds only the canonical
`workspace-backend` image and cannot reintroduce a `workspace-mail` image.

Operators can select the `production_migration` profile in a manual
`workflow_dispatch` run to prepare the four immutable Workspace versions
required for the PostgreSQL-canonical cutover. This historical migration path
uses the explicit `exordos/exordos-production-migration.yaml` build config so
its accepted mail root remains available only for compatibility and rollback:

1. a compatibility version that preserves the exact current backend root and
   introduces the newly built mail root without enabling the writer gate;
2. a migration stage in `mail_projection` that reuses the accepted
   compatibility mail root and introduces the new backend root;
3. a prebuilt pre-write rollback wrapper that reuses the exact stage backend
   image;
4. the canonical wrapper that also reuses the exact stage backend image.

The current production backend and mail root images are read only from the
`WORKSPACE_PRODUCTION_CURRENT_BACKEND_IMAGE_URN` and
`WORKSPACE_PRODUCTION_CURRENT_MAIL_IMAGE_URN` GitHub secret. It is not a
workflow input, repository file, GitHub Actions artifact, or logged output.
The rendered element manifests in the configured private Exordos repository
necessarily record the selected immutable root image.

The private evidence root is read only from the
`WORKSPACE_PRODUCTION_MIGRATION_EVIDENCE_DIR` GitHub secret. It must identify a
durable, access-controlled filesystem on the self-hosted runner. The workflow
uses an owner-only, run-specific directory below that root and publishes no
migration evidence as a GitHub Actions artifact. Manifests, inventories, push
logs, image URNs, internal resource identifiers, and private paths must never
be copied into GitHub job summaries or artifacts.

The `publish` dispatch input has literal behavior for the migration profile:

- `publish=false` builds and verifies the four versions but does not push any
  of them to the Exordos repository;
- `publish=true` builds, verifies, and pushes all four versions. This is the
  only form of the run that produces deployable migration versions.

The profile performs these operations sequentially in one job:

1. Create an empty local commit and a unique prerelease-version tag without
   changing the checked-out source tree. The version is derived from the
   nearest strict `major.minor.patch` release tag across the complete reachable
   history, so neither a reachable prerelease tag nor a long interval between
   releases can break Exordos version parsing.
2. Build and verify the compatibility version with the exact current backend
   root and the newly built mail root. It retains all persistent disks and
   resources in `mail_projection`, preserves the current remote STARTTLS mail
   configuration and service identities, and omits the writer-gate role,
   config, and attester. Mail bootstrap and reload defer successfully until
   mail/PKI configuration and the exact compatibility marker are all present;
   Exim prestart permits an absent gate config only during this compatibility
   phase. Delivery of the compatibility marker invokes the idempotent final
   reload, so every configuration delivery order converges.
3. Build and verify the migration stage with the newly built backend root and
   the exact accepted compatibility mail root. It enables writer-gate role,
   config, and attester without using a static service alias or restarting Exim
   when the gate config is delivered. The gate config precedes the enforced
   marker, whose reload hook is the only final bootstrap trigger.
4. Read the exact backend root image from the rendered stage manifest and
   verify that it is the backend image produced by the stage inventory.
5. Build and verify the canonical wrapper with the exact stage backend root,
   `postgresql_canonical`, explicit confirmation, the accepted compatibility
   mail-image pin, and
   retained legacy mail resources.
6. Consecutive patch bases make semantic-version precedence strictly
   `compatibility < stage < canonical < rollback`; build metadata is never used
   to establish deployment order.
7. Build and verify the rollback wrapper with the exact stage backend root,
   `mail_projection`, canonical confirmation disabled, the accepted
   compatibility mail-image pin,
   and every legacy resource retained. This artifact is the only approved
   rollback target before the first canonical write.
8. Atomically make the complete private runner-local evidence directory durable
   with state `prepared` before the first repository push. A publishing failure
   must never leave the only evidence in a temporary directory. Do not expose
   deployable version outputs while evidence is only `prepared`.
9. When `publish=true`, push compatibility, stage, rollback, and canonical in
   that order,
   without force-overwriting any existing repository version. Canonical is
   published last, only after the exact pre-write rollback target exists. After
   all four pushes, atomically transition evidence to `published`; on any push
   failure, transition it to `publication_failed` while retaining completed
   push logs in the durable archive. When `publish=false`, skip all four pushes
   and atomically transition evidence from `prepared` to `verified_only`.
   Read back the terminal state, verify the relative evidence digest and exact
   source/version bindings from the finalized archive, and expose the four
   non-sensitive version outputs only after a successfully read-back
   `published` state. A `verified_only` run emits no deployable version outputs.

All four artifact trees receive SHA-256 inventories and every compressed image
is checked with `zstd -t`. Verification proves that compatibility preserves the
current backend root while selecting the built mail root without gate runtime,
and that stage, canonical, and rollback select that exact accepted mail root.
Canonical and rollback reuse the stage backend image. Every version retains the
backend data/control disks, mail data disk, mail config/PKI resources, and mail
DNS identity. The canonical backend must contain no Messenger-mail
configuration and the canonical artifact declares no mail runtime services.

The private evidence bundle records the source commit and tree, pinned UI
commit, exact CLI version, all four version strings and their verified strict
semantic-version ordering, rendered manifests,
SHA-256 inventories, compression checks, and sanitized command results. For a
publishing run it also records the four push logs. A missing secret, archive
collision, tag collision, source-tree change, unexpected manifest, image
mismatch, compression failure, partial publication, or existing repository
version fails the workflow. The workflow never deploys an element.

Evidence state transitions are monotonic and durable:
`prepared -> published`, `prepared -> publication_failed`, or
`prepared -> verified_only`. The state and push logs are updated in the private
archive with owner-only permissions and atomic replacement, and the relative
evidence digest is recomputed after each terminal state/log transition. A
`publication_failed` bundle is retained for reconciliation; it must never be
treated as a deployable four-version set or silently removed by a retry.

Before a production cutover, an operator must read back the finalized private
evidence bundle, refresh the configured Exordos repository, and prove that all
four exact versions are `AVAILABLE` in the intended repository and that the
evidence state is `published`. A successful GitHub job alone is not deployment
authorization.
