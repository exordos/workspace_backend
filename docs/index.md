# Workspace Backend documentation

Workspace Backend documentation is available in four languages. The Markdown
and diagram source trees have the same structure in every language.

- [English](en/index.md)
- [Русский](ru/index.md)
- [Deutsch](de/index.md)
- [中文](zh/index.md)

## Canonical machine-readable contracts

OpenAPI documents are not translated: identifiers, schemas, enum values, paths,
and protocol descriptions remain one canonical source of truth.

- [Workspace public API](public_workspace_api.yaml)

External accounts contain their owner, provider, generic settings and revision.
Workspace does not process external chats, provider delivery or synchronization.
Messages, streams and topics retain `source_name` and `source` as
`native` and `{"kind":"native"}`. Architecture proposals below predate this
cleanup; the generated public API describes the current supported contract.

## Translation contract

All four language trees contain the same Markdown and PlantUML paths. Code
blocks, routes, JSON fields, model names, task kinds, UUID formulas, and other
machine identifiers are intentionally identical across translations. Every
PlantUML source has a rendered SVG beside it in the same language tree.
