# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import conftest


MIGRATION_FILE = "0207-Freeze-provider-history-as-native-Workspace-data-4d2649.py"


def test_zulip_history_becomes_native_workspace_data(_database, db):
    migration = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )._load_migrations()[MIGRATION_FILE]
    migration.downgrade(db)

    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    binding_uuid = sys_uuid.uuid4()
    credential_uuid = sys_uuid.uuid4()
    provider_uuid = sys_uuid.uuid4()

    db.execute(
        """
        INSERT INTO m_external_accounts_v2 (
            uuid, owner_user_uuid, provider, settings, credential_present
        ) VALUES (%s, %s, 'zulip', '{"server_url":"https://zulip.invalid"}', TRUE)
        """,
        (account_uuid, user_uuid),
    )
    db.execute(
        """
        INSERT INTO m_external_credentials_v2 (
            uuid, external_account_uuid, key_version, envelope
        ) VALUES (%s, %s, 1, '{"ciphertext":"removed"}')
        """,
        (credential_uuid, account_uuid),
    )
    db.execute(
        """
        INSERT INTO m_workspace_users (
            uuid, username, source, status, email, avatar,
            provider_uuid, external_account_uuid, provider_external_id,
            created_at, updated_at
        ) VALUES (
            %s, %s, 'zulip', 'active', 'person@zulip.invalid',
            'urn:gravatar:00000000000000000000000000000000',
            %s, %s, '17', NOW(), NOW()
        )
        """,
        (user_uuid, f"zulip-{user_uuid}", provider_uuid, account_uuid),
    )
    db.execute(
        """
        INSERT INTO m_workspace_streams (
            uuid, project_id, name, description, user_uuid,
            source_name, source, provider_uuid, external_account_uuid,
            provider_external_id, provider_metadata
        ) VALUES (
            %s, %s, 'Imported stream', 'history', %s,
            'zulip', '{"kind":"zulip","stream_id":42}', %s, %s, '42',
            '{"server_url":"https://zulip.invalid"}'
        )
        """,
        (stream_uuid, project_uuid, user_uuid, provider_uuid, account_uuid),
    )
    db.execute(
        """
        INSERT INTO m_workspace_stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid, role
        ) VALUES (%s, %s, %s, %s, %s, 'owner')
        """,
        (binding_uuid, project_uuid, stream_uuid, user_uuid, user_uuid),
    )
    db.execute(
        """
        INSERT INTO m_workspace_stream_topics (
            uuid, project_id, stream_uuid, name, source_name, source,
            provider_uuid, external_account_uuid, provider_external_id
        ) VALUES (
            %s, %s, %s, 'Imported topic', 'zulip',
            '{"kind":"zulip","stream_id":42}', %s, %s, 'topic'
        )
        """,
        (topic_uuid, project_uuid, stream_uuid, provider_uuid, account_uuid),
    )
    db.execute(
        """
        INSERT INTO m_workspace_messages (
            uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload,
            source_name, source, provider_uuid, external_account_uuid,
            provider_external_id, provider_metadata
        ) VALUES (
            %s, %s, %s, %s, %s, '{"kind":"markdown","content":"kept"}',
            'zulip', '{"kind":"zulip","message_id":"99"}', %s, %s, '99',
            '{"original_url":"https://zulip.invalid/#narrow/near/99"}'
        )
        """,
        (
            message_uuid,
            project_uuid,
            stream_uuid,
            topic_uuid,
            user_uuid,
            provider_uuid,
            account_uuid,
        ),
    )

    migration.upgrade(db)

    assert db.execute(
        """
        SELECT author_uuid, source_name, payload->>'content'
        FROM workspace_v3.messages
        WHERE project_id = %s AND uuid = %s
        """,
        (project_uuid, message_uuid),
    ).fetchone() == (user_uuid, "zulip", "kept")
    assert db.execute(
        "SELECT source, email FROM workspace_v3.users WHERE uuid = %s",
        (user_uuid,),
    ).fetchone() == ("iam", None)
    assert db.execute(
        """
        SELECT stream.source_name, topic.source_name
        FROM workspace_v3.streams AS stream
        JOIN workspace_v3.topics AS topic
          ON topic.project_id = stream.project_id
         AND topic.stream_uuid = stream.uuid
        WHERE stream.project_id = %s AND stream.uuid = %s
        """,
        (project_uuid, stream_uuid),
    ).fetchone() == ("native", "native")
    assert db.execute(
        """
        SELECT source_name, source, provider_uuid, external_account_uuid,
               provider_external_id, provider_metadata
        FROM m_workspace_messages
        WHERE project_id = %s AND uuid = %s
        """,
        (project_uuid, message_uuid),
    ).fetchone() == ("zulip", {"kind": "zulip"}, None, None, None, None)
    assert (
        db.execute(
            "SELECT count(*) FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT to_regclass('workspace_v3.provider_consumers')",
        ).fetchone()[0]
        is None
    )
    assert (
        db.execute(
            "SELECT to_regclass('workspace_v3.provider_entity_states')",
        ).fetchone()[0]
        is None
    )
