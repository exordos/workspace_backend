# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import psycopg
import pytest
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest
from workspace.user_api.dm import models


MIGRATION = "0214-Expand-descriptions-to-10000-characters-fa3fa9.py"
PREVIOUS_MIGRATION = "0213-Allow-global-background-tasks-without-a-project-3ee2ca.py"


def _description_columns(connection):
    return connection.execute(
        """
        SELECT table_name, data_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'description'
        ORDER BY table_name
        """
    ).fetchall()


def _views(connection):
    return connection.execute(
        "SELECT viewname, definition FROM pg_views "
        "WHERE schemaname = 'public' ORDER BY viewname"
    ).fetchall()


def test_description_migration_preserves_data_and_views_and_is_reversible(_database):
    with conftest.isolated_migration_database(PREVIOUS_MIGRATION) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            original_columns = _description_columns(connection)
            original_views = _views(connection)
            service = models.Service(
                name="Example",
                description="Existing description",
                service_url="https://example.com",
            )
            service.insert()
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(MIGRATION)

            columns = _description_columns(connection)
            assert len(columns) == len(original_columns) == 9
            assert all(row[1:] == ("character varying", 10000) for row in columns)
            assert _views(connection) == original_views
            assert connection.execute(
                "SELECT description FROM catalog_services WHERE uuid = %s",
                (service.uuid,),
            ).fetchone() == ("Existing description",)

            description = "\N{CYRILLIC SMALL LETTER YA}" * 10000
            service.description = description
            service.update()
            assert connection.execute(
                "SELECT description FROM catalog_services WHERE uuid = %s",
                (service.uuid,),
            ).fetchone() == (description,)
            with pytest.raises(psycopg.errors.StringDataRightTruncation):
                connection.execute(
                    "UPDATE catalog_services SET description = %s WHERE uuid = %s",
                    (description + "x", service.uuid),
                )

            service.description = "Existing description"
            service.update()
            engine.rollback_migration(MIGRATION)
            assert _description_columns(connection) == original_columns
            assert _views(connection) == original_views
            engine.apply_migration(MIGRATION)
            assert _description_columns(connection) == columns


@pytest.mark.parametrize("resource", ["streams", "files"])
@pytest.mark.parametrize("length", [10000, 10001])
def test_api_description_limit_and_round_trip(api, db, resource, length):
    description = "\N{CYRILLIC SMALL LETTER YA}" * length
    values = {"name": "Example", "description": description}
    if resource == "streams":
        values.update(source_name="native", source={"kind": "native"})
    else:
        values.update(
            stream_uuid=conftest.seed_user_stream(
                db, api.project_id, api.user_uuid, "Files"
            ),
            content_type="text/plain",
            size_bytes=0,
            hash="example",
        )
    path = f"/v1/messenger/{resource}/"
    response = api.post(path, json=values)
    if length > 10000:
        assert response.status_code == 400, response.text
        return

    assert response.status_code in (200, 201), response.text
    assert response.json()["description"] == description
    stored = api.get(path + response.json()["uuid"])
    assert stored.status_code == 200, stored.text
    assert stored.json()["description"] == description
    assert (
        db.execute(
            "SELECT count(*) FROM m_workspace_events "
            "WHERE project_id = %s AND payload->>'description' = %s",
            (api.project_id, description),
        ).fetchone()[0]
        > 0
    )
