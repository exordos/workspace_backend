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
from workspace.messenger_api.dm import v2_models


USER_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
PROJECT_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")
KEY_UUID = sys_uuid.UUID("80bc4f0b-87ac-4d8d-9350-e5f88f61ca20")


@pytest.fixture(params=[models.WorkspaceUserStream, v2_models.WorkspaceUserStream])
def stream_factory(request):
    def create(**metadata):
        return request.param(
            uuid=sys_uuid.uuid4(),
            project_id=PROJECT_UUID,
            user_uuid=USER_UUID,
            owner=USER_UUID,
            role="owner",
            name="Encryption metadata",
            source_name="native",
            source=models.NativeSource(),
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
        source={"kind": "native"},
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
