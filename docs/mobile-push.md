# Native encrypted Android push

Workspace registers one FCM token and one HPKE public key per authenticated
user, project and device through `PUT /api/workspace/v1/push_devices/{uuid}`.
The private X25519 key never leaves the phone. Updating the same UUID rotates
the token or key; deleting it unregisters the device.

Topic counter changes create a minimal `WorkspaceTopicSignal` in the same
transaction as the ordinary projection. The independent
`workspace-push-handler` waits until counters and the WebSocket publication
agree, reads the current notification policy and first unread preview, and
creates one encrypted SDK event per current Android device. No device lookup,
encryption or network request runs in the counter transaction.

The handler reads the topic, membership, published counters and preview within
one PostgreSQL statement snapshot. Pending counter or publication work skips
the preview lookup. The chosen message must still belong to that topic and be
accessible to that user before its UUID or contents are sent.

Each event contains one compact Android payload. The routing envelope and
preview are bound with HPKE associated data and the preview is encrypted with
`HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM`. Muted topics are suppressed;
mentions-only topics produce updates only for active unread changes. An
end-to-end encrypted chat gets a generic preview because its plaintext is not
available to Workspace.

The separately managed `workspace-push-sender` uses the SDK sender lifecycle
for delivery, retries and status polling. Before first delivery it rechecks the
membership and the exact device UUID, token and key snapshot. A permanent
`registration_token_inactive` result removes the registration through the
normal RestAlchemy model only if that same snapshot is still current. Terminal
events immediately clear the token and ciphertext and are deleted after one
hour.

When upgrading a development installation, the migration converts pending
legacy single-device events to the current format without changing their
UUID or ciphertext. Already terminal events and unsupported legacy batches
have their device data cleared and are not retried.

## Direct Notification topology

Install `workspace_mobile_push` after `workspace`, `exordos_notification` and
`notification_firebase`. The producer element owns the fixed
`notification_topic_update` event type, its Firebase Android template, the
restricted IAM sender identity and the rotating token. It imports only the
shared Firebase provider from `notification_firebase` and sends directly to
the same installation's Notification API through its existing HTTPS ingress
at `/api/notification/v1/events/`. No relay Notification instance is needed.
This protects the connection from Workspace to the ingress; the ingress's
upstream connection to Notification is still HTTP.

The connector's Core Values Store variable
`workspace_mobile_push_notification_host` defaults to `exordos.com`. For a
different installation, including a test Realm, add and select a value for
that variable before sending push events. Enter only the hostname of that
installation's trusted Notification ingress, without a scheme, port, or path.
Use the complete Realm hostname, not its parent DNS zone. Core renders
`https://<host>/api/notification` into the managed Workspace configuration;
no DNS resource export, Realm-specific build, or HTTP fallback is needed.

The template passes `device_uuid`, `registration_token`, the encrypted
`payload` string and Android `priority` to the Firebase transport. Firebase
credentials remain only on the Notification node at
`/etc/exordos_notification/firebase-service-account.json` with mode `0600`.
The Workspace node receives only its platform-managed Notification bearer
token. Core renders it as `[mobile_push] notification_token` in the private
oslo.config drop-in `/etc/workspace/workspace.conf.d/91-mobile-push-token.conf`
with mode `0600`. When the token changes, the config hook restarts only
`workspace-push-sender` so it reads the new token at startup. The API and
the handler can still queue events when the sender has no token.

An `ACTIVE` Notification event proves only that FCM accepted the request. Full
acceptance must also verify receipt, HPKE decryption, notification display and
navigation on a physical device.

## Tests

Unit tests cover the public RFC 9180 vector, the Python/Android wire fixture,
the direct manifest contract and redaction. PostgreSQL integration tests cover
counter snapshots, policy, retries, membership fencing and stale-token cleanup:

```bash
WORKSPACE_TEST_DB_URL='<DEDICATED_TEST_POSTGRES_URL>' \
  .tox/develop/bin/python -m pytest \
  workspace/tests/integration/test_mobile_push.py \
  workspace/tests/integration/test_topic_push.py
```

The integration fixture recreates the test database's public schema. Never run
it against a retained installation database.
