# Workspace Calendar API

Calendar resources belong to the active IAM project and current user. The API
supports personal one-time events.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/api/workspace/v1/calendar/calendars/` | List or create calendars. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/calendar/calendars/{calendar_uuid}` | Read, rename or remove an empty calendar. |
| `GET`, `POST` | `/api/workspace/v1/calendar/events/` | List or create events. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/calendar/events/{event_uuid}` | Read, edit or delete an event. |
| `POST` | `/api/workspace/v1/calendar/events/{event_uuid}/actions/move/invoke` | Move to another owned calendar. |

An event's `payload` has `kind: event`, a `title`, optional `description` and
`location`, and required `starts_at` and `ends_at`. Both times use UTC with
a `Z` suffix. The client handles display time zones and all-day presentation.
For events that overlap a time window, send `starts_before` with the window end
and `ends_after` with its start. Both bounds are exclusive. The collection also
supports the normal pagination and `calendar_uuid` filter.

Events and calendars use the shared Workspace cursor and `calendar.event.*` and
`calendar.calendar.*` realtime kinds.

## External Accounts

Use `/api/workspace/v1/external_accounts/` for accounts linked to Mail, Calendar
or Messenger. `/api/workspace/v1/messenger/external_accounts/` is an alias.
The read-only `capabilities` array describes which sections can reference the
account. Connection and delivery status are separate from capabilities.
