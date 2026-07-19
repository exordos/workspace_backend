# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import ast
import inspect
import pathlib
import types
import uuid as sys_uuid

import pytest
import webob
from restalchemy.api import applications
from restalchemy.api import contexts

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.dm import helpers as dm_helpers
from workspace.messenger_api.dm import external_models
from workspace.workspace_api.api import app as workspace_app


def test_projected_external_metadata_round_trips_as_public_nested_contract():
    account_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    values = {
        "source": {"kind": "zulip", "stream_id": 42},
        "provider": {
            "kind": "zulip",
            "account_uuid": str(account_uuid),
            "external_id": "provider-message-7",
            "capabilities": {
                "messenger.message.edit": {
                    "available": False,
                    "revision": 1,
                    "limits": {},
                    "unavailable_reason": {
                        "code": "account_degraded",
                        "message": "External account is degraded.",
                    },
                }
            },
        },
        "delivery": {
            "external_operation_uuid": str(operation_uuid),
            "status": "manual_reconciliation_required",
            "safe_error": "Delivery outcome requires confirmation.",
            "can_retry": True,
            "can_discard": True,
            "updated_at": "2026-07-17T00:00:00Z",
            "duplicate_risk": True,
            "retry_requires_confirmation": True,
            "original_url": "https://zulip.invalid/#narrow/id/7",
            "reconciliation_reason": "unsafe_provider_state",
        },
    }

    stored = sql_canonical_store.SQLCanonicalMessengerStore._projection_values(values)
    assert stored["external_account_uuid"] == str(account_uuid)
    assert stored["provider_external_id"] == "provider-message-7"
    assert stored["delivery_status"] == "manual_reconciliation_required"
    assert stored["provider_metadata"] == values["provider"]
    assert stored["delivery_metadata"] == values["delivery"]

    public = resource_projection.as_dict(stored, "message_reactions")
    assert public["provider"] == values["provider"]
    assert public["delivery"] == values["delivery"]
    assert "provider_metadata" not in public
    assert "delivery_metadata" not in public


def test_zb_acc_01_create_selector_strips_write_only_api_key():
    create = external_models.EXTERNAL_ACCOUNT_CREATE_SETTINGS_TYPE.from_simple_type(
        {
            "kind": "zulip",
            "server_url": "https://zulip.example.invalid",
            "email": "owner@example.invalid",
            "api_key": "write-only",
            "selection_mode": "all",
            "history_depth": "30_days",
            "default_project_id": str(sys_uuid.uuid4()),
        }
    )
    values = external_models.EXTERNAL_ACCOUNT_CREATE_SETTINGS_TYPE.to_simple_type(
        create
    )
    values.pop("api_key")
    sanitized = external_models.EXTERNAL_ACCOUNT_SETTINGS_TYPE.from_simple_type(values)

    assert "api_key" not in (
        external_models.EXTERNAL_ACCOUNT_SETTINGS_TYPE.to_simple_type(sanitized)
    )


def test_zb_acc_01_provider_neutral_routes_are_exposed():
    assert routes.ApiEndpointRoute.external_accounts is routes.ExternalAccountRoute
    assert routes.ApiEndpointRoute.external_chats is routes.ExternalChatRoute
    assert routes.ApiEndpointRoute.external_operations is routes.ExternalOperationRoute
    assert routes.ApiEndpointRoute.external_bridge_instances is (
        routes.ExternalBridgeInstanceRoute
    )
    assert not hasattr(routes.ApiEndpointRoute, "zulip_accounts")


@pytest.mark.parametrize(
    "request_operation",
    (
        controllers.ExternalAccountController.create,
        controllers.ExternalAccountController.update,
        controllers.ExternalAccountController.reconnect._post,
        controllers.ExternalAccountController.disconnect._post,
        controllers.ExternalAccountController.delete,
        controllers.ExternalChatController._change_assignment,
        controllers.ExternalOperationController.retry._post,
        controllers.ExternalOperationController.delete,
        controllers.ExternalProviderPolicyController.update,
        controllers.ExternalProviderPolicyController._change_status,
        sql_canonical_store.SQLCanonicalReadStore.filter_draft_page,
        dm_helpers._workspace_session,
        dm_helpers._create_workspace_stream_binding_message_flags,
    ),
)
def test_request_operations_do_not_open_database_session_managers(
    request_operation,
):
    source = inspect.getsource(request_operation)

    assert ".session_manager(" not in source


def test_production_database_session_boundaries_are_centralized():
    workspace_root = pathlib.Path(controllers.__file__).parents[2]
    failures = []
    for path in workspace_root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            name = node.func.attr
            call = ast.get_source_segment(source, node) or name
            location = f"{path.relative_to(workspace_root)}:{node.lineno}"
            if name == "session_manager":
                allowed = path.name == "agents.py" and call.startswith(
                    "ctx.session_manager("
                )
                if not allowed:
                    failures.append(f"{location}: {call}")
            elif name == "get_session":
                if not call.startswith("contexts.Context().get_session("):
                    failures.append(f"{location}: {call}")
            elif name in {"commit", "rollback"}:
                failures.append(f"{location}: {call}")
            elif name == "close" and isinstance(node.func.value, ast.Name):
                if node.func.value.id in {"session", "s", "engine"}:
                    failures.append(f"{location}: {call}")

    assert failures == []


def test_zb_contract_001_public_openapi_exposes_exact_ui_boundary():
    application = applications.OpenApiApplication(
        route_class=workspace_app.get_api_application(),
        openapi_engine=workspace_app.get_openapi_engine(),
    )
    request = webob.Request.blank("/specifications/3.0.3")
    request.application = application
    request.api_context = contexts.RequestContext(request)
    specification = application.openapi_engine.build_openapi_specification(
        "3.0.3",
        request,
    )
    paths = specification["paths"]
    schemas = specification["components"]["schemas"]
    account_root = "/v1/messenger/external_accounts/"
    create_settings = paths[account_root]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["settings"]

    assert create_settings["properties"]["api_key"]["writeOnly"] is True
    assert (
        "api_key"
        not in schemas["ExternalAccount_Get"]["properties"]["settings"]["properties"]
    )
    reconnect = paths[
        "/v1/messenger/external_accounts/{ExternalAccountUuid}/actions/reconnect/invoke"
    ]["post"]
    assert {parameter["name"] for parameter in reconnect["parameters"]} >= {
        "ExternalAccountUuid",
        "If-Match",
    }
    chat = schemas["ExternalChat_Get"]["properties"]
    assert {
        "source",
        "display_name",
        "selected",
        "status",
        "project_id",
        "history_depth",
        "capabilities",
    } <= set(chat)
    assert "provider_chat_id" not in chat
    operation = schemas["ExternalOperation_Get"]["properties"]
    assert "manual_reconciliation_required" in operation["status"]["enum"]
    assert {
        "duplicate_risk",
        "retry_requires_confirmation",
        "original_url",
        "attempt_history",
        "reconciliation_state",
        "reconciliation_reason",
        "reconciliation_evidence",
    } <= set(operation)
    assert "/v1/messenger/external_operations/actions/preflight/invoke" in paths
    provider_policy = paths["/v1/messenger/external_provider_policies/{kind}"]
    assert {
        parameter["name"] for parameter in provider_policy["get"]["parameters"]
    } == {"kind"}
    assert {
        parameter["name"] for parameter in provider_policy["put"]["parameters"]
    } == {"kind", "If-Match"}
    assert provider_policy["put"]["parameters"][1]["required"] is True
    for method in ("get", "put"):
        assert (
            provider_policy[method]["responses"][200]["headers"]["ETag"]["schema"][
                "pattern"
            ]
            == '^"[1-9][0-9]*"$'
        )


def test_zb_msg_003_manual_retry_requires_duplicate_risk_confirmation(
    monkeypatch,
):
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
        ),
        headers={},
    )
    controller = controllers.ExternalOperationController(request)
    monkeypatch.setattr(
        controllers,
        "_update_internal_fields",
        lambda resource, values, session=None: [
            setattr(resource, name, value) for name, value in values.items()
        ],
    )
    queued_record_uuid = sys_uuid.uuid4()
    request_session = object()
    monkeypatch.setattr(
        controllers.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    queued_sessions = []

    def retry_provider_operation(session, **kwargs):
        assert kwargs == {
            "external_operation_uuid": Operation.uuid,
            "next_attempt": 3,
        }
        queued_sessions.append(session)
        return {
            "uuid": queued_record_uuid,
            "project_id": request.context.project_id,
        }

    monkeypatch.setattr(
        controllers.provider_data,
        "retry_provider_operation",
        retry_provider_operation,
    )
    emitted = []
    monkeypatch.setattr(
        controllers.provider_data,
        "publish_operation_event",
        lambda session, resource, project_id, kind: emitted.append(
            (session, resource, project_id, kind)
        ),
    )

    class Operation:
        uuid = sys_uuid.uuid4()
        details = {}
        can_retry = True
        retry_requires_confirmation = True
        attempt_history = []
        attempt = 2
        status = "manual_reconciliation_required"
        safe_error = "Provider commit could not be proven"
        duplicate_risk = True
        original_url = "https://zulip.example.invalid/#narrow/id/42"
        reconciliation_state = "manual_required"
        reconciliation_reason = "unsafe_provider_state"
        reconciliation_evidence = {"checks_completed": 3}
        revision = 4

        def update_dm(self, values):
            for name, value in values.items():
                setattr(self, name, value)

        def update(self):
            return None

    operation = Operation()
    retry = controllers.ExternalOperationController.retry._post

    with pytest.raises(Exception):
        retry(controller, operation)
    result = retry(
        controller,
        operation,
        confirm_duplicate_risk=True,
    )

    assert result.status == "queued"
    assert result.attempt == 3
    assert result.details == {"record_uuid": str(queued_record_uuid)}
    assert result.attempt_history == [
        {
            "attempt": 2,
            "status": "manual_reconciliation_required",
            "safe_error": "Provider commit could not be proven",
            "duplicate_risk": True,
            "original_url": "https://zulip.example.invalid/#narrow/id/42",
            "reconciliation_state": "manual_required",
            "reconciliation_reason": "unsafe_provider_state",
        }
    ]
    assert result.duplicate_risk is False
    assert emitted
    assert queued_sessions == [request_session]


def test_external_operation_discard_uses_provider_queue_transaction(monkeypatch):
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
        ),
        headers={},
    )
    controller = controllers.ExternalOperationController(request)
    request_session = object()
    monkeypatch.setattr(
        controllers.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    calls = []
    monkeypatch.setattr(
        controllers.provider_data,
        "discard_provider_operation",
        lambda session, **kwargs: (
            calls.append((session, kwargs))
            or {"uuid": sys_uuid.uuid4(), "project_id": request.context.project_id}
        ),
    )
    emitted = []
    controller._emit_event = lambda resource, kind, hidden_fields=(), session=None: (
        emitted.append((resource, kind, hidden_fields, session))
    )
    monkeypatch.setattr(
        controllers,
        "_update_internal_fields",
        lambda resource, values, session=None: [
            setattr(resource, name, value) for name, value in values.items()
        ],
    )
    synced = []
    monkeypatch.setattr(
        controllers.provider_data,
        "sync_operation_target_delivery",
        lambda session, resource, project_id: synced.append(
            (session, resource, project_id)
        ),
    )

    class Operation:
        uuid = sys_uuid.uuid4()
        can_discard = True
        status = "queued"
        revision = 1

        def delete(self, session=None):
            calls.append(("delete", session))

    operation = Operation()
    controller.get = lambda uuid: operation

    controller.delete(operation.uuid)

    assert calls[0] == (
        request_session,
        {"external_operation_uuid": operation.uuid},
    )
    assert operation.status == "discarded"
    assert synced == [(request_session, operation, request.context.project_id)]
    assert calls[1] == ("delete", request_session)
    assert emitted[0][3] is request_session


def test_external_operation_preflight_uses_effective_chat_capability(monkeypatch):
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    session = object()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_uuid,
            user_uuid=user_uuid,
        ),
        headers={},
    )
    controller = controllers.ExternalOperationController(request)
    provider_calls = []
    controller._require_provider_enabled = lambda provider, session=None: (
        provider_calls.append((provider, session))
    )
    monkeypatch.setattr(
        controllers.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    account_calls = []
    stream_calls = []
    chat_calls = []
    account = types.SimpleNamespace(
        uuid=account_uuid,
        provider="zulip",
        live_ready=True,
        capabilities={"messenger.message.send": {"available": True}},
    )
    chat = types.SimpleNamespace(
        selected=True,
        status="live",
        transition_pending=False,
        capabilities={"messenger.message.send": {"available": False}},
    )
    monkeypatch.setattr(
        controllers.external_models.ExternalAccount,
        "objects",
        types.SimpleNamespace(
            get_one=lambda **kwargs: account_calls.append(kwargs) or account
        ),
    )
    monkeypatch.setattr(
        controllers.models.WorkspaceStream,
        "objects",
        types.SimpleNamespace(
            get_one=lambda **kwargs: (
                stream_calls.append(kwargs) or types.SimpleNamespace(uuid=stream_uuid)
            )
        ),
    )
    monkeypatch.setattr(
        controllers.external_models.ExternalChat,
        "objects",
        types.SimpleNamespace(
            get_all=lambda **kwargs: chat_calls.append(kwargs) or [chat]
        ),
    )

    result = controllers.ExternalOperationController.preflight._post(
        controller,
        None,
        external_account_uuid=account_uuid,
        action="messenger.message.send",
        target={"type": "stream", "uuid": str(stream_uuid)},
    )

    assert result["allowed"] is False
    assert result["losses"] == []
    assert account_calls[0]["session"] is session
    assert provider_calls == [("zulip", session)]
    assert stream_calls[0]["session"] is session
    assert chat_calls[0]["session"] is session


def test_external_chat_projection_move_uses_storage_boundary(monkeypatch):
    values = {
        "chat_uuid": sys_uuid.uuid4(),
        "revision": 3,
        "owner_uuid": sys_uuid.uuid4(),
        "stream_uuid": sys_uuid.uuid4(),
        "old_project_uuid": sys_uuid.uuid4(),
        "new_project_uuid": sys_uuid.uuid4(),
        "write_new": False,
        "write_old": True,
    }
    calls = []
    monkeypatch.setattr(
        controllers.api_store,
        "move_stream_projection",
        lambda **kwargs: calls.append(kwargs),
    )

    controllers._journal_projection_move(
        values["chat_uuid"],
        values["revision"],
        values["owner_uuid"],
        values["stream_uuid"],
        values["old_project_uuid"],
        values["new_project_uuid"],
        write_new=values["write_new"],
        write_old=values["write_old"],
    )

    assert calls == [values]


def test_zb_sec_002_bridge_admin_actions_require_exact_iam_permission(monkeypatch):
    class IamContext:
        permissions = []

        def get_introspection_info(self):
            return types.SimpleNamespace(permissions=self.permissions)

    iam_context = IamContext()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            iam_context=iam_context,
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
        ),
        headers={},
    )
    controller = controllers.ExternalBridgeInstanceController(request)

    with pytest.raises(Exception):
        controller._require_permission("workspace.external_bridge_instance.suspend")
    iam_context.permissions = ["workspace.external_bridge_instance.*"]
    with pytest.raises(Exception):
        controller._require_permission("workspace.external_bridge_instance.suspend")
    iam_context.permissions = ["workspace.external_bridge_instance.suspend"]
    controller._require_permission("workspace.external_bridge_instance.suspend")

    class BridgeInstance:
        status = "active"
        revision = 2
        provider = "zulip"

    bridge = BridgeInstance()
    request_session = object()
    monkeypatch.setattr(
        controllers.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        controllers,
        "_update_internal_fields",
        lambda resource, values, session=None: [
            setattr(resource, name, value) for name, value in values.items()
        ],
    )
    monkeypatch.setattr(
        controllers.sql_state,
        "refresh_effective_capabilities",
        lambda session, provider_kind: None,
    )
    suspend = controllers.ExternalBridgeInstanceController.suspend._post
    result = suspend(controller, bridge)

    assert result.status == "suspended"
    assert result.revision == 3


def test_external_account_delete_purges_projection_and_copied_files(monkeypatch):
    account_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    account = types.SimpleNamespace(
        uuid=account_uuid,
        owner_user_uuid=owner_uuid,
        provider="zulip",
        desired_generation=2,
        delete=lambda session=None: deleted.append(("account", session)),
    )
    chat = types.SimpleNamespace(
        uuid=chat_uuid,
        revision=3,
        owner_user_uuid=owner_uuid,
        provider="zulip",
        projection_stream_uuid=stream_uuid,
        project_id=project_uuid,
    )
    file_row = {
        "uuid": file_uuid,
        "storage_type": "local",
        "storage_object_id": str(file_uuid),
    }

    class Result:
        def fetchall(self):
            return [file_row]

    class Session:
        def execute(self, statement, params):
            statements.append((" ".join(statement.split()), params))
            return Result()

    session = Session()
    deleted = []
    statements = []
    journaled = []
    desired_deletes = []
    file_deletes = []
    controller = controllers.ExternalAccountController(
        types.SimpleNamespace(
            context=types.SimpleNamespace(
                project_id=project_uuid,
                user_uuid=owner_uuid,
            )
        )
    )
    controller.get = lambda uuid: account
    controller._emit_event = lambda *args, **kwargs: None
    monkeypatch.setattr(
        controllers.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    monkeypatch.setattr(
        controllers.ExternalAccountController,
        "_credential",
        staticmethod(
            lambda resource, current_session: types.SimpleNamespace(
                envelope={
                    "associated_data": {"bridge_instance_uuid": str(sys_uuid.uuid4())}
                }
            )
        ),
    )
    monkeypatch.setattr(
        type(controllers.external_models.ExternalChat.objects),
        "get_all",
        lambda self, **kwargs: [chat],
    )
    monkeypatch.setattr(
        controllers,
        "_journal_projection_move",
        lambda *args, **kwargs: journaled.append((args, kwargs)),
    )
    monkeypatch.setattr(
        controllers.sql_state,
        "append_delete",
        lambda *args: desired_deletes.append(args[3:]),
    )
    monkeypatch.setattr(
        controllers.messenger_events,
        "create_external_resource_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        controllers.file_storage,
        "delete_workspace_file",
        lambda *args, **kwargs: file_deletes.append(("object", args, kwargs)),
    )
    monkeypatch.setattr(
        controllers.file_storage,
        "delete_workspace_file_metadata",
        lambda *args, **kwargs: file_deletes.append(("metadata", args, kwargs)),
    )

    controller.delete(account_uuid)

    assert journaled and journaled[0][1] == {"write_new": False}
    assert any(
        statement.startswith("DELETE FROM m_workspace_streams")
        for statement, _ in statements
    )
    assert desired_deletes == [
        ("external_chat_assignment", chat_uuid, 4),
        ("external_account", account_uuid, 3),
    ]
    assert deleted == [("account", session)]
    assert [item[0] for item in file_deletes] == ["object", "metadata"]
