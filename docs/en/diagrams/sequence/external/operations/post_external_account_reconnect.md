# Reconnect an external account

[← The main index of the documentation](../../../../index.md) ·
[Index of sequence diagrams](../../README.md) ·
[The external integration section/runtime](../README.md)

`POST /api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke`

Check and replace the write-only accounts, then resume synchronization.

![Sequence diagram](diagrams/post_external_account_reconnect.svg)

[The source that you can edit PlantUML](diagrams/post_external_account_reconnect.puml)

## The request

No additional query parameters other than the path variables mentioned above.

Titles in addition to bearer tokens:

- `If-Match: "<revision>"` It 's mandatory .

```json
{
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "api_key": "write-only"
  }
}
```

## A Successful Answer

HTTP `200`:

```json
{
  "uuid": "0d4ae1d0-30ad-4d15-bf26-5789e8406201",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "00000000-0000-4000-8000-000000000001"
  },
  "credential_present": true,
  "status": "connecting",
  "live_ready": false,
  "safe_error": null,
  "capabilities": {},
  "desired_generation": 8,
  "applied_generation": 7,
  "last_progress_at": "2026-07-17T12:00:00Z",
  "created_at": "2026-07-17T11:00:00Z",
  "updated_at": "2026-07-17T12:00:00Z",
  "revision": 8
}
```

The responses of the resources with the revision contain strict `ETag: "<revision>"`.

## The errors

| HTTP | Behaviour in public |
| --- | --- |
| `403` | No `workspace.external_account.reconnect` permission or resource is outside authorized area. |
| `404` | Resource not available or not visible in specified area. |
| `428` | Not available `If-Match`. |
| `412` | The revision doesn 't match. |
| `403` | Provider policy/status forbids re-connection. |
| `400` | For unacceptable path values, query parameters or body, a standard validation error is used RESTAlchemy. |

Example of validation error body:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## The border RestAlchemy

Target resource/controller ad (supply documentation, not production code)):

```python
class ExternalAccount(models.ModelWithUUID, models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "m_external_accounts_v2"

    owner_user_uuid = properties.property(types.UUID(), required=True)
    settings = properties.property(EXTERNAL_ACCOUNT_SETTINGS_TYPE, required=True)
    status = properties.property(types.Enum(ACCOUNT_STATUSES), read_only=True)
    capabilities = properties.property(types.Dict(), read_only=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class ExternalAccountController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        ExternalAccount, hidden_fields=["owner_user_uuid"]
    )
```

`owner_user_uuid` hidden; public `settings.default_project_id` is a scalar UUID property. In the target repository, the indexed `owner_user_uuid` refers to the user Workspace with `ON DELETE CASCADE`, and the extracted indexed `default_project_uuid`  to the project registry with `ON DELETE RESTRICT`; when serialized, the latter is still embedded in `settings`. Public RestAlchemy ads do not use `relationships.relationship` for JSON in the form UUID because the relation (relationship) is serialized as URI. At the boundary of the physical scheme, each canonical non-polymorphic connection `*_uuid` is an indexed external key with a clearly selected reference action..

## Synchronous transaction

1. Authenticate the query, define the domain, check the resolution/body and find the canonical string for the indexed key.
2. Validate the new Zulip credential against the expected verified realm UUID,
   provider user ID and normalized `delivery_email`; any discrepancy is rejected
   fail-closed Before the replacement.
3. Block revision; encrypt and replace account data; set
   `connecting` status, not yet ready for live-work; add desired
   status and unchanged update outbox; record the transaction.
4. Returns the response after the transaction is recorded.
   credential, connection, lease and sync working without change.

## Background processing, events and consistency

Typed `delivery_snapshot_event` serves exact external-account
scope; topic task The desired state is not available without placement.
is a stable job control plane.

The background handler in one DB transaction records the materialized state and the ready envelope of the full image `external_account.updated`; both commit or rollback effects together. After commit a separate handler WebSocket sends, repeats and plays it; API/worker does not own client connections.

Consistency visible to the client: account replacement and desired generation are recorded; verification, detection, applied generation and live-work readiness converge asynchronously.

Reconnect It runs exactly the same bootstrap as connect: whole-account lease,
new supported queue/boundary, sequential realtime and only then history root.
Old queue/cursor The lasting recovery is not necessary.:
[`zulip_bridge/coordination_and_recovery.md`](../../../../zulip_bridge/coordination_and_recovery.md).
Bridge It 's running private calls under the current realm-bound mTLS certificate;
Workspace independently verifies current certificate/identity generation and
account lease. Failing to check the new Zulip `api_key` does not change either of these S2S
credential, Not old. account connection state.

## Idempotency and parallelism

UUID The account is created by the client at the time of creation; business uniqueness allows for one account `(owner_user_uuid, provider_kind)`..

Repeaters use stable business keys and the current state of the original. Each immutable outbox event creates a separate task with a unique `outbox_event_uuid`; the re-delivery of this task must be idempotent, coalescing is absent. Monopoly processing of the topic Messenger from new entries to old ones is only applied when the affected canonical placement actually refers to `(project_id, topic_uuid)`; admin/read provider operations do not create an artificial topic and are not included in this queue.

## The sources

- [`workspace_api.md`](../../../../workspace_api.md) — authoritative public routes, general JSON, page, events and contract WebSocket.
- [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md) — sanitized lifecycle of external resources, permissions and provider semantics.

[← The main index of the documentation](../../../../index.md) ·
[Index of sequence diagrams](../../README.md) ·
[The external integration section/runtime](../README.md)
