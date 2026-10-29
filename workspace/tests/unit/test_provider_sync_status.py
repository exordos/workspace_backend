# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import uuid

import pytest
from restalchemy.common import exceptions

from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import v2_models


@pytest.mark.parametrize(
    "model",
    (
        v2_models.WorkspaceStream,
        v2_models.WorkspaceStreamBinding,
        v2_models.WorkspaceStreamTopic,
        v2_models.WorkspaceUserTopicBinding,
        v2_models.WorkspaceMessage,
        v2_models.WorkspaceMessageReactionFact,
        models.WorkspaceFile,
    ),
)
def test_domain_entities_share_the_read_only_sync_status(model):
    prop = model.properties.properties["status"](None)
    assert prop.is_read_only()
    assert prop.property_type.values == ["new", "syncing", "active", "error"]


@pytest.mark.parametrize("source,expected", (("native", "active"), ("zulip", "new")))
def test_new_models_derive_status_from_their_origin(source, expected):
    assert (
        base.WorkspaceProviderBase(
            source_name=source,
            provider_uuid=uuid.uuid4() if source == "zulip" else None,
        ).status
        == expected
    )


@pytest.mark.parametrize("status", ("new", "syncing", "error"))
def test_native_models_cannot_have_provider_processing_status(status):
    with pytest.raises(exceptions.ValidationErrorException):
        base.WorkspaceProviderBase(source_name="native", status=status).validate()


def test_provider_event_snapshots_default_to_active():
    payload = event_payloads.TopicCreatedEventPayload(
        uuid=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_uuid=uuid.uuid4(),
        stream_uuid=uuid.uuid4(),
        name="Topic",
        source_name="zulip",
        provider_uuid=uuid.uuid4(),
    )
    assert payload.status == "active"


def test_zulip_entity_can_wait_for_its_external_identity():
    pending = base.WorkspaceProviderBase(
        source_name="zulip", provider_uuid=uuid.uuid4()
    )
    pending.validate()
    assert pending.provider_identity is None
    assert pending.status == "new"


def test_zulip_entity_requires_its_provider_before_creation():
    with pytest.raises(exceptions.ValidationErrorException):
        base.WorkspaceProviderBase(source_name="zulip").validate()
