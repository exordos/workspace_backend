# Native encrypted Workspace push

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

## Install a push transport

Install Workspace 3.2.1 or later and a local Exordos Notification. Choose one
Workspace push Element. Both Elements own the same Workspace event type and
sender services, so switch between them by uninstalling the current Element
before installing the replacement.

| Element | Delivery |
| --- | --- |
| `workspace_direct_firebase_push` | Local Notification sends to Firebase. |
| `workspace_mesh_firebase_push` | Local Notification forwards to a separate Notification that sends to Firebase. |

Both Elements use the local Notification API at `/api/notification/v1/events/`.
Select `workspace_mobile_push_notification_host` in Core Values Store and enter
the trusted local Notification ingress hostname, without a scheme, port or path.
Core uses HTTPS from Workspace to that ingress. The default ingress may connect
to its Notification node over HTTP on the installation network.

The Workspace sender receives a rotating Core IAM token for the local
Notification. Only that token is written to the Workspace node. Both Elements
use the same backend and Android event format.

### Direct delivery

Install `workspace_direct_firebase_push` with a Firebase-capable Notification
build. The Workspace element creates the Firebase provider. Set the Core Secret
`notification-firebase-service-account` to the Firebase service-account JSON
for the Android app's project. Core writes the credential to
`/etc/exordos_notification/firebase-service-account.json` on the local
Notification node as `root:root` with mode `0600`.

### Mesh delivery

Install a Notification build that supports the `notification_relay` provider,
then install `workspace_mesh_firebase_push`. On the receiving installation,
create a dedicated Firebase event type, default Android Firebase template and
IAM identity for this forwarding installation. Grant that identity create and
read permissions for its dedicated event type. Use a separate Firebase
provider on the receiver and provision the Firebase project credentials there.

Set the local Core Secret `workspace-mesh-upstream` to the receiving
Notification connection JSON. It contains `notification_endpoint`,
`iam_endpoint`, `event_type_uuid`, `project_uuid`, and an `access_token`
or `refresh_token` issued by the receiving Core. Use distinct local and
receiving Notification installations. Core writes this secret to
`/etc/exordos_notification/workspace-mesh-upstream.json` on the local
Notification node with mode `0600`. The receiving Notification holds the
Firebase service-account credential.

The local Notification forwards the encrypted payload and polls the receiving
event's status. A permanent `registration_token_inactive` response reaches
Workspace for the original device. A pending upstream event remains pending
locally and retains its original UUID across retries.

For either Element, reserve `secret.secret.read` and `config.config.read` in the
system project for trusted operators. When the generated credential file
changes, Core restarts the Notification delivery worker.

### Switch from an existing push Element

Allow pending deliveries to finish. Record the selected
`workspace_mobile_push_organization_uuid` and local Notification host before
uninstalling the old `workspace_mobile_push` Element. Install one of the new
Elements and set these values again before enabling delivery. Preserve the
organization UUID so the app continues to match encrypted events to its
existing connection. Keep the Workspace database and its device registrations.
Provision the chosen transport's credential before testing delivery.

An `ACTIVE` Notification event means Firebase accepted the send. Verify
receipt, decryption, notification display, and navigation on a physical device.

### Add iOS delivery

Keep the existing direct or mesh Firebase connector active for the common
push handler, sender, installation UUID and HTTPS endpoint. With APNs-capable
Workspace and Notification builds, install `workspace_apns_push`. This
element configures the Workspace app's APNs provider on the local Notification
and adds a separate event type, template and scoped Core IAM token. iOS delivery goes directly from the local Notification to
APNs in both topologies. Android still uses its selected connector.

Set the `notification-apns-credentials` value in Core Secrets. Use a JSON
object keyed by `sandbox`, `production`, or both. Each configured environment
needs its Apple `team_id`, `key_id`, and PEM `private_key`. The element places
the credential on the local Notification node and restarts its delivery worker
when the secret changes. The iOS app uses the same
device registration endpoint and HPKE wire format. It registers platform
`ios` with transport `apns_sandbox` for a development-signed app or
`apns` for distribution. The token is the lowercase hexadecimal APNs
address. Discovery uses `apns_push_notifications_enabled` from server settings.
For APNs, the delivery snapshot includes the environment and registration
timestamp, so a delayed invalidation cannot remove a fresh registration.

Verify a normal alert and an authenticated tap-through on the target iPhone.
Test read-state updates while the app is active. iOS schedules those updates
according to its background delivery policies.

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
