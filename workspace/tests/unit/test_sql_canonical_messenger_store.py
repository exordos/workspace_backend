# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import inspect
import types
import uuid as sys_uuid

import pytest

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.api import sql_canonical_store


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
PROJECTION_OWNER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")


class FakeObjects:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def get_all(self, **kwargs):
        self.calls.append(("get_all", kwargs))
        return self.rows

    def get_one(self, **kwargs):
        self.calls.append(("get_one", kwargs))
        return self.rows[0]

    def get_one_or_none(self, **kwargs):
        self.calls.append(("get_one_or_none", kwargs))
        return None if not self.rows else self.rows[0]


def _fake_model(objects, properties):
    return type(
        "FakeModel",
        (),
        {
            "objects": objects,
            "properties": types.SimpleNamespace(
                properties={name: object() for name in properties},
            ),
            "get_id_property_name": classmethod(lambda cls: "uuid"),
        },
    )


def test_resource_reads_are_project_and_user_scoped(monkeypatch):
    row = {"uuid": sys_uuid.uuid4()}
    objects = FakeObjects([row])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setitem(
        sql_canonical_store.RESOURCE_MODELS,
        "streams",
        model,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {**value, "resource": resource},
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    result = store.filter_resources(
        "streams",
        {"name": dm_filters.EQ("general")},
        {"uuid": "asc"},
    )

    assert result == [{**row, "resource": "streams"}]
    _operation, query = objects.calls[0]
    assert query["filters"]["project_id"].value == PROJECT_UUID
    assert query["filters"]["user_uuid"].value == USER_UUID
    assert query["filters"]["name"].value == "general"
    assert query["order_by"] == {"uuid": "asc"}


def test_provider_collection_serialization_does_not_lookup_each_canonical_row(
    monkeypatch,
):
    rows = [
        {
            "uuid": sys_uuid.uuid4(),
            "project_id": PROJECT_UUID,
            "source": {"kind": "zulip"},
            "provider_metadata": {
                "kind": "zulip",
                "account_uuid": str(sys_uuid.uuid4()),
                "external_id": f"stream-{index}",
                "capabilities": {},
            },
            "delivery_metadata": None,
        }
        for index in range(50)
    ]
    objects = FakeObjects(rows)
    model = _fake_model(
        objects,
        (
            "uuid",
            "project_id",
            "source",
            "provider_metadata",
            "delivery_metadata",
        ),
    )
    monkeypatch.setitem(sql_canonical_store.RESOURCE_MODELS, "streams", model)
    lookups = []
    monkeypatch.setattr(
        sql_canonical_store.resource_projection.EXTENSION_CANONICAL_MODELS[
            "streams"
        ].objects,
        "get_one_or_none",
        lambda **kwargs: lookups.append(kwargs),
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    result = store.filter_resources("streams", {}, {"uuid": "asc"})

    assert len(result) == 50
    assert lookups == []
    assert all(item["provider"]["kind"] == "zulip" for item in result)


def test_direct_stream_participant_validation_uses_identity_pair(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    rows = {
        stream_uuid: {
            "user_uuid": USER_UUID,
            "direct_user_uuid": USER_UUID,
            "private_index": f"{USER_UUID}:{USER_UUID}",
        },
        peer_uuid: {
            "user_uuid": USER_UUID,
            "direct_user_uuid": PROJECTION_OWNER_UUID,
            "private_index": f"{USER_UUID}:{PROJECTION_OWNER_UUID}",
        },
    }

    class Session:
        def execute(self, _statement, values):
            return types.SimpleNamespace(fetchone=lambda: rows[values[1]])

    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: Session()),
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)

    store._validate_stream_participants(stream_uuid, [USER_UUID])
    store._validate_stream_participants(
        peer_uuid,
        [USER_UUID, PROJECTION_OWNER_UUID],
    )
    with pytest.raises(Exception):
        store._validate_stream_participants(
            stream_uuid,
            [USER_UUID, PROJECTION_OWNER_UUID],
        )
    with pytest.raises(Exception):
        store._validate_stream_participants(peer_uuid, [USER_UUID])


def test_message_page_uses_created_at_uuid_keyset(monkeypatch):
    marker_uuid = sys_uuid.uuid4()
    marker = types.SimpleNamespace(
        uuid=marker_uuid,
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
    )
    row = {"uuid": sys_uuid.uuid4()}
    objects = FakeObjects([marker])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceUserMessage", model)
    monkeypatch.setitem(
        sql_canonical_store.RESOURCE_MODELS,
        "messages",
        model,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: value,
    )
    objects.rows = [marker]
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    objects.get_all = lambda **kwargs: (
        objects.calls.append(("get_all", kwargs)) or [row]
    )
    result = store.filter_message_page({}, marker_uuid, "asc", 51)

    assert result == [row]
    _operation, marker_query = objects.calls[0]
    assert marker_query["filters"]["uuid"].value == marker_uuid
    _operation, page_query = objects.calls[1]
    assert page_query["order_by"] == {"created_at": "asc", "uuid": "asc"}
    assert page_query["limit"] == 51


def test_draft_page_reuses_current_context_session(monkeypatch):
    draft_uuid = sys_uuid.uuid4()
    draft = types.SimpleNamespace(uuid=draft_uuid)
    rows = types.SimpleNamespace(fetchall=lambda: [{"uuid": draft_uuid}])
    session = types.SimpleNamespace(execute=lambda statement, params: rows)
    context = types.SimpleNamespace(get_session=lambda: session)
    objects = FakeObjects([draft])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setattr(sql_canonical_store.contexts, "Context", lambda: context)
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceDraft", model)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    result = store.filter_draft_page({}, None, "asc", 21)

    assert result == [{"uuid": draft_uuid}]
    _operation, query = objects.calls[0]
    assert query["session"] is session
    assert "session_manager" not in inspect.getsource(
        sql_canonical_store.SQLCanonicalReadStore,
    )


def test_file_list_uses_scoped_acl_without_public_user_cross_product(monkeypatch):
    file_uuid = sys_uuid.uuid4()
    row = {
        "uuid": file_uuid,
        "viewer_user_uuid": USER_UUID,
    }
    objects = FakeObjects([row])
    model = _fake_model(
        objects,
        ("uuid", "project_id", "user_uuid", "viewer_user_uuid"),
    )
    monkeypatch.setitem(sql_canonical_store.RESOURCE_MODELS, "files", model)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: value.copy(),
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    assert store.filter_resources("files", {}, {"uuid": "asc"}) == [{"uuid": file_uuid}]
    _operation, query = objects.calls[0]
    filters = query["filters"]
    assert isinstance(filters, dm_filters.AND)
    assert "project_id" in repr(filters)
    assert str(PROJECT_UUID) in repr(filters)
    assert "viewer_user_uuid" in repr(filters)
    assert str(USER_UUID) in repr(filters)
    assert "acl_mode" in repr(filters)
    assert "public" in repr(filters)


def test_public_file_view_has_no_users_cross_join():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0113-remove-legacy-Messenger-mail-storage-eec69a.py"
    ).read_text()

    assert "CROSS JOIN" not in migration
    assert 'JOIN "m_workspace_stream_bindings" AS bindings' in migration
    assert 'JOIN "m_workspace_file_accesses" AS accesses' not in migration
    assert 'NULL::UUID AS "viewer_user_uuid"' in migration


def test_provider_projection_access_includes_bound_members():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0121-grant-external-projection-access-to-members-35e3d3.py"
    ).read_text()

    assert 'JOIN "m_workspace_stream_bindings" AS binding' in migration
    assert "binding.stream_uuid = chat.projection_stream_uuid" in migration
    assert "binding.project_id = chat.project_id" in migration
    assert "binding.user_uuid" in migration
    assert "account.owner_user_uuid AS user_uuid" in migration
    assert "account.uuid::text::varchar(2048) AS source_scope" in migration


def test_revoked_stream_messages_require_current_membership():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / (
            "migrations/"
            "0122-revoke-external-projection-access-on-stream-removal-640b9d.py"
        )
    ).read_text()

    assert "e.\"object_type\" <> 'message'" in migration
    assert 'FROM "m_workspace_stream_bindings" AS binding' in migration
    assert 'binding."stream_uuid" =' in migration
    assert "(e.\"payload\"->>'stream_uuid')::uuid" in migration
    assert 'binding."user_uuid" = e."user_uuid"' in migration


def test_external_message_visibility_uses_canonical_projection_access():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / ("migrations/0123-deduplicate-and-revoke-external-chat-memberships-aadb67.py")
    ).read_text()

    assert "ROW_NUMBER() OVER" in migration
    assert "candidate.provider_realm_id" in migration
    assert "candidate.provider_chat_id" in migration
    assert "m_workspace_external_chat_membership_revocations" in migration
    assert "external_stream.\"source_name\" <> 'native'" in migration
    assert 'JOIN "m_confirmed_external_account_access" AS stream_access' in migration


def test_external_account_access_is_unique_across_selected_chats():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0124-deduplicate-external-account-access-78c745.py"
    ).read_text()

    assert "SELECT DISTINCT" in migration
    assert "project_id," in migration
    assert "user_uuid," in migration
    assert "account_type," in migration
    assert "source_scope" in migration
    assert "FROM ranked_candidates" in migration


def test_canonical_message_write_uses_db_helper_in_request_scope(monkeypatch):
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    payload = {"kind": "markdown", "content": "hello"}
    calls = []
    row = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
    )
    session = object()
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "create_workspace_user_message",
        lambda **kwargs: calls.append(kwargs) or row,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {
            "uuid": value.uuid,
            "resource": resource,
        },
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(store, "_queue_provider_operation", lambda **kwargs: None)

    result = store.create_message(
        {
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": payload,
        }
    )

    assert result == {"uuid": message_uuid, "resource": "messages"}
    assert calls == [
        {
            "project_id": PROJECT_UUID,
            "user_uuid": USER_UUID,
            "session": session,
            "enforce_visibility": True,
            "compact_events": True,
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": payload,
        }
    ]


def test_canonical_store_has_no_mail_replay_or_nested_session_boundary():
    source = inspect.getsource(sql_canonical_store)

    assert "mail_service" not in source
    assert "messenger_mail" not in source
    assert "sql_store" not in source
    assert "session_manager" not in source


def test_provider_operation_uses_projection_owner_target_in_request_transaction(
    monkeypatch,
):
    stream_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    lock_calls = []

    class Session:
        def execute(self, statement, params):
            lock_calls.append((statement, params))
            return types.SimpleNamespace(fetchone=lambda: {"uuid": account_uuid})

    session = Session()
    stream_objects = FakeObjects(
        [
            types.SimpleNamespace(
                external_account_uuid=account_uuid,
                user_uuid=PROJECTION_OWNER_UUID,
            )
        ]
    )
    account = types.SimpleNamespace(uuid=account_uuid)
    bridge = types.SimpleNamespace(uuid=bridge_uuid)
    monkeypatch.setattr(
        sql_canonical_store.models.WorkspaceStream,
        "objects",
        stream_objects,
    )
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    calls = []
    target_calls = []
    monkeypatch.setattr(
        sql_canonical_store.provider_data,
        "resolve_provider_target",
        lambda current_session, **kwargs: (
            target_calls.append((current_session, kwargs))
            or (account, object(), bridge)
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.provider_data,
        "enqueue_provider_operation",
        lambda current_session, **kwargs: (
            calls.append((current_session, kwargs))
            or (types.SimpleNamespace(uuid=kwargs["operation_uuid"]), sys_uuid.uuid4())
        ),
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )

    store._queue_provider_operation(
        operation_kind="message.create",
        target_type="message",
        target_uuid=target_uuid,
        stream_uuid=stream_uuid,
        payload={"uuid": target_uuid},
    )

    assert calls[0][0] is session
    assert calls[0][1]["bridge_instance_uuid"] == bridge_uuid
    assert calls[0][1]["external_account_uuid"] == account_uuid
    assert calls[0][1]["project_id"] == PROJECT_UUID
    assert calls[0][1]["owner_user_uuid"] == USER_UUID
    assert "FOR KEY SHARE" in lock_calls[0][0]
    assert lock_calls[0][1] == (account_uuid,)
    assert target_calls == [
        (
            session,
            {
                "project_id": PROJECT_UUID,
                "owner_user_uuid": PROJECTION_OWNER_UUID,
                "external_account_uuid": account_uuid,
                "stream_uuid": stream_uuid,
                "capability_name": "messenger.message.send",
            },
        )
    ]


def test_stream_binding_add_queues_only_new_provider_memberships(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    existing_user_uuid = sys_uuid.uuid4()
    added_user_uuid = sys_uuid.uuid4()
    existing_binding_uuid = sys_uuid.uuid4()
    added_binding_uuid = sys_uuid.uuid4()
    provider_target = (object(), object())
    bindings = [
        types.SimpleNamespace(
            uuid=existing_binding_uuid,
            stream_uuid=stream_uuid,
            user_uuid=existing_user_uuid,
            who_uuid=USER_UUID,
            role="member",
        ),
        types.SimpleNamespace(
            uuid=added_binding_uuid,
            stream_uuid=stream_uuid,
            user_uuid=added_user_uuid,
            who_uuid=USER_UUID,
            role="member",
        ),
    ]
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        store,
        "_stream_participants",
        lambda requested_stream_uuid: (
            [existing_user_uuid] if requested_stream_uuid == stream_uuid else []
        ),
    )
    validated = []
    monkeypatch.setattr(
        store,
        "_validate_stream_participants",
        lambda requested_stream_uuid, participants: validated.append(
            (requested_stream_uuid, set(participants))
        ),
    )
    targets = []
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda requested_stream_uuid, operation_kind=None: (
            targets.append((requested_stream_uuid, operation_kind)) or provider_target
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_or_create_workspace_stream_bindings",
        lambda *args, **kwargs: bindings,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda row, _resource, **_kwargs: {
            "uuid": str(row.uuid),
            "stream_uuid": str(row.stream_uuid),
            "user_uuid": str(row.user_uuid),
            "who_uuid": str(row.who_uuid),
            "role": row.role,
        },
    )
    queued = []
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queued.append(kwargs),
    )

    result = store.perform_action(
        "stream_bindings",
        stream_uuid,
        "add_users",
        {"member": [str(existing_user_uuid), str(added_user_uuid)]},
    )

    assert len(result) == 2
    assert validated == [
        (
            stream_uuid,
            {existing_user_uuid, added_user_uuid},
        )
    ]
    assert targets == [(stream_uuid, "membership.add")]
    assert queued == [
        {
            "operation_kind": "membership.add",
            "target_type": "stream_binding",
            "target_uuid": added_binding_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "uuid": str(added_binding_uuid),
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(added_user_uuid),
                "who_uuid": str(USER_UUID),
                "role": "member",
            },
            "provider_target": provider_target,
        }
    ]


def test_stream_binding_add_propagates_policy_block_before_mutation(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    added_user_uuid = sys_uuid.uuid4()
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_stream_participants", lambda _stream_uuid: [])
    monkeypatch.setattr(
        store,
        "_validate_stream_participants",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sql_canonical_store.messenger_exceptions.ExternalResourceForbiddenError()
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_or_create_workspace_stream_bindings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("policy block must precede canonical membership mutation")
        ),
    )

    with pytest.raises(
        sql_canonical_store.messenger_exceptions.ExternalResourceForbiddenError
    ):
        store.perform_action(
            "stream_bindings",
            stream_uuid,
            "add_users",
            {"member": [str(added_user_uuid)]},
        )


def test_stream_binding_delete_queues_provider_membership_removal(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    binding_uuid = sys_uuid.uuid4()
    removed_user_uuid = sys_uuid.uuid4()
    remaining_user_uuid = sys_uuid.uuid4()
    provider_target = (object(), object())
    binding = types.SimpleNamespace(
        uuid=binding_uuid,
        stream_uuid=stream_uuid,
        user_uuid=removed_user_uuid,
        who_uuid=USER_UUID,
        role="member",
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_binding_for_update", lambda _uuid: binding)
    monkeypatch.setattr(
        store,
        "_stream_participants",
        lambda requested_stream_uuid: (
            [removed_user_uuid, remaining_user_uuid]
            if requested_stream_uuid == stream_uuid
            else []
        ),
    )
    validated = []
    monkeypatch.setattr(
        store,
        "_validate_stream_participants",
        lambda requested_stream_uuid, participants: validated.append(
            (requested_stream_uuid, tuple(participants))
        ),
    )
    targets = []
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda requested_stream_uuid, operation_kind=None: (
            targets.append((requested_stream_uuid, operation_kind)) or provider_target
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda row, _resource, **_kwargs: {
            "uuid": str(row.uuid),
            "stream_uuid": str(row.stream_uuid),
            "user_uuid": str(row.user_uuid),
            "who_uuid": str(row.who_uuid),
            "role": row.role,
        },
    )
    deleted = []
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "delete_workspace_stream_binding",
        lambda *args: deleted.append(args),
    )
    queued = []
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queued.append(kwargs),
    )

    assert store.delete_resource("stream_bindings", binding_uuid) is None

    assert validated == [(stream_uuid, (remaining_user_uuid,))]
    assert targets == [(stream_uuid, "membership.remove")]
    assert deleted == [(PROJECT_UUID, binding_uuid)]
    assert queued == [
        {
            "operation_kind": "membership.remove",
            "target_type": "stream_binding",
            "target_uuid": binding_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "uuid": str(binding_uuid),
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(removed_user_uuid),
                "who_uuid": str(USER_UUID),
                "role": "member",
            },
            "provider_target": provider_target,
        }
    ]


def test_reaction_update_queues_previous_provider_state(monkeypatch):
    reaction_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    reaction = types.SimpleNamespace(
        uuid=reaction_uuid,
        message_uuid=message_uuid,
        emoji_name="thumbs_up",
    )
    updated = types.SimpleNamespace(
        uuid=reaction_uuid,
        message_uuid=message_uuid,
        emoji_name="heart",
    )
    message = types.SimpleNamespace(stream_uuid=stream_uuid)
    provider_target = (object(), object())
    queued = []
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "projection_values",
        lambda values: values,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda row, _resource, **_kwargs: {
            "uuid": str(row.uuid),
            "message_uuid": str(row.message_uuid),
            "emoji_name": row.emoji_name,
        },
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_message_reaction",
        lambda *_args: reaction,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_message",
        lambda *_args: message,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "update_workspace_message_reaction",
        lambda *_args, **_kwargs: updated,
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *_args: provider_target,
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queued.append(kwargs),
    )

    result = store.update_resource(
        "message_reactions",
        reaction_uuid,
        {"emoji_name": "heart"},
    )

    assert result["emoji_name"] == "heart"
    assert queued == [
        {
            "operation_kind": "reaction.update",
            "target_type": "reaction",
            "target_uuid": reaction_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "uuid": str(reaction_uuid),
                "message_uuid": str(message_uuid),
                "emoji_name": "heart",
                "previous_message_uuid": str(message_uuid),
                "previous_emoji_name": "thumbs_up",
            },
            "provider_target": provider_target,
        }
    ]


def test_provider_capability_rejection_precedes_canonical_message_mutation(
    monkeypatch,
):
    stream_uuid = sys_uuid.uuid4()
    mutation_calls = []
    queue_calls = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *args: (_ for _ in ()).throw(
            sql_canonical_store.ra_exceptions.ValidationErrorException()
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "create_workspace_user_message",
        lambda **kwargs: mutation_calls.append(kwargs),
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queue_calls.append(kwargs),
    )

    with pytest.raises(sql_canonical_store.ra_exceptions.ValidationErrorException):
        store.create_message(
            {
                "uuid": sys_uuid.uuid4(),
                "stream_uuid": stream_uuid,
                "topic_uuid": sys_uuid.uuid4(),
                "payload": {"kind": "markdown", "content": "hello"},
            }
        )

    assert mutation_calls == []
    assert queue_calls == []


def test_provider_topic_move_fails_before_canonical_mutation(monkeypatch):
    source_stream_uuid = sys_uuid.uuid4()
    destination_stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    provider_target = (object(), object())
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "projection_values",
        lambda values: values,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_stream_topic",
        lambda *args: types.SimpleNamespace(
            uuid=topic_uuid,
            stream_uuid=source_stream_uuid,
        ),
    )
    targets = []
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda stream_uuid, operation_kind=None: (
            targets.append((stream_uuid, operation_kind)) or provider_target
        ),
    )
    mutations = []
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "update_workspace_user_stream_topic",
        lambda *args, **kwargs: mutations.append((args, kwargs)),
    )

    with pytest.raises(sql_canonical_store.ra_exceptions.ValidationErrorException):
        store.update_resource(
            "stream_topics",
            topic_uuid,
            {"stream_uuid": destination_stream_uuid},
        )

    assert targets == [
        (source_stream_uuid, "topic.update"),
        (destination_stream_uuid, None),
    ]
    assert mutations == []


def test_topic_summary_prompt_action_is_canonical_only(monkeypatch):
    topic_uuid = sys_uuid.uuid4()
    topic = types.SimpleNamespace(
        uuid=topic_uuid,
        project_id=PROJECT_UUID,
        user_uuid=USER_UUID,
    )
    calls = []
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "set_workspace_user_stream_topic_summary_prompt",
        lambda *args, **kwargs: calls.append((args, kwargs)) or topic,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": str(value.uuid)},
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: pytest.fail("summary metadata must remain canonical-only"),
    )

    result = store.perform_action(
        "stream_topics",
        topic_uuid,
        "set_summary_prompt",
        {"summary_system_prompt": "Focus on decisions."},
    )

    assert result == {"uuid": str(topic_uuid)}
    assert calls == [
        (
            (PROJECT_UUID, USER_UUID, topic_uuid),
            {"summary_system_prompt": "Focus on decisions."},
        ),
    ]


def test_provider_read_operation_preserves_exact_workspace_order(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    first_message_uuid = sys_uuid.uuid4()
    last_message_uuid = sys_uuid.uuid4()
    calls = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: calls.append(kwargs) or object(),
    )

    store._queue_provider_read(
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        message_uuids=[first_message_uuid, last_message_uuid],
        target_type="message",
        target_uuid=last_message_uuid,
    )
    store._queue_provider_read(
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        message_uuids=[],
        target_type="message",
        target_uuid=last_message_uuid,
    )

    assert calls == [
        {
            "operation_kind": "read_state.set",
            "target_type": "message",
            "target_uuid": last_message_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(USER_UUID),
                "message_uuids": [
                    str(first_message_uuid),
                    str(last_message_uuid),
                ],
                "read": True,
            },
        }
    ]


def test_stream_read_queues_only_exact_unread_projection(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    row = types.SimpleNamespace(uuid=stream_uuid)
    session = object()
    call_order = []
    queued = []
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_stream",
        lambda *args, **kwargs: call_order.append("visible") or row,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_stream_messages",
        lambda *args, **kwargs: call_order.append("update") or (row, message_uuids),
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    provider_target = object()
    monkeypatch.setattr(
        store,
        "_lock_provider_account_for_stream",
        lambda *args: call_order.append("account-lock") or True,
    )
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *args, **kwargs: (
            call_order.append("provider-target") or provider_target
        ),
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_read",
        lambda **kwargs: queued.append(kwargs),
    )

    assert store.perform_action("streams", stream_uuid, "read", {}) == {
        "uuid": stream_uuid
    }
    assert call_order == ["visible", "account-lock", "update", "provider-target"]
    assert queued == [
        {
            "stream_uuid": stream_uuid,
            "topic_uuid": None,
            "message_uuids": message_uuids,
            "target_type": "stream",
            "target_uuid": stream_uuid,
            "provider_target": provider_target,
        }
    ]


def test_topic_read_queues_exact_unread_projection(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    topic = types.SimpleNamespace(uuid=topic_uuid, stream_uuid=stream_uuid)
    session = object()
    call_order = []
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_stream_topic",
        lambda *args, **kwargs: call_order.append("visible") or topic,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_stream_topic_messages",
        lambda *args, **kwargs: call_order.append("update") or (topic, message_uuids),
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    queued = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    provider_target = object()
    monkeypatch.setattr(
        store,
        "_lock_provider_account_for_stream",
        lambda *args: call_order.append("account-lock") or True,
    )
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *args, **kwargs: (
            call_order.append("provider-target") or provider_target
        ),
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_read",
        lambda **kwargs: queued.append(kwargs),
    )

    store.perform_action("stream_topics", topic_uuid, "read", {})

    assert call_order == ["visible", "account-lock", "update", "provider-target"]
    assert queued == [
        {
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "message_uuids": message_uuids,
            "target_type": "topic",
            "target_uuid": topic_uuid,
            "provider_target": provider_target,
        }
    ]


def test_duplicate_message_read_does_not_queue_provider_operation(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    message = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        read=True,
    )
    session = object()
    call_order = []
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_message",
        lambda *args, **kwargs: call_order.append("visible") or message,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_message",
        lambda *args, **kwargs: call_order.append("update") or (message, []),
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    queued = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)

    def unexpected_provider_target(*args):
        del args
        raise AssertionError(
            "idempotent no-op must not validate provider capability"
        )

    monkeypatch.setattr(
        store,
        "_lock_provider_account_for_stream",
        lambda *args: call_order.append("account-lock") or True,
    )
    monkeypatch.setattr(
        store,
        "_provider_target",
        unexpected_provider_target,
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queued.append(kwargs),
    )

    store.perform_action("messages", message_uuid, "read", {})

    assert call_order == ["visible", "account-lock", "update"]
    assert queued == []


def test_read_up_to_locks_provider_before_update_without_unread_probe(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    created_at = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    message = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        created_at=created_at,
    )
    first_message_uuid = sys_uuid.uuid4()
    message_uuids = [first_message_uuid, message_uuid]
    session = object()
    call_order = []
    read_calls = []
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_message",
        lambda *args, **kwargs: message,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_topic_messages_to_message",
        lambda *args, **kwargs: (
            call_order.append("update")
            or read_calls.append((args, kwargs))
            or (message, message_uuids)
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    queued = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    provider_target = object()
    monkeypatch.setattr(
        store,
        "_lock_provider_account_for_stream",
        lambda *args: call_order.append("account-lock") or True,
    )
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *args, **kwargs: (
            call_order.append("provider-target") or provider_target
        ),
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_read",
        lambda **kwargs: queued.append(kwargs),
    )

    store.perform_action("messages", message_uuid, "read_up_to", {})

    assert call_order == ["account-lock", "update", "provider-target"]
    assert read_calls[0][1]["current_message"] is message
    assert read_calls[0][1]["return_message_uuids"] is True
    assert queued[0]["message_uuids"] == message_uuids
    assert queued[0]["provider_target"] is provider_target


def test_canonical_factory_separates_event_store_without_mail_runtime():
    factory = sql_canonical_store.SQLCanonicalMessengerStoreFactory()

    with factory(PROJECT_UUID, USER_UUID) as store:
        assert isinstance(store, sql_canonical_store.SQLCanonicalMessengerStore)
    with factory.event_store(PROJECT_UUID, USER_UUID) as store:
        assert isinstance(store, sql_canonical_store.PostgresEventStore)


class CursorSession:
    def __init__(self, cursor, events=()):
        self.cursor = cursor
        self.events = list(events)
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))
        if statement.lstrip().startswith("INSERT"):
            row = None
            rows = []
        elif statement.lstrip().startswith("WITH direct_events"):
            row = None
            rows = self.events
        else:
            row = self.cursor
            rows = []
        return types.SimpleNamespace(
            fetchone=lambda: row,
            fetchall=lambda: rows,
        )


def _event_store(monkeypatch, cursor, events=()):
    session = CursorSession(cursor, events)
    context = types.SimpleNamespace(get_session=lambda: session)
    monkeypatch.setattr(sql_canonical_store.contexts, "Context", lambda: context)
    return (
        sql_canonical_store.PostgresEventStore(PROJECT_UUID, USER_UUID),
        session,
    )


def test_canonical_store_events_preserve_cursor_scope_order_and_limit(monkeypatch):
    generation = sys_uuid.uuid4()
    events = [types.SimpleNamespace(epoch_version=21)]
    _postgres_store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
        events,
    )
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "event_row_to_messenger_event",
        lambda event: {"epoch_version": event.epoch_version},
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )

    result = store.events_after(
        {"epoch_version": dm_filters.GT(20)},
        order_by={"epoch_version": "asc"},
        epoch_generation=str(generation),
        limit=3,
    )

    assert result == [{"epoch_version": 21}]
    statement, parameters = session.statements[2]
    assert statement.count("LIMIT %s") == 3
    assert 'FROM "m_workspace_events"' in statement
    assert 'FROM "m_workspace_broadcast_message_events_v1"' in statement
    assert statement.count('FROM "m_workspace_stream_bindings" AS binding') == 2
    assert statement.count("(event.\"payload\"->>'stream_uuid')::uuid") == 6
    assert (
        statement.count('JOIN "m_confirmed_external_account_access" AS stream_access')
        == 2
    )
    assert "UNION ALL" in statement
    assert parameters == (
        PROJECT_UUID,
        USER_UUID,
        20,
        3,
        USER_UUID,
        PROJECT_UUID,
        20,
        3,
        3,
    )


def test_canonical_store_event_cursor_delegates_to_postgres_store(monkeypatch):
    generation = sys_uuid.uuid4()
    _postgres_store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )

    assert store.event_cursor() == {
        "epoch_generation": str(generation),
        "current_epoch_version": 41,
        "minimum_epoch_version": 13,
    }
    assert [params for _statement, params in session.statements] == [
        (PROJECT_UUID, USER_UUID),
        (PROJECT_UUID, USER_UUID),
    ]


def test_postgres_event_cursor_preserves_public_shape(monkeypatch):
    generation = sys_uuid.uuid4()
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )

    assert store.event_cursor() == {
        "epoch_generation": str(generation),
        "current_epoch_version": 41,
        "minimum_epoch_version": 13,
    }
    assert len(session.statements) == 2
    assert (
        'ON CONFLICT ("project_id", "user_uuid") DO NOTHING'
        in (session.statements[0][0])
    )


def test_postgres_event_cursor_combines_direct_and_audience_watermarks(monkeypatch):
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": sys_uuid.uuid4(),
            "current_epoch_version": 81,
            "pruned_through_epoch_version": 40,
        },
    )

    assert store.current_epoch() == 81
    query = session.statements[1][0]
    assert 'LEFT JOIN "m_workspace_event_audience_members_v1"' in query
    assert 'MAX(audience."current_epoch_version")' in query
    assert 'MAX(audience."pruned_through_epoch_version")' in query
    assert "GROUP BY" in query


def test_postgres_event_cursor_rejects_mail_generation(monkeypatch):
    generation = sys_uuid.uuid4()
    store, _session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )

    with pytest.raises(
        sql_canonical_store.messenger_exceptions.EventsCursorExpiredError
    ) as error:
        store.events_after(
            {"epoch_version": dm_filters.GT(20)},
            epoch_generation="old-mail-generation",
        )

    assert error.value.as_dict()["reason"] == "epoch_generation_changed"
    assert error.value.as_dict()["epoch_generation"] == str(generation)


def test_postgres_events_are_user_scoped_and_keep_epoch_order(monkeypatch):
    generation = sys_uuid.uuid4()
    events = [types.SimpleNamespace(epoch_version=21)]
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
        events,
    )
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "event_row_to_messenger_event",
        lambda event: {"epoch_version": event.epoch_version},
    )

    result = store.events_after(
        {"epoch_version": dm_filters.GT(20)},
        epoch_generation=str(generation),
        limit=25,
    )

    assert result == [{"epoch_version": 21}]
    statement, parameters = session.statements[2]
    assert 'recipient."user_uuid" = %s' in statement
    assert 'event."project_id" = %s' in statement
    assert 'event."epoch_version" > %s' in statement
    assert parameters[-1] == 25


def test_postgres_events_keep_model_path_for_additional_filters(monkeypatch):
    generation = sys_uuid.uuid4()
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )
    events = [types.SimpleNamespace(epoch_version=21)]
    objects = FakeObjects(events)
    model = _fake_model(
        objects,
        ("epoch_version", "project_id", "user_uuid", "object_type"),
    )
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceVisibleEvent", model)
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "pack_workspace_event",
        lambda event: {"epoch_version": event.epoch_version},
    )

    result = store.events_after(
        {
            "epoch_version": dm_filters.GT(20),
            "object_type": dm_filters.EQ("message"),
        },
        order_by={"epoch_version": "desc"},
        epoch_generation=str(generation),
        limit=25,
    )

    assert result == [{"epoch_version": 21}]
    assert len(session.statements) == 2
    _operation, query = objects.calls[0]
    assert query["filters"]["project_id"].value == PROJECT_UUID
    assert query["filters"]["user_uuid"].value == USER_UUID
    assert query["filters"]["epoch_version"].value == 20
    assert query["filters"]["object_type"].value == "message"
    assert query["order_by"] == {"epoch_version": "desc"}
    assert query["limit"] == 25


def test_event_retention_advances_watermark_before_delete():
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    statements = []

    def execute(statement, params):
        statements.append((statement, params))
        return types.SimpleNamespace(fetchone=lambda: {"count": 17})

    result = sql_canonical_store.prune_expired_events(
        types.SimpleNamespace(execute=execute),
        now,
    )

    assert result == 17
    assert 'INSERT INTO "m_workspace_event_cursors"' in statements[0][0]
    assert 'UPDATE "m_workspace_event_audience_snapshots_v1"' in statements[0][0]
    assert 'DELETE FROM "m_workspace_events"' in statements[0][0]
    assert 'DELETE FROM "m_workspace_broadcast_message_events_v1"' in (statements[0][0])
    assert "LIMIT %s" in statements[0][0]
    assert "WHERE NOT EXISTS" in statements[1][0]
    assert 'INSERT INTO "m_workspace_event_cursors"' in statements[1][0]
    assert 'MAX(audience."current_epoch_version")' in statements[1][0]
    assert 'MAX(audience."pruned_through_epoch_version")' in statements[1][0]
    assert 'GROUP BY audience."project_id", member."user_uuid"' in statements[1][0]
    assert 'DELETE FROM "m_workspace_event_audience_snapshots_v1"' in statements[2][0]
    assert "WHERE NOT EXISTS" in statements[2][0]
    assert statements[0][1] == (
        now - sql_canonical_store.EVENT_RETENTION,
        now - sql_canonical_store.EVENT_RETENTION,
        sql_canonical_store.EVENT_PRUNE_BATCH_SIZE,
    )


def test_event_retention_has_created_at_leading_index():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0111-index-Messenger-event-retention-cutoff-117285.py"
    ).read_text()

    assert '"created_at", "project_id", "user_uuid", "epoch_version"' in migration
    assert "m_workspace_events_retention_cutoff_idx" in migration
