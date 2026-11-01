# Workspace Backend documentation

Workspace Backend documentation is available in four languages. The Markdown
and diagram source trees have the same structure in every language.

- [English](en/index.md)
- [Русский](ru/index.md)
- [Deutsch](de/index.md)
- [中文](zh/index.md)

## Zulip message conversion examples

[Open the rendered Zulip → Workspace Markdown comparison](zulip_messages_preview.html).
The offline preview shows the source and expected converted messages side by
side, including nested quotes, mentions, attachments, and unsupported features.

## Canonical machine-readable contracts

OpenAPI documents are not translated: identifiers, schemas, enum values, paths,
and protocol descriptions remain one canonical source of truth.

- [Workspace public API](public_workspace_api.yaml)

External accounts contain their owner, provider and generic settings.
Workspace does not process external chats, provider delivery or synchronization.
Messages, streams and topics retain `source_name` and `source` as
`native` and `{"kind":"native"}`. Architecture proposals below predate this
cleanup; the generated public API describes the current supported contract.

## Query plan validation

- [Message pages, sparse flags and reference lookups](message_page_queries.md)

## Translation contract

All four language trees contain the same Markdown and PlantUML paths. Code
blocks, routes, JSON fields, model names, task kinds, UUID formulas, and other
machine identifiers are intentionally identical across translations. Every
PlantUML source has a rendered SVG beside it in the same language tree.
