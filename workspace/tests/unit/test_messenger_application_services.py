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
    return {
        "uuid": account_uuid,
        "settings": {
            "kind": "zulip",
            "server_url": "https://zulip.example.invalid",
            "email": "owner@example.invalid",
            "api_key": "provider-secret",
            "selection_mode": "explicit",
            "history_depth": "30_days",
            "default_project_id": project_id,
        },
    }


def test_external_account_create_reuses_the_caller_session(monkeypatch):
    session = object()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    calls = []

    class AccountObjects:
        def get_all(self, **kwargs):
            calls.append(("accounts", kwargs["session"]))
            return []

    class Account:
        objects = AccountObjects()

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.desired_generation = 1
            self.status = "connecting"

        def insert(self, *, session):
            calls.append(("account.insert", session))

    class Credential:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def insert(self, *, session):
            calls.append(("credential.insert", session))

    class PolicyObjects:
        def get_one_or_none(self, **kwargs):
            calls.append(("policy", kwargs["session"]))
            return types.SimpleNamespace(
                enabled=True,
                emergency_suspended=False,
                limits={"max_accounts": 1},
            )

    class Policy:
        objects = PolicyObjects()

    envelope = {"associated_data": {"bridge_instance_uuid": str(sys_uuid.uuid4())}}

    def encrypt(current_session, *args):
        calls.append(("encrypt", current_session, args[-1]))
        return {"identity_generation": 3}, envelope

    def append_upsert(current_session, *args):
        calls.append(("desired", current_session, args[-1]))

    def create_event(*args, **kwargs):
        calls.append(("event", kwargs["session"], args))

    monkeypatch.setattr(
        application_services.external_models,
        "ExternalAccount",
        Account,
    )
    monkeypatch.setattr(
        application_services.external_models,
        "ExternalCredential",
        Credential,
    )
    monkeypatch.setattr(
        application_services.external_models,
        "ExternalProviderPolicy",
        Policy,
    )
    monkeypatch.setattr(
        application_services.credential_crypto,
        "encrypt_for_active_bridge",
        encrypt,
    )
    monkeypatch.setattr(
        application_services.sql_state,
        "append_upsert",
        append_upsert,
    )
    monkeypatch.setattr(
        application_services.messenger_events,
        "create_external_resource_event",
        create_event,
    )

    account = application_services.ExternalAccountApplicationService.create(
        session,
        application_services.ExternalAccountActor(owner_uuid, project_uuid),
        _account_spec(project_uuid, account_uuid),
    )

    assert account.uuid == account_uuid
    assert all(call[1] is session for call in calls)
    encrypted = next(call for call in calls if call[0] == "encrypt")
    assert encrypted[2] == {
        "server_url": "https://zulip.example.invalid",
        "email": "owner@example.invalid",
        "api_key": "provider-secret",
    }
    desired = next(call for call in calls if call[0] == "desired")[2]
    assert desired["settings"] == {
        "kind": "zulip",
        "server_url": "https://zulip.example.invalid",
        "selection_mode": "explicit",
        "history_depth": "30_days",
        "default_project_id": str(project_uuid),
    }
    event = next(call for call in calls if call[0] == "event")
    assert event[2][0:2] == (project_uuid, owner_uuid)


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


def test_external_chat_select_materialized_reuses_the_caller_session(monkeypatch):
    session = object()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    calls = []

    class Property:
        def __init__(self, name):
            self.name = name

        def set_value_force(self, value):
            calls.append((f"property.{self.name}", value))

    chat = types.SimpleNamespace(
        uuid=chat_uuid,
        external_account_uuid=account_uuid,
        projection_stream_uuid=stream_uuid,
        provider="zulip",
        selected=False,
        revision=4,
        properties={
            name: Property(name)
            for name in ("selected", "project_id", "status", "revision")
        },
        update=lambda *, session: calls.append(("chat.update", session)),
    )
    account = types.SimpleNamespace(uuid=account_uuid, provider="zulip")
    credential = types.SimpleNamespace(
        envelope={"associated_data": {"bridge_instance_uuid": str(bridge_uuid)}}
    )

    class ChatObjects:
        def get_one(self, **kwargs):
            calls.append(("chat.get", kwargs["session"]))
            return chat

        def get_all(self, **kwargs):
            calls.append(("chat.list", kwargs["session"]))
            return []

    class Chat:
        objects = ChatObjects()

    class AccountObjects:
        def get_one(self, **kwargs):
            calls.append(("account.get", kwargs["session"]))
            return account

    class Account:
        objects = AccountObjects()

    class StreamObjects:
        def get_one(self, **kwargs):
            calls.append(("stream.get", kwargs["session"]))
            return object()

    class Stream:
        objects = StreamObjects()

    monkeypatch.setattr(application_services.external_models, "ExternalChat", Chat)
    monkeypatch.setattr(
        application_services.external_models,
        "ExternalAccount",
        Account,
    )
    monkeypatch.setattr(application_services.models, "WorkspaceStream", Stream)
    monkeypatch.setattr(
        application_services,
        "require_external_provider_enabled",
        lambda current_session, *args: (
            calls.append(("policy", current_session))
            or types.SimpleNamespace(limits={"max_selected_chats_per_account": 5})
        ),
    )
    monkeypatch.setattr(
        application_services,
        "external_credential",
        lambda current_account, current_session: (
            calls.append(("credential", current_session)) or credential
        ),
    )
    monkeypatch.setattr(
        application_services.sql_state,
        "external_chat_assignment_desired",
        lambda current_chat, *, session: (
            calls.append(("desired", session)) or {"uuid": str(current_chat.uuid)}
        ),
    )
    monkeypatch.setattr(
        application_services.sql_state,
        "append_upsert",
        lambda current_session, *args: calls.append(("append", current_session, args)),
    )
    monkeypatch.setattr(
        application_services.messenger_events,
        "create_external_resource_event",
        lambda *args, **kwargs: calls.append(("event", kwargs["session"])),
    )

    result = application_services.ExternalChatApplicationService.select_materialized(
        session,
        application_services.ExternalAccountActor(owner_uuid, project_uuid),
        chat_uuid,
    )

    assert result is chat
    session_calls = [
        call
        for call in calls
        if call[0]
        in {
            "chat.get",
            "chat.list",
            "account.get",
            "stream.get",
            "policy",
            "chat.update",
            "credential",
            "desired",
            "append",
            "event",
        }
    ]
    assert all(call[1] is session for call in session_calls)
    append = next(call for call in calls if call[0] == "append")
    assert append[2] == (str(bridge_uuid), "zulip", {"uuid": str(chat_uuid)})
    assert ("property.selected", True) in calls
    assert ("property.project_id", project_uuid) in calls
    assert ("property.revision", 5) in calls
