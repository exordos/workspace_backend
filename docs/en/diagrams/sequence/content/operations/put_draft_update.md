# `PUT /api/workspace/v1/messenger/drafts/{draft_uuid}`


Common target-invariant of reliability: each immutable outbox event outputs exactly one immutable typed task with unique `outbox_event_uuid`; coalescing is absent. Task stores the actual exact scope key, uses lease/fencing, retry/backoff, max attempts/DLQ, reaper and idempotent effect guard. Topic scope is applied only to placement/message-binding work; shared rows do not receive implicit fallback on topic.

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [Content and users Workspace](../README.md)

Status: the target specification of the operation, first developed in the documentation.
remains unchanged and is the normative [`workspace_api.md`](../../../../workspace_api.md).
This file describes the transaction and projection target boundaries; it doesn ' t
production code, migration SQL or new endpoint.

![Sequence diagram](diagrams/put_draft_update.svg)

[The source that you can edit PlantUML](diagrams/put_draft_update.puml)

## Purpose and public contract

Replace only the owner 's Markdown payload with Optimistic Competition.

Authentication: Bearer IAM; `project_id` and current `user_uuid` tokens are taken from context IAM.

## Path and query settings

| Location | Name of the person | Type / rule |
| --- | --- | --- |
| The way | `draft_uuid` | UUID |
| The title | `If-Match` | a strictly necessary, accurate audit, for example `"1"` |

The collection pagination, where it is provided, preserves the current contract `page_limit` and UUID
`page_marker` and returns `X-Pagination-Limit`, and
`X-Pagination-Marker` Only if there 's a next page ..

## The body of the query

```json
{
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  }
}
```

## A Successful Answer

`200`

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  },
  "revision": 2,
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:01:00Z"
}
```

Answer title: `ETag: "2"`.

## Errors and authorization

Only `payload` is accepted. Missing `If-Match` returns `428`. Wrong/outdated revision returns `412` with current draft snapshot and ETag. Wrong payload `payload` returns `400`; unavailable draft returns not found.

General form of response for validation error:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## The target boundary RestAlchemy

```python
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceDraft(models.ModelWithUUID, models.ModelWithProject,
                     models.ModelWithTimestamp, orm.SQLStorableMixin):
    # Contract boundary only; target physical naming/decomposition is not selected.
    __tablename__ = "m_workspace_drafts"
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_uuid = properties.property(types.UUID(), required=True, read_only=True)
    payload = properties.property(types.Dict(), required=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class WorkspaceDraftController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceDraft,
        convert_underscore=False,
        process_filters=True,
    )
    # Narrow overrides preserve owner scope, keyset marker, ETag and If-Match.
```

Each public reference to an entity is declared a scalar UUID property RestAlchemy, not `relationship` (which would be serialized as URI). The corresponding physical column `*_uuid`  an indexed external key with an explicitly selected reference action. Therefore, public JSON keeps UUID unchanged.

The target internal model of the drafts is not deliberately reworked here. The announcement fixes an unchanged scalar boundary UUID/ETag. The physical UUID-columns of the user/stream/topic remain FK-indexed with cascading behavior from the current contract; the relationship RestAlchemy should not change the public one. UUID JSON.

## Synchronous path API

1. Get the exact value `If-Match`.
2. Block the owner &amp; apos; s draft and compare `revision`.
3. Replace only `payload`, enlarge `revision` and update the timestamp.
4. Add internal unmodifiable domain entry to the outbox without public derivative.
5. Save the transaction and return the new line/ETag.

## Outbox, Typed tasks, worker and real-time work

No message or meter projection planned.

The internal immutable outbox event returns one `delivery_snapshot_event`,
which idempotently fixes the absence of a public derivative and ends;
The Workspace event row and WebSocket-delivery are not created.

## Idempotence, keys and races

Comparing and updating the revision prevents loss of updates. Repeating with the outdated ETag gets `412` and cannot overwrite newer content.

## The moment of visibility for the client

The initiator client immediately sees the fixed draft. Other clients will only see it after a reboot or explicit re-request of the drafts; no consistent update is sent in the end..

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [Content and users Workspace](../README.md)
