# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import pytest
from restalchemy.common import exceptions

from workspace.bridges.zulip.dm import models as zulip_models
from workspace.common import models as common_models
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import v2_models
from workspace.user_api.dm import models as user_models


@pytest.mark.parametrize(
    "model",
    [
        common_models.ModelWithNameDesc,
        common_models.ModelWithRequiredNameDesc,
        user_models.Service,
        user_models.WorkspaceStream,
        messenger_models.WorkspaceStream,
        messenger_models.WorkspaceUserStream,
        messenger_models.WorkspaceFile,
        v2_models.WorkspaceStream,
        v2_models.WorkspaceUserStream,
        event_payloads.StreamCreatedEventPayload,
        event_payloads.StreamUpdatedEventPayload,
        event_payloads.FileCreatedEventPayload,
        event_payloads.FileUpdatedEventPayload,
        zulip_models.ZulipChannel,
    ],
    ids=lambda model: f"{model.__module__}.{model.__name__}",
)
def test_description_accepts_10000_characters_and_rejects_more(model):
    description = "\N{CYRILLIC SMALL LETTER YA}" * 10000
    field = model.properties.instantiate_property("description", description)

    assert field.value == description
    assert field.get_property_type().to_openapi_spec({})["maxLength"] == 10000
    with pytest.raises(exceptions.TypeError):
        model.properties.instantiate_property("description", description + "x")


def test_required_name_and_description_defaults_are_preserved():
    assert common_models.ModelWithNameDesc().name == ""
    assert common_models.ModelWithNameDesc().description == ""
    assert common_models.ModelWithRequiredNameDesc(name="Example").description == ""
    with pytest.raises(exceptions.PropertyRequired):
        common_models.ModelWithRequiredNameDesc()
