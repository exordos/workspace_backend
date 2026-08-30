# `POST /api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke`


Common target-invariant of reliability: each immutable outbox event outputs exactly one immutable typed task with unique `outbox_event_uuid`; coalescing is absent. Task stores the actual exact scope key, uses lease/fencing, retry/backoff, max attempts/DLQ, reaper and idempotent effect guard. Topic scope is applied only to placement/message-binding work; shared rows do not receive implicit fallback on topic.

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [Content and users Workspace](../README.md)

Status: the target specification of the operation, first developed in the documentation.
remains unchanged and is the normative [`workspace_api.md`](../../../../workspace_api.md).
This file describes the transaction and projection target boundaries; it doesn ' t
production code, migration SQL or new endpoint.

![Sequence diagram](diagrams/post_user_avatar_upload.svg)

[The source that you can edit PlantUML](diagrams/post_user_avatar_upload.puml)

## Purpose and public contract

Automatically download and select a user avatar of an authenticated user.

Authentication: Bearer IAM; `project_id` and current `user_uuid` tokens are taken from context IAM.

## Path and query settings

| Location | Name of the person | Type / rule |
| --- | --- | --- |
| The way | `user_uuid` | must match the UUID authenticated user |

The collection pagination, where it is provided, preserves the current contract `page_limit` and UUID
`page_marker` and returns `X-Pagination-Limit`, and
`X-Pagination-Marker` Only if there 's a next page ..

## The body of the query

This operation uses `multipart/form-data`, not the body. JSON.

Mandatory field of the form `file`: binary data PNG, JPEG, GIF or WebP, maximum 25 MiB.

## A Successful Answer

`201`

```json
{
  "uuid": "11111111-1111-1111-1111-111111111111",
  "username": "admin",
  "source": "iam",
  "status": "active",
  "status_emoji": "coffee",
  "status_text": "Focusing",
  "first_name": "Workspace",
  "last_name": "Administrator",
  "email": "admin@example.com",
  "avatar": "urn:image:f11353e0-712d-4b99-a716-5cdba848cc05",
  "last_ping_at": "2026-07-17T08:00:00Z",
  "created_at": "2026-07-01T08:00:00Z",
  "updated_at": "2026-07-17T08:01:00Z"
}
```



## Errors and authorization

Only own UUID is accepted. Missing file, unsupported declared MIME/signature, empty content, or size over 25 MiB return validation error.

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


class WorkspaceUser(models.ModelWithUUID, models.ModelWithTimestamp,
                    orm.SQLStorableMixin):
    __tablename__ = "messenger_users"
    username = properties.property(types.String(min_length=1, max_length=128), required=True)
    source = properties.property(types.Enum(["iam", "zulip"]), default="iam")
    status = properties.property(types.Enum(["active", "idle", "offline", "do_not_disturb"]))
    status_emoji = properties.property(types.AllowNone(types.String(max_length=64)))
    status_text = properties.property(types.AllowNone(types.String(max_length=256)))
    avatar = properties.property(types.String(max_length=2048), required=True)


class WorkspaceUserController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceUser,
        convert_underscore=False,
        process_filters=True,
    )
    # Narrow own-user IAM refresh and presence/avatar actions preserve the API.
```

Each public reference to an entity is declared a scalar UUID property RestAlchemy, not `relationship` (which would be serialized as URI). The corresponding physical column `*_uuid`  an indexed external key with an explicitly selected reference action. Therefore, public JSON keeps UUID unchanged.

`WorkspaceUser` — public UUID-like links of the provider remain scalar fields in the sanitized container of the provider; physical links  indexed FK. Identity fields belonging to IAM are read-only to browser queries.

## Synchronous path API

1. Check your own UUID, MIME, signature and size.
2. Save bytes and associated metadata of public ACL without UUID stream.
3. In one transaction, the database inserted metadata of the file, updated only `user.avatar` and added unchanged domain entries of the avatar/file in outbox.
4. Save the transaction and return the user.
5. When the link is updated, revoke/delete the replaced user avatar.

## Outbox, Typed tasks, worker and real-time work

Public record `user.updated` materializes after fixing the avatar link and the file metadata..

Separate immutable `delivery_snapshot_event` with exact user scope reads
The last canonical user and atomically creates ready records
`user.updated` with effect guard on `outbox_event_uuid`.
The dispatcher sends, repeats or plays them; the worker WebSocket-
He doesn 't have any connections ..

## Idempotence, keys and races

The canonical user line prevents the user from choosing an avatar in a broken way. The error before the transaction is fixed compensates for the newly saved bytes. Deleting the replaced data takes into account the links and allows repeat.

## The moment of visibility for the client

The current client immediately receives the updated canonical user. Other clients receive the full shot `user.updated` after the accepted delay of projection/dispatch.

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [Content and users Workspace](../README.md)
