# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import importlib.util
import itertools
import uuid

import pytest
from restalchemy.common import contexts

from workspace.tests.integration import conftest


FLAGS = ("read", "pinned", "starred", "mentioned")
TABLES = ("unreads", "pins", "stars", "mentions")


@pytest.fixture
def flag_messages(api, db):
    stream = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Flags")
    topic = conftest.seed_stream_topic(
        db, api.project_id, stream, api.user_uuid, "Flags", is_default=True
    )
    expected = {}
    for bits in range(16):
        message, placement = uuid.uuid4(), uuid.uuid4()
        db.execute(
            "INSERT INTO messenger_messages(uuid,project_id,author_uuid,content_uuid,created_at) "
            "VALUES(%s,%s,%s,messenger_resolve_message_content("
            '\'{"kind":"markdown","content":"Flags"}\'::jsonb,NULL),'
            "timestamp '2026-01-01')",
            (message, api.project_id, api.user_uuid),
        )
        db.execute(
            "INSERT INTO messenger_message_placements"
            "(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
            (placement, api.project_id, message, stream, topic),
        )
        db.execute(
            "INSERT INTO messenger_user_message_bindings"
            "(uuid,project_id,user_uuid,placement_uuid) VALUES(%s,%s,%s,%s)",
            (uuid.uuid4(), api.project_id, api.user_uuid, placement),
        )
        flags = [bool(bits & (1 << bit)) for bit in range(4)]
        for table, present in zip(TABLES, flags):
            if present:
                db.execute(
                    f"INSERT INTO messenger_user_message_{table} "
                    "(project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (api.project_id, api.user_uuid, placement, stream, topic),
                )
        flags[0] = not flags[0]
        expected[str(placement)] = dict(zip(FLAGS, flags))
    return stream, topic, expected


def _page(api, **params):
    response = api.get("/v1/messenger/messages/", params=params)
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_all_sparse_flag_filter_combinations_keep_their_http_results(
    api, flag_messages, direction
):
    stream, topic, expected = flag_messages
    for values in itertools.product((None, False, True), repeat=4):
        filters = {
            name: value for name, value in zip(FLAGS, values) if value is not None
        }
        response = _page(
            api,
            stream_uuid=stream,
            topic_uuid=topic,
            sort_key="created_at",
            sort_dir=direction,
            page_limit=100,
            **{name: str(value).lower() for name, value in filters.items()},
        )
        rows = response.json()
        wanted = sorted(
            (
                identifier
                for identifier, flags in expected.items()
                if all(flags[name] == value for name, value in filters.items())
            ),
            reverse=direction == "desc",
        )
        assert [row["uuid"] for row in rows] == wanted, filters
        assert all(
            {name: row[name] for name in FLAGS} == expected[row["uuid"]] for row in rows
        )


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sparse_mention_pages_keep_equal_timestamp_keysets(
    api, flag_messages, direction
):
    _stream, _topic, expected = flag_messages
    wanted = sorted(
        (
            key
            for key, flags in expected.items()
            if flags["mentioned"] and not flags["read"]
        ),
        reverse=direction == "desc",
    )
    params = {
        "mentioned": "true",
        "read": "false",
        "sort_key": "created_at",
        "sort_dir": direction,
        "page_limit": 2,
    }
    seen = []
    for _ in range(3):
        response = _page(api, **params)
        seen.extend(row["uuid"] for row in response.json())
        marker = response.headers.get("X-Pagination-Marker")
        if marker is None:
            break
        params["page_marker"] = marker
    assert seen == wanted
    assert len(seen) == 4
    assert marker is None


@pytest.mark.parametrize(
    "case",
    [
        "deleted_message",
        "deleted_topic",
        "deleted_stream",
        "inactive_membership",
        "other_user",
        "other_project",
    ],
)
def test_sparse_mention_pages_preserve_access_and_deletion_checks(
    api, db, flag_messages, case
):
    stream, topic, _expected = flag_messages
    if case.startswith("deleted_"):
        table, column, value = {
            "deleted_message": ("messenger_messages", "uuid", None),
            "deleted_topic": ("messenger_topics", "uuid", topic),
            "deleted_stream": ("messenger_streams", "uuid", stream),
        }[case]
        if value is None:
            db.execute(
                "UPDATE messenger_messages SET deleted_at=now() WHERE project_id=%s",
                (api.project_id,),
            )
        else:
            db.execute(
                f"UPDATE {table} SET deleted_at=now() WHERE project_id=%s AND {column}=%s",
                (api.project_id, value),
            )
    elif case == "inactive_membership":
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false "
            "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (api.project_id, api.user_uuid, stream),
        )
    params = {"mentioned": "true", "read": "false", "page_limit": 100}
    scope = {"user": uuid.uuid4()} if case == "other_user" else {}
    if case == "other_project":
        scope["project"] = uuid.uuid4()
    response = api.get("/v1/messenger/messages/", params=params, **scope)
    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.isolated_schema
@pytest.mark.parametrize(
    "migration_prefix",
    ["0289-Expose-sparse-message-flags-", "0290-Keep-message-view-history-joins-"],
)
def test_sparse_flag_view_upgrade_and_downgrade_preserve_existing_rows(
    api, db, flag_messages, migration_prefix
):
    _stream, _topic, _expected = flag_messages
    path = next(conftest.MIGRATIONS_DIR.glob(migration_prefix + "*.py"))
    spec = importlib.util.spec_from_file_location("sparse_view_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    query = (
        "SELECT * FROM messenger_api_user_messages_v1 WHERE project_id=%s ORDER BY uuid"
    )
    expected = db.execute(query, (api.project_id,)).fetchall()
    assert len(expected) == 16
    for operation in ("downgrade", "upgrade"):
        with contexts.Context().session_manager() as session:
            getattr(migration.migration_step, operation)(session)
        assert db.execute(query, (api.project_id,)).fetchall() == expected
        assert len(_page(api, page_limit=100).json()) == 16
