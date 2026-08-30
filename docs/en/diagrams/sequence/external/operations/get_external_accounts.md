# List of external accounts

[← The main index of the documentation](../../../../index.md) ·
[Index of sequence diagrams](../../README.md) ·
[External integration and execution time section](../README.md)

`GET /api/workspace/v1/messenger/external_accounts/`

List the global external accounts for the realm that belong to the current user IAM.

![Sequence diagram](diagrams/get_external_accounts.svg)

[The source that you can edit PlantUML](diagrams/get_external_accounts.puml)

## The request

The request parameters are contracted:

- `status`
- `page_limit`
- `page_marker` (UUID Last accounted for)

Behavior `page_limit` in the current implementation: no parameter or `0` means
Unlimited sample; negative or non-integer value gives HTTP `400`;
Any positive value is used without a maximum and without a limit
Redesignating the answer to `ExternalResourceController` bypasses
The standard headings .`X-Pagination-*`Target policy: no`0` => `100`; `1..500`is accepted exactly; negative, incomplete and`>500` => HTTP `400`Unbounded mode is missing; the full export client goes until the next one is missing marker.

The body is missing. JSON.

## A Successful Answer

HTTP `200`:

```json
[
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
    "status": "live",
    "live_ready": true,
    "safe_error": null,
    "capabilities": {},
    "desired_generation": 7,
    "applied_generation": 7,
    "last_progress_at": "2026-07-17T12:00:00Z",
    "created_at": "2026-07-17T11:00:00Z",
    "updated_at": "2026-07-17T12:00:00Z",
    "revision": 7
  }
]
```

## The errors

| HTTP | Behaviour in public |
| --- | --- |
| `403` | No `workspace.external_account.read` permission or resource is outside authorized area. |
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

`owner_user_uuid` It 's hidden . It 's public .`settings.default_project_id`is a scalar .UUIDIn the target storage, indexed.`owner_user_uuid`It 's a user reference .WorkspaceI 'm with you .`ON DELETE CASCADE`, but the extracted one is indexed .`default_project_uuid`to the project register with`ON DELETE RESTRICT`The last one is still in the series .`settings`- Public announcements .RestAlchemyThey don 't use it .`relationships.relationship`For theJSONIn the form .UUIDBecause the relationship is serialized asURIAt the boundary of the physical scheme , every canonical non-polymorphic connection`*_uuid`is an indexed external key with a clearly selected reference action. Sanitizers hide the owner, credentials, raw provider ID, private certificate, internal address and raw protocol fields.

## Synchronous transaction

1. Authenticate the query and define the project/user domain IAM.
2. Check the path, request settings and required permissions.
3. Execute one indexed read with the canonical line or pre-materialized read surface area saved.
4. Serial only sanitized public fields.

A read transaction does not write an outbox domain entry, a typed projection task, a desired state command, or a ready public event. During the request it does not execute `COUNT`, `GROUP BY`, correlated subquery, fan-out binding, provider call, or cache fix.

## Background processing, events and consistency

Typed projection tasks: none.

No public event Workspace is created for this operation, so the separate controller WebSocket has nothing to deliver.

Consistency visible to the client: no additional delay; response is an authoritative recorded image.

## Idempotency and parallelism

UUID The account is created by the client at the time of creation; business uniqueness allows for one account `(owner_user_uuid, provider_kind)`..

Repeaters use stable business keys and the current state of the outbox. Each immutable outbox event creates a separate task with a unique `outbox_event_uuid`; the re-delivery of this task must be idempotent, coalescing is absent. Monopoly processing of the topic Messenger from new entries to old ones is only applied when the affected canonical placement actually refers to `(project_id, topic_uuid)`; admin/read provider operations do not create an artificial topic and are not included in this queue.

## The sources

- [`workspace_api.md`](../../../../workspace_api.md) — authoritative public routes, general JSON, page, events and contract WebSocket.
- [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md) — sanitized lifecycle of external resources, permissions and provider semantics.

[← The main index of the documentation](../../../../index.md) ·
[Index of sequence diagrams](../../README.md) ·
[External integration and execution time section](../README.md)
