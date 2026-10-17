# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import application_services
from workspace.messenger_api.dm import external_models


def test_registration_accepts_a_provider_without_an_enrolled_bridge(monkeypatch):
    session = object()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    writes = []
    events = []

    def insert(account, *, session):
        writes.append((account, session))

    def emit(*args, **kwargs):
        events.append((args, kwargs))

    monkeypatch.setattr(external_models.ExternalAccount, "insert", insert)
    monkeypatch.setattr(
        application_services.messenger_events, "create_external_resource_event", emit
    )
    account = application_services.ExternalAccountApplicationService.create(
        session,
        application_services.ExternalAccountActor(owner_uuid, project_uuid),
        {
            "uuid": account_uuid,
            "project_id": sys_uuid.uuid4(),
            "provider": "example",
            "settings": {"account_name": "External account"},
        },
    )

    assert account.uuid == account_uuid
    assert account.project_id == project_uuid
    assert account.owner_user_uuid == owner_uuid
    assert account.provider == "example"
    assert account.status == external_models.ExternalAccountStatus.NEW.value
    assert account.error_description is None
    assert account.settings == {"account_name": "External account"}
    assert writes == [(account, session)]
    assert events[0][0][:3] == (project_uuid, owner_uuid, account)
    assert events[0][1]["session"] is session


@pytest.mark.parametrize("status", list(external_models.ExternalAccountStatus))
def test_external_account_accepts_status(status):
    account = external_models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        owner_user_uuid=sys_uuid.uuid4(),
        provider="example",
        settings={},
        status=status.value,
    )

    assert account.status == status.value


@pytest.mark.parametrize("status", ["connecting", "NEW"])
def test_external_account_rejects_invalid_status(status):
    with pytest.raises(ra_exc.TypeError):
        external_models.ExternalAccount(
            project_id=sys_uuid.uuid4(),
            owner_user_uuid=sys_uuid.uuid4(),
            provider="example",
            settings={},
            status=status,
        )


def test_external_account_stores_error_description():
    account = external_models.ExternalAccount(
        project_id=sys_uuid.uuid4(),
        owner_user_uuid=sys_uuid.uuid4(),
        provider="example",
        settings={},
        status=external_models.ExternalAccountStatus.ERROR.value,
        error_description="Authentication failed",
    )

    assert account.status == "error"
    assert account.error_description == "Authentication failed"
