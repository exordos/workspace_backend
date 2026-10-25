# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""A visible legacy DM must remain visible when duplicate copies merge."""

import uuid

import psycopg
import pytest
from restalchemy.common import contexts

from migrations import zulip_upgrade_data
from workspace.tests.integration import conftest


@pytest.mark.parametrize(
    "archived,expected",
    [
        ([True, True, False], False),
        ([False, True, True], False),
        ([True, True, True], True),
    ],
)
@pytest.mark.parametrize("destination_exists", [False, True])
def test_merge_preserves_unarchived_direct_conversation(
    http_server, archived, expected, destination_exists
):
    # Exercise the published rekey helper on its last physical-payload schema.
    head = next(conftest.MIGRATIONS_DIR.glob("0272-*.py")).name
    with conftest.isolated_migration_database(head) as url:
        project, owner, peer = (uuid.uuid4() for _ in range(3))
        streams = [uuid.UUID(int=index) for index in range(1, 4)]
        destination = streams[0] if destination_exists else uuid.UUID(int=4)
        with psycopg.connect(url, autocommit=True) as db:
            for user in (owner, peer):
                conftest.seed_workspace_user(db, user, str(user))
                db.execute(
                    "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES(%s,%s)",
                    (project, user),
                )
            for stream, is_archived in zip(streams, archived):
                db.execute(
                    "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name,private,direct_user_uuid,is_archived) "
                    "VALUES(%s,%s,%s,'Legacy DM',true,%s,%s)",
                    (stream, project, owner, peer, is_archived),
                )
                db.execute(
                    "INSERT INTO messenger_stream_bindings(uuid,project_id,stream_uuid,user_uuid,who_uuid) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (uuid.uuid4(), project, stream, owner, owner),
                )
        mapping = {
            ("stream", str(project), str(stream)): str(destination)
            for stream in streams
        }
        with contexts.Context().session_manager() as session:
            zulip_upgrade_data.rekey(session, mapping)
        response = conftest.ApiClient(http_server, owner, project).get(
            "/v1/messenger/streams/" + str(destination)
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_archived"] is expected
