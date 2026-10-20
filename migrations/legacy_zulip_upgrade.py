# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Frozen handoff boundary for installations preceding native Zulip v5."""

import hashlib
import json

HANDOFF_TABLE = "workspace_zulip_v5_credential_handoff"
STATE_TABLE = "workspace_zulip_v5_legacy_state"


def envelope_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_credential_handoff(session):
    credentials = session.execute("""
        SELECT credential.external_account_uuid, credential.envelope,
               account.settings
        FROM m_external_credentials_v2 AS credential
        JOIN m_external_accounts_v2 AS account
          ON account.uuid=credential.external_account_uuid
        WHERE account.provider='zulip'
    """).fetchall()
    if not credentials:
        return
    exists = session.execute(
        "SELECT to_regclass('public.workspace_zulip_v5_credential_handoff') AS name"
    ).fetchone()["name"]
    if exists is None:
        raise RuntimeError(
            "Legacy Zulip credential handoff is incomplete; run the automatic "
            "Workspace bootstrap before retiring the legacy provider"
        )
    receipts = {
        row["account_uuid"]: row
        for row in session.execute(
            "SELECT account_uuid, source_hash, verified_at, settings_hash "
            "FROM workspace_zulip_v5_credential_handoff"
        ).fetchall()
    }
    for credential in credentials:
        receipt = receipts.get(credential["external_account_uuid"])
        settings = credential["settings"]
        connection = {
            key: settings.get(key) for key in ("server_url", "email", "api_key")
        }
        if (
            receipt is None
            or receipt["verified_at"] is None
            or receipt["source_hash"] != envelope_hash(credential["envelope"])
            or not all(connection.values())
            or receipt["settings_hash"] != envelope_hash(connection)
        ):
            raise RuntimeError("Legacy Zulip credential handoff is incomplete")


def capture(session):
    require_credential_handoff(session)
    session.execute("""
        CREATE TABLE workspace_zulip_v5_legacy_state (
            kind text NOT NULL,
            identity_key text NOT NULL,
            data jsonb NOT NULL,
            PRIMARY KEY (kind,identity_key)
        );
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'account', uuid::text, to_jsonb(account) - 'settings' ||
            jsonb_build_object('settings', settings - 'api_key')
        FROM m_external_accounts_v2 AS account WHERE provider='zulip';
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'chat', uuid::text, to_jsonb(chat)
        FROM m_external_chats_v2 AS chat WHERE provider='zulip';
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'stream', project_id::text || ':' || uuid::text, to_jsonb(stream)
        FROM messenger_streams AS stream
        WHERE stream.source_name='zulip' OR EXISTS (SELECT 1 FROM m_external_chats_v2 AS chat
                      WHERE chat.projection_stream_uuid=stream.uuid
                        AND chat.project_id=stream.project_id);
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'topic', project_id::text || ':' || uuid::text, to_jsonb(topic)
        FROM messenger_topics AS topic
        WHERE EXISTS (SELECT 1 FROM workspace_zulip_v5_legacy_state AS stream
                      WHERE stream.kind='stream' AND (stream.data->>'uuid')::uuid=topic.stream_uuid);
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'user', uuid::text, to_jsonb(person)
        FROM m_workspace_users AS person WHERE provider_external_id IS NOT NULL;
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'message', project_id::text || ':' || uuid::text, to_jsonb(message) || jsonb_build_object(
            'placements', (SELECT jsonb_agg(to_jsonb(placement))
                FROM messenger_message_placements AS placement
                WHERE placement.project_id=message.project_id
                  AND placement.message_uuid=message.uuid))
        FROM messenger_messages AS message WHERE provider_message_id IS NOT NULL;
        INSERT INTO workspace_zulip_v5_legacy_state
        SELECT 'file', uuid::text, to_jsonb(file)
        FROM m_workspace_files AS file WHERE provider_uuid IS NOT NULL
            OR external_account_uuid IS NOT NULL;
    """)
