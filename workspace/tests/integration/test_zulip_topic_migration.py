# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as state_tests
from workspace.tests.integration import test_zulip_messages as history_tests


def _source(account, channel, identifier, subject):
    now = datetime.datetime.now(datetime.timezone.utc)
    topic = models.ZulipTopic(
        uuid=uuid.uuid5(channel.stream_uuid, subject),
        stream_id=42,
        name=subject,
        max_id=identifier,
    )
    topic.insert()
    text = f"[Discussion](urn:topic:{topic.uuid})"
    digest = hashlib.sha256(text.encode()).hexdigest()
    content = models.MessageContent(
        zulip_content=text,
        workspace_content=text,
        zulip_content_hash=digest,
        workspace_content_hash=digest,
        zulip_content_updated_at=now,
        workspace_content_updated_at=now,
    )
    content.insert()
    source = models.ZulipMessage(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        message_uuid=uuid.uuid5(
            uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host()), str(identifier)
        ),
        message_id=identifier,
        sender_id=7,
        timestamp=int(now.timestamp()),
        channel=channel,
        topic=topic,
        content=content,
        read=True,
        starred=True,
    )
    source.insert()
    return source


@pytest.mark.isolated_schema
@pytest.mark.parametrize("open_twin", [False, True])
@pytest.mark.parametrize("orphaned", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_existing_checked_topic_migration_preserves_messages_preferences_and_links(
    _database, open_twin, orphaned, native
):
    previous = (
        "0263-Index-Workspace-entity-references-in-canonical-message-content-4272f7.py"
    )
    with conftest.isolated_migration_database(previous) as url:
        with psycopg.connect(url, autocommit=True) as connection:
            owner, project = uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(connection, owner, f"topic-migration-{owner}")
            account = external_models.ExternalAccount(
                project_id=project,
                owner_user_uuid=owner,
                provider="zulip",
                external_account_id="zulip.example.com:7",
                settings={"server_url": "https://zulip.example.com"},
            )
            account.insert()
            channel = history_tests._channel(account)
            checked = _source(account, channel, 40, "✔ General")
            if open_twin:
                _source(account, channel, 41, "General")
            state_tests._prepare(account)
            with contexts.Context().session_manager():
                while models.ZulipMessageState.reconcile_batch(100, project_id=project):
                    pass
            old = checked.topic.uuid
            if native:
                connection.execute(
                    "UPDATE messenger_topics SET source_name='native',source='{\"kind\":\"native\"}' WHERE project_id=%s AND uuid=%s",
                    (project, old),
                )
            canonical = uuid.uuid5(channel.stream_uuid, "General")
            connection.execute(
                "UPDATE messenger_user_topic_bindings SET notification_mode='mute',updated_at='2030-01-01' "
                "WHERE project_id=%s AND topic_uuid=%s",
                (project, old),
            )
            before = connection.execute(
                "SELECT uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
                (project,),
            ).fetchall()
            draft, journal, event = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            connection.execute(
                "INSERT INTO m_workspace_drafts(uuid,project_id,user_uuid,stream_uuid,topic_uuid,payload) "
                'VALUES(%s,%s,%s,%s,%s,\'{"kind":"markdown","content":"Unsent draft"}\'::jsonb)',
                (draft, project, owner, channel.stream_uuid, old),
            )
            connection.execute(
                "INSERT INTO m_workspace_topic_summary_journal(uuid,project_id,topic_uuid,summary,boundary_message_uuid,boundary_message_created_at) "
                "VALUES(%s,%s,%s,'Retained summary',%s,NOW())",
                (journal, project, old, before[0][0]),
            )
            connection.execute(
                "INSERT INTO m_workspace_topic_summary_jobs(project_id,topic_uuid,status,effective_prompt,prompt_fingerprint) "
                "VALUES(%s,%s,'waiting_endpoint','Summarize','test')",
                (project, old),
            )
            connection.execute(
                "INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload) "
                "VALUES(%s,%s,'topic_state_projection','topic',%s,jsonb_build_object('source_kind','topic.updated','topic_uuid',%s::text,'content',%s::text))",
                (event, project, f"{project}:{old}", old, str(old)),
            )
            if orphaned:
                checked.delete()
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(engine.get_latest_migration())
            assert connection.execute(
                "SELECT name,is_done FROM messenger_topics WHERE project_id=%s AND uuid=%s",
                (project, canonical),
            ).fetchone() == ("General", not open_twin)
            assert (
                connection.execute(
                    "SELECT uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
                    (project,),
                ).fetchall()
                == before
            )
            assert connection.execute(
                "SELECT DISTINCT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
                (project,),
            ).fetchall() == [(canonical,)]
            assert connection.execute(
                "SELECT notification_mode FROM messenger_user_topic_bindings WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
                (project, owner, canonical),
            ).fetchone() == ("mute",)
            assert connection.execute(
                "SELECT topic_uuid,payload->>'content' FROM m_workspace_drafts WHERE uuid=%s",
                (draft,),
            ).fetchone() == (canonical, "Unsent draft")
            assert connection.execute(
                "SELECT topic_uuid,summary,boundary_message_uuid FROM m_workspace_topic_summary_journal WHERE uuid=%s",
                (journal,),
            ).fetchone() == (canonical, "Retained summary", before[0][0])
            assert connection.execute(
                "SELECT status FROM m_workspace_topic_summary_jobs WHERE project_id=%s AND topic_uuid=%s",
                (project, canonical),
            ).fetchone() == ("waiting_endpoint",)
            assert connection.execute(
                "SELECT scope_key,payload->>'topic_uuid',payload->>'content' FROM messenger_domain_outbox_events WHERE uuid=%s",
                (event,),
            ).fetchone() == (f"{project}:{canonical}", str(canonical), str(old))
            for task in models.Task.objects.get_all(
                filters={
                    "project_id": filters.EQ(project),
                    "content": filters.JSONFields({"kind": "rewrite_topic_references"}),
                    "status": filters.EQ("new"),
                }
            ):
                with contexts.Context().session_manager():
                    task.execute()
            texts = connection.execute(
                "SELECT payload->>'content' FROM messenger_messages WHERE project_id=%s",
                (project,),
            ).fetchall()
            assert all(str(old) not in text for (text,) in texts)
            assert all(f"urn:topic:{canonical}" in text for (text,) in texts)
