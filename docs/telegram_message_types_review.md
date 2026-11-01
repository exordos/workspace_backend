# Workspace message roundtrip review

The [offline review page](telegram_message_types_review.html) starts with canonical Workspace payloads. Every case shows both Workspace → Zulip → Workspace and Workspace → Telegram → Workspace, including a second cycle. The 79 existing Zulip cases and 17 Telegram cases supply coverage; neither provider defines the Workspace model.

The page captures unchanged production converters. It renders source and returned Workspace content with the local UI renderer and outbound Zulip with its local client renderer. Telegram is a text/entity illustration. Exact Markdown equality and second-cycle stability are diagnostics, not semantic or delivery acceptance. The Telegram capture excludes runtime restoration of replies, media and identities; the Zulip capture uses synthetic mapping context. Their counts must not be treated as comparable readiness percentages.

Observed behavior and proposed changes are separate. The proposed contract preserves semantic structure through both directions and repeated cycles. Native provider features are preferred. A representation without a native equivalent needs a readable fallback and canonical data retained in Workspace. Restoring an unchanged linked message must be version-aware and must never overwrite provider edits. Missing information cannot be reconstructed from unrelated provider text. Nested quote diagrams are proposals, not an implemented reversible codec.

Rebuild with the development Python environment and local UI/Zulip checkouts:

```sh
.tox/develop/bin/python workspace/tests/fixtures/build_telegram_message_types_review.py
REVIEW_RENDER_OUTPUT=/tmp/workspace-roundtrip-rendered.json ZULIP_SOURCE_DIR=/path/to/zulip WORKSPACE_UI_SOURCE_DIR=/path/to/workspace_ui node workspace/tests/fixtures/render_zulip_messages_preview.mjs
REVIEW_RENDER_OUTPUT=/tmp/workspace-roundtrip-rendered.json WORKSPACE_UI_SOURCE_DIR=/path/to/workspace_ui node workspace/tests/fixtures/render_telegram_message_types_review.mjs
```

The intermediate rendered JSON can be removed after the build. The resulting HTML is self-contained. The generator checks all 96 cases, unique identities, 192 provider cycles, their 576 columns, priority quote examples and embedded resource URLs. Users, messages and files are synthetic; no provider API is called.

Implementation acceptance still requires semantic regression tests and live provider roundtrips, including externally created/edited messages, quote context, user resolution, native replies, actual media bytes, routing, reactions and lifecycle events. Telegram entity offsets use UTF-16; see [entities](https://core.telegram.org/api/entities). Its documented formatting restrictions disallow nested blockquote entities; see [formatting options](https://core.telegram.org/bots/api#formatting-options).
