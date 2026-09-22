# Provider Entity API contract

Workspace owns this contract. Provider bridges consume it; they must not copy or
extend it independently. The generated OpenAPI document is the machine-readable
source of truth, and this page records the synchronization rules that are easy to
miss when reading schemas alone.

## Version

- HTTP base: `/v1/provider/`
- bootstrap schema: `schema_version: 2`
- timestamps: UTC ISO-8601
- authenticated caller: the `workspace.provider.sync` permission and one enabled
  provider registration for the current project and IAM user

`schema_version` versions the bootstrap and entity synchronization protocol. It
is intentionally independent from the `/v1` public client API version.

## Resources

`users`, `streams`, `stream_bindings`, `topics`, `topic_bindings`, `messages`,
`message_flags`, and `message_reactions` are available through:

- `PUT /v1/provider/registration` to idempotently register the authenticated IAM
  identity; it requires `workspace.provider.sync`, and changing its existing
  provider UUID or name returns `409 provider_registration_conflict`
- `GET|PUT|DELETE /v1/provider/entities/{type}/{uuid}`
- `GET /v1/provider/entities/{type}` with keyset pagination
- `POST /v1/provider/entities/actions/apply/invoke` for an atomic batch of up to
  500 operations
- `GET /v1/provider/bootstrap?mode=paged` for a consistent manifest followed by
  paged entity reads
- `GET /v1/provider/bootstrap` for the equivalent NDJSON snapshot

The generated OpenAPI document defines the complete payload shape for each
endpoint.

## Identity and ordering

Provider entity UUIDs are stable identities. Non-user UUIDs belong to exactly
one project; trying to reuse one in another project returns
`409 entity_project_conflict`. Users are global identities and can be attached
to multiple projects.

Every upsert carries a SHA-256 `content_hash`. Bridges should also send
`source_updated_at`:

- older source timestamps are acknowledged as `unchanged` and cannot overwrite
  newer data;
- the same timestamp and hash is an idempotent retry;
- the same timestamp with different content returns
  `409 entity_version_conflict`;
- a newer timestamp is applied, while a newer timestamp with identical content
  advances the provider cursor without rewriting the entity.

Omitting `source_updated_at` remains supported for simple clients; Workspace
then uses its current UTC processing time.

## Bootstrap and live continuation

The bootstrap manifest fixes `epoch_generation` and
`snapshot_epoch_version`. After importing every page from that snapshot, the
bridge continues from the returned event position. A changed/pruned epoch means
the snapshot is no longer resumable and the bridge must bootstrap again.

Provider-originated changes are not echoed to the same provider consumer. Other
providers and user clients still receive their normal events.
