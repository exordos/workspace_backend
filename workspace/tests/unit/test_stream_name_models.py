# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import pytest
from restalchemy.common import exceptions

from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import v2_models


@pytest.mark.parametrize(
    "model",
    [
        base.WorkspaceStreamBase,
        base.WorkspaceUserStreamBase,
        v2_models.WorkspaceStream,
        v2_models.WorkspaceUserStream,
        event_payloads.StreamCreatedEventPayload,
        event_payloads.StreamUpdatedEventPayload,
        event_payloads.StreamReadEventPayload,
    ],
)
def test_stream_name_accepts_2048_characters_and_rejects_more(model):
    name = "\N{CYRILLIC SMALL LETTER YA}" * 2048
    field = model.properties.instantiate_property("name", name)

    assert field.value == name
    assert field.get_property_type().to_openapi_spec({})["maxLength"] == 2048
    with pytest.raises(exceptions.TypeError):
        model.properties.instantiate_property("name", name + "x")
