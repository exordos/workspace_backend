# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Exercise the real previous schema, including hidden flags and queued work."""

import importlib.util
import uuid

import pytest
from psycopg.types import json as pg_json
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.messenger_api.dm import v2_models
from workspace.services.messenger_workers import counters
from workspace.tests.integration import conftest


@pytest.mark.pre_sparse_schema
def test_sparse_migration_preserves_flags_and_rekeys_pending_provider_work(db):
    project, user = uuid.uuid4(), uuid.uuid4()
    stream = conftest.seed_user_stream(db, project, user, "Migration")
    topic = conftest.seed_stream_topic(db, project, stream, user, "Flags", True)
    expected = {name: set() for name in ("unreads", "stars", "pins", "mentions")}
    visible = []
    for index in range(8):
        canonical, placement, binding, state = [uuid.uuid4() for _ in range(4)]
        db.execute(
            "INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,created_at) "
            "VALUES(%s,%s,%s,messenger_resolve_message_content(%s::jsonb,NULL),NOW()+%s*interval '1 second')",
            (
                canonical,
                project,
                user,
                pg_json.Jsonb({"kind": "markdown", "content": f"Message {index}"}),
                index,
            ),
        )
        db.execute(
            "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
            (placement, project, canonical, stream, topic),
        )
        db.execute(
            "INSERT INTO messenger_user_message_bindings(uuid,project_id,user_uuid,placement_uuid,visibility,membership_generation,relation_role,permissions) VALUES(%s,%s,%s,%s,%s,%s,'member','{}'::jsonb)",
            (
                binding,
                project,
                user,
                placement,
                "hidden" if index == 6 else "visible",
                2 if index == 7 else 1,
            ),
        )
        unread, star, pin, mention = (
            index % 2 == 0,
            index % 3 == 0,
            index % 4 == 0,
            index % 5 == 0,
        )
        db.execute(
            "INSERT INTO messenger_user_message_states(uuid,project_id,user_uuid,placement_uuid,read_at,starred,pinned,mentioned,membership_generation) "
            "VALUES(%s,%s,%s,%s,CASE WHEN %s THEN NULL ELSE NOW() END,%s,%s,%s,%s)",
            (
                state,
                project,
                user,
                placement,
                unread,
                star,
                pin,
                mention,
                2 if index == 7 else 1,
            ),
        )
        db.execute(
            "INSERT INTO bridge_zulip_v5_flag_changes(project_id,state_uuid) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (project, state),
        )
        if index != 7:
            for name, value in zip(expected, (unread, star, pin, mention)):
                if value:
                    expected[name].add(placement)
            if index != 6:
                visible.append(binding)
    filename = next(conftest.MIGRATIONS_DIR.glob("0285-Replace-message-states-*.py"))
    spec = importlib.util.spec_from_file_location("sparse_migration", filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
    for name, placements in expected.items():
        assert {
            row[0]
            for row in db.execute(
                f"SELECT placement_uuid FROM messenger_user_message_{name} WHERE project_id=%s AND user_uuid=%s",
                (project, user),
            ).fetchall()
        } == placements
    assert {
        row[0]
        for row in db.execute(
            "SELECT uuid FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s",
            (project, user),
        ).fetchall()
    } == set(visible)
    assert {
        row[0]
        for row in db.execute(
            "SELECT state_uuid FROM bridge_zulip_v5_flag_changes WHERE project_id=%s",
            (project,),
        ).fetchall()
    } == set(visible)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_counter_updates WHERE project_id=%s",
            (project,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute("SELECT to_regclass('messenger_user_message_states')").fetchone()[0]
        is None
    )


@pytest.mark.pre_sparse_schema
def test_sparse_migration_normalizes_the_legacy_members_role_before_recount(db):
    project, user = uuid.uuid4(), uuid.uuid4()
    stream = conftest.seed_user_stream(db, project, user, "Legacy role")
    topic = conftest.seed_stream_topic(db, project, stream, user, "Unread", True)
    db.execute(
        "UPDATE messenger_stream_bindings SET role='members' "
        "WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (project, stream, user),
    )
    stream_filters = {
        "project_id": filters.EQ(project),
        "uuid": filters.EQ(uuid.UUID(stream)),
        "user_uuid": filters.EQ(user),
    }
    # The copied pre-upgrade row already fails typed API/snapshot reads.
    with pytest.raises(TypeError, match="members"):
        with contexts.Context().session_manager() as session:
            v2_models.WorkspaceUserStream.objects.get_all(
                filters=stream_filters, session=session
            )
    filename = next(conftest.MIGRATIONS_DIR.glob("0285-Replace-message-states-*.py"))
    spec = importlib.util.spec_from_file_location("sparse_role_migration", filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
    with contexts.Context().session_manager() as session:
        assert counters.process_pending(session, 10) == 1
    assert db.execute(
        "SELECT next_retry_at FROM messenger_counter_updates "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (project, user, topic),
    ).fetchall() == []
    with contexts.Context().session_manager() as session:
        streams = v2_models.WorkspaceUserStream.objects.get_all(
            filters=stream_filters, session=session
        )
        assert len(streams) == 1
        assert streams[0].role == "member"
