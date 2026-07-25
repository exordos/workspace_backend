# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Verified provider-identity linking for external Messenger projections."""

import typing
import uuid as sys_uuid


_PROVIDER_IDENTITY_NAMESPACE = sys_uuid.UUID("fda6f96e-c86d-5c94-976d-4e813e3f3655")
_PAYLOAD_REFERENCE_TABLES = (
    "m_workspace_broadcast_message_events_v1",
    "m_workspace_event_recipient_payloads_v1",
)
_PAYLOAD_REWRITE_BATCH_SIZE = 100
_PAYLOAD_REWRITE_ROW_BATCH_SIZE = 500
_REFERENCE_UPDATE_ROW_BATCH_SIZE = 20_000


class IdentityMergePending(RuntimeError):
    """Signal that a committed merge batch needs another report retry."""


def _update_uuid_reference_batch(
    session: typing.Any,
    *,
    table_name: str,
    column_name: str,
    legacy_user_uuid: sys_uuid.UUID,
    canonical_user_uuid: sys_uuid.UUID,
) -> None:
    """Move one bounded batch of relational UUID references."""
    rows = session.execute(
        f"""
        WITH batch AS (
            SELECT ctid
            FROM "{table_name}"
            WHERE "{column_name}" = %s
            LIMIT %s
        )
        UPDATE "{table_name}" AS target
        SET "{column_name}" = %s
        FROM batch
        WHERE target.ctid = batch.ctid
        RETURNING 1
        """,
        (
            legacy_user_uuid,
            _REFERENCE_UPDATE_ROW_BATCH_SIZE,
            canonical_user_uuid,
        ),
    ).fetchall()
    if len(rows) == _REFERENCE_UPDATE_ROW_BATCH_SIZE:
        raise IdentityMergePending


def _rewrite_payload_uuid_references(
    session: typing.Any,
    replacements: list[tuple[sys_uuid.UUID, sys_uuid.UUID]],
) -> None:
    """Rewrite payload UUIDs in bounded, retryable row batches."""
    unique_replacements: dict[sys_uuid.UUID, sys_uuid.UUID] = {}
    for legacy_user_uuid, canonical_user_uuid in replacements:
        if legacy_user_uuid == canonical_user_uuid:
            continue
        existing = unique_replacements.setdefault(
            legacy_user_uuid,
            canonical_user_uuid,
        )
        if existing != canonical_user_uuid:
            raise ValueError("Legacy identity has conflicting canonical users")
    ordered = sorted(unique_replacements.items(), key=lambda item: item[0].int)
    for offset in range(0, len(ordered), _PAYLOAD_REWRITE_BATCH_SIZE):
        batch = ordered[offset : offset + _PAYLOAD_REWRITE_BATCH_SIZE]
        expression = "payload::text"
        replacement_values: list[object] = []
        patterns = []
        for legacy_user_uuid, canonical_user_uuid in batch:
            expression = f"replace({expression}, %s, %s)"
            legacy_text = str(legacy_user_uuid)
            replacement_values.extend((legacy_text, str(canonical_user_uuid)))
            patterns.append(f"%{legacy_text}%")
        for table_name in _PAYLOAD_REFERENCE_TABLES:
            rows = session.execute(
                f"""
                WITH batch AS (
                    SELECT ctid
                    FROM "{table_name}"
                    WHERE payload::text LIKE ANY(%s::text[])
                    ORDER BY ctid
                    LIMIT %s
                )
                UPDATE "{table_name}" AS target
                SET payload = ({expression})::jsonb
                FROM batch
                WHERE target.ctid = batch.ctid
                RETURNING 1
                """,
                (
                    patterns,
                    _PAYLOAD_REWRITE_ROW_BATCH_SIZE,
                    *replacement_values,
                ),
            ).fetchall()
            if len(rows) == _PAYLOAD_REWRITE_ROW_BATCH_SIZE:
                raise IdentityMergePending


def _invalidate_direct_event_history(session: typing.Any) -> None:
    """Force clients to reload canonical state instead of replaying stale UUIDs."""
    session.execute(
        """
        UPDATE m_workspace_event_cursors
        SET epoch_generation = %s,
            pruned_through_epoch_version = GREATEST(
                pruned_through_epoch_version,
                current_epoch_version
            ),
            updated_at = NOW()
        """,
        (sys_uuid.uuid4(),),
    )


def canonical_provider_identity_uuid(
    provider: str,
    provider_realm_uuid: sys_uuid.UUID,
    provider_user_id: str,
) -> sys_uuid.UUID:
    """Return one external UUID per provider identity inside a provider realm."""
    return sys_uuid.uuid5(
        _PROVIDER_IDENTITY_NAMESPACE,
        f"{provider}:{provider_realm_uuid}:{provider_user_id}",
    )


def bind_verified_account_owner(
    session: typing.Any,
    *,
    provider: str,
    account_uuid: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    provider_realm_uuid: sys_uuid.UUID,
    provider_user_id: str,
) -> sys_uuid.UUID | None:
    """Bind an authenticated provider account to its IAM owner, fail closed."""
    account = session.execute(
        """
        SELECT owner_user_uuid, provider, provider_realm_uuid,
               provider_owner_user_id
        FROM m_external_accounts_v2
        WHERE uuid = %s
        FOR UPDATE
        """,
        (account_uuid,),
    ).fetchone()
    if (
        account is None
        or account["owner_user_uuid"] != owner_user_uuid
        or account["provider"] != provider
    ):
        raise ValueError("External account provider identity ownership is invalid")
    if account["provider_realm_uuid"] is not None and (
        account["provider_realm_uuid"] != provider_realm_uuid
        or account["provider_owner_user_id"] != provider_user_id
    ):
        raise ValueError("External account provider identity changed")
    duplicate = session.execute(
        """
        SELECT uuid, owner_user_uuid
        FROM m_external_accounts_v2
        WHERE provider = %s
          AND provider_realm_uuid = %s
          AND provider_owner_user_id = %s
          AND uuid != %s
        FOR UPDATE
        """,
        (
            provider,
            provider_realm_uuid,
            provider_user_id,
            account_uuid,
        ),
    ).fetchone()
    if duplicate is not None:
        raise ValueError("Provider identity is already linked to another account")
    link = session.execute(
        """
        SELECT workspace_user_uuid, link_kind
        FROM m_external_provider_identity_links_v1
        WHERE provider = %s
          AND provider_realm_uuid = %s
          AND provider_user_id = %s
        FOR UPDATE
        """,
        (provider, provider_realm_uuid, provider_user_id),
    ).fetchone()
    legacy_user_uuid = None
    if link is not None:
        linked_user_uuid = sys_uuid.UUID(str(link["workspace_user_uuid"]))
        if (
            link["link_kind"] == "verified_account_owner"
            and linked_user_uuid != owner_user_uuid
        ):
            raise ValueError("Provider identity belongs to another IAM user")
        if linked_user_uuid != owner_user_uuid:
            legacy_user_uuid = linked_user_uuid
        else:
            session.execute(
                """
                UPDATE m_external_provider_identity_links_v1
                SET link_kind = 'verified_account_owner',
                    updated_at = NOW()
                WHERE provider = %s
                  AND provider_realm_uuid = %s
                  AND provider_user_id = %s
                """,
                (
                    provider,
                    provider_realm_uuid,
                    provider_user_id,
                ),
            )
    else:
        session.execute(
            """
            INSERT INTO m_external_provider_identity_links_v1 (
                provider, provider_realm_uuid, provider_user_id,
                workspace_user_uuid, link_kind
            ) VALUES (%s, %s, %s, %s, 'verified_account_owner')
            """,
            (
                provider,
                provider_realm_uuid,
                provider_user_id,
                owner_user_uuid,
            ),
        )
    session.execute(
        """
        UPDATE m_external_accounts_v2
        SET provider_realm_uuid = %s,
            provider_owner_user_id = %s,
            updated_at = NOW()
        WHERE uuid = %s
        """,
        (provider_realm_uuid, provider_user_id, account_uuid),
    )
    return legacy_user_uuid


def resolve_provider_identity(
    session: typing.Any,
    *,
    provider: str,
    provider_realm_uuid: sys_uuid.UUID,
    provider_user_id: str,
) -> sys_uuid.UUID:
    """Resolve a provider identity without ever using email as proof."""
    link = session.execute(
        """
        SELECT workspace_user_uuid
        FROM m_external_provider_identity_links_v1
        WHERE provider = %s
          AND provider_realm_uuid = %s
          AND provider_user_id = %s
        """,
        (provider, provider_realm_uuid, provider_user_id),
    ).fetchone()
    if link is not None:
        return sys_uuid.UUID(str(link["workspace_user_uuid"]))
    workspace_user_uuid = canonical_provider_identity_uuid(
        provider,
        provider_realm_uuid,
        provider_user_id,
    )
    session.execute(
        """
        INSERT INTO m_external_provider_identity_links_v1 (
            provider, provider_realm_uuid, provider_user_id,
            workspace_user_uuid, link_kind
        ) VALUES (%s, %s, %s, %s, 'provider_identity')
        ON CONFLICT (
            provider, provider_realm_uuid, provider_user_id
        ) DO NOTHING
        """,
        (
            provider,
            provider_realm_uuid,
            provider_user_id,
            workspace_user_uuid,
        ),
    )
    link = session.execute(
        """
        SELECT workspace_user_uuid
        FROM m_external_provider_identity_links_v1
        WHERE provider = %s
          AND provider_realm_uuid = %s
          AND provider_user_id = %s
        """,
        (provider, provider_realm_uuid, provider_user_id),
    ).fetchone()
    return sys_uuid.UUID(str(link["workspace_user_uuid"]))


def merge_account_scoped_provider_identities(
    session: typing.Any,
    *,
    provider: str,
    account_uuid: sys_uuid.UUID,
    provider_realm_uuid: sys_uuid.UUID,
) -> list[sys_uuid.UUID]:
    """Merge every legacy identity owned by one now-verified account."""
    legacy_identities = session.execute(
        """
        SELECT uuid, provider_external_id
        FROM m_workspace_users
        WHERE source = 'zulip'
          AND external_account_uuid = %s
          AND provider_external_id IS NOT NULL
          AND provider_external_id != ''
        ORDER BY uuid
        FOR UPDATE
        """,
        (account_uuid,),
    ).fetchall()
    changed_chat_uuids: set[sys_uuid.UUID] = set()
    resolved_identities = []
    for legacy_identity in legacy_identities:
        legacy_user_uuid = sys_uuid.UUID(str(legacy_identity["uuid"]))
        canonical_user_uuid = resolve_provider_identity(
            session,
            provider=provider,
            provider_realm_uuid=provider_realm_uuid,
            provider_user_id=legacy_identity["provider_external_id"],
        )
        if legacy_user_uuid == canonical_user_uuid:
            continue
        resolved_identities.append((legacy_user_uuid, canonical_user_uuid))
    replacements: list[tuple[sys_uuid.UUID, sys_uuid.UUID]] = []
    for legacy_user_uuid, canonical_user_uuid in resolved_identities:
        replacements.append((legacy_user_uuid, canonical_user_uuid))
        merge_workspace_user_identity(
            session,
            legacy_user_uuid,
            canonical_user_uuid,
            rewrite_payloads=False,
            rewrite_chats=False,
            delete_legacy=False,
        )
    _rewrite_payload_uuid_references(session, replacements)
    for legacy_user_uuid, canonical_user_uuid in replacements:
        legacy_text = str(legacy_user_uuid)
        canonical_text = str(canonical_user_uuid)
        rows = session.execute(
            """
            UPDATE m_external_chats_v2
            SET source = replace(source::text, %s, %s)::jsonb,
                revision = revision + 1,
                updated_at = NOW()
            WHERE position(%s in source::text) > 0
            RETURNING uuid
            """,
            (legacy_text, canonical_text, legacy_text),
        ).fetchall()
        changed_chat_uuids.update(sys_uuid.UUID(str(row["uuid"])) for row in rows)
    if replacements:
        _invalidate_direct_event_history(session)
    for legacy_user_uuid, _canonical_user_uuid in replacements:
        session.execute(
            "DELETE FROM m_workspace_users WHERE uuid = %s",
            (legacy_user_uuid,),
        )
    return sorted(changed_chat_uuids)


def merge_workspace_user_identity(
    session: typing.Any,
    legacy_user_uuid: sys_uuid.UUID,
    canonical_user_uuid: sys_uuid.UUID,
    *,
    rewrite_payloads: bool = True,
    rewrite_chats: bool = True,
    delete_legacy: bool = True,
) -> list[sys_uuid.UUID]:
    """Move an old account-scoped external user onto its canonical UUID."""
    if legacy_user_uuid == canonical_user_uuid:
        return []
    legacy = session.execute(
        """
        SELECT uuid, created_at, updated_at, username, source, status,
               first_name, last_name, email, last_ping_at,
               status_emoji, status_text, avatar, provider_uuid,
               external_account_uuid, provider_external_id
        FROM m_workspace_users
        WHERE uuid = %s
        FOR UPDATE
        """,
        (legacy_user_uuid,),
    ).fetchone()
    if legacy is None:
        return []
    if legacy["source"] != "zulip":
        raise ValueError("Only external provider identities may be merged")
    canonical = session.execute(
        """
        SELECT uuid, source
        FROM m_workspace_users
        WHERE uuid = %s
        FOR UPDATE
        """,
        (canonical_user_uuid,),
    ).fetchone()
    canonical_source = legacy["source"] if canonical is None else canonical["source"]
    if canonical is None:
        session.execute(
            """
            UPDATE m_workspace_users
            SET username = LEFT(username, 80) || '-legacy-' || uuid::text,
                updated_at = NOW()
            WHERE uuid = %s
            """,
            (legacy_user_uuid,),
        )
        session.execute(
            """
            INSERT INTO m_workspace_users (
                uuid, created_at, updated_at, username, source, status,
                first_name, last_name, email, last_ping_at,
                status_emoji, status_text, avatar, provider_uuid,
                external_account_uuid, provider_external_id
            ) VALUES (
                %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            (
                canonical_user_uuid,
                legacy["created_at"],
                legacy["username"],
                legacy["source"],
                legacy["status"],
                legacy["first_name"],
                legacy["last_name"],
                legacy["email"],
                legacy["last_ping_at"],
                legacy["status_emoji"],
                legacy["status_text"],
                legacy["avatar"],
                legacy["provider_uuid"],
                legacy["external_account_uuid"],
                legacy["provider_external_id"],
            ),
        )
    session.execute(
        """
        INSERT INTO m_workspace_stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid,
            role, notification_mode, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), project_id, stream_uuid, %s, who_uuid,
            role, notification_mode, created_at, updated_at
        FROM m_workspace_stream_bindings
        WHERE user_uuid = %s
        ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        """
        UPDATE m_workspace_drafts
        SET user_uuid = %s,
            updated_at = NOW()
        WHERE user_uuid = %s
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_stream_bindings WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    session.execute(
        """
        INSERT INTO m_workspace_user_message_flags (
            uuid, user_uuid, project_id, read, pinned, starred,
            created_at, updated_at
        )
        SELECT
            uuid, %s, project_id, read, pinned, starred,
            created_at, updated_at
        FROM m_workspace_user_message_flags
        WHERE user_uuid = %s
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET read = (
                m_workspace_user_message_flags.read OR EXCLUDED.read
            ),
            pinned = (
                m_workspace_user_message_flags.pinned
                OR EXCLUDED.pinned
            ),
            starred = (
                m_workspace_user_message_flags.starred
                OR EXCLUDED.starred
            ),
            updated_at = GREATEST(
                m_workspace_user_message_flags.updated_at,
                EXCLUDED.updated_at
            )
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_user_message_flags WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    session.execute(
        """
        INSERT INTO m_workspace_user_topic_flags (
            uuid, user_uuid, project_id, is_done, created_at, updated_at
        )
        SELECT
            uuid, %s, project_id, is_done, created_at, updated_at
        FROM m_workspace_user_topic_flags
        WHERE user_uuid = %s
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET is_done = (
                m_workspace_user_topic_flags.is_done
                OR EXCLUDED.is_done
            ),
            updated_at = GREATEST(
                m_workspace_user_topic_flags.updated_at,
                EXCLUDED.updated_at
            )
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_user_topic_flags WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    session.execute(
        """
        INSERT INTO m_workspace_file_accesses (
            uuid, project_id, file_uuid, user_uuid,
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(), project_id, file_uuid, %s,
            created_at, updated_at
        FROM m_workspace_file_accesses
        WHERE user_uuid = %s
        ON CONFLICT (project_id, file_uuid, user_uuid) DO NOTHING
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_file_accesses WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    session.execute(
        """
        DELETE FROM m_workspace_message_reactions AS legacy
        USING m_workspace_message_reactions AS canonical
        WHERE legacy.user_uuid = %s
          AND canonical.user_uuid = %s
          AND canonical.message_uuid = legacy.message_uuid
          AND canonical.emoji_name = legacy.emoji_name
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    session.execute(
        """
        INSERT INTO m_workspace_event_cursors (
            project_id, user_uuid, current_epoch_version,
            pruned_through_epoch_version, created_at, updated_at
        )
        SELECT project_id, %s, current_epoch_version,
               pruned_through_epoch_version, created_at, updated_at
        FROM m_workspace_event_cursors
        WHERE user_uuid = %s
        ON CONFLICT (project_id, user_uuid) DO UPDATE
        SET current_epoch_version = GREATEST(
                m_workspace_event_cursors.current_epoch_version,
                EXCLUDED.current_epoch_version
            ),
            pruned_through_epoch_version = GREATEST(
                m_workspace_event_cursors.pruned_through_epoch_version,
                EXCLUDED.pruned_through_epoch_version
            ),
            updated_at = GREATEST(
                m_workspace_event_cursors.updated_at,
                EXCLUDED.updated_at
            )
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_event_cursors WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    references = session.execute(
        """
        SELECT child.relname AS table_name,
               child_column.attname AS column_name
        FROM pg_constraint AS foreign_key
        JOIN pg_class AS child
          ON child.oid = foreign_key.conrelid
        JOIN pg_attribute AS child_column
          ON child_column.attrelid = child.oid
         AND child_column.attnum = foreign_key.conkey[1]
        WHERE foreign_key.contype = 'f'
          AND foreign_key.confrelid = 'm_workspace_users'::regclass
          AND array_length(foreign_key.conkey, 1) = 1
        ORDER BY child.relname, child_column.attname
        """
    ).fetchall()
    for reference in references:
        table_name = reference["table_name"].replace('"', '""')
        column_name = reference["column_name"].replace('"', '""')
        _update_uuid_reference_batch(
            session,
            table_name=table_name,
            column_name=column_name,
            legacy_user_uuid=legacy_user_uuid,
            canonical_user_uuid=canonical_user_uuid,
        )
    session.execute(
        """
        DELETE FROM m_workspace_event_audience_members_v1 AS legacy
        USING m_workspace_event_audience_members_v1 AS canonical
        WHERE legacy.user_uuid = %s
          AND canonical.user_uuid = %s
          AND canonical.audience_snapshot_uuid =
              legacy.audience_snapshot_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    _update_uuid_reference_batch(
        session,
        table_name="m_workspace_event_audience_members_v1",
        column_name="user_uuid",
        legacy_user_uuid=legacy_user_uuid,
        canonical_user_uuid=canonical_user_uuid,
    )
    session.execute(
        """
        DELETE FROM m_workspace_event_recipient_payloads_v1 AS legacy
        USING m_workspace_event_recipient_payloads_v1 AS canonical
        WHERE legacy.user_uuid = %s
          AND canonical.user_uuid = %s
          AND canonical.event_uuid = legacy.event_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    _update_uuid_reference_batch(
        session,
        table_name="m_workspace_event_recipient_payloads_v1",
        column_name="user_uuid",
        legacy_user_uuid=legacy_user_uuid,
        canonical_user_uuid=canonical_user_uuid,
    )
    _update_uuid_reference_batch(
        session,
        table_name="m_workspace_streams",
        column_name="direct_user_uuid",
        legacy_user_uuid=legacy_user_uuid,
        canonical_user_uuid=canonical_user_uuid,
    )
    legacy_text = str(legacy_user_uuid)
    canonical_text = str(canonical_user_uuid)
    if rewrite_payloads:
        _rewrite_payload_uuid_references(
            session,
            [(legacy_user_uuid, canonical_user_uuid)],
        )
    changed_chats = []
    if rewrite_chats:
        changed_chats = session.execute(
            """
            UPDATE m_external_chats_v2
            SET source = replace(source::text, %s, %s)::jsonb,
                revision = revision + 1,
                updated_at = NOW()
            WHERE position(%s in source::text) > 0
            RETURNING uuid
            """,
            (legacy_text, canonical_text, legacy_text),
        ).fetchall()
    session.execute(
        """
        UPDATE m_external_provider_identity_links_v1
        SET workspace_user_uuid = %s,
            link_kind = CASE
                WHEN %s = 'iam' THEN 'verified_account_owner'
                ELSE link_kind
            END,
            updated_at = NOW()
        WHERE workspace_user_uuid = %s
        """,
        (canonical_user_uuid, canonical_source, legacy_user_uuid),
    )
    if delete_legacy:
        _invalidate_direct_event_history(session)
        session.execute(
            "DELETE FROM m_workspace_users WHERE uuid = %s",
            (legacy_user_uuid,),
        )
    return [sys_uuid.UUID(str(row["uuid"])) for row in changed_chats]


def delete_unreferenced_provider_identities(
    session: typing.Any,
) -> list[sys_uuid.UUID]:
    """Remove stale external rows left behind by already deleted accounts."""
    references = session.execute(
        """
        SELECT child.relname AS table_name,
               child_column.attname AS column_name
        FROM pg_constraint AS foreign_key
        JOIN pg_class AS child
          ON child.oid = foreign_key.conrelid
        JOIN pg_attribute AS child_column
          ON child_column.attrelid = child.oid
         AND child_column.attnum = foreign_key.conkey[1]
        WHERE foreign_key.contype = 'f'
          AND foreign_key.confrelid = 'm_workspace_users'::regclass
          AND array_length(foreign_key.conkey, 1) = 1
        ORDER BY child.relname, child_column.attname
        """
    ).fetchall()
    candidates = session.execute(
        """
        SELECT provider_user.uuid
        FROM m_workspace_users AS provider_user
        LEFT JOIN m_external_accounts_v2 AS account
          ON account.uuid = provider_user.external_account_uuid
        WHERE provider_user.source = 'zulip'
          AND account.uuid IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM m_external_provider_identity_links_v1 AS link
              WHERE link.workspace_user_uuid = provider_user.uuid
          )
        ORDER BY provider_user.created_at, provider_user.uuid
        LIMIT 500
        """
    ).fetchall()
    deleted = []
    for candidate in candidates:
        user_uuid = sys_uuid.UUID(str(candidate["uuid"]))
        referenced = False
        for reference in references:
            table_name = reference["table_name"].replace('"', '""')
            column_name = reference["column_name"].replace('"', '""')
            row = session.execute(
                f'SELECT 1 FROM "{table_name}" WHERE "{column_name}" = %s LIMIT 1',
                (user_uuid,),
            ).fetchone()
            if row is not None:
                referenced = True
                break
        if referenced:
            continue
        for table_name, column_name in (
            ("m_workspace_streams", "direct_user_uuid"),
            ("m_workspace_event_audience_members_v1", "user_uuid"),
            ("m_workspace_event_recipient_payloads_v1", "user_uuid"),
        ):
            row = session.execute(
                f'SELECT 1 FROM "{table_name}" WHERE "{column_name}" = %s LIMIT 1',
                (user_uuid,),
            ).fetchone()
            if row is not None:
                referenced = True
                break
        if referenced:
            continue
        session.execute(
            "DELETE FROM m_workspace_users WHERE uuid = %s",
            (user_uuid,),
        )
        deleted.append(user_uuid)
    return deleted
