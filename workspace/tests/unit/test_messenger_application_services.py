# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import uuid as sys_uuid

import pytest

from workspace.messenger_api import application_services
from workspace.messenger_api import exceptions
from workspace.messenger_api.api import controllers


def _account_spec(project_id, account_uuid):
    return {"uuid": account_uuid, "provider": "example", "settings": {}}


def test_external_account_controller_is_a_thin_request_session_adapter(monkeypatch):
    session = object()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    spec = _account_spec(project_uuid, account_uuid)
    expected = object()
    calls = []
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                iam_context=types.SimpleNamespace(
                    get_introspection_info=lambda: types.SimpleNamespace(
                        permissions=["workspace.external_account.create"]
                    )
                ),
                project_id=project_uuid,
                user_uuid=owner_uuid,
            )
        )
    )

    monkeypatch.setattr(
        controllers.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )

    def create(current_session, actor, current_spec):
        calls.append((current_session, actor, current_spec))
        return expected

    monkeypatch.setattr(
        controllers.application_services.ExternalAccountApplicationService,
        "create",
        staticmethod(create),
    )

    assert controller.create(**spec) is expected
    assert calls == [
        (
            session,
            application_services.ExternalAccountActor(owner_uuid, project_uuid),
            spec,
        )
    ]


def test_external_account_controller_rejects_missing_exact_permission(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    spec = _account_spec(project_uuid, sys_uuid.uuid4())
    iam_context = types.SimpleNamespace(
        get_introspection_info=lambda: types.SimpleNamespace(permissions=[])
    )
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                iam_context=iam_context,
                project_id=project_uuid,
                user_uuid=owner_uuid,
            )
        )
    )
    called = []
    monkeypatch.setattr(
        controllers.application_services.ExternalAccountApplicationService,
        "create",
        staticmethod(lambda *args: called.append(args)),
    )

    with pytest.raises(exceptions.ExternalResourceForbiddenError):
        controller.create(**spec)

    assert called == []
