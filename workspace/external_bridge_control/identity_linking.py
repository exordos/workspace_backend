# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Verified provider-identity linking for external Messenger projections."""

import typing
import urllib.parse
import uuid as sys_uuid

from workspace.messenger_api import reaction_users
from workspace.messenger_api.dm import read_state


_PAYLOAD_REFERENCE_TABLES = (
    "m_workspace_broadcast_message_events_v1",
    "m_workspace_event_recipient_payloads_v1",
)
_PAYLOAD_REWRITE_BATCH_SIZE = 100
_PAYLOAD_REWRITE_ROW_BATCH_SIZE = 500
_REFERENCE_UPDATE_ROW_BATCH_SIZE = 20_000
_IDENTITY_RECONCILIATION_MAX_ATTEMPTS = 100_000


class ProviderScopeConflict(ValueError):
    """A verified provider realm would make one chat span projects."""


class IdentityMergePending(RuntimeError):
    """Signal that a committed merge batch needs another report retry."""


def normalize_provider_origin(server_url: object) -> str:
    """Return a canonical HTTP origin for pre-discovery provider fencing."""
    if not isinstance(server_url, str):
        raise ValueError("External provider server URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(server_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("External provider server URL is invalid") from error
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("External provider server URL is invalid")
    try:
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("External provider server URL is invalid") from error
    if not hostname:
        raise ValueError("External provider server URL is invalid")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        return f"{scheme}://{hostname}:{port}"
    return f"{scheme}://{hostname}"


def _lock_verified_realm_project_scope(
    session: typing.Any,
    *,
    provider: str,
    account_uuid: sys_uuid.UUID,
    provider_realm_uuid: sys_uuid.UUID,
) -> None:
    """Serialize and reject conflicting selections before binding a realm."""
    selected = session.execute(
        """
        SELECT provider_chat_id
        FROM m_external_chats_v2
        WHERE external_account_uuid = %s AND provider = %s
          AND selected AND project_id IS NOT NULL
        ORDER BY provider_chat_id
        """,
        (account_uuid, provider),
    ).fetchall()
    provider_chat_ids = [row["provider_chat_id"] for row in selected]
    for provider_chat_id in provider_chat_ids:
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                "provider-chat-project-v1:"
                f"realm:{provider_realm_uuid}:{provider}:{provider_chat_id}",
            ),
        )
    if not provider_chat_ids:
        return
    conflict = session.execute(
        """
        SELECT selected.provider_chat_id
        FROM m_external_chats_v2 AS selected
        JOIN m_external_chats_v2 AS conflicting
          ON conflicting.provider = selected.provider
         AND conflicting.provider_chat_id = selected.provider_chat_id
         AND conflicting.selected
         AND conflicting.project_id IS DISTINCT FROM selected.project_id
        JOIN m_external_accounts_v2 AS conflicting_account
          ON conflicting_account.uuid = conflicting.external_account_uuid
        WHERE selected.external_account_uuid = %s
          AND selected.provider = %s
          AND selected.selected
          AND selected.project_id IS NOT NULL
          AND conflicting.external_account_uuid <> %s
          AND conflicting_account.provider_realm_uuid = %s
        LIMIT 1
        """,
        (account_uuid, provider, account_uuid, provider_realm_uuid),
    ).fetchone()
    if conflict is not None:
        raise ProviderScopeConflict(
            "Provider chat is already selected in another Workspace project"
        )


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


def _merge_messenger_v2_identity(
    session: typing.Any,
    legacy_user_uuid: sys_uuid.UUID,
    canonical_user_uuid: sys_uuid.UUID,
) -> None:
    """Merge v2 identity rows without violating user-scoped uniqueness."""
    session.execute(
        """
        INSERT INTO messenger_project_users (project_id, user_uuid)
        SELECT project_id, %s
        FROM messenger_project_users
        WHERE user_uuid = %s
        ON CONFLICT (project_id, user_uuid) DO NOTHING
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        """
        UPDATE messenger_stream_bindings AS canonical
        SET active = canonical.active OR legacy.active,
            membership_generation = GREATEST(
                canonical.membership_generation,
                legacy.membership_generation
            ),
            membership_started_at = CASE
                WHEN legacy.active AND NOT canonical.active
                THEN legacy.membership_started_at
                WHEN canonical.active AND NOT legacy.active
                THEN canonical.membership_started_at
                ELSE LEAST(
                    canonical.membership_started_at,
                    legacy.membership_started_at
                )
            END,
            who_uuid = CASE
                WHEN legacy.active AND NOT canonical.active THEN legacy.who_uuid
                WHEN canonical.active AND NOT legacy.active THEN canonical.who_uuid
                WHEN legacy.updated_at > canonical.updated_at THEN legacy.who_uuid
                ELSE canonical.who_uuid
            END,
            role = CASE
                WHEN legacy.active AND NOT canonical.active THEN legacy.role
                WHEN canonical.active AND NOT legacy.active THEN canonical.role
                WHEN legacy.updated_at > canonical.updated_at THEN legacy.role
                ELSE canonical.role
            END,
            notification_mode = CASE
                WHEN legacy.notification_updated_at
                    > canonical.notification_updated_at
                THEN legacy.notification_mode
                ELSE canonical.notification_mode
            END,
            notification_updated_at = GREATEST(
                canonical.notification_updated_at,
                legacy.notification_updated_at
            ),
            unread_count = GREATEST(
                canonical.unread_count,
                legacy.unread_count
            ),
            active_unread_count = GREATEST(
                canonical.active_unread_count,
                legacy.active_unread_count
            ),
            passive_unread_count = GREATEST(
                canonical.passive_unread_count,
                legacy.passive_unread_count
            ),
            last_message_uuid = COALESCE(
                canonical.last_message_uuid,
                legacy.last_message_uuid
            ),
            created_at = LEAST(canonical.created_at, legacy.created_at),
            updated_at = GREATEST(canonical.updated_at, legacy.updated_at)
        FROM messenger_stream_bindings AS legacy
        WHERE legacy.user_uuid = %s AND canonical.user_uuid = %s
          AND canonical.project_id = legacy.project_id
          AND canonical.stream_uuid = legacy.stream_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    session.execute(
        """
        UPDATE messenger_user_message_states AS canonical
        SET read_at = COALESCE(canonical.read_at, legacy.read_at),
            mentioned = canonical.mentioned OR legacy.mentioned,
            starred = canonical.starred OR legacy.starred,
            pinned = canonical.pinned OR legacy.pinned,
            updated_at = GREATEST(canonical.updated_at, legacy.updated_at)
        FROM messenger_user_message_states AS legacy
        WHERE legacy.user_uuid = %s AND canonical.user_uuid = %s
          AND canonical.project_id = legacy.project_id
          AND canonical.placement_uuid = legacy.placement_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    session.execute(
        """
        DELETE FROM messenger_folder_items AS legacy
        USING messenger_folder_items AS canonical
        WHERE legacy.user_uuid = %s AND canonical.user_uuid = %s
          AND canonical.project_id = legacy.project_id
          AND canonical.folder_uuid = legacy.folder_uuid
          AND canonical.stream_uuid = legacy.stream_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    _update_uuid_reference_batch(
        session,
        table_name="messenger_folder_items",
        column_name="user_uuid",
        legacy_user_uuid=legacy_user_uuid,
        canonical_user_uuid=canonical_user_uuid,
    )
    for table_name, identity_columns in (
        ("messenger_stream_bindings", ("project_id", "stream_uuid")),
        ("messenger_user_topic_bindings", ("project_id", "topic_uuid")),
        ("messenger_user_message_bindings", ("project_id", "placement_uuid")),
        ("messenger_user_message_states", ("project_id", "placement_uuid")),
        ("messenger_user_folder_bindings", ("project_id", "folder_uuid")),
        ("messenger_event_membership_guards", ("event_uuid",)),
    ):
        equality = " AND ".join(
            f"canonical.{column} = legacy.{column}" for column in identity_columns
        )
        session.execute(
            f"""
            DELETE FROM {table_name} AS legacy
            USING {table_name} AS canonical
            WHERE legacy.user_uuid = %s AND canonical.user_uuid = %s
              AND {equality}
            """,
            (legacy_user_uuid, canonical_user_uuid),
        )
    session.execute(
        """
        DELETE FROM messenger_message_reaction_facts AS legacy
        USING messenger_message_reaction_facts AS canonical
        WHERE legacy.user_uuid = %s AND canonical.user_uuid = %s
          AND canonical.project_id = legacy.project_id
          AND canonical.canonical_message_uuid = legacy.canonical_message_uuid
          AND canonical.emoji_name = legacy.emoji_name
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    for table_name, column_name in (
        ("messenger_streams", "owner_uuid"),
        ("messenger_streams", "direct_user_uuid"),
        ("messenger_stream_bindings", "user_uuid"),
        ("messenger_stream_bindings", "who_uuid"),
        ("messenger_user_topic_bindings", "user_uuid"),
        ("messenger_messages", "author_uuid"),
        ("messenger_user_message_bindings", "user_uuid"),
        ("messenger_user_message_states", "user_uuid"),
        ("messenger_user_folder_bindings", "user_uuid"),
        ("messenger_message_reaction_facts", "user_uuid"),
        ("messenger_event_membership_guards", "user_uuid"),
    ):
        _update_uuid_reference_batch(
            session,
            table_name=table_name,
            column_name=column_name,
            legacy_user_uuid=legacy_user_uuid,
            canonical_user_uuid=canonical_user_uuid,
        )
    session.execute(
        """
        DELETE FROM messenger_project_users
        WHERE user_uuid = %s
        """,
        (legacy_user_uuid,),
    )


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


def invalidate_direct_event_history(
    session: typing.Any,
    *,
    project_id: object | None = None,
    user_uuid: object | None = None,
    stream_uuid: object | None = None,
) -> None:
    """Force clients to reload canonical state instead of replaying stale UUIDs."""
    session.execute(
        """
        UPDATE m_workspace_event_cursors AS cursor
        SET epoch_generation = %s,
            pruned_through_epoch_version = GREATEST(
                pruned_through_epoch_version,
                current_epoch_version
            ),
            updated_at = NOW()
        WHERE (%s::uuid IS NULL OR project_id = %s::uuid)
          AND (%s::uuid IS NULL OR user_uuid = %s::uuid)
          AND (
              %s::uuid IS NULL
              OR cursor.user_uuid IN (
                  SELECT binding.user_uuid
                  FROM m_workspace_stream_bindings AS binding
                  WHERE binding.project_id = cursor.project_id
                    AND binding.stream_uuid = %s::uuid
              )
          )
        """,
        (
            sys_uuid.uuid4(),
            project_id,
            project_id,
            user_uuid,
            user_uuid,
            stream_uuid,
            stream_uuid,
        ),
    )


def canonical_provider_identity_uuid(
    provider: str,
    provider_realm_uuid: sys_uuid.UUID,
    provider_user_id: str,
) -> sys_uuid.UUID:
    """Return one external UUID per provider identity inside a provider realm."""
    if provider != "zulip":
        raise ValueError("Unsupported provider identity namespace")
    if (
        not provider_user_id.isascii()
        or not provider_user_id.isdecimal()
        or str(int(provider_user_id)) != provider_user_id
    ):
        raise ValueError("Provider user ID must use shortest unsigned decimal form")
    return sys_uuid.uuid5(provider_realm_uuid, f"user:{provider_user_id}")


def _rewrite_json_uuid_references(
    session: typing.Any,
    *,
    table_name: str,
    column_name: str,
    replacements: list[tuple[sys_uuid.UUID, sys_uuid.UUID]],
    touch_updated_at: bool = True,
) -> None:
    """Rewrite identity UUIDs in one provider-control JSONB column."""
    for offset in range(0, len(replacements), _PAYLOAD_REWRITE_BATCH_SIZE):
        batch = replacements[offset : offset + _PAYLOAD_REWRITE_BATCH_SIZE]
        expression = f'"{column_name}"::text'
        replacement_values: list[object] = []
        patterns = []
        for legacy_user_uuid, canonical_user_uuid in batch:
            legacy_text = str(legacy_user_uuid)
            expression = f"replace({expression}, %s, %s)"
            replacement_values.extend((legacy_text, str(canonical_user_uuid)))
            patterns.append(f"%{legacy_text}%")
        session.execute(
            f"""
            UPDATE "{table_name}"
            SET "{column_name}" = ({expression})::jsonb
                {', updated_at = NOW()' if touch_updated_at else ''}
            WHERE "{column_name}"::text LIKE ANY(%s::text[])
            """,
            (*replacement_values, patterns),
        )


def reconcile_legacy_provider_identity_links(session: typing.Any) -> int:
    """Canonicalize pre-realm-scoped Zulip identity links during upgrade."""
    rows = session.execute(
        """
        SELECT provider, provider_realm_uuid, provider_user_id,
               workspace_user_uuid
        FROM m_external_provider_identity_links_v1
        WHERE provider = 'zulip' AND link_kind = 'provider_identity'
        ORDER BY provider_realm_uuid, provider_user_id
        """
    ).fetchall()
    replacements_by_legacy: dict[sys_uuid.UUID, sys_uuid.UUID] = {}
    link_replacements: list[
        tuple[str, sys_uuid.UUID, str, sys_uuid.UUID, sys_uuid.UUID]
    ] = []
    for row in rows:
        provider = str(row["provider"])
        provider_realm_uuid = sys_uuid.UUID(str(row["provider_realm_uuid"]))
        provider_user_id = str(row["provider_user_id"])
        legacy_user_uuid = sys_uuid.UUID(str(row["workspace_user_uuid"]))
        canonical_user_uuid = canonical_provider_identity_uuid(
            provider,
            provider_realm_uuid,
            provider_user_id,
        )
        if legacy_user_uuid == canonical_user_uuid:
            continue
        prior = replacements_by_legacy.setdefault(
            legacy_user_uuid,
            canonical_user_uuid,
        )
        if prior != canonical_user_uuid:
            raise ValueError(
                "Legacy provider identity maps to multiple canonical users"
            )
        link_replacements.append(
            (
                provider,
                provider_realm_uuid,
                provider_user_id,
                legacy_user_uuid,
                canonical_user_uuid,
            )
        )
    if not link_replacements:
        return 0

    legacy_user_uuids = sorted(replacements_by_legacy, key=lambda value: value.int)
    conflicting_owner = session.execute(
        """
        SELECT workspace_user_uuid
        FROM m_external_provider_identity_links_v1
        WHERE link_kind = 'verified_account_owner'
          AND workspace_user_uuid = ANY(%s::uuid[])
        LIMIT 1
        """,
        (legacy_user_uuids,),
    ).fetchone()
    if conflicting_owner is not None:
        raise ValueError(
            "Provider identity UUID is also bound to a verified IAM owner"
        )
    invalid_user = session.execute(
        """
        SELECT uuid, source
        FROM m_workspace_users
        WHERE (
                uuid = ANY(%s::uuid[])
                AND source <> 'zulip'
              )
           OR (
                uuid = ANY(%s::uuid[])
                AND source <> 'zulip'
              )
        LIMIT 1
        """,
        (
            legacy_user_uuids,
            list(replacements_by_legacy.values()),
        ),
    ).fetchone()
    if invalid_user is not None:
        raise ValueError("Provider identity collides with a non-provider user")

    replacements = sorted(
        replacements_by_legacy.items(),
        key=lambda item: item[0].int,
    )
    existing_legacy_user_uuids = {
        sys_uuid.UUID(str(row["uuid"]))
        for row in session.execute(
            """
            SELECT uuid
            FROM m_workspace_users
            WHERE uuid = ANY(%s::uuid[])
            """,
            (legacy_user_uuids,),
        ).fetchall()
    }
    for legacy_user_uuid, canonical_user_uuid in replacements:
        if legacy_user_uuid not in existing_legacy_user_uuids:
            continue
        for _attempt in range(_IDENTITY_RECONCILIATION_MAX_ATTEMPTS):
            try:
                merge_workspace_user_identity(
                    session,
                    legacy_user_uuid,
                    canonical_user_uuid,
                    rewrite_payloads=False,
                    rewrite_chats=False,
                    delete_legacy=False,
                )
            except IdentityMergePending:
                continue
            break
        else:
            raise RuntimeError("Provider identity reconciliation did not converge")

    for _attempt in range(_IDENTITY_RECONCILIATION_MAX_ATTEMPTS):
        try:
            _rewrite_payload_uuid_references(session, replacements)
        except IdentityMergePending:
            continue
        break
    else:
        raise RuntimeError("Provider identity payload rewrite did not converge")
    for table_name, column_name in (
        ("m_external_chats_v2", "source"),
        ("m_external_bridge_desired_resources_v1", "resource"),
        ("m_external_bridge_desired_changes_v1", "resource"),
    ):
        _rewrite_json_uuid_references(
            session,
            table_name=table_name,
            column_name=column_name,
            replacements=replacements,
            touch_updated_at=(
                table_name != "m_external_bridge_desired_changes_v1"
            ),
        )
    for (
        provider,
        provider_realm_uuid,
        provider_user_id,
        legacy_user_uuid,
        canonical_user_uuid,
    ) in link_replacements:
        session.execute(
            """
            UPDATE m_external_provider_identity_links_v1
            SET workspace_user_uuid = %s, updated_at = NOW()
            WHERE provider = %s AND provider_realm_uuid = %s
              AND provider_user_id = %s
              AND link_kind = 'provider_identity'
              AND workspace_user_uuid = %s
            """,
            (
                canonical_user_uuid,
                provider,
                provider_realm_uuid,
                provider_user_id,
                legacy_user_uuid,
            ),
        )
    if existing_legacy_user_uuids:
        session.execute(
            """
            DELETE FROM m_workspace_users
            WHERE source = 'zulip' AND uuid = ANY(%s::uuid[])
            """,
            (sorted(existing_legacy_user_uuids, key=lambda value: value.int),),
        )
        invalidate_direct_event_history(session)
    return len(link_replacements)


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
    _lock_verified_realm_project_scope(
        session,
        provider=provider,
        account_uuid=account_uuid,
        provider_realm_uuid=provider_realm_uuid,
    )
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
    _resources_locked: bool = False,
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
    if replacements and not _resources_locked:
        _lock_identity_merge_resources(
            session,
            (user_uuid for replacement in replacements for user_uuid in replacement),
        )
    for legacy_user_uuid, canonical_user_uuid in resolved_identities:
        merge_workspace_user_identity(
            session,
            legacy_user_uuid,
            canonical_user_uuid,
            rewrite_payloads=False,
            rewrite_chats=False,
            delete_legacy=False,
            _resources_locked=True,
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
        invalidate_direct_event_history(session)
    for legacy_user_uuid, _canonical_user_uuid in replacements:
        session.execute(
            "DELETE FROM m_workspace_users WHERE uuid = %s",
            (legacy_user_uuid,),
        )
    return sorted(changed_chat_uuids)


def _find_identity_merge_projects(
    session: typing.Any,
    user_uuids: typing.Iterable[sys_uuid.UUID],
) -> list[typing.Any]:
    values = sorted(set(user_uuids), key=str)
    if not values:
        return []
    return session.execute(
        """
        SELECT DISTINCT affected.project_id
        FROM (
            SELECT project_id FROM m_workspace_stream_bindings
            WHERE user_uuid = ANY(%s::uuid[])
            UNION ALL
            SELECT project_id FROM m_workspace_user_message_flags
            WHERE user_uuid = ANY(%s::uuid[])
            UNION ALL
            SELECT project_id FROM m_workspace_user_topic_read_stats_v1
            WHERE user_uuid = ANY(%s::uuid[])
            UNION ALL
            SELECT project_id FROM m_workspace_read_memberships_v1
            WHERE user_uuid = ANY(%s::uuid[])
            UNION ALL
            SELECT project_id FROM m_workspace_message_mentions_v1
            WHERE user_uuid = ANY(%s::uuid[])
            UNION ALL
            SELECT project_id FROM m_workspace_messages
            WHERE user_uuid = ANY(%s::uuid[])
            UNION ALL
            SELECT project_id FROM m_workspace_message_reactions
            WHERE user_uuid = ANY(%s::uuid[])
        ) AS affected
        ORDER BY affected.project_id
        """,
        (values, values, values, values, values, values, values),
    ).fetchall()


def _lock_identity_merge_resources(
    session: typing.Any,
    user_uuids: typing.Iterable[sys_uuid.UUID],
    project_uuids: typing.Iterable[sys_uuid.UUID] = (),
) -> set[sys_uuid.UUID]:
    values = sorted(set(user_uuids), key=str)
    read_state.lock_read_state_schema_shared(session)
    for user_uuid in values:
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"workspace-user-resource-v1:{user_uuid}",),
        )
    affected_project_ids = set(project_uuids) | {
        sys_uuid.UUID(str(row["project_id"]))
        for row in _find_identity_merge_projects(session, values)
    }
    while True:
        ordered_project_ids = sorted(affected_project_ids, key=str)
        session.execute("SAVEPOINT identity_project_discovery")
        try:
            read_state.lock_message_structure(session, ordered_project_ids)
            read_state.lock_projects(session, ordered_project_ids)
            refreshed_project_ids = affected_project_ids | {
                sys_uuid.UUID(str(row["project_id"]))
                for row in _find_identity_merge_projects(session, values)
            }
            if refreshed_project_ids != affected_project_ids:
                session.execute("ROLLBACK TO SAVEPOINT identity_project_discovery")
                session.execute("RELEASE SAVEPOINT identity_project_discovery")
                affected_project_ids = refreshed_project_ids
                continue
            session.execute("RELEASE SAVEPOINT identity_project_discovery")
            break
        except Exception:
            session.execute("ROLLBACK TO SAVEPOINT identity_project_discovery")
            session.execute("RELEASE SAVEPOINT identity_project_discovery")
            raise
    read_state.bump_project_structure_revisions(session, affected_project_ids)
    read_state.reset_identity_sensitive_progress(session, affected_project_ids)
    return affected_project_ids


def lock_identity_merge_resources(
    session: typing.Any,
    user_uuids: typing.Iterable[sys_uuid.UUID],
    project_uuids: typing.Iterable[sys_uuid.UUID] = (),
) -> set[sys_uuid.UUID]:
    """Prelock a complete identity-rewrite batch in canonical order."""
    return _lock_identity_merge_resources(session, user_uuids, project_uuids)


def merge_workspace_user_identity(
    session: typing.Any,
    legacy_user_uuid: sys_uuid.UUID,
    canonical_user_uuid: sys_uuid.UUID,
    *,
    rewrite_payloads: bool = True,
    rewrite_chats: bool = True,
    delete_legacy: bool = True,
    _resources_locked: bool = False,
) -> list[sys_uuid.UUID]:
    """Move an old account-scoped external user onto its canonical UUID."""
    if legacy_user_uuid == canonical_user_uuid:
        return []
    if not _resources_locked:
        _lock_identity_merge_resources(
            session,
            (legacy_user_uuid, canonical_user_uuid),
        )
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
    reaction_group_rows = session.execute(
        """
        SELECT DISTINCT project_id, message_uuid, emoji_name
        FROM m_workspace_message_reactions
        WHERE user_uuid = %s
        ORDER BY project_id, message_uuid, emoji_name
        """,
        (legacy_user_uuid,),
    ).fetchall()
    reaction_groups_by_project: dict[
        sys_uuid.UUID,
        list[tuple[sys_uuid.UUID, str]],
    ] = {}
    for row in reaction_group_rows:
        project_id = sys_uuid.UUID(str(row["project_id"]))
        reaction_groups_by_project.setdefault(project_id, []).append(
            (
                sys_uuid.UUID(str(row["message_uuid"])),
                str(row["emoji_name"]),
            )
        )
    for project_id, groups in sorted(reaction_groups_by_project.items()):
        reaction_users.lock_messages(
            project_id,
            (message_uuid for message_uuid, _emoji_name in groups),
            session=session,
        )
    session.execute(
        """
        INSERT INTO m_workspace_stream_bindings (
            uuid, project_id, stream_uuid, user_uuid, who_uuid,
            role, notification_mode, notification_updated_at,
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(), project_id, stream_uuid, %s, who_uuid,
            role, notification_mode, notification_updated_at,
            created_at, updated_at
        FROM m_workspace_stream_bindings
        WHERE user_uuid = %s
        ON CONFLICT (project_id, stream_uuid, user_uuid) DO UPDATE
        SET notification_mode = CASE
                WHEN m_workspace_stream_bindings.notification_updated_at
                    >= EXCLUDED.notification_updated_at
                THEN m_workspace_stream_bindings.notification_mode
                ELSE EXCLUDED.notification_mode
            END,
            notification_updated_at = GREATEST(
                m_workspace_stream_bindings.notification_updated_at,
                EXCLUDED.notification_updated_at
            )
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
        INSERT INTO m_workspace_user_read_chunks_v1 (
            user_uuid, chunk_number,
            read_bits, created_at, updated_at
        )
        SELECT %s, chunk_number,
               read_bits, created_at, updated_at
        FROM m_workspace_user_read_chunks_v1
        WHERE user_uuid = %s
        ON CONFLICT (user_uuid, chunk_number)
        DO UPDATE SET
            read_bits = (
                m_workspace_user_read_chunks_v1.read_bits
                | EXCLUDED.read_bits
            ),
            updated_at = GREATEST(
                m_workspace_user_read_chunks_v1.updated_at,
                EXCLUDED.updated_at
            )
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_user_read_chunks_v1 WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    read_stat_scopes = session.execute(
        """
        SELECT DISTINCT project_id, topic_uuid
        FROM m_workspace_user_topic_read_stats_v1
        WHERE user_uuid IN (%s, %s)
        ORDER BY project_id, topic_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    ).fetchall()
    session.execute(
        """
        DELETE FROM m_workspace_user_topic_read_stats_v1
        WHERE user_uuid = %s
        """,
        (legacy_user_uuid,),
    )
    scopes_by_project: dict[
        sys_uuid.UUID, list[tuple[sys_uuid.UUID, sys_uuid.UUID]]
    ] = {}
    for scope in read_stat_scopes:
        scopes_by_project.setdefault(scope["project_id"], []).append(
            (canonical_user_uuid, scope["topic_uuid"])
        )
    for project_id, scopes in scopes_by_project.items():
        read_state._refresh_topic_read_stats(
            session,
            project_id,
            scopes,
        )
    session.execute(
        """
        INSERT INTO m_workspace_read_memberships_v1 (
            project_id, user_uuid, stream_uuid, last_detached_sequence,
            created_at, updated_at
        )
        SELECT project_id, %s, stream_uuid, last_detached_sequence,
               created_at, updated_at
        FROM m_workspace_read_memberships_v1
        WHERE user_uuid = %s
        ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE
        SET last_detached_sequence = CASE
                WHEN m_workspace_read_memberships_v1.last_detached_sequence IS NULL
                  OR EXCLUDED.last_detached_sequence IS NULL
                    THEN NULL
                ELSE GREATEST(
                    m_workspace_read_memberships_v1.last_detached_sequence,
                    EXCLUDED.last_detached_sequence
                )
            END,
            updated_at = GREATEST(
                m_workspace_read_memberships_v1.updated_at,
                EXCLUDED.updated_at
            )
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        "DELETE FROM m_workspace_read_memberships_v1 WHERE user_uuid = %s",
        (legacy_user_uuid,),
    )
    session.execute(
        """
        DELETE FROM m_workspace_message_mentions_v1 AS legacy
        USING m_workspace_message_mentions_v1 AS canonical
        WHERE legacy.user_uuid = %s
          AND canonical.user_uuid = %s
          AND canonical.message_uuid = legacy.message_uuid
        """,
        (legacy_user_uuid, canonical_user_uuid),
    )
    session.execute(
        """
        UPDATE m_workspace_message_mentions_v1
        SET user_uuid = %s
        WHERE user_uuid = %s
        """,
        (canonical_user_uuid, legacy_user_uuid),
    )
    session.execute(
        """
        INSERT INTO m_workspace_user_topic_flags (
            uuid, user_uuid, project_id, is_done, notification_mode,
            notification_updated_at, created_at, updated_at
        )
        SELECT
            uuid, %s, project_id, is_done, notification_mode,
            notification_updated_at, created_at, updated_at
        FROM m_workspace_user_topic_flags
        WHERE user_uuid = %s
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET is_done = (
                m_workspace_user_topic_flags.is_done
                OR EXCLUDED.is_done
            ),
            notification_mode = CASE
                WHEN m_workspace_user_topic_flags.notification_updated_at
                    >= EXCLUDED.notification_updated_at
                THEN m_workspace_user_topic_flags.notification_mode
                ELSE EXCLUDED.notification_mode
            END,
            notification_updated_at = GREATEST(
                m_workspace_user_topic_flags.notification_updated_at,
                EXCLUDED.notification_updated_at
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
          AND left(child.relname, 10) <> 'messenger_'
        ORDER BY child.relname, child_column.attname
        """
    ).fetchall()
    for reference in references:
        table_name = reference["table_name"].replace('"', '""')
        column_name = reference["column_name"].replace('"', '""')
        is_reaction_user_reference = (
            table_name == "m_workspace_message_reactions" and column_name == "user_uuid"
        )
        try:
            _update_uuid_reference_batch(
                session,
                table_name=table_name,
                column_name=column_name,
                legacy_user_uuid=legacy_user_uuid,
                canonical_user_uuid=canonical_user_uuid,
            )
        except IdentityMergePending:
            if is_reaction_user_reference:
                for project_id, groups in sorted(reaction_groups_by_project.items()):
                    reaction_users.refresh_groups(
                        project_id,
                        groups,
                        session=session,
                    )
            raise
        if is_reaction_user_reference:
            for project_id, groups in sorted(reaction_groups_by_project.items()):
                reaction_users.refresh_groups(
                    project_id,
                    groups,
                    session=session,
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
    _merge_messenger_v2_identity(
        session,
        legacy_user_uuid,
        canonical_user_uuid,
    )
    if delete_legacy:
        invalidate_direct_event_history(session)
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
