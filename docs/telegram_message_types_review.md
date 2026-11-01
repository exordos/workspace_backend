# Telegram message types: review before conversion changes

The [offline review page](telegram_message_types_review.html) captures the complete existing Zulip fixture corpus and the native Telegram fixtures through the unchanged Telegram converter. It is a review artifact, not an acceptance claim for real Telegram delivery.

Each Zulip case includes the actual local Zulip rendering, the actual Workspace rendering, the current Telegram text/entities, references delegated to runtime, and exact reverse-converted Markdown. Observed behavior and proposed changes are separate. Quote diagrams are proposals and do not modify the converter.

Rebuild from the backend checkout using its development Python environment and the current local UI and Zulip checkouts:

```sh
.tox/develop/bin/python workspace/tests/fixtures/build_telegram_message_types_review.py
ZULIP_SOURCE_DIR=/path/to/zulip WORKSPACE_UI_SOURCE_DIR=/path/to/workspace_ui node workspace/tests/fixtures/render_zulip_messages_preview.mjs
WORKSPACE_UI_SOURCE_DIR=/path/to/workspace_ui node workspace/tests/fixtures/render_telegram_message_types_review.mjs
```

The current review captures 79 Zulip cases and 17 native Telegram cases. The generator checks corpus coverage, unique case names, the three comparison columns, priority quote examples and embedded resource URLs. The page requires no API calls or external resources. Users, messages and files are synthetic.

The Telegram column is an entity-based illustration, not a client screenshot. Media uploads, user resolution, native reply targets, edits, reactions and routing need separate runtime tests. The proposed nested quote fallback uses explicit text levels because Telegram disallows nested blockquote entities; see the official [formatting constraints](https://core.telegram.org/bots/api#formatting-options).
