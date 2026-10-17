# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import v2_models


USER_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
PEER_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture(params=[models.WorkspaceStream, v2_models.WorkspaceStream])
def stream_factory(request):
    def create(**identity):
        owner_field = (
            "user_uuid" if request.param is models.WorkspaceStream else "owner_uuid"
        )
        source = (
            models.NativeSource()
            if request.param is models.WorkspaceStream
            else {"kind": "native"}
        )
        return request.param(
            project_id=sys_uuid.uuid4(),
            name="Native stream",
            source_name="native",
            source=source,
            **{owner_field: USER_UUID},
            **identity,
        )

    return create


@pytest.mark.parametrize("peer_uuid", [USER_UUID, PEER_UUID])
def test_private_stream_requires_identity_pair(stream_factory, peer_uuid):
    stream = stream_factory(private=True, direct_user_uuid=peer_uuid)

    assert stream.private is True
    assert stream.direct_user_uuid == peer_uuid


def test_private_stream_without_peer_is_rejected(stream_factory):
    with pytest.raises(ra_exc.ValidationErrorException):
        stream_factory(private=True)


def test_ordinary_stream_without_peer_is_valid(stream_factory):
    stream = stream_factory()

    assert stream.private is False
    assert stream.direct_user_uuid is None
