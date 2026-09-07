"""Optional sibling-source converter test through the real Provider v2 consumer."""

import copy
import json
import uuid as sys_uuid
import pytest

from workspace.tests.integration import test_history_import as ht
from workspace.tests.integration import test_provider_partial_moves as moves
from workspace.external_bridge_control import provider_data, sql_state
from workspace.messenger_api.dm import external_models
from restalchemy.dm import filters as dm_filters
from workspace.tests.integration import conftest

converter = pytest.importorskip("workspace_zulip_bridge.converter")
provider_protocol = pytest.importorskip("workspace_zulip_bridge.provider_protocol")
ct = pytest.importorskip("workspace_zulip_bridge.tests.test_converter")
history_setup = ht.history_setup


def wire(s, ingress, commands):
    with s.session_factory() as session:
        response = ingress.handle(
            "POST",
            "/api/workspace-provider/v2/commands",
            {},
            json.dumps({"commands": commands}).encode(),
            b"certificate",
            request_session=session,
        )
        assert response.status == 200, response.body
        return json.loads(response.body)


@pytest.mark.parametrize("missing", [False, True])
def test_real_converter_v2_consumer(history_setup, missing):
    s = history_setup
    ingress, _, _ = ht._history_v1_ingress(s)
    store = ct.FakeStore(
        account_uuid=str(s.account),
        owner_uuid=str(s.owner),
        project_uuid=str(s.project),
    )
    store.assignments["channel:42"] = {
        "uuid": str(s.chat),
        "generation": 1,
        "project_id": str(s.project),
        "selected": True,
    }
    store.remember_provider_mapping(
        str(s.account),
        "identity",
        "7",
        str(s.owner),
        {"display_name": "Synthetic Owner"},
    )
    message = {
        **ct._stream_message(message_id=1, subject="Import"),
        "sender_id": 7,
        "sender_full_name": "Synthetic Owner",
    }
    full = converter.event_records(
        store,
        str(s.account),
        "queue",
        {"id": 1, "type": "message", "message": message},
        original_url="https://zulip.example.test",
    )
    kinds = {"identity.upsert", "topic.upsert", "message.create", "message.update"}
    commands = [
        provider_protocol.command_payload(store, r)
        for r in full
        if r["operation"]["kind"] in kinds
    ]
    for command in commands:
        if command["kind"] == "message.upsert":
            command["provider_sequence"] = "1700000000"
    wire(
        s,
        ingress,
        [c for c in commands if c["kind"] != "message.upsert"] if missing else commands,
    )
    if not missing:
        # The v2 translator must preserve the recovery revision fence too.
        before_recovery = moves.snapshot(s)
        for sequence in (None, "unversioned", "1700000000"):
            recovery = copy.deepcopy(
                next(c for c in commands if c["kind"] == "message.upsert")
            )
            recovery["provider_event_key"] = str(sys_uuid.uuid4())
            recovery["provider_sequence"] = sequence
            recovery["payload"]["provider_metadata"]["missing_base_recovery"] = True
            recovery["payload"]["payload"]["content"] = "Unordered recovery snapshot"
            with pytest.raises(provider_data.ProviderBatchError):
                wire(s, ingress, [recovery])
            assert moves.snapshot(s) == before_recovery
    store.provider_mapping(str(s.account), "topic", "42:Moved")["metadata"].update(
        stream_uuid=store.provider_mapping(str(s.account), "stream", "channel:42")[
            "workspace_uuid"
        ],
        name="Moved",
    )
    topic = conftest.seed_stream_topic(s.db, s.project, s.stream, s.owner, "Moved")
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_chats_v2 SET source=jsonb_set(source,'{topics}',(source->'topics') || %s::jsonb),revision=revision+1 WHERE uuid=%s",
            (
                json.dumps(
                    [
                        {
                            "topic_uuid": topic,
                            "provider_topic_id": "42:Moved",
                            "name": "Moved",
                        }
                    ]
                ),
                s.chat,
            ),
        )
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(s.chat)}, session=session
        )
        sql_state.append_upsert(
            session,
            s.bridge,
            "zulip",
            sql_state.external_chat_assignment_desired(chat, session=session),
        )
    store.auto_materialize = False
    event = {
        "id": 2,
        "type": "update_message",
        "message_id": 1,
        "message_ids": [1],
        "stream_id": 42,
        "orig_subject": "Import",
        "subject": "Moved",
        "propagate_mode": "change_one",
        "edit_timestamp": 1700000010,
    }
    partial = converter.event_records(
        store, str(s.account), "queue", event, original_url="https://zulip.example.test"
    )
    commands = [
        provider_protocol.command_payload(store, r)
        for r in partial
        if r["operation"]["kind"] in kinds
    ]
    wire(s, ingress, [c for c in commands if c["kind"] != "message.upsert"])
    updates = [c for c in commands if c["kind"] == "message.upsert"]
    assert len(updates) == 1 and "payload" not in updates[0]["payload"]
    assert (
        updates[0]["payload"]["provider_metadata"]["provider_original_url"]
        == "https://zulip.example.test/#narrow/near/1"
    )
    if missing:
        with pytest.raises(provider_data.ProviderMessageBaseMissingError):
            wire(s, ingress, updates)
        assert moves.snapshot(s)["canonical"] == []
    else:
        wire(s, ingress, updates)
        after = moves.snapshot(s)
        assert after["legacy"][0]["payload"]["content"] == "hello"
        wire(s, ingress, updates)
        assert moves.snapshot(s) == after
