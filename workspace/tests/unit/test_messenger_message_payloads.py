import base64
import uuid

import pytest
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import types_dynamic

from workspace.messenger_api.dm import message_payloads


PAYLOAD_EXAMPLES = (
    {"kind": "markdown", "content": "Message text"},
    {
        "kind": "e2ee",
        "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        "content": "Encrypted envelope",
    },
    {
        "kind": "key_request",
        "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        "device_id": "requesting-device",
        "device_name": "Requesting device",
        "public_key": "Device public key",
    },
    {
        "kind": "key_grant",
        "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        "request_message_uuid": "11111111-1111-4111-8111-111111111111",
        "sender_device_id": "sender-device",
        "recipient_device_id": "requesting-device",
        "content": "Encrypted symmetric key",
    },
    {
        "kind": "key_reject",
        "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        "request_message_uuid": "11111111-1111-4111-8111-111111111111",
        "sender_device_id": "sender-device",
        "recipient_device_id": "requesting-device",
    },
    {
        "kind": "key_announced",
        "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
    },
)
OPAQUE_FIELD_LIMITS = {
    ("markdown", "content"): 40_000,
    ("e2ee", "content"): 213_336,
    ("key_grant", "content"): 40_000,
    ("key_request", "public_key"): 2048,
}
OPAQUE_FIELD_CASES = [
    (payload, field)
    for payload in PAYLOAD_EXAMPLES
    for field in ("content", "public_key")
    if field in payload
]
DEVICE_FIELD_CASES = [
    (payload, field)
    for payload in PAYLOAD_EXAMPLES
    for field in (
        "device_id",
        "device_name",
        "sender_device_id",
        "recipient_device_id",
    )
    if field in payload
]


@pytest.fixture(params=PAYLOAD_EXAMPLES, ids=lambda payload: payload["kind"])
def payload_data(request):
    return request.param.copy()


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


@pytest.mark.parametrize(
    "character",
    [
        pytest.param("x", id="ascii"),
        pytest.param("\u044f", id="two-byte-utf8"),
        pytest.param("\u20ac", id="three-byte-utf8"),
        pytest.param("\U0001f600", id="four-byte-utf8"),
    ],
)
def test_e2ee_payload_fits_base64_encoded_maximum_markdown(character):
    markdown = message_payloads.MarkdownPayload(
        content=character * message_payloads.MARKDOWN_CONTENT_MAX_LENGTH,
    )
    data = {
        "kind": "e2ee",
        "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
        "content": base64.b64encode(markdown.content.encode("utf-8")).decode("ascii"),
    }
    payload_type = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE

    payload = payload_type.from_simple_type(data)

    assert payload_type.to_simple_type(payload) == data


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


def test_message_payload_round_trip_preserves_all_fields(payload_data):
    payload_type = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE

    payload = payload_type.from_simple_type(payload_data)

    assert payload.kind == payload_data["kind"]
    assert payload_type.to_simple_type(payload) == payload_data


def test_message_payload_rejects_unknown_fields(payload_data):
    payload_data["extra"] = "Unknown field"

    with pytest.raises(ra_exc.ParseError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(payload_data)


@pytest.mark.parametrize("field", ["public_key", "content"])
def test_key_announcement_rejects_key_material(field):
    with pytest.raises(ra_exc.ParseError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "key_announced",
                "key_uuid": "80bc4f0b-87ac-4d8d-9350-e5f88f61ca20",
                field: "Key material",
            },
        )


@pytest.mark.parametrize(
    "payload_data,field",
    [
        (payload, field)
        for payload in PAYLOAD_EXAMPLES
        for field in payload
        if field != "kind"
    ],
)
def test_message_payload_rejects_missing_required_fields(payload_data, field):
    data = payload_data.copy()
    del data[field]

    with pytest.raises(ra_exc.PropertyRequired):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(data)


@pytest.mark.parametrize("kind", ["unknown", "", None])
def test_message_payload_rejects_unknown_kind(kind):
    with pytest.raises(types_dynamic.UnknownType):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type({"kind": kind})


def test_message_payload_requires_kind():
    with pytest.raises(types_dynamic.UnknownType):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {"content": "Message text"},
        )


@pytest.mark.parametrize(
    "payload_data,field",
    [
        (payload, field)
        for payload in PAYLOAD_EXAMPLES
        for field in ("key_uuid", "request_message_uuid")
        if field in payload
    ],
)
@pytest.mark.parametrize("value", ["not-a-uuid", ""])
def test_message_payload_rejects_invalid_uuid(payload_data, field, value):
    with pytest.raises(ra_exc.ParseError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {**payload_data, field: value},
        )


@pytest.mark.parametrize(
    "payload_data,field,length",
    [
        (payload, field, length)
        for payload, field in OPAQUE_FIELD_CASES
        for length in (1, OPAQUE_FIELD_LIMITS[payload["kind"], field])
    ],
)
def test_message_payload_accepts_opaque_string_boundaries(payload_data, field, length):
    data = {**payload_data, field: "x" * length}
    payload_type = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE

    payload = payload_type.from_simple_type(data)

    assert payload_type.to_simple_type(payload) == data


@pytest.mark.parametrize(
    "payload_data,field,length",
    [
        (payload, field, length)
        for payload, field in OPAQUE_FIELD_CASES
        for length in (0, OPAQUE_FIELD_LIMITS[payload["kind"], field] + 1)
    ],
)
def test_message_payload_rejects_invalid_opaque_string_length(
    payload_data, field, length
):
    with pytest.raises(ra_exc.TypeError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {**payload_data, field: "x" * length},
        )


@pytest.mark.parametrize("payload_data,field", OPAQUE_FIELD_CASES)
def test_message_payload_preserves_opaque_non_base64_strings(payload_data, field):
    data = {**payload_data, field: "  opaque text!\n\t"}
    payload_type = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE

    payload = payload_type.from_simple_type(data)

    assert payload_type.to_simple_type(payload) == data


@pytest.mark.parametrize("payload_data,field", DEVICE_FIELD_CASES)
@pytest.mark.parametrize("length", [0, 255])
def test_message_payload_accepts_device_string_boundaries(payload_data, field, length):
    data = {**payload_data, field: "x" * length}
    payload_type = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE

    payload = payload_type.from_simple_type(data)

    assert payload_type.to_simple_type(payload) == data


@pytest.mark.parametrize("payload_data,field", DEVICE_FIELD_CASES)
def test_message_payload_rejects_device_string_over_limit(payload_data, field):
    with pytest.raises(ra_exc.TypeError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {**payload_data, field: "x" * 256},
        )


@pytest.mark.parametrize("payload_data,field", OPAQUE_FIELD_CASES + DEVICE_FIELD_CASES)
@pytest.mark.parametrize("value", [123, True, []])
def test_message_payload_rejects_non_string_fields(payload_data, field, value):
    with pytest.raises(ra_exc.TypeError):
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
            {**payload_data, field: value},
        )
