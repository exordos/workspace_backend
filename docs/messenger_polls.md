# Native Messenger polls

Apply the database migration and deploy poll readers and workers. Migration 0300 refuses downgrade while published polls exist, to avoid losing their votes.

Send a poll through the ordinary message endpoint. UUIDs are stable within the question or option list; array order is the display order.

```json
{
  "kind": "poll",
  "questions": [{
    "uuid": "00000000-0000-4000-8000-000000000001",
    "text": "Where should we meet?",
    "options": [
      {"uuid": "00000000-0000-4000-8000-000000000002", "text": "Office"},
      {"uuid": "00000000-0000-4000-8000-000000000003", "text": "Online"}
    ]
  }],
  "multiple_answers": false,
  "is_anonymous": true,
  "allow_revote": false,
  "results_visibility": "after_vote"
}
```

A poll needs at least one question and two options per question. The maxima are 20 questions and 20 options per question; changing these constants does not invalidate published polls. The required poll-level `multiple_answers` applies to every question, while selections remain independent per question. A question may include `correct_option_uuids` drawn from its options. Readers always receive these IDs. Optional creation fields are `closes_at` (a future timestamp with a time zone, default null) and `allow_adding_options` (default false). The server supplies `closed_at`.

Use the public message UUID for the following endpoints. All four POST actions return the current message, including the caller's poll state.

| Endpoint relative to `messages/{uuid}/` | Body or query |
| --- | --- |
| `POST actions/vote/invoke` | `{question_uuid, option_uuid, selected}`. Set or remove one selection. Repeating its current value makes no change. |
| `POST actions/close_poll/invoke` | `{}`. Close permanently. |
| `POST actions/update_settings/invoke` | `{settings: {closes_at, allow_adding_options}}`. Save both fields together; null removes a future deadline. |
| `POST actions/add_poll_option/invoke` | `{question_uuid, option: {uuid, text}}`. Append without voting. Retrying the same UUID and text succeeds even after closing. |
| `GET poll_voters/` | `question_uuid`, `option_uuid`; optional `page_limit` (default 50, maximum 100) and `page_marker`. Returns objects containing only `uuid`; use `X-Pagination-Marker` for the next page. Anonymous polls forbid this lookup for everyone. |

Only a current participant can vote or append. The author and current stream owners, administrators and moderators can manage the poll. Without revoting, a single choice cannot be replaced and existing selections cannot be removed; a question in a multiple-choice poll still accepts additional selections. Questions are independent. Once the deadline passes or a manager closes the poll, no further changes are accepted. Ordinary message edits cannot change polls or convert another message into a poll. Forwarded results are Markdown snapshots.

Message reads include `poll_state` (null for other kinds). It contains `revision`, `is_closed`, distinct `voter_count`, ordered `questions` with per-question `voter_count` and `option_counts` keyed by option UUID, and the reader's `my_votes` as `{question_uuid, option_uuids}` entries. Missing votes produce zero counts and an empty own-answer list. Counts are available to the API regardless of `results_visibility` (`always` or `after_vote`); the latter is a presentation rule.

## Realtime ordering

A vote, manual close, settings change or appended option updates the existing message through `message.updated`. A vote does not create unread activity. Each reader receives their own `my_votes`; the voter is not identified in the update. Apply the poll payload and state together: a lower `poll_state.revision` is stale for both, but does not determine the ordering of read flags or reactions. Crossing `closes_at` does not change the revision or require a worker. At the same revision, an older snapshot must not reopen an expired poll.

## Storage and sizing

Votes belong to the canonical message and the project user. Deleting either cascades to its votes; leaving a stream alone preserves votes. A real vote advances the poll revision without changing the message content, timestamp or unread state. Poll metadata edits use immutable content pages. No cron job is required for deadlines.

For a local PostgreSQL 17 sizing check, 25 polls had 1,000 voters each, four questions and two selections per voter per question (200,000 rows). On warm data, the median aggregate snapshot query took 3.34 ms for one reader and 4.24 ms for a batch of 128; personalizing the latter in Python took 0.48 ms. A single uncontended mutation including its response snapshot held the poll lock for 4.06 ms median and 4.84 ms p95 (24 updates). These measurements describe this fixture, not production latency. The project-user index reduced a lookup for account cleanup from 2,724 shared blocks to six.
