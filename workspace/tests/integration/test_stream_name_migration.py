# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import uuid

import psycopg
from psycopg import sql
from psycopg import rows
import pytest
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest


MIGRATION = "0299-Expand-stream-names-to-2048-characters-400671.py"
PREVIOUS_MIGRATION = (
    "0298-Restrict-Zulip-default-labels-to-private-conversations-141e3a.py"
)


@pytest.fixture
def delegated_roles(_database):
    names = ["cassi_grant_" + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(name)))
        try:
            yield names
        finally:
            for name in reversed(names):
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def _views(connection):
    return connection.execute(
        "SELECT relation.relname,pg_get_viewdef(relation.oid),relation.reloptions,"
        "relation.relowner,COALESCE(relation.relacl,acldefault('r',relation.relowner))::text,"
        "(SELECT jsonb_agg(jsonb_build_array(a.attname,a.attacl::text) ORDER BY a.attnum) "
        " FROM pg_attribute a WHERE a.attrelid=relation.oid AND a.attnum>0 "
        " AND NOT a.attisdropped AND a.attacl IS NOT NULL) "
        "FROM pg_class relation JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace "
        "WHERE namespace.nspname='public' AND relation.relkind='v' ORDER BY relation.relname"
    ).fetchall()


@pytest.mark.isolated_schema
@pytest.mark.parametrize("storage_prepared", [False, True])
def test_stream_name_migration_preserves_rows_views_and_rejects_lossy_downgrade(
    storage_prepared,
):
    with conftest.isolated_migration_database(PREVIOUS_MIGRATION) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            project, user = uuid.uuid4(), uuid.uuid4()
            stream = conftest.seed_user_stream(connection, project, user, "Existing")
            connection.execute(
                "CREATE VIEW stream_name_reader WITH (security_barrier=true,security_invoker=true) "
                "AS SELECT uuid,name FROM messenger_api_user_streams_v1; "
                "GRANT SELECT ON stream_name_reader TO PUBLIC; "
                "REVOKE INSERT ON stream_name_reader FROM CURRENT_USER; "
                "GRANT SELECT (name) ON stream_name_reader TO PUBLIC; "
                "GRANT UPDATE (name) ON stream_name_reader TO CURRENT_USER WITH GRANT OPTION"
            )
            views = _views(connection)
            assert next(row[-1] for row in views if row[0] == "stream_name_reader")
            expanded_views = [
                (
                    row[0],
                    row[1].replace(
                        "::character varying(255) AS name",
                        "::character varying(2048) AS name",
                    )
                    if row[0] == "messenger_api_user_streams_v1"
                    else row[1],
                    *row[2:],
                )
                for row in views
            ]
            name = "\N{CYRILLIC SMALL LETTER YA}" * 2048
            update = "UPDATE messenger_streams SET name=%s WHERE uuid=%s"
            with pytest.raises(psycopg.errors.StringDataRightTruncation):
                connection.execute(update, (name, stream))
            if storage_prepared:
                spec = importlib.util.spec_from_file_location(
                    "stream_name_migration", conftest.MIGRATIONS_DIR / MIGRATION
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with connection.transaction():
                    with connection.cursor(row_factory=rows.dict_row) as session:
                        module._alter_name(session, 2048, expand_view=False)
                assert _views(connection) == views
                connection.execute(update, (name, stream))
                assert connection.execute(
                    "SELECT name FROM messenger_api_user_streams_v1 WHERE uuid=%s",
                    (stream,),
                ).fetchone() == (name[:255],)
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(MIGRATION)
            assert _views(connection) == expanded_views
            assert connection.execute(
                "SELECT name FROM messenger_streams WHERE uuid=%s", (stream,)
            ).fetchone() == (name if storage_prepared else "Existing",)
            connection.execute(update, (name, stream))
            assert connection.execute(
                "SELECT name FROM messenger_api_user_streams_v1 WHERE uuid=%s",
                (stream,),
            ).fetchone() == (name,)
            with pytest.raises(psycopg.errors.StringDataRightTruncation):
                connection.execute(update, (name + "x", stream))
            with pytest.raises(ValueError, match="Cannot narrow stream names"):
                engine.rollback_migration(MIGRATION)
            assert _views(connection) == expanded_views
            assert connection.execute(
                "SELECT name FROM messenger_streams WHERE uuid=%s", (stream,)
            ).fetchone() == (name,)
            connection.execute(update, ("Existing", stream))
            engine.rollback_migration(MIGRATION)
            assert _views(connection) == views
            with pytest.raises(psycopg.errors.StringDataRightTruncation):
                connection.execute(update, ("x" * 256, stream))
            engine.apply_migration(MIGRATION)
            connection.execute(update, (name, stream))


@pytest.mark.isolated_schema
def test_stream_name_migration_refuses_to_change_delegated_grant_chains(
    delegated_roles,
):
    with conftest.isolated_migration_database(PREVIOUS_MIGRATION) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            owner, recipient = map(sql.Identifier, delegated_roles)
            connection.execute(
                "CREATE VIEW stream_name_reader AS SELECT name FROM messenger_streams"
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {},{}").format(
                    owner, recipient
                )
            )
            connection.execute(
                sql.SQL(
                    "GRANT SELECT (name) ON stream_name_reader TO {} WITH GRANT OPTION"
                ).format(owner)
            )
            connection.execute(sql.SQL("SET ROLE {}").format(owner))
            connection.execute(
                sql.SQL("GRANT SELECT (name) ON stream_name_reader TO {}").format(
                    recipient
                )
            )
            connection.execute("RESET ROLE")
            views = _views(connection)
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            with pytest.raises(ValueError, match="delegated view grants"):
                engine.apply_migration(MIGRATION)
            assert _views(connection) == views
            assert connection.execute(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name='messenger_streams' AND column_name='name'"
            ).fetchone() == (255,)
            assert connection.execute(
                "SELECT has_column_privilege(%s,'stream_name_reader','name','SELECT')",
                (delegated_roles[1],),
            ).fetchone() == (True,)
            connection.execute(
                sql.SQL(
                    "REVOKE GRANT OPTION FOR SELECT (name) ON stream_name_reader FROM {} CASCADE"
                ).format(owner)
            )
            assert connection.execute(
                "SELECT has_column_privilege(%s,'stream_name_reader','name','SELECT')",
                (delegated_roles[1],),
            ).fetchone() == (False,)


@pytest.mark.parametrize("length", [255, 256, 2048, 2049])
def test_stream_api_name_limit_and_round_trip(api, length):
    name = "\N{CYRILLIC SMALL LETTER YA}" * length
    path = "/v1/messenger/streams/"
    response = api.post(path, json={"name": name, "source_name": "native"})
    if length > 2048:
        assert response.status_code == 400, response.text
        return
    assert response.status_code == 201, response.text
    stream = response.json()
    assert stream["name"] == name
    stored = api.get(path + stream["uuid"])
    assert stored.status_code == 200, stored.text
    assert stored.json()["name"] == name
    updated = api.put(path + stream["uuid"], json={"name": "a" * 2048})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "a" * 2048
    rejected = api.put(path + stream["uuid"], json={"name": "a" * 2049})
    assert rejected.status_code == 400, rejected.text
    assert api.get(path + stream["uuid"]).json()["name"] == "a" * 2048
