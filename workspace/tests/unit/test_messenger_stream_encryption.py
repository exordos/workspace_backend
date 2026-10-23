# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import events
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import v2_store
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import v2_models


USER_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
PROJECT_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")
KEY_UUID = sys_uuid.UUID("80bc4f0b-87ac-4d8d-9350-e5f88f61ca20")


@pytest.fixture(params=[models.WorkspaceUserStream, v2_models.WorkspaceUserStream])
def stream_factory(request):
    def create(**metadata):
        role = metadata.pop("role", "owner")
        return request.param(
            uuid=sys_uuid.uuid4(),
            project_id=PROJECT_UUID,
            user_uuid=USER_UUID,
            owner=USER_UUID,
            role=role,
            name="Encryption metadata",
            source_name="native",
            provider_uuid=None,
            provider_identity=None,
            **metadata,
        )

    return create


def test_stream_encryption_defaults(stream_factory):
    stream = stream_factory()

    assert stream.encryption is False
    assert stream.current_encryption_key_uuid is None


def test_canonical_stream_encryption_defaults():
    stream = v2_models.WorkspaceStream(
        uuid=sys_uuid.uuid4(),
        project_id=PROJECT_UUID,
        owner_uuid=USER_UUID,
        name="Encryption metadata",
        provider_uuid=None,
        provider_identity=None,
    )

    assert stream.encryption is False
    assert stream.current_encryption_key_uuid is None


@pytest.mark.parametrize(
    "encryption,key_uuid",
    [(False, None), (True, KEY_UUID), (False, KEY_UUID)],
)
def test_stream_responses_and_events_preserve_encryption_metadata(
    stream_factory, encryption, key_uuid
):
    stream = stream_factory(
        encryption=encryption,
        current_encryption_key_uuid=key_uuid,
    )
    snapshots = [
        v2_store._public(stream, "streams"),
        resource_projection.as_dict(stream, "streams"),
        events._stream_from_event_payload(stream),
    ]

    for snapshot in snapshots:
        assert snapshot["encryption"] is encryption
        assert snapshot["current_encryption_key_uuid"] == (
            None if key_uuid is None else str(key_uuid)
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"encryption": "true"},
        {"encryption": 1},
        {"current_encryption_key_uuid": "not-a-uuid"},
    ],
)
def test_stream_encryption_rejects_invalid_types(stream_factory, metadata):
    with pytest.raises(ra_exc.TypeError):
        stream_factory(**metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"encryption": False, "current_encryption_key_uuid": None},
        {"encryption": True, "current_encryption_key_uuid": KEY_UUID},
    ],
)
def test_stream_creation_accepts_consistent_encryption(metadata):
    settings = base.WorkspaceStreamEncryptionCreation(**metadata)

    assert settings.encryption is metadata.get("encryption", False)
    assert settings.current_encryption_key_uuid == metadata.get(
        "current_encryption_key_uuid"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"encryption": True},
        {"encryption": True, "current_encryption_key_uuid": None},
        {"current_encryption_key_uuid": KEY_UUID},
        {"encryption": False, "current_encryption_key_uuid": KEY_UUID},
        {
            "source_name": "zulip",
            "encryption": True,
            "current_encryption_key_uuid": KEY_UUID,
        },
    ],
)
def test_stream_creation_rejects_inconsistent_encryption(metadata):
    with pytest.raises(ra_exc.ValidationErrorException):
        base.WorkspaceStreamEncryptionCreation(**metadata)


@pytest.mark.parametrize("field", ["encryption", "current_encryption_key_uuid"])
def test_stream_updates_reject_encryption_fields(field):
    with pytest.raises(ra_exc.ValidationErrorException):
        base.WorkspaceUserStreamBase.validate_encryption_update(
            {"name": "Rename", field: None}
        )


@pytest.mark.parametrize("source_name", ["native", "zulip"])
def test_e2ee_messages_require_a_native_stream(source_name):
    payload = message_payloads.E2EEPayload(key_uuid=KEY_UUID, content="opaque data")

    if source_name == "zulip":
        with pytest.raises(ra_exc.ValidationErrorException):
            base.WorkspaceUserStreamBase.validate_message_kind(
                source_name, payload.KIND
            )
    else:
        base.WorkspaceUserStreamBase.validate_message_kind(source_name, payload.KIND)


@pytest.mark.parametrize("source_name", ["native", "zulip"])
def test_markdown_messages_remain_allowed_in_both_stream_sources(source_name):
    payload = message_payloads.MarkdownPayload(content="Message")

    base.WorkspaceUserStreamBase.validate_message_kind(source_name, payload.KIND)


@pytest.mark.parametrize("role", ["owner", "administrator", "moderator", "member"])
def test_only_stream_owners_can_announce_keys(stream_factory, role):
    stream = stream_factory(role=role)

    if role == "owner":
        stream.validate_message_creation("key_announced")
    else:
        with pytest.raises(ra_exc.ValidationErrorException):
            stream.validate_message_creation("key_announced")


@pytest.mark.parametrize("role", ["owner", "administrator", "moderator", "member"])
def test_key_announcement_permission_does_not_restrict_markdown(stream_factory, role):
    stream_factory(role=role).validate_message_creation("markdown")


@pytest.mark.parametrize(
    "original,new_kind",
    [
        (message_payloads.KeyAnnouncedPayload(key_uuid=KEY_UUID), "key_announced"),
        (message_payloads.KeyAnnouncedPayload(key_uuid=KEY_UUID), "markdown"),
        (message_payloads.MarkdownPayload(content="Text"), "key_announced"),
    ],
)
def test_key_announcements_cannot_be_created_or_changed_by_editing(original, new_kind):
    message = base.WorkspaceMessageFieldsBase(
        stream_uuid=sys_uuid.uuid4(),
        topic_uuid=sys_uuid.uuid4(),
        source_name="native",
        provider_uuid=None,
        provider_identity=None,
        payload=original,
    )

    with pytest.raises(ra_exc.ValidationErrorException):
        message.validate_key_announcement_update(new_kind)
