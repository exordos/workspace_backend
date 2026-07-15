# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest

from workspace.common import urns
from workspace.provider_api.dm import models


def test_workspace_entity_urn_roundtrip():
    entity_uuid = sys_uuid.uuid4()
    value = urns.build(urns.MAIL_MESSAGE, entity_uuid)

    entity_type, parsed_uuid = urns.parse(
        value,
        expected_type=urns.MAIL_MESSAGE,
    )

    assert value == f"urn:mail-message:{entity_uuid}"
    assert entity_type == urns.MAIL_MESSAGE
    assert parsed_uuid == entity_uuid


def test_workspace_entity_urn_rejects_wrong_type():
    with pytest.raises(ValueError):
        urns.parse(
            urns.build(urns.MAIL_FOLDER, sys_uuid.uuid4()),
            expected_type=urns.MAIL_MESSAGE,
        )


def test_provider_rejects_unknown_kind():
    with pytest.raises(ValueError):
        models.WorkspaceProvider(
            uuid=sys_uuid.uuid4(),
            name="Unsupported",
            supported_kinds=["unknown"],
        )
