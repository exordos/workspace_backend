# GET /api/workspace/v1/messenger/message_reactions/

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [The section Core Messenger](../README.md)

## Status and boundary of current contract

The method, path, public JSON, and authorization follow the current contract from [`workspace_api.md`](../../../../workspace_api.md); target pagination `100/500` is a separate observable behavior change.

![Sequence diagram](diagrams/get_message_reactions_list.svg)

The source that you can edit: [`get_message_reactions_list.puml`](diagrams/get_message_reactions_list.puml).

## The operation

**Method and way:** `GET /api/workspace/v1/messenger/message_reactions/`

**Purpose:** To get a list of reactions to visible messages.

## A public request

Without a body.:

```http
GET /api/workspace/v1/messenger/message_reactions/?message_uuid=a93dca35-3061-4748-bda4-7f6f8c660ea5&page_limit=100
Authorization: Bearer <access_token>
```

Current semantics RestAlchemy: missing or equal to `0` `page_limit` gives unlimited sample; negative or non-integer value gives HTTP `400`; positive value has no maximum. This is the current gap. Target: missing or `0` => `100`; `1..500` is accepted accurately; negative, non-integer or `>500` => HTTP `400` without clamp; unbounded mode is absent. marker.

## A successful public response

HTTP `200`:

```json
[
  {
    "uuid": "bd4b7632-8788-435a-93cc-6873657335c6",
    "project_id": "22222222-2222-2222-2222-222222222222",
    "message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
    "user_uuid": "11111111-1111-1111-1111-111111111111",
    "emoji_name": "thumbs_up",
    "provider": null,
    "delivery": null,
    "created_at": "2026-06-22T10:12:00Z",
    "updated_at": "2026-06-22T10:12:00Z"
  }
]
```

## Public errors

The bearer-token IAM and project area are required; the invisible or missing resource or marker gives `404`..

## The target boundary RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceMessageReactionView(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_message_reactions_v1"

    message_uuid = properties.property(types.UUID(), read_only=True)
    canonical_message_uuid = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceMessageReactionController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceMessageReactionView, convert_underscore=False, process_filters=True,
    )
```

Public `message_uuid`  scalar UUID placement; internal
`canonical_message_uuid` field permissions are hidden.UUIDThe original fact
The physical links remain indexed FK, and the
The original metadata of the provider/delivery is closed.

## Synchronized reading path

1. Interpret the public filter `message_uuid` as
   `MESSAGE_PLACEMENT.uuid`, Re-establish placement and check through its stream
   active `USER_STREAM_BINDING` and equality membership generation.
   canonical message and attach the message only for
   Cleaned up .`provider`/`delivery`. Never aggregate when reading.
2. Returns the result directly from the indexed representation without calculations.
3. Do not add transactional outbox entries, tasks, work with projections, public events or WebSocket.

## Idempotency and consistency visible to the client

This GET has no side effects. It can observe the permissible lag from an earlier record, but does not perform recovery, fan-out distribution, `COUNT`, `GROUP BY`, window or lateral operations, correlated subqueries or search for missing bindings.

Public `message_uuid` in each line remains placement UUID and specifies access
check. Raw facts/snapshots canonical-message-global and visible to all
placements This privacy trade-off has been adopted.
How did you do that? Critic risk #8.

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [The section Core Messenger](../README.md)
