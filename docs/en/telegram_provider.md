# Telegram external accounts

This provider uses a Telegram user account through MTProto. Configure the client application on the server, then authenticate from the external-account dialog with a QR code and, if enabled, the Telegram two-step verification password. Application credentials alone do not authorize a user account.

## Configuration

The independent `workspace-telegram` process reads the same configuration file as the API. An unset application ID disables the provider. The platform generates a separate encryption key; preserve it with the database backup. Existing encrypted sessions cannot be restored without the matching key.

```ini
[telegram]
api_id = 0
api_hash =
secret_encryption_key =
history_batch_size = 100
poll_interval = 1
```

Place application credentials in the operator-managed root-only `/etc/workspace/telegram.conf` override. The API and daemon read this after the generated main configuration. Keep the override in a separate platform configuration resource targeting the backend node, with its application hash supplied by a platform secret reference; element upgrades must not own or reset this credential. Use the platform secret store for credentials. Never place account sessions, login tokens, passwords, or application hashes in public external-account settings, messages, logs, or client caches. API authentication responses are owner and project scoped, never cached, and excluded from body logging even at DEBUG level.

## Account and authentication contract

Create an external account with `provider=telegram`. Public settings are empty: all accessible dialogs and their complete history are imported automatically. Legacy `history_depth`, `selection_mode`, and `peer_keys` inputs are discarded. The provider identity is the fixed Telegram hostname. The existing external-account route and status values remain available to older clients.

The owner invokes the following action using the existing external-account update permission:

```text
POST /v1/messenger/external_accounts/{uuid}/actions/telegram_auth/invoke
{"operation":"start"}
{"operation":"status"}
{"operation":"password","generation":"attempt-uuid","password":"example-only"}
{"operation":"cancel","generation":"attempt-uuid"}
```

A login attempt expires after five minutes. The QR token renews within that attempt. Closing the form cancels only its current generation; an older form cannot cancel a newer attempt. Passwords are encrypted, consumed once, and expire with the attempt. The daemon revokes an authorization completed after cancellation. Account deletion disables synchronization, detaches the owner's memberships, and requests Telegram logout before removing the root account and its dependent state.

## Synchronization and identity

Each account has one connection and a PostgreSQL advisory lock. The daemon runs direct/group differences, channel reconciliation, media replay, catalog and outgoing delivery as separate jobs. Private dialogs, basic groups and channels each run the same history loop independently, so a large channel file cannot postpone the first private or group history page. Their GetHistory retry deadline is shared and checked before every peer; account status remains syncing until all selected histories finish. Telegram and Zulip share COPY staging, set-based content/root/placement/binding merging, personal flag reconciliation, and counter scheduling. Fetched Telegram pages are persisted before merging; page removal and checkpoints commit with the canonical batch. Both importers yield to realtime and publication backlog. History does not emit one websocket event per imported message. Service-only pages still advance the checkpoint. History always reads everything Telegram makes accessible to that account. The catalog refreshes every 60 seconds. An unknown direct/group dialog in the common update difference wakes the same catalog job immediately, so its initial history does not wait for the periodic timer; known-dialog messages do not trigger another scan. Contacts, dialog counterparts and message authors become stable Telegram users in the Workspace directory. Verified IAM profiles remain authoritative.

Private dialogs and basic-group messages use account-scoped identities. Channel and supergroup message identities are shared within the project and provider. Telegram streams keep membership-restricted Workspace access. Personal folders contain direct dialogs; Telegram channels and basic groups appear in Channels independently of this access flag. Telegram streams are private Workspace streams; another user's source account must independently import the same channel before that user receives membership. Ordinary chats use the default topic `telegram`; forum replies retain their source topic. Zulip conversations without a source subject use `zulip`. The web client groups visible unencrypted direct streams only by the exact counterpart Workspace UUID, project and viewer. Each provider topic retains its physical stream UUID for delivery and authorization; native topics use the existing canonical native direct stream. Names, emails and usernames never merge identities. Periodic catalog reconciliation reuses the same stream writer to update an existing direct counterpart after a verified account link; it preserves the physical conversation and grants no membership to the newly linked user.

Reaction echoes reuse the canonical message/user/emoji key and preserve an existing Workspace reaction UUID; repeated source updates and subsequent removal do not create a second fact. The durable inbox fences source snapshots against newer updates. A source journal gap triggers an authoritative history rescan; covered page ranges reconcile deleted messages without deleting newer arrivals. Outgoing sends persist the random identifier before the remote write and use authoritative receipts. Early own-message echoes wait in the inbox until their receipt binds the original Workspace UUID. An uncertain duplicate identifier remains an explicit error rather than being correlated by message text.

Text, source timestamps, replies, individual photos/documents, edits, deletes, ordinary emoji reactions, and the owner's read acknowledgements use canonical Messenger entities. Read-only source chats reject sends. Secret Chats, calls, stories, payments, participant administration, custom emoji, and multi-attachment outgoing albums are outside the current implementation. Telegram may impose additional message, file, reaction, or rate limits; flood waits retain the durable checkpoint and retry deadline.

## Measurements and acceptance

The authentication status includes per-chat history start, first committed message, completion, imported message/file/byte counts, history-fetch time, merge time, and preparation time. Throughput is imported messages divided by elapsed wall time; report file volume and provider flood waits separately. These local counters do not prove delivery at Telegram.

Acceptance must record actual source and deployed artifacts, QR authorization, complete accessible chat scope, cold import, replay, restart recovery, realtime latency during backfill, attachment byte equality at both endpoints, and negative access checks. Verify native messaging and fresh Zulip traffic in both directions after the final restart. Keep synthetic/local timings separate from measured live Telegram results.

## Message conversion examples

[Open the offline Telegram ↔ Workspace comparison](../telegram_messages_preview.html). Its synthetic Telegram view illustrates MTProto entities rather than reproducing a Telegram client. Workspace uses its actual renderer and theme. The same JSON examples are asserted against production conversion in unit tests; the generator also checks rendered DOM semantics.

Run `node workspace/tests/fixtures/render_zulip_messages_preview.mjs --telegram` with local Workspace UI and Zulip source checkouts. `WORKSPACE_UI_SOURCE_DIR` and `ZULIP_SOURCE_DIR` override their paths. The common generator retains the existing Zulip page.

Telegram entities become escaped CommonMark: bold, italic, strike, spoilers, code, language-aware blocks, labeled links and block quotes. UTF-16 offsets preserve non-BMP text. Outgoing Workspace CommonMark is parsed into MTProto text/entities rather than Telethon's incompatible legacy Markdown. Underline currently retains plain text because Workspace has no underline syntax; this limitation is visible on the comparison page.

Workspace quote/message references become native Telegram reply headers when their target belongs to the same source dialog. Attachment and reply references are parsed as CommonMark links; literal examples inside code are retained as text. Unsupported Workspace-only links retain their readable label instead of an invalid Telegram URL.

## Realtime isolation and posting permissions

Direct/group differences, channel reconciliation, durable media replay, history, catalog refresh and outgoing delivery run as separate account jobs. A slow channel RPC or file download does not hold the direct-message loop. Raw channel updates prioritize that channel during catch-up. Media identities are committed to the durable inbox before advancing the difference cursor; replay refetches them and survives restart. Authentication revocation stops the account; ordinary history/catalog failures retain their checkpoint and a scoped retry deadline. Status diagnostics expose retry deadlines and error types without provider exception text.

The public stream capability `can_send` is computed per viewer from membership, posting policy, source permissions and the connected account. API writes are rejected before creating a canonical message or outgoing intent when sending is unavailable. Native `announcement_only` channels allow owner, administrator and moderator posting; only owners/administrators manage this policy and elevated roles. The existing `announce` creation-notification flag has separate semantics. Announcement-only creation is currently available for unencrypted native channels. Telegram broadcasts reflect the subscriber's actual posting rights.

Imported Telegram identities reuse external-account records without an authorization session, as Zulip imports do. A later verified login can claim an imported identity, but cannot take an identity already connected to another Workspace user. Mentions resolve only through the selected topic's provider and project, including stable imported UUID references after account linking. Names and email addresses are not identity matches.

## Telegram notification modes

Stream modes synchronize in both directions. `all_messages` explicitly unmutes the Telegram chat. `muted` and `mentions_only` both use Telegram's long mute; Workspace retains their distinct local notification behavior. An echo of the same mute value preserves the local mode. A later effective source change maps to `muted` or `all_messages`. Telegram has no exact equivalents for every Workspace mode. Topic `default`, `follow`, `unmute` and `mute` remain Workspace topic preferences and do not rewrite the whole Telegram chat.

Initial dialog settings and Telegram's users/groups/broadcast defaults are imported with inheritance. Pts-less notification updates are consumed separately. Pending local intents and observation timestamps prevent stale source snapshots from overwriting a newer local preference. Outgoing changes set only `mute_until`, preserving sound and preview settings. Acceptance changes must snapshot and restore the targeted source preferences.
