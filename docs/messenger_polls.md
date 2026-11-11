# Native Messenger polls

Apply the database migration and deploy poll readers and workers. Migration 0301 refuses downgrade while published polls exist, to avoid losing their votes.

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

To retry a send after a lost response, submit the same message UUID and creation payload to the original stream and topic. The server compares the request with the first content revision and returns the current message. This also works if the poll has since closed or the creation limits have changed.

Use the public message UUID for the following endpoints. All four POST actions return the current message, including the caller's poll state.

| Endpoint relative to `messages/{uuid}/` | Body or query |
| --- | --- |
| `POST actions/vote/invoke` | `{question_uuid, option_uuid, selected}`. Set or remove one selection. Repeating its current value makes no change. |
| `POST actions/close_poll/invoke` | `{}`. Close permanently. |
| `POST actions/update_settings/invoke` | `{settings: {closes_at, allow_adding_options}}`. Save both fields together; null removes a future deadline. |
| `POST actions/add_poll_option/invoke` | `{question_uuid, option: {uuid, text}}`. Append without voting. Retrying the same UUID and text succeeds even after closing. |
| `GET poll_voters/` | `question_uuid`, `option_uuid`; optional `page_limit` (default 50, range 1–100) and `page_marker`. Returns objects containing only `uuid`; use `X-Pagination-Marker` for the next page. Anonymous polls forbid this lookup for everyone. |

Only a current participant can vote or append. The author and current stream owners, administrators and moderators can manage the poll. Without revoting, a single choice cannot be replaced and existing selections cannot be removed; a question in a multiple-choice poll still accepts additional selections. Questions are independent. Once the deadline passes or a manager closes the poll, no further changes are accepted. Ordinary message edits cannot change polls or convert another message into a poll. Forwarded results are Markdown snapshots.

Message reads include `poll_state` (null for other kinds). It contains `revision`, `is_closed`, distinct `voter_count`, ordered `questions` with per-question `voter_count` and `option_counts` keyed by option UUID, and the reader's `my_votes` as `{question_uuid, option_uuids}` entries. Missing votes produce zero counts and an empty own-answer list. Counts are available to the API regardless of `results_visibility` (`always` or `after_vote`); clients control when they display those counts.

Each question can also include `option_voter_previews`, a map from option UUID to up to 20 voter UUIDs in ascending UUID order. Empty options have no entry. Non-anonymous polls include previews for visible results: when results are always visible, the poll is closed, or the reader has voted on that question. Anonymous polls and questions with no eligible preview omit this field. A preview whose length matches `option_counts` for that option is complete. Otherwise use its last UUID as the exclusive `page_marker` for `poll_voters/` to continue the list.

## Zulip interoperability

Unencrypted native and Zulip conversations accept poll payloads. A poll uses a
native interactive Zulip widget when it has one question, distinct single-line
options, public results, multiple answers, revoting and option additions enabled,
no anonymous voting, no correct-answer keys, and no closing deadline. Leading or
trailing whitespace and mention commands also select the preview path, so the
bridge never silently changes the question or sends unexpected notifications.

Compatible polls synchronize creation, selections, withdrawn votes, added
options and question edits from Zulip. Workspace clicks use the connected
account of the voter; a user without an active matching account cannot vote as
someone else. Updates reuse the ordinary durable inbox and outgoing intent
queue. Repeated submessages and retries do not duplicate votes. An incoming
provider snapshot preserves local selections whose outgoing writes are pending.
Provider widgets outside the native payload limits remain readable snapshots.

Zulip widgets cannot close or enforce a different voting policy. Those settings
are fixed for a compatible poll in a Zulip conversation; the API returns 409
rather than accepting a change that the other client would ignore. Create an
extended poll when a deadline, anonymity or another restriction is needed.

Extended Workspace polls stay interactive in Workspace. Zulip receives a
read-only preview with every question and option, an explicit partial-support
notice and a **View and vote in Workspace** link to that exact placement. The
preview does not export personal selections or voter identities. A provider
echo cannot replace the authoritative poll with its Markdown preview. Text is
literal, so a preview cannot notify mentioned people; an oversized preview is
rejected instead of being silently truncated.

Set `workspace_url` in the connected Zulip account's settings to the public
Workspace origin, for example `https://workspace.example.test`. This deployment
setting is required for extended polls: workers must not guess another server's
URL. The link includes the project, stream, topic and public message UUID and
continues to require normal Workspace authentication and access permissions.

## Realtime ordering

A vote, manual close, settings change or appended option updates the existing message through `message.updated`. A vote does not create unread activity. Each reader receives their own `my_votes` and eligible previews; the event does not identify the actor separately. Apply the poll payload and state together: a lower `poll_state.revision` is stale for both, but does not determine the ordering of read flags or reactions. Crossing `closes_at` does not change the revision or require a worker. At the same revision, an older snapshot must not reopen an expired poll.

## Storage and sizing

The first release computes poll totals from the vote rows. The history query selects the page keys first and aggregates only its polls. A poll action reads the totals for its response after applying the action. Reading the content, revision, counts, personal choices and bounded voter previews in one SQL statement keeps them consistent during concurrent votes or account deletion. A materialized total would have to track both paths in the same transaction.

The voter list has its own page-size contract. Omitting `page_limit` returns up to 50 UUIDs. Values from 1 through 100 are accepted, and zero or values above 100 return HTTP 400. A withdrawn vote can still serve as the exclusive UUID boundary for the next page.

Votes belong to the canonical message and the project user. Deleting either cascades to its votes; leaving a stream alone preserves votes. A real vote advances the poll revision without changing the message content, timestamp or unread state. Poll metadata edits use immutable content pages. No cron job is required for deadlines.

For a local PostgreSQL 17 sizing check, 25 polls had 1,000 voters each, four questions and two selections per voter per question (200,000 rows). On warm data, the median aggregate snapshot query took 3.34 ms for one reader and 4.24 ms for a batch of 128; personalizing the latter in Python took 0.48 ms. A single uncontended mutation including its response snapshot held the poll lock for 4.06 ms median and 4.84 ms p95 (24 updates). These measurements describe this fixture, not production latency. The project-user index reduced a lookup for account cleanup from 2,724 shared blocks to six.

## Upgrading a privately extended legacy schema

A historical private schema can contain `messenger_topics_redirect_fkey`, whose
composite `ON DELETE SET NULL` also clears the non-null `project_id`. Physical
deletion during migration 0294 then fails if a surviving topic points to a
soft-deleted target. This column and constraint are absent from the current
standard schema.

After taking a database backup, an operator can run
[`tools/repair_legacy_topic_redirect.sql`](../tools/repair_legacy_topic_redirect.sql)
against that selected database, then rerun the normal migration command. The
opt-in repair preserves project IDs and clears only the nullable redirect when
its target is deleted. It is idempotent, does nothing when the extension is
absent, and refuses an unexpected constraint definition. It does not mark any
migration applied or bypass normal migration execution.

History restoration reconciles compatible Zulip poll widgets through the durable inbox after each imported page commits. Replaying a page preserves an existing interactive Workspace poll and its current votes until that refresh completes. Extended Workspace polls retain their original payload when the provider preview is read back.

A Zulip-backed poll cannot switch from a text preview to an interactive widget by changing its settings: Zulip initializes widgets only when a message is created. Such a transition returns 409 without changing the poll; create a new compatible poll instead. Other settings changes that keep an extended poll in preview mode remain supported by the API.
