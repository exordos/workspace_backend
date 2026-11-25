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

Create a CalDAV account by sending the following JSON to the external-accounts
collection. A user can connect several accounts in one Workspace project. The
worker discovers every calendar that supports events for each account.

```json
{
  "provider": "caldav",
  "settings": {
    "url": "https://calendar.example.org/remote.php/dav/",
    "username": "customer",
    "password": "application-password"
  }
}
```

Use the server's HTTPS CalDAV address. Redirects can stay on that HTTPS origin.
If the host redirects to a different origin, use the final CalDAV address.
Workspace stores the password encrypted and omits it from responses and events.
The synchronization worker changes `status` to `active` when discovery and the
first pull succeed. It synchronizes one-time events within a year on either side
of the current date.

Use the existing event routes to create, edit, or delete one-time events on a
writable CalDAV calendar. Workspace uses the provider's ETag so a concurrent
provider edit is kept and picked up on the next sync. Provider calendar
collections, recurring series, invitations, and moves involving a CalDAV
calendar are outside these operations.

For safe retries after an uncertain create result, supply a client-generated
event `uuid` on `POST /v1/calendar/events/` and reuse it with the same calendar
and payload. CalDAV creation uses that UUID to identify the remote event and
recover the same resource on retry.

To disconnect, send `{"disabled": true}` to the account's update route. This
removes the stored password and stops the next sync. Imported resources stay
linked to the disabled account. Accounts with imported resources can be
disconnected through this update, which preserves their links. To reconnect,
update `disabled` to `false` and
include a new `settings.password`. To use a different endpoint or login, create
another account.
