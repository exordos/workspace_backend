"""Provider partial moves through the real batch transaction and SQL guards."""

import copy
import json
import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import provider_data, provider_event_apply
from workspace.external_bridge_control import sql_state
from workspace.history_import import contract
from workspace.tests.integration import conftest
from workspace.tests.integration import test_history_import as history_tests
from workspace.messenger_api.dm import external_models
from restalchemy.dm import filters as dm_filters

history_setup = history_tests.history_setup


def apply(s, ingress, event):
    with s.session_factory() as session:
        response = ingress.handle(
            "POST",
            "/api/workspace-provider/v1/events",
            {},
            contract.encode({"events": [event]}),
            b"certificate",
            request_session=session,
        )
        assert response.status == 200, response.body
        return json.loads(response.body)


def snapshot(s):
    with s.session_factory() as session:
        return {
            "legacy": session.execute(
                "SELECT uuid,topic_uuid,stream_uuid,created_at,payload,provider_metadata FROM m_workspace_messages WHERE project_id=%s ORDER BY uuid",
                (s.project,),
            ).fetchall(),
            "canonical": session.execute(
                "SELECT uuid,provider_realm_uuid,provider_message_id,created_at,payload,deleted_at FROM messenger_messages WHERE project_id=%s ORDER BY uuid",
                (s.project,),
            ).fetchall(),
            "placements": session.execute(
                "SELECT uuid,message_uuid,stream_uuid,topic_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
                (s.project,),
            ).fetchall(),
            "events": session.execute(
                "SELECT uuid FROM messenger_domain_outbox_events WHERE project_id=%s ORDER BY uuid",
                (s.project,),
            ).fetchall(),
        }


def setup_move(s):
    ingress, event, _desired = history_tests._history_v1_ingress(s)
    apply(s, ingress, event)
    before = snapshot(s)
    topic = conftest.seed_stream_topic(s.db, s.project, s.stream, s.owner, "Moved")
    move = copy.deepcopy(event)
    move["provider_event_uuid"] = str(sys_uuid.uuid4())
    move["provider_sequence"] = "2"
    resource = move["payload"]["resource"]
    resource.pop("payload")
    resource.pop("read")
    resource["provider_metadata"] = {}
    resource["topic_uuid"] = str(topic)
    return ingress, move, before


def test_partial_move_old_producer_passes_real_identity_guard_and_replay(history_setup):
    s = history_setup
    ingress, event, before = setup_move(s)
    first = apply(s, ingress, event)
    after = snapshot(s)
    assert after["legacy"][0]["topic_uuid"] == sys_uuid.UUID(
        event["payload"]["resource"]["topic_uuid"]
    )
    assert after["legacy"][0]["payload"] == before["legacy"][0]["payload"]
    assert after["legacy"][0]["created_at"] == before["legacy"][0]["created_at"]
    assert (
        after["legacy"][0]["provider_metadata"]["provider_original_url"]
        == before["legacy"][0]["provider_metadata"]["provider_original_url"]
    )
    assert len(after["canonical"]) == len(after["placements"]) == 1
    assert after["canonical"][0]["uuid"] == before["canonical"][0]["uuid"]
    assert after["canonical"][0]["provider_message_id"] == "1"
    repeated = apply(s, ingress, event)
    assert not first["results"][0]["duplicate"]
    assert repeated["results"][0]["duplicate"]
    assert snapshot(s) == after
    equivalent = copy.deepcopy(event)
    equivalent["provider_event_uuid"] = str(sys_uuid.uuid4())
    apply(s, ingress, equivalent)
    assert snapshot(s) == after


def test_explicit_null_link_rolls_back_move_through_sql_guard(history_setup):
    s = history_setup
    ingress, event, _before = setup_move(s)
    before = snapshot(s)
    event["payload"]["resource"]["provider_metadata"]["provider_original_url"] = None
    with pytest.raises(Exception) as error:
        apply(s, ingress, event)
    cause = error.value
    states = set()
    while cause is not None:
        states.add(getattr(cause, "sqlstate", None))
        states.add(getattr(cause, "code", None))
        cause = cause.__cause__ or cause.__context__
    assert "23514" in states
    assert snapshot(s) == before


def test_missing_base_is_classified_and_rolls_back_batch(history_setup):
    s = history_setup
    ingress, event, _desired = history_tests._history_v1_ingress(s)
    event["payload"]["resource"].pop("payload")
    with pytest.raises(provider_data.ProviderMessageBaseMissingError) as error:
        apply(s, ingress, event)
    assert error.value.error == "provider_message_base_missing"
    assert snapshot(s)["canonical"] == []
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT 1 FROM m_external_provider_events_v1 WHERE provider_event_uuid=%s",
                (event["provider_event_uuid"],),
            ).fetchone()
            is None
        )


def test_full_recovery_respects_history_tombstone(history_setup):
    s = history_setup
    ingress, event, _desired = history_tests._history_v1_ingress(s)
    event["payload"]["resource"]["provider_metadata"]["missing_base_recovery"] = True
    with s.session_factory() as session:
        session.execute(
            "INSERT INTO m_external_history_tombstones_v1 (provider_realm_uuid,provider_message_id) VALUES (%s,'1')",
            (s.realm,),
        )
    apply(s, ingress, event)
    assert snapshot(s)["canonical"] == []


@pytest.mark.parametrize("cross_project", [False, True])
def test_partial_move_between_streams_preserves_identity_and_recipients(
    history_setup, cross_project
):
    s = history_setup
    ingress, event, before = setup_move(s)
    destination = sys_uuid.uuid4() if cross_project else s.project
    stream = sys_uuid.UUID(
        conftest.seed_user_stream(s.db, destination, s.owner, "Destination")
    )
    topic = sys_uuid.UUID(
        conftest.seed_stream_topic(
            s.db, destination, stream, s.owner, "Moved", is_default=True
        )
    )
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_chats_v2 SET project_id=%s,projection_stream_uuid=%s,revision=revision+1,source=jsonb_set(source,'{topics,0,topic_uuid}',to_jsonb(%s::text)) WHERE uuid=%s",
            (destination, stream, str(topic), s.chat),
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
    event["project_id"] = str(destination)
    event["payload"]["resource"].update(stream_uuid=str(stream), topic_uuid=str(topic))
    apply(s, ingress, event)
    with s.session_factory() as session:
        rows = session.execute(
            "SELECT uuid,project_id,stream_uuid,topic_uuid,payload,created_at,provider_metadata FROM m_workspace_messages WHERE uuid=%s",
            (event["payload"]["resource"]["uuid"],),
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert (row["project_id"], row["stream_uuid"], row["topic_uuid"]) == (
            destination,
            stream,
            topic,
        )
        assert row["created_at"] == before["legacy"][0]["created_at"]
        assert row["payload"] == before["legacy"][0]["payload"]
        assert (
            row["provider_metadata"]["provider_original_url"]
            == before["legacy"][0]["provider_metadata"]["provider_original_url"]
        )
        canonical = session.execute(
            "SELECT uuid,project_id FROM messenger_messages WHERE provider_realm_uuid=%s AND provider_message_id='1'",
            (s.realm,),
        ).fetchall()
        assert len(canonical) == 1
        assert canonical[0]["uuid"] == before["canonical"][0]["uuid"]
        placements = session.execute(
            "SELECT stream_uuid,topic_uuid FROM messenger_message_placements WHERE message_uuid=%s",
            (canonical[0]["uuid"],),
        ).fetchall()
        assert placements == [{"stream_uuid": stream, "topic_uuid": topic}]
        assert provider_event_apply._canonical_message_recipients(
            session, destination, sys_uuid.UUID(event["payload"]["resource"]["uuid"])
        ) == [s.owner]
        if cross_project:
            assert (
                provider_event_apply._canonical_message_recipients(
                    session,
                    s.project,
                    sys_uuid.UUID(event["payload"]["resource"]["uuid"]),
                )
                == []
            )


@pytest.mark.parametrize("sequence", ["0", "1", None, "unversioned", "2"])
def test_recovery_snapshot_cannot_regress_live_state(history_setup, sequence):
    s = history_setup
    ingress, event, _desired = history_tests._history_v1_ingress(s)
    apply(s, ingress, event)
    before = snapshot(s)
    event["provider_event_uuid"] = str(sys_uuid.uuid4())
    event["provider_sequence"] = sequence
    event["payload"]["resource"]["payload"]["content"] = "Obsolete fetched snapshot"
    event["payload"]["resource"]["provider_metadata"]["missing_base_recovery"] = True
    if sequence in ("1", None, "unversioned"):
        with pytest.raises(provider_data.ProviderBatchError):
            apply(s, ingress, event)
    else:
        apply(s, ingress, event)
    if sequence == "2":
        assert (
            snapshot(s)["legacy"][0]["payload"]["content"]
            == "Obsolete fetched snapshot"
        )
    else:
        assert snapshot(s) == before


@pytest.mark.parametrize("invalid", ["read", "topic", "stream", "created_at", "source"])
def test_invalid_partial_destination_is_not_classified_as_missing_base(
    history_setup, invalid
):
    s = history_setup
    ingress, event, _desired = history_tests._history_v1_ingress(s)
    resource = event["payload"]["resource"]
    resource.pop("payload")
    if invalid == "source":
        resource.update(
            source_name="zulip", source={"kind": "zulip", "stream_id": "bad"}
        )
    else:
        resource[
            {
                "read": "read",
                "topic": "topic_uuid",
                "stream": "stream_uuid",
                "created_at": "created_at",
            }[invalid]
        ] = str(sys_uuid.uuid4()) if invalid in {"topic", "stream"} else "bad"
    with pytest.raises(provider_data.ProviderBatchError) as error:
        apply(s, ingress, event)
    assert error.value.error == "provider_event_batch_rejected"
    assert snapshot(s)["canonical"] == []


def test_explicit_valid_link_wins_through_real_guard(history_setup):
    s = history_setup
    ingress, event, _before = setup_move(s)
    link = "https://zulip.example.test/#narrow/near/1"
    event["payload"]["resource"]["provider_metadata"]["provider_original_url"] = link
    apply(s, ingress, event)
    assert (
        snapshot(s)["legacy"][0]["provider_metadata"]["provider_original_url"] == link
    )


@pytest.mark.parametrize("message_id", [None, 1])
def test_actual_guard_accepts_message_source_without_requiring_link(
    history_setup, message_id
):
    s = history_setup
    ingress, event, _desired = history_tests._history_v1_ingress(s)
    apply(s, ingress, event)

    def change_identity():
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_workspace_messages SET source=jsonb_set(source,'{message_id}',%s::jsonb),provider_metadata=provider_metadata-'provider_original_url' WHERE project_id=%s AND uuid=%s",
                (
                    json.dumps(message_id),
                    s.project,
                    event["payload"]["resource"]["uuid"],
                ),
            )

    before = snapshot(s)
    if message_id is None:
        with pytest.raises(Exception) as error:
            change_identity()
        assert getattr(error.value, "sqlstate", None) == "23514"
        assert snapshot(s) == before
    else:
        change_identity()
        assert len(snapshot(s)["canonical"]) == 1
        assert (
            "provider_original_url" not in snapshot(s)["legacy"][0]["provider_metadata"]
        )


def test_live_missing_base_then_real_history_import_then_move(history_setup):
    s = history_setup
    ingress, event, desired = history_tests._history_v1_ingress(s)
    resource = event["payload"]["resource"]
    resource.pop("payload")
    with pytest.raises(provider_data.ProviderMessageBaseMissingError):
        apply(s, ingress, event)
    envelope = history_tests.envelope(s, count=1)
    envelope["sources"][0]["assignment_generation"] = desired["generation"]
    job = history_tests.accept(s, envelope)
    assert history_tests.finish(s, job)["status"] == "complete"
    with s.session_factory() as session:
        row = session.execute(
            "SELECT uuid FROM m_workspace_messages WHERE project_id=%s AND provider_external_id='1'",
            (s.project,),
        ).fetchone()
    # A v1 producer synchronizes the server reference before a new operation.
    resource["uuid"] = str(row["uuid"])
    resource["topic_uuid"] = conftest.seed_stream_topic(
        s.db, s.project, s.stream, s.owner, "After import"
    )
    resource["provider_metadata"] = {
        "provider_original_url": "https://zulip.example.test/#narrow/near/1"
    }
    event["provider_event_uuid"] = str(sys_uuid.uuid4())
    apply(s, ingress, event)
    result = snapshot(s)
    assert len(result["canonical"]) == 1
    assert result["legacy"][0]["topic_uuid"] == sys_uuid.UUID(resource["topic_uuid"])


def test_partial_move_retains_link_with_canonical_legacy_realm_proof(history_setup):
    s = history_setup
    ingress, event, _ = setup_move(s)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_workspace_messages SET provider_metadata=provider_metadata-'provider_realm_uuid' WHERE project_id=%s AND uuid=%s",
            (s.project, event["payload"]["resource"]["uuid"]),
        )
    before = snapshot(s)
    assert "provider_realm_uuid" not in before["legacy"][0]["provider_metadata"]
    assert before["canonical"][0]["provider_realm_uuid"] == s.realm
    apply(s, ingress, event)
    after = snapshot(s)
    assert (
        after["legacy"][0]["provider_metadata"]["provider_original_url"]
        == before["legacy"][0]["provider_metadata"]["provider_original_url"]
    )
    assert after["legacy"][0]["topic_uuid"] == sys_uuid.UUID(
        event["payload"]["resource"]["topic_uuid"]
    )
    assert after["legacy"][0]["payload"] == before["legacy"][0]["payload"]
    assert after["canonical"][0]["uuid"] == before["canonical"][0]["uuid"]
