# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

import uuid as sys_uuid

import pytest

from workspace.common import constants
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import provider_store
from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import v2_models
from workspace.user_api.dm import models as user_models


DESCRIPTION_MODELS = (
    base.WorkspaceStreamBase,
    base.WorkspaceUserStreamBase,
    event_payloads.FileEventPayloadBase,
    models.WorkspaceFile,
    v2_models.WorkspaceStream,
    user_models.Service,
    user_models.WorkspaceStream,
)


@pytest.mark.parametrize("model", DESCRIPTION_MODELS)
def test_workspace_description_limit_is_ten_thousand_characters(model):
    description_type = model.properties.properties["description"].get_property_type()

    assert description_type.validate("x" * constants.WORKSPACE_DESCRIPTION_MAX_LENGTH)
    assert not description_type.validate(
        "x" * (constants.WORKSPACE_DESCRIPTION_MAX_LENGTH + 1)
    )
    assert description_type.to_openapi_spec({})["maxLength"] == 10_000


@pytest.mark.parametrize(
    "description",
    [None, 100, "x" * 10_001],
    ids=("null", "integer", "too-long"),
)
def test_provider_stream_rejects_invalid_description_before_database_work(
    description,
):
    store = object.__new__(provider_store.ProviderEntityStore)

    with pytest.raises(messenger_exceptions.ProviderApiError) as error:
        store.upsert(
            "streams",
            sys_uuid.uuid4(),
            b"0" * 32,
            {"description": description},
        )

    assert error.value.status == 422
    assert error.value.error == "invalid_description"
