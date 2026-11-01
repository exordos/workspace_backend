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

External accounts are scoped to their owner and project. Zulip and Telegram
synchronize through the canonical Messenger model. Streams, topics and messages
retain their provider identity; native resources remain independent. Telegram
application credentials and account sessions stay server-side. See the provider
guides and generated public API for the current contracts.

## Translation contract

All four language trees contain the same Markdown and PlantUML paths. Code
blocks, routes, JSON fields, model names, task kinds, UUID formulas, and other
machine identifiers are intentionally identical across translations. Every
PlantUML source has a rendered SVG beside it in the same language tree.

[Open the rendered Telegram ↔ Workspace conversion comparison](telegram_messages_preview.html).
