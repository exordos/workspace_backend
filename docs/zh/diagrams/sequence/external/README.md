# Accounts and events

External accounts store an owner, a required provider, and generic settings.
Creating an account does not connect or synchronize it. Workspace provides
account CRUD; updates require the current `If-Match` revision.

Workspace entities retain native `source_name` and `source` fields.
Messages and attachments use ordinary membership permissions and file ACLs.

See the [current public API contract](../../../../public_workspace_api.yaml).

Native event operations:

- [Epoch](operations/get_epoch.md)
- [REST events](operations/get_events.md)
- [WebSocket events](operations/websocket_events.md)
