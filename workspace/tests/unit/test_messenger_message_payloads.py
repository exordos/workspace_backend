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


@pytest.mark.parametrize(
    ("payload", "model_type"),
    (
        (
            {
                "kind": "e2ee",
                "key_uuid": "11111111-1111-4111-8111-111111111111",
                "content": "opaque-envelope",
            },
            message_payloads.EncryptedPayload,
        ),
        (
            {
                "kind": "key_request",
                "key_uuid": "11111111-1111-4111-8111-111111111111",
                "device_id": "iphone-1",
                "device_name": "iPhone",
                "public_key": "device-public-key",
            },
            message_payloads.KeyRequestPayload,
        ),
        (
            {
                "kind": "key_grant",
                "key_uuid": "11111111-1111-4111-8111-111111111111",
                "request_message_uuid": "22222222-2222-4222-8222-222222222222",
                "sender_device_id": "mac-1",
                "recipient_device_id": "iphone-1",
                "content": "encrypted-private-key",
            },
            message_payloads.KeyGrantPayload,
        ),
        (
            {
                "kind": "key_reject",
                "key_uuid": "11111111-1111-4111-8111-111111111111",
                "request_message_uuid": "22222222-2222-4222-8222-222222222222",
                "sender_device_id": "mac-1",
                "recipient_device_id": "iphone-1",
            },
            message_payloads.KeyRejectPayload,
        ),
        (
            {
                "kind": "key_announced",
                "key_uuid": "11111111-1111-4111-8111-111111111111",
                "public_key": "chat-public-key",
            },
            message_payloads.KeyAnnouncedPayload,
        ),
    ),
)
def test_encrypted_chat_payloads_round_trip_strictly(payload, model_type):
    parsed = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(payload)

    assert isinstance(parsed, model_type)
    assert (
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.to_simple_type(parsed)
        == payload
    )

    with pytest.raises(ra_exc.ParseError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {**payload, "unknown": "must-not-survive"}
        )


def test_non_markdown_payload_content_is_not_exposed_as_markdown():
    assert (
        message_payloads.markdown_content(
            {
                "kind": "e2ee",
                "key_uuid": "11111111-1111-4111-8111-111111111111",
                "content": "](urn:user:22222222-2222-4222-8222-222222222222)",
            }
        )
        == ""
    )


def test_encryption_key_type_rejects_unknown_fields():
    key = {
        "key_uuid": "11111111-1111-4111-8111-111111111111",
        "public_key": "chat-public-key",
    }

    parsed = message_payloads.ENCRYPTION_KEY_TYPE.from_simple_type(key)
    assert message_payloads.ENCRYPTION_KEY_TYPE.to_simple_type(parsed) == key
    with pytest.raises(ValueError):
        message_payloads.ENCRYPTION_KEY_TYPE.from_simple_type(
            {**key, "private_key": "never"}
        )
