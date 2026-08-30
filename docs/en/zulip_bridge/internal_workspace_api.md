# The total internal Workspace API for Zulip Bridge

Status: **proposal; first Provider Data API v2 wire-part is recorded**.

[← The main index of the documentation](../index.md) · [The index Zulip Bridge](README.md) · [Matrix of events](event_coverage.md) · [A look at the architecture](architecture_overview.md)

Both Bridge processes call one internal variant of the usual Workspace API.
It's a private service-to-service boundary over the same application services and
RestAlchemy transaction rules, It's a set of functions that create target canonical entities.
is not a new public client API and does not give Bridge direct access to
to the tables.

The current closed Provider API is described in
[`workspace_provider_api_v1.yaml`](../../workspace_provider_api_v1.yaml), And his
control/file security profile — - What ?
[`zulip_bridge_control_api_v1.yaml`](../../zulip_bridge_control_api_v1.yaml) and
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml). Target
The Commission is obliged to reuse this already sold realm-bound mTLS authentication.
The first implementation uses exact routes and wire format from
[`workspace_provider_api_v2.yaml`](../../workspace_provider_api_v2.yaml), and
The scope/identity/idempotency decisions are recorded in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).
Alternative authentication mechanism not being designed.

## Current S2S authentication  mandatory target boundary

Zulip Bridge It uses the existing separate private process/listener
`workspace-external-bridge-api`, not public Workspace nginx and not browser IAM
token. TLS 1.2+ is completed in the backend process; a normal query must present
client certificate, Signed realm control CA. HTTP forwarding header,
bearer token or the body field is not a source service identity.

Certificate contains exactly one URI SAN in the current format:

```text
https://schemas.genesis-corporation.ru/workspace/external-bridge/v1/realms/{realm_uuid}/providers/{provider_kind}/instances/{bridge_instance_uuid}/generations/{identity_generation}
```

Workspace He's pulling it out. `realm_uuid`, `provider_kind`,
`bridge_instance_uuid` and positive `identity_generation`, checks current
certificate fingerprint, active generation and backend state on each request,
including reused TLS connection. Certificate identity does not contain account or
project: server-side desired assignments And transaction-time checks narrow it down to
I 'm not allowed to . external account/chat/project.

Lifecycle Re-used without new credential protocol:

1. Platform It gives out a separate one-time enrollment secret on Bridge installation
   and generation through a secure Core-managed config.
   verifier; token value is not constant service credential.
2. Bridge Obtains realm CA through existing HMAC-authenticated bootstrap,
   generates the private key locally and sends CSR to `/v1/enrollments` with
   `X-Workspace-Enrollment-Token`. Successful release atomically closes generation;
   The same `request_uuid` and CSR repeats are idempotent, the changed replay is rejected.
3. Client leaf lives `30 days`, renewal starts `7 days` before expiry and
   is authenticated with an existing mTLS certificate./CSRIt 's being created .
   on the Bridge; old and new leaf are allowed at the same time no more `24 hours`.
4. Suspend It's a very simple way to get the request to cancel immediately. identity generation;
   certificate The loss of/expiry requires
   operator-controlled enrollment-secret rotation, No , not really . shared long-lived token.

Private key It 's just a matter of persistent Bridge disk. Backend PKI/enrollment
state stored in root-owned mode-`0700` dedicated store, separate sensitive
files are written as mode `0600`; raw enrollment token, verifier, client private key and
credential payload are prohibited in logs/errors. Account lease/fencing generation
remains a separate mutable authorization/ownership check: valid mTLS
certificate without active matching account assignment/lease does not allow command.

Failure boundary already defined: certificate, rejected TLS stack, may not
get HTTP response; missing/not current application identity gives
`401`; current instance state or assignment prohibits request through `403`;
invalid cross-scope command The proposal doesn't add.
new auth error shape in public Workspace API.

This is the mechanism chosen because it already serves the same long-lived
External Bridge process And all three. current private resource groups: control,
Provider data and files. Public IAM bearer refers to user/browser request;
The one-time enrollment header only returns the first one . certificate; HPKE credential
envelope and single-object file capability protect payload/object, but not
They're not alternatives. mTLS.

## Service identity and server-owned scope

After mTLS authentication, Workspace receives unchanged service context:

- certificate-bound `realm_uuid`, `bridge_instance_uuid`, provider kind `zulip`
  and `identity_generation`;
- Separately checked whole-account lease/fencing generation;
- Permitted external account/assignment generations;
- realm/project mapping, The one that keeps Workspace;
- Allowable set logical commands;
- the current provider policy, suspension/revocation and capability set.

Bridge transmits provider object/event identity and payload, but not authoritative
`project_id`, `source`, Workspace `user_uuid`, If so, you can use the following functions:
fields need wire envelope for tracing, Workspace compares them with server-owned
mapping and rejects the discrepancy; the client value never determines
tenant or author.

For each command Workspace inside the request transaction rechecks:

1. mTLS service identity active, certificate/identity generation The current,
   instance No , not really . suspended/revoked;
2. external account I 'm assigned to this . bridge/provider, active lease generation
   matches and provider policy allows operation;
3. provider object It belongs to the authorized account/chat scope;
4. server-owned project/stream/topic/user mappings exist and have the same
   tenant identity;
5. mutation Allowed capability and not crossed project boundary.

Composite tenant FK and `UNIQUE(project_id, ...)` remain the last physical
Service preflight does not replace transaction-time authorization.

## Two stable identities

`provider_object_key` and `provider_event_key` solve different problems.

| Key | Purpose | Obligatory property |
| --- | --- | --- |
| `provider_object_key` | Find one logical entity Zulip at create/update/delete and after restart | The same for realtime/history and stable within fresh import |
| `provider_event_key` | De-duplicate one provider mutation/delivery and output one immutable outbox event | One source event/version gives one key, retry does not change it |

The semantic composition identity:

| Kind | Provider object identity |
| --- | --- |
| user | verified realm UUID + typed `provider_user_id` |
| stream/chat | verified realm UUID + typed channel/conversation identity |
| topic | Workspace-owned durable mapping `(realm,channel,current name/alias history)` → stable canonical `TOPIC.uuid` |
| message | verified realm UUID + typed numeric `provider_message_id`; importing account not included in canonical identity |
| reaction | canonical provider message identity + actor provider user identity + exact `emoji_name` |
| membership | provider stream/chat identity + provider user identity |
| file/attachment | `(verified realm UUID, typed attachment_id)`; canonical file one, normalized message↔file links are separate |

For bidirectional commands the envelope also contains `origin` and
`causation_uuid`/Workspace provider operation UUID. Outbound Workspace
operation first durable connects causation to provider object/version, and
The returned Zulip event validates this operation without generating a new one
If the provider does not return client UUID, the server uses
durable operation receipt + provider object key + version/state; timestamp No , not really .
is evidence of echo. direction/source-of-truth matrix:
[`event_coverage.md`](event_coverage.md).

Numeric provider UUIDv5 uses exact algorithm:
`UUIDv5(namespace=verified_realm_uuid,
name="<entity_type>:<decimal_provider_id>")`. Permitted lowercase ASCII
types: `user`, `channel`, `message`, `attachment`. Provider ID It 's serialized as
unsigned shortest base-10 ASCII (`0` Or digits without leading zeros, sign,
whitespace/locale formatting); name bytes — exact ASCII/UTF-8 without NUL/BOM/
newline/additional fields. Project/account UUID are not namespace. Exact
keys for events/direct conversations are defined by solutions `3A/5A` in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

The old Workspace UUID of the previous import is not included in the key. fresh import
creates a new canonical row, and repeats the same operation inside this import
It returns/updates it through provider mapping. message create Workspace
itself assigns internal `MESSAGE.uuid` and deterministically gets public
placement UUID from canonical topic/message.

## Logical command directory

The names below describe semantic command types, not assert HTTP route names.

| Logical command | Primary write Workspace | Idempotency/object rule |
| --- | --- | --- |
| `identity.claim` / `user.ensure_external` | Verified account claim existing identity or create/reuse unmanaged external user; email only candidate, not proof | realm+user ID; conflicting verified owner fail-closed |
| `user.mapping.refresh` / `user.lifecycle.update` | Existing managed/unmanaged ordinary-user mapping: supported name/avatar/role/custom value/active state; email - It 's been removed . | provider user key + field/version/event key |
| `bot.create` / `bot.deactivate` | Special Workspace bot/external user; I 'm just Zulip-origin lifecycle | provider bot user key + event key/version; metadata update unsupported |
| `stream.create_from_provider` | Canonical `STREAM` + provider mapping only from Zulip `stream/create` | provider channel key + event key; native Workspace stream create does not call this command |
| `stream.update` / `stream.delete` | Pass the mapped provider change to Workspace domain service; it selects archive/history/bindings/visibility and writes outbox | provider chat key + event key/version; Bridge does not apply policy |
| `topic.resolve` / `topic.rename` | Workspace-owned durable mapping with alias history; mandatory `TOPIC` under immutable stream/project owner | realm+channel+current/old topic name; whole rename Keep it UUID |
| `membership.upsert` / `membership.revoke` | Passing membership fact; Workspace on stream settings changes persistent binding/generation, historical visibility and message bindings | provider stream+user key + event key/version; composition change It doesn't. stream |
| `message.create` | `MESSAGE` + `MESSAGE_PLACEMENT` + author binding/state + outbox | provider message key + create event key |
| `message.update` | canonical content/source/provider/delivery version + outbox | same provider message key + update event key/version |
| `message.move` | Resolve one canonical `MESSAGE`; delete source placement and create target topic placement | provider message/version + target topic + event key; target placement has new UUIDv5, old URL `404` |
| `message.delete` | provider tombstone/current delete semantics + outbox | same provider message key + delete event key/version |
| `message_flag.update` | Placement-scoped `USER_MESSAGE_STATE.read_at`/`starred` | provider message+user+flag+op+event key |
| `reaction.upsert` / `reaction.delete` | one canonical-message-global reaction fact + outbox | message+actor+`emoji_name` + event key |
| `file.allocate` / `file.finalize` | bounded single-object lifecycle and canonical file metadata | realm+typed `attachment_id`; repeated accounts/retries reuse row |
| `attachment.upsert` / `attachment.delete` | normalized message↔file relation + outbox | message provider key + realm/attachment key |
| `presence.publish` / `typing.publish` | Ephemeral scoped relay with access check and TTL; no canonical message write | origin+user+scope/state+short-lived causation key |
| `user_status.update` | Persistent mapped `status_text`/emoji state + outbox | provider user+status version/event key |
| `account.lease.*` / `account.bootstrap.*` | Whole-account lease/fencing, queue boundary and bootstrap generation | account UUID + monotonic generation |
| `history.root.*` / `history.stream_task.*` | Root discovery and immutable per-stream range task lifecycle | account+boundary+selection/range+stream; no message checkpoint v1 |

Commands not opening generic operation write any model». Unknown kind,
unmapped tenant, stale service generation, unsupported capability Or attempted
Substitute project/user to cause a failure mutation.

Names in the directory  logical proposal types, not public paths.
Unverified email claim-to be managed user, not allowed Workspace stream
create create Zulip channel and do not convert unsupported event to generic
upsert. Import can only create unmanaged external user without session.

Bridge does not calculate Workspace domain policy before command: group/private member
change and channel archive/delete are transmitted reliably as provider facts.
Workspace transaction It decides historical access, bindings and visibility.

## The boundary of the outgoing provider operations

For the Workspace-origin mutation from bidirectional coverage primary transaction
adds an immutable outbox event.
Without loss , it can run durable provider operation with unique source outbox
event UUID, server-owned account/object mapping, `origin=workspace`,
`causation_uuid` and expected version/state. Realtime Connector gets
operation across this boundary, calls Zulip and returns durable
receipt/confirmation. Exact queue/HTTP transport, derivation mechanism and ack
schema remain OPEN #1; the application does not publish user token and does not use
public WebSocket event How did you do that? transport.

Direction guard is a server: for example, native Workspace stream create
does not create an outbound channel operation. Own Zulip queue echo is allowed
on receipt/object/version and completes causation, but does not pass repeatedly as
Provider call retry keeps the same operation identity.

## Transaction boundary The message

`message.create` It 's atomically executed .:

1. Lock/dedupe realm-scoped `provider_object_key` and `provider_event_key` under
   active account lease generation.
2. If the event is already committed, return the same semantic result without a new mutation.
3. Allow server-owned author/stream/topic/project mappings.
4. Create or restore one canonical `MESSAGE` by provider key.
5. Create one mandatory `MESSAGE_PLACEMENT`; authoritative uniqueness —
   `(project_id,message_uuid,stream_uuid,topic_uuid)`.
6. Get public placement UUID as
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`.
7. Create author `USER_MESSAGE_BINDING` and `USER_MESSAGE_STATE` with current
   membership generation.
8. Write both an immutable outbox event and a committed idempotency receipt in the same folder DB
   transaction.
9. Commit Or rollback all the lines together.

Bridge The recipient doesn't expect a fan-out.WorkspaceThe workers are pulling out . bindings/states
receivers, snapshots/counters and durable ready events through a common one-event →
one-task protocol. Details of the canonical task types are in
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#task_kinds-и-routing).

## Update/delete ordering

For one provider object Workspace compares provider version/sequence,
If the source provides it:

- repeat the same version and payload — idempotent success;
- older version  stale no-op with new state;
- New version  one mutation + one outbox event;
- identical to the conflicting payload/version  terminal conflict for
  DLQ/reconciliation, Not really . silent overwrite.

Update/delete, The previously created overlap/newest-first range, do not create
synthetic `MESSAGE`. Workspace retains durable deferred dependency or
Returns retryable missing-base outcome. Exact wire coding outcome
remains OPEN, but the durable dependency belongs to Workspace, not local Bridge DB.

## Reactions

Public action addresses placement UUID for access check, but import command
Finds canonical message through provider message mapping. Source of truth — raw
fact With a key .
`(project_id,canonical_message_uuid,user_uuid,emoji_name)`. Realtime/history
retry Message-scoped Workspace worker materializes
`reactions`/`reaction_users` In all placements; Bridge snapshots not writing.

## Files and attachments

Bridge It doesn't get bucket-wide credentials and it doesn't write storage metadata.
authorization Workspace It gives out single-object transfer capability, checks
size/hash and fixes the finalize/attachment relationship.
The borders are in
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml).

Target Must keep the properties:

- One bounded object on allocation;
- finalize and attachment link are idempotent;
- bytes commit does not make metadata visible until Workspace transaction;
- retry It doesn 't create a second one . blob/row/link;
- delete does not delete physical object when retained native reference;
- provider identity `(realm_uuid,attachment_id)` Reuse one file;
- physical object It 's only deleted after zero native/provider references.

## Semantic results and errors

Wire statuses Not yet selected, but the outcomes should be different.:

| Outcome | Meaning | The action Bridge |
| --- | --- | --- |
| applied | Primary mutation and outbox committed | Realtime receives event terminal; history continues current task |
| duplicate/no-op | The same provider event/state already committed | Terminal without repeating outbox/ready event |
| stale | The newer provider state is already fixed . | Terminal no-op + metric |
| deferred | Missing mapping/base dependency durable - What ? Workspace | Terminal for source unit; repair after dependency |
| retryable | Timeout/rate limit/temporary unavailable, commit Not proven | Repeat same key; realtime is not reading next event |
| permanent/terminal | Provider rejection or invalid scope/conflicting identity | `permanent_failed`/DLQ evidence; endless retry/silent skip It 's forbidden . |

If the answer is lost after commit, the repeat with the same event key must prove
commit and return duplicate/same result. retry
It 's forbidden ..

## Audit and privacy

Logs and traces contain certificate-bound bridge instance/generation, provider
kind, account/mapping UUID, object/event key digest, outcome and latency, but not
enrollment token/verifier, certificate private key, user token, API key, raw
credential or full private payload. Workspace audit remains tenant-scoped.

Provider mappings and latest hidden raw/converter metadata live with entity.
Completed history tasks and successful outbound operations are cleared through
`30 days`, permanent-failure operation/code/reason — through .`90 days`It 's ...
internal retention No new ones . public fields/actions.

The uncovered details of wire routes/transport and provider-key serialization are listed only in
[The index](README.md#единый-список-open-решений-zulip-bridge).

[← The main index of the documentation](../index.md) · [The index Zulip Bridge](README.md) · [Matrix of events](event_coverage.md) · [A look at the architecture](architecture_overview.md)
