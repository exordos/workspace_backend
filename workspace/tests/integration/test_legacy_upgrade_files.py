# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import hashlib
import json
import uuid
from unittest import mock

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest
from workspace.upgrade import legacy_data


@pytest.mark.parametrize("old_formula", [False, True])
@pytest.mark.parametrize("matching_scope", [False, True])
def test_attachment_handoff_reuses_only_copied_bytes_in_the_same_scope(
    monkeypatch, old_formula, matching_scope
):
    head = migrations.MigrationEngine(
        str(conftest.MIGRATIONS_DIR)
    ).get_latest_migration()
    with conftest.isolated_migration_database(head) as url:
        project, owner, account_uuid, chat_uuid, old_stream = (
            uuid.uuid4() for _ in range(5)
        )
        stream = uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_DNS, "chat.example.test"), "20")
        file_uuid = (
            uuid.uuid5(
                legacy_data.LEGACY_NAMESPACE,
                f"zulip-file:{account_uuid}:{chat_uuid}:/user_uploads/example/file.txt",
            )
            if not old_formula
            else uuid.uuid4()
        )
        body = b"Copied attachment bytes"
        digest = hashlib.sha256(body).hexdigest()
        with psycopg.connect(url, autocommit=True) as db:
            conftest.seed_workspace_user(db, owner, "attachment-owner")
            db.execute(
                "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES(%s,%s)",
                (project, owner),
            )
            db.execute(
                "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name) VALUES(%s,%s,%s,'Files')",
                (stream, project, owner),
            )
            stored_stream = stream if matching_scope else uuid.uuid4()
            if not matching_scope:
                db.execute(
                    "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name) VALUES(%s,%s,%s,'Other private files')",
                    (stored_stream, project, owner),
                )
            db.execute(
                "INSERT INTO m_workspace_files(uuid,project_id,user_uuid,stream_uuid,name,description,content_type,size_bytes,hash,storage_type,storage_id,storage_object_id,acl_mode) VALUES(%s,%s,%s,%s,'file.txt','','text/plain',%s,%s,'file','','copied.txt','stream')",
                (file_uuid, project, owner, stored_stream, len(body), digest),
            )
            db.execute(
                "CREATE TABLE workspace_zulip_v5_legacy_state(kind text,identity_key text,data jsonb,PRIMARY KEY(kind,identity_key))"
            )
            db.execute(
                "INSERT INTO workspace_zulip_v5_legacy_state VALUES('file',%s,%s::jsonb)",
                (str(file_uuid), json.dumps({"uuid": str(file_uuid)})),
            )
        account = mock.Mock(
            uuid=account_uuid,
            project_id=project,
            settings={"server_url": "https://chat.example.test"},
        )
        account.get_host.return_value = "chat.example.test"
        saved = {
            "mapping": [
                {
                    "kind": "stream",
                    "project_id": str(project),
                    "old_uuid": str(old_stream),
                    "new_uuid": str(stream),
                }
            ],
            "chat": [
                {
                    "uuid": str(chat_uuid),
                    "external_account_uuid": str(account_uuid),
                    "project_id": str(project),
                    "projection_stream_uuid": str(old_stream),
                }
            ],
            "file": [{"uuid": str(file_uuid)}],
        }
        downloader = mock.Mock()
        downloader._get.return_value = {
            "url": "https://objects.example.test/temporary-file"
        }
        transport = mock.MagicMock()
        response = (
            transport.__enter__.return_value.get.return_value.__enter__.return_value
        )
        response.iter_content.return_value = [body[:8], body[8:]]
        monkeypatch.setattr(legacy_data.requests, "Session", lambda: transport)
        message = {
            "type": "stream",
            "stream_id": 20,
            "content": "[file](/user_uploads/example/file.txt)",
        }
        with contexts.Context().session_manager() as session:
            assert legacy_data.seed_file_links(
                session, account, [message], saved, downloader
            ) == int(matching_scope)
            assert (
                legacy_data.seed_file_links(
                    session, account, [message], saved, downloader
                )
                == 0
            )
        if matching_scope and not old_formula:
            downloader._get.assert_not_called()
        else:
            downloader._get.assert_called_with("/user_uploads/example/file.txt")
            transport.__enter__.return_value.get.assert_called_with(
                "https://objects.example.test/temporary-file",
                stream=True,
                timeout=(5, 30),
            )
        with psycopg.connect(url) as db:
            assert db.execute("SELECT count(*) FROM m_workspace_files").fetchone() == (
                1,
            )
            assert db.execute(
                "SELECT file_uuid FROM bridge_zulip_v5_file_links"
            ).fetchall() == ([(file_uuid,)] if matching_scope else [])
