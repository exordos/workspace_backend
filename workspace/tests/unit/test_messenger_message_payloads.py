import uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api.dm import message_payloads


def test_markdown_payload_accepts_workspace_content_limit():
    payload = message_payloads.MarkdownPayload(
        content="x" * message_payloads.MARKDOWN_CONTENT_MAX_LENGTH,
    )

    assert len(payload.content) == message_payloads.MARKDOWN_CONTENT_MAX_LENGTH


def test_markdown_payload_rejects_content_over_workspace_limit():
    with pytest.raises(ra_exc.TypeError):
        message_payloads.MarkdownPayload(
            content="x" * (message_payloads.MARKDOWN_CONTENT_MAX_LENGTH + 1),
        )


def test_markdown_payload_recognizes_canonical_user_urn_mention():
    user_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")
    payload = message_payloads.MarkdownPayload(
        content=f"Hello [Jane Doe](urn:user:{user_uuid})",
    )

    assert payload.is_user_mentioned(user_uuid)


def test_markdown_payload_does_not_treat_plain_or_legacy_text_as_mention():
    user_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")

    for content in (
        f"urn:user:{user_uuid}",
        f"@{user_uuid}",
        f"<@{user_uuid}>",
    ):
        payload = message_payloads.MarkdownPayload(content=content)
        assert not payload.is_user_mentioned(user_uuid)
