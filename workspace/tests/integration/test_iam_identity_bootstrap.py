# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Current IAM UUIDs must never adopt a historical user by profile metadata."""

import uuid

from workspace.tests.integration import conftest


def test_first_request_with_historical_username_keeps_distinct_identities(
    http_server, db
):
    current_user = uuid.uuid4()
    historical_user = uuid.uuid4()
    project = uuid.uuid4()
    username = f"user-{current_user}"
    email = f"{current_user}@example.invalid"
    conftest.seed_workspace_user(db, historical_user, username)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_workspace_users SET email=%s WHERE uuid=%s",
            (email, historical_user),
        )
    client = conftest.ApiClient(http_server, current_user, project)

    response = client.post(
        "/v1/messenger/streams/",
        json={"name": "Current IAM identity", "source_name": "native"},
        headers={"X-Test-User-Email": email},
    )

    assert response.status_code == 201, response.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid,username,email FROM m_workspace_users WHERE uuid=ANY(%s::uuid[]) ORDER BY uuid",
            ([current_user, historical_user],),
        )
        assert cursor.fetchall() == sorted(
            [(current_user, username, email), (historical_user, username, email)]
        )
        cursor.execute(
            "SELECT user_uuid FROM messenger_project_users WHERE project_id=%s",
            (project,),
        )
        assert cursor.fetchall() == [(current_user,)]
    assert response.json()["source_name"] == "native"


def test_iam_username_change_does_not_adopt_another_user(http_server, db):
    current_user = uuid.uuid4()
    historical_user = uuid.uuid4()
    project = uuid.uuid4()
    username = f"user-{current_user}"
    email = f"{current_user}@example.invalid"
    conftest.seed_workspace_user(db, current_user, f"previous-{current_user}")
    conftest.seed_workspace_user(db, historical_user, username)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_workspace_users SET email=%s WHERE uuid=%s",
            (email, historical_user),
        )
    client = conftest.ApiClient(http_server, current_user, project)

    response = client.get("/v1/me/", headers={"X-Test-User-Email": email})

    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == str(current_user)
    assert response.json()["username"] == username
    assert response.json()["email"] == email
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT username,email FROM m_workspace_users WHERE uuid=%s",
            (historical_user,),
        )
        assert cursor.fetchone() == (username, email)


def test_equal_username_does_not_inherit_historical_private_membership(http_server, db):
    historical_user = uuid.uuid4()
    current_user = uuid.uuid4()
    project = uuid.uuid4()
    historical_client = conftest.ApiClient(http_server, historical_user, project)
    response = historical_client.post(
        "/v1/messenger/streams/",
        json={"name": "Historical private", "invite_only": True},
    )
    assert response.status_code == 201, response.text
    stream = response.json()["uuid"]
    email = f"{current_user}@example.invalid"
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_workspace_users SET username=%s,email=%s WHERE uuid=%s",
            (f"user-{current_user}", email, historical_user),
        )
    client = conftest.ApiClient(http_server, current_user, project)

    response = client.get(
        "/v1/messenger/streams/", headers={"X-Test-User-Email": email}
    )

    assert response.status_code == 200, response.text
    assert stream not in {item["uuid"] for item in response.json()}
    assert client.get(f"/v1/messenger/streams/{stream}").status_code == 404
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT user_uuid FROM messenger_stream_bindings WHERE stream_uuid=%s AND active",
            (uuid.UUID(stream),),
        )
        assert cursor.fetchall() == [(historical_user,)]


def test_workspace_user_has_only_uuid_as_unique_identity_key(db):
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT ARRAY(
                SELECT a.attname
                FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, n)
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                ORDER BY k.n
            )
            FROM pg_index i
            WHERE i.indrelid='m_workspace_users'::regclass AND i.indisunique
            """
        )
        assert cursor.fetchall() == [(["uuid"],)]
