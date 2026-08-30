# POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [The section Core Messenger](../README.md)

## Status and boundary of current contract

The method, path, public JSON, and authorization follow the current contract from [`workspace_api.md`](../../../../workspace_api.md); bounded pagination and asynchronous visibility follow separately adopted target compatibility ADR.

![Sequence diagram](diagrams/post_message_star_action.svg)

The source that you can edit: [`post_message_star_action.puml`](diagrams/post_message_star_action.puml).

## The operation

**Method and way:** `POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke`

**Purpose:** To set the global status for the message in the current user's selected.

## A public request

Without a body. JSON.

## A successful public response

HTTP `200`:

```json
{
  "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "author_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "markdown",
    "content": "Привет, Workspace"
  },
  "user_uuid": "33333333-3333-3333-3333-333333333333",
  "read": true,
  "pinned": false,
  "starred": true,
  "is_own": false,
  "mentioned": false,
  "reactions": {},
  "reaction_users": {},
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
}
```

## Public errors

The bearer-token IAM and the project area are required. An incorrect UUID or request body is given by HTTP `400`; missing or unavailable in this area resource  `404`. Standard documented validation error body:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## The target boundary RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceUserMessage(models.ModelWithProject, orm.SQLStorableMixin):
    __tablename__ = "messenger_api_user_messages_v1"

    binding_uuid = properties.property(types.UUID(), id_property=True)
    uuid = properties.property(types.UUID(), read_only=True)
    stream_uuid = properties.property(types.UUID(), read_only=True)
    topic_uuid = properties.property(types.UUID(), read_only=True)
    author_uuid = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceMessageController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserMessage, convert_underscore=False, process_filters=True,
    )
```

Public `uuid` and route ID are equal to `MESSAGE_PLACEMENT.uuid`; canonical `MESSAGE.uuid` and `binding_uuid` are hidden. Placement unambiguously selects state, and action simultaneously checks active membership and generation.

## Synchronous transaction

1. Allow public placement UUID and current user access.
2. Set a unique value USER_MESSAGE_STATE.starred=true.
3. Only add immutable outbox event for a separate event when changing task
   scope `user-message` `(project_id,user_uuid,placement_uuid)`.

Affected state: USER_MESSAGE_STATE, access area and transactional outbox; container counters are never stored in message binding.

## Typed tasks and background performers

Tasks: separate immutable task `read_counters` for source outbox event; without coalescing.

Fenced owner exact scope `user-message` Read the current state and prepare
user event; topic lock is not used. Task lifecycle
includes retry/backoff, DLQ/reaper and idempotent effect on `outbox_event_uuid`.

## Public events and WebSocket

`message.updated` The dispatcher sends the event only when the user ' s

## Idempotence, races and time characteristics visible to the client

Setup of idmpotent state; current state changes immediately, and aggregates and events may lag shortly.

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [The section Core Messenger](../README.md)
