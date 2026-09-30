# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

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
            "provider": "example",
            "settings": {"account_name": "External account"},
        },
    )

    assert account.uuid == account_uuid
    assert account.owner_user_uuid == owner_uuid
    assert account.provider == "example"
    assert account.settings == {"account_name": "External account"}
    assert writes == [(account, session)]
    assert events[0][0][:3] == (project_uuid, owner_uuid, account)
    assert events[0][1]["session"] is session
