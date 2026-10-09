# Sticker interoperability with Zulip

## Objective and invariant

Workspace messages keep the canonical Markdown representation
`![sticker](urn:sticker:<uuid>)`. Zulip receives the original media bytes through
the existing upload pipeline and a normal attachment link with the label
`workspace-sticker:v1:<uuid>`. A verified marked attachment is restored to the
canonical sticker reference before submitting a Provider API event.

This does not import new stickers into the catalog. Ordinary images remain
ordinary images, even when their bytes match an existing sticker.

## Confirmed implementation boundaries

- Backend: `workspace/external_bridge_control/files.py`,
  `file_repository.py`, `service.py`, and runtime wiring in
  `workspace/cmd/external_bridge_api.py`.
- Bridge repository: `exordos/workspace_zulip_bridge`.
- Bridge outgoing conversion: `zulip_adapter.py`,
  `_convert_workspace_content`; it currently calls `export_file`, uploads
  using the Zulip client, and changes the destination with
  `link.with_destination`. It preserves the original image syntax flag.
- Bridge incoming conversion: `converter.py` calls the file resolver from
  `service.py`; `file_api.py` owns authenticated file API requests.
- Provider ingress already accepts canonical sticker Markdown. Stickers must
  not be added to ordinary WorkspaceFile cross-project reprojection: catalog
  references are global and do not require per-chat file records.

## Wire contracts

### Outgoing bytes

Extend existing `PUT /v1/file-transfers/outgoing/{transfer_uuid}` to accept
`urn:sticker:<uuid>`. Keep the existing assignment authorization, idempotency,
download transport, and response envelope. For stickers, `file_uuid` is the
catalog UUID, `name` is `<uuid>.<format>`, and size/hash/type describe the
original catalog media. Resolve only active, nonblocked stickers using the
current request session. The sticker URN is exact and never has a query suffix.
Preserve all ordinary attachment ACL checks.

No synthetic WorkspaceFile, extra upload endpoint, public bucket access, new
database table, or separate request transaction is needed. A catalog lookup
replaces the file-sidecar lookup only for sticker URNs. Existing expiring
single-object grants are reused. Issuing a new grant rechecks visibility;
previously issued grants retain their existing expiry semantics.

### Incoming catalog verification

Add a read-only private endpoint under the same authenticated bridge service:

`GET /v1/stickers/{uuid}?external_account_uuid=<uuid>&external_chat_uuid=<uuid>`

Required account/chat parameters identify a currently authorized assignment.
No outgoing operation or transfer is fabricated for history synchronization.

Response `200` contains exactly `uuid`, `sha256`, `size_bytes`, and
`content_type`. It never contains a storage key, credentials, or download URL.
Unavailable or unknown catalog entries return `404`; unauthorized assignments
return `403`. Invalid input uses the existing private API validation errors.
Transient service errors remain failures eligible for the existing retry flow.

### Markdown marker

The complete link label must be `workspace-sticker:v1:<uuid>`. Preserve the
existing bridge's outgoing `[...]` versus `![...]` structure; do not require a
new Zulip Markdown feature level. Only parsed attachment links are eligible;
plain text, code spans, and fenced code are not metadata.

The marker identifies the attachment type, not the message author or operation.
It is public and is not an authorization credential. The physical uploaded
filename does not need to contain the marker.

## Outgoing algorithm

1. Parse Markdown using the existing converter and recognize sticker URNs.
2. Authorize/download the original object using the extended file API.
3. Reuse the existing provider upload and operation/retry machinery.
4. Set the attachment label to the versioned marker and its destination to
   the returned Zulip upload path. Leave other message elements unchanged.
5. Keep the original Workspace payload intact.

## Incoming algorithm

1. Parse raw Zulip Markdown through the existing live/history conversion path.
2. For an upload link with a supported marker, read current scoped catalog
   metadata and compare the original downloaded bytes by SHA-256 and size.
   Never hash a thumbnail; provider MIME labels alone are not identity proof.
3. On a verified match, return `![sticker](urn:sticker:<uuid>)` without ordinary
   incoming file allocation or creating a WorkspaceFile record.
4. With no marker, a malformed/unknown version marker, or a verified mismatch,
   continue the existing ordinary image import using downloaded bytes. For a
   supported valid marker whose UUID is unavailable, render the generic
   `Sticker unavailable` placeholder and do not import an ordinary image;
   deleted, missing, and blocked states are intentionally indistinguishable.
5. Do not convert an authorization or transient network failure into a permanent
   ordinary-image decision; preserve the existing error/retry behavior.

Canonicalize the restored label to `sticker` so repeated synchronization does
not alternate labels. Marked copies in new Zulip messages can become stickers
after verification; they remain new messages, not echoes of the original.

## Echo, edits, and history

Normalize the attachment before existing canonical content comparisons and
Provider event submission. Continue using existing operation/provider identity
mapping for message deduplication; a marker must never be used as a message ID.

Retain real edits: text edits preserve matching stickers; removal removes the
element; replacement with different bytes or removal of the marker produces an
ordinary attachment. Do not ignore all updates to Workspace-origin messages.
History, second-account delivery, early events, and restart recovery must not
depend on an in-memory outgoing marker cache. Audit ambiguous-send reconciliation
as well as normal live events for use of the same normalization.

## Component responsibilities and dependencies

1. **Backend media and metadata:** scoped resolvers and private endpoints own
   authorization, catalog visibility, media access, and the stable response
   schema used by the bridge.
2. **Bridge conversion:** the file client and outgoing/incoming converters own
   marker handling, provider upload, byte verification, history, and
   reconciliation while preserving ordinary attachment behavior.
3. **Provider event processing:** updates and cross-project reprojection preserve
   canonical sticker URNs without requiring ordinary WorkspaceFile lookup.
4. **Contract verification:** backend and bridge tests cover the shared private
   API schema, authorization, PostgreSQL behavior, conversion, retry paths, and
   storage adapters. Manual Zulip acceptance remains separate evidence because
   unit fakes and S3 presign mocks do not prove client rendering or a complete
   provider roundtrip.

## Acceptance matrix

| Case | Expected result |
| --- | --- |
| Sticker sent from Workspace | Zulip media; Workspace sticker URN preserved |
| Own echo, duplicate or early event | No duplicate; same canonical sticker |
| Second account or history without send cache | Verified sticker restored |
| Same bytes without marker | Ordinary image |
| Marker with different original bytes | Ordinary image |
| Unknown version or malformed marker | Ordinary image |
| Supported marker with unavailable UUID | Generic `Sticker unavailable` placeholder; no ordinary image import |
| Assignment denied | Authorization failure, no catalog metadata disclosure |
| Temporary lookup/download failure | Existing failure/retry behavior |
| Mixed text, images and multiple stickers | Order and element types preserved |
| Code containing marker syntax | Literal code unchanged |
| Text edit, attachment replacement or deletion | Actual edit reflected |
| Sticker becomes blocked before renewed grant | New grant denied |
| Cross-project message move | Global sticker reference unchanged |

## Deployment and verification

Automated tests, real PostgreSQL checks, mocked provider/storage checks, and
manual Zulip acceptance provide different evidence and are reported separately.
Marker visibility in Zulip clients must be measured, not assumed to be hidden.

Deploy the backend first and the compatible bridge second. Keep sticker sends
disabled until the bridge is deployed. Enable the UI delete action only after
the backend DELETE API is deployed.

Release acceptance includes real Zulip rendering, network retry behavior, and
the complete mTLS/S3/provider roundtrip. Automated or mocked checks do not
replace these integration checks.
