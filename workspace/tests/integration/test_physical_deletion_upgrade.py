# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""A queued legacy deletion stays deliverable after its root is purged."""

import json
import uuid

import psycopg
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


def test_upgrade_preserves_pending_delete_cursor_and_prunes_orphan_unreads(_database):
    with conftest.isolated_migration_database(
        "0293-Bound-topic-summary-boundary-lookups-cebf12.py"
    ) as url:
        project = uuid.uuid4()
        users = sorted([uuid.uuid4(), uuid.uuid4()])
        messages = [uuid.uuid4(), uuid.uuid4()]
        event = uuid.uuid4()
        with psycopg.connect(url, autocommit=True) as db:
            stream = conftest.seed_user_stream(
                db, project, users[0], "Deletion upgrade"
            )
            conftest.seed_user_stream_binding(db, project, stream, users[1])
            topic = conftest.seed_stream_topic(
                db, project, stream, users[0], "Upgrade", is_default=True
            )
            for message in messages:
                db.execute(
                    'INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid) VALUES(%s,%s,%s,messenger_resolve_message_content(\'{"kind":"markdown","content":"Upgrade"}\',NULL))',
                    (message, project, users[0]),
                )
                db.execute(
                    "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
                    (message, project, message, stream, topic),
                )
                for user in users:
                    db.execute(
                        "INSERT INTO messenger_user_message_bindings(uuid,project_id,user_uuid,placement_uuid) VALUES(gen_random_uuid(),%s,%s,%s)",
                        (project, user, message),
                    )
                    db.execute(
                        "INSERT INTO messenger_user_message_unreads VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (project, user, message, stream, topic),
                    )
            db.execute(
                "DELETE FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (project, users[1], messages[1]),
            )
            payload = {
                "source_kind": "message.deleted",
                "canonical_message_uuid": messages[0],
                "author_uuid": users[0],
                "source_name": "native",
                "provider_uuid": None,
                "provider_identity": None,
                "placement": {
                    "uuid": messages[0],
                    "stream_uuid": stream,
                    "topic_uuid": topic,
                },
            }
            db.execute(
                "INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload) VALUES(%s,%s,'delivery_snapshot_event','message',%s,%s::jsonb)",
                (
                    event,
                    project,
                    f"{project}:{messages[0]}",
                    json.dumps(payload, default=str),
                ),
            )
            db.execute(
                "UPDATE messenger_projection_tasks SET progress_uuid=%s,processed_count=1 WHERE outbox_event_uuid=%s",
                (users[0], event),
            )
            db.execute(
                "UPDATE messenger_messages SET deleted_at=now() WHERE project_id=%s AND uuid=%s",
                (project, messages[0]),
            )
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(next(conftest.MIGRATIONS_DIR.glob("0294-*")).name)
            payload, progress, processed = db.execute(
                "SELECT payload,progress_uuid,processed_count FROM messenger_projection_tasks WHERE outbox_event_uuid=%s",
                (event,),
            ).fetchone()
            assert payload["recipients"] == [str(users[1])]
            assert progress is None and processed == 0
            assert (
                db.execute(
                    "SELECT 1 FROM messenger_messages WHERE uuid=%s", (messages[0],)
                ).fetchone()
                is None
            )
            assert (
                db.execute(
                    "SELECT 1 FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                    (project, users[1], messages[1]),
                ).fetchone()
                is None
            )
            native._drain()
            assert db.execute(
                "SELECT status FROM messenger_projection_tasks WHERE outbox_event_uuid=%s",
                (event,),
            ).fetchone() == ("completed",)
