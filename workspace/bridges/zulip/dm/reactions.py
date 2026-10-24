# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Shared reaction observations and durable local add/remove intentions."""

import datetime
import json
import typing
import uuid

from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm
from restalchemy.storage.sql import sessions

from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import owned_projection

from workspace.common import emoji
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import synchronization
from workspace.messenger_api.dm import v2_models


if typing.TYPE_CHECKING:
    pass


def _session() -> sessions.PgSQLSession:
    return contexts.Context().get_session()


def _naive(value: datetime.datetime) -> datetime.datetime:
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _enqueue(project_id: uuid.UUID, message_uuid: uuid.UUID) -> None:
    _session().execute(
        """INSERT INTO bridge_zulip_v5_reaction_changes(project_id, canonical_message_uuid)
           SELECT %s,%s WHERE EXISTS (
               SELECT 1 FROM bridge_zulip_v5_reactions AS reaction
               JOIN bridge_zulip_v5_reaction_messages AS message ON message.uuid=reaction.reaction_message_uuid
               WHERE message.project_id=%s AND message.canonical_message_uuid=%s)
           OR EXISTS (SELECT 1 FROM zulip_outgoing_intents
               WHERE project_id=%s AND entity_type='reaction' AND data->>'message_uuid'=%s)
           ON CONFLICT (project_id, canonical_message_uuid) DO UPDATE
               SET revision=EXCLUDED.revision, next_retry_at=NULL""",
        (
            project_id,
            message_uuid,
            project_id,
            message_uuid,
            project_id,
            str(message_uuid),
        ),
    )


def message_for(
    account: external_models.ExternalAccount, message_id: int
) -> dict[str, typing.Any]:
    _session().execute(
        "SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s AND uuid=%s FOR KEY SHARE",
        (account.project_id, account.uuid),
    )
    hostname = account.get_host()
    message_uuid = uuid.uuid5(
        account.project_id, f"zulip-reactions:{hostname}:{message_id}"
    )
    row = (
        _session()
        .execute(
            """INSERT INTO bridge_zulip_v5_reaction_messages
               (uuid, project_id, hostname, message_id, observer_account_uuid)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (project_id, hostname, message_id) DO UPDATE
               SET observer_account_uuid=EXCLUDED.observer_account_uuid
           RETURNING *""",
            (message_uuid, account.project_id, hostname, message_id, account.uuid),
        )
        .fetchone()
    )
    if row["canonical_message_uuid"] is None:
        bind(row)
    return row


def bind(message: dict[str, typing.Any]) -> None:
    imported_uuid = uuid.uuid5(
        message["project_id"],
        str(
            uuid.uuid5(
                uuid.uuid5(uuid.NAMESPACE_DNS, message["hostname"]),
                str(message["message_id"]),
            )
        ),
    )
    row = (
        _session()
        .execute(
            """SELECT canonical.uuid FROM messenger_messages AS canonical
           WHERE canonical.project_id=%s AND canonical.deleted_at IS NULL
             AND canonical.uuid=COALESCE((SELECT delivery.message_uuid FROM bridge_zulip_v5_deliveries AS delivery
                 WHERE delivery.project_id=%s AND delivery.hostname=%s AND delivery.zulip_message_id=%s),%s)
             AND EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                 WHERE placement.project_id=canonical.project_id AND placement.message_uuid=canonical.uuid)
           LIMIT 1""",
            (
                message["project_id"],
                message["project_id"],
                message["hostname"],
                message["message_id"],
                imported_uuid,
            ),
        )
        .fetchone()
    )
    if row is None:
        _session().execute(
            "UPDATE bridge_zulip_v5_reaction_messages SET next_retry_at=(NOW() AT TIME ZONE 'UTC')+INTERVAL '5 seconds' WHERE uuid=%s",
            (message["uuid"],),
        )
        return
    message["canonical_message_uuid"] = row["uuid"]
    _session().execute(
        "UPDATE bridge_zulip_v5_reaction_messages SET canonical_message_uuid=%s,next_retry_at=NULL WHERE uuid=%s",
        (row["uuid"], message["uuid"]),
    )
    _enqueue(message["project_id"], row["uuid"])


def _record(
    message: dict[str, typing.Any],
    data: dict[str, typing.Any],
    present: bool,
    observed_at: datetime.datetime,
    event_id: int = -1,
    queue_id: str | None = None,
) -> uuid.UUID:
    reaction_uuid = uuid.uuid5(
        message["uuid"],
        f"{data['user_id']}:{data['reaction_type']}:{data['emoji_code']}",
    )
    _session().execute(
        """INSERT INTO bridge_zulip_v5_reactions
               (uuid,reaction_message_uuid,user_id,reaction_type,emoji_code,emoji_name,present,observed_at,event_id,queue_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (reaction_message_uuid,user_id,reaction_type,emoji_code) DO UPDATE
               SET emoji_name=EXCLUDED.emoji_name,present=EXCLUDED.present,
                   observed_at=EXCLUDED.observed_at,event_id=EXCLUDED.event_id,queue_id=EXCLUDED.queue_id
               WHERE bridge_zulip_v5_reactions.observed_at < EXCLUDED.observed_at
                  OR (bridge_zulip_v5_reactions.observed_at=EXCLUDED.observed_at
                      AND bridge_zulip_v5_reactions.queue_id IS NOT DISTINCT FROM EXCLUDED.queue_id
                      AND bridge_zulip_v5_reactions.event_id < EXCLUDED.event_id)""",
        (
            reaction_uuid,
            message["uuid"],
            data["user_id"],
            data["reaction_type"],
            data["emoji_code"],
            data["emoji_name"],
            present,
            _naive(observed_at),
            event_id,
            queue_id,
        ),
    )
    return reaction_uuid


def record_event(
    account: external_models.ExternalAccount,
    data: dict[str, typing.Any],
    observed_at: datetime.datetime,
    event_id: int,
    queue_id: str,
) -> None:
    barrier = (
        _session()
        .execute(
            """SELECT 1 FROM bridge_zulip_v5_history_batches AS batch
        JOIN bridge_zulip_v5_message_fingerprints AS fingerprint
          ON fingerprint.external_account_uuid=batch.external_account_uuid
          AND fingerprint.first_id=batch.first_id
        WHERE batch.external_account_uuid=%s AND batch.project_id=%s
          AND batch.hostname=%s AND batch.first_id=(%s::bigint-1)/1000*1000+1
          AND batch.covered_to>=%s AND batch.observed_at>=%s AND batch.ready
          AND fingerprint.message_id=%s
          AND fingerprint.hashes->>'reactions' IS NOT NULL
          AND NOT fingerprint.hashes @> '{"absent":true}'::jsonb LIMIT 1""",
            (
                account.uuid,
                account.project_id,
                account.get_host(),
                data["message_id"],
                data["message_id"],
                _naive(observed_at),
                data["message_id"],
            ),
        )
        .fetchone()
    )
    if barrier is not None:
        return
    message = message_for(account, data["message_id"])
    if (
        message["snapshot_at"] is not None
        and _naive(observed_at) <= message["snapshot_at"]
    ):
        return
    _record(message, data, data["op"] == "add", observed_at, event_id, queue_id)


def observe_snapshot(
    account: external_models.ExternalAccount,
    messages: list[dict[str, typing.Any]],
    observed_at: datetime.datetime,
) -> None:
    for data in messages:
        if "reactions" not in data:
            continue
        message = message_for(account, data["id"])
        at = _naive(observed_at)
        if message["snapshot_at"] is not None and at <= message["snapshot_at"]:
            continue
        present = [
            _record(message, reaction, True, observed_at)
            for reaction in data["reactions"]
        ]
        _session().execute(
            """UPDATE bridge_zulip_v5_reactions SET present=false,observed_at=%s,event_id=-1,queue_id=NULL
               WHERE reaction_message_uuid=%s AND observed_at<=%s AND NOT (uuid=ANY(%s::uuid[]))""",
            (at, message["uuid"], at, present),
        )
        _session().execute(
            "UPDATE bridge_zulip_v5_reaction_messages SET snapshot_at=%s WHERE uuid=%s",
            (at, message["uuid"]),
        )
        if message["canonical_message_uuid"] is not None:
            _enqueue(account.project_id, message["canonical_message_uuid"])


def _publish(fact: v2_models.WorkspaceMessageReactionFact, kind: str) -> None:
    reaction = {
        name: str(getattr(fact, name)) for name in ("uuid", "project_id", "user_uuid")
    }
    reaction.update(
        message_uuid=str(fact.placement_uuid),
        emoji_name=fact.emoji_name,
        created_at=fact.created_at.isoformat(),
        updated_at=fact.updated_at.isoformat(),
    )
    payload = {
        "source_kind": f"message_reaction.{kind}",
        "reaction_uuid": str(fact.uuid),
        "placement_uuid": str(fact.placement_uuid),
        "reaction": reaction,
        "origin": "zulip",
        "emit_reaction_event": True,
    }
    _session().execute(
        """INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
           VALUES (%s,%s,'reaction_snapshot','message',%s,%s::jsonb)""",
        (
            uuid.uuid4(),
            fact.project_id,
            f"{fact.project_id}:{fact.canonical_message_uuid}",
            json.dumps(payload),
        ),
    )


def _targets(
    project_id: uuid.UUID, canonical_uuid: uuid.UUID, user_uuid: uuid.UUID
) -> list[dict[str, typing.Any]]:
    return (
        _session()
        .execute(
            """SELECT DISTINCT external_account_uuid,external_account_id,message_id
           FROM bridge_zulip_v5_flag_targets
           WHERE project_id=%s AND message_uuid=%s AND user_uuid=%s""",
            (project_id, canonical_uuid, user_uuid),
        )
        .fetchall()
    )


def remote_state(
    message: dict[str, typing.Any], user_id: int, code: str
) -> tuple[bool | None, datetime.datetime | None, str | None]:
    row = (
        _session()
        .execute(
            """SELECT * FROM bridge_zulip_v5_reactions WHERE reaction_message_uuid=%s
           AND user_id=%s AND reaction_type='unicode_emoji' AND emoji_code=%s""",
            (message["uuid"], user_id, code),
        )
        .fetchone()
    )
    if row is not None:
        return row["present"], row["observed_at"], row["emoji_name"]
    return (
        (False if message["snapshot_at"] is not None else None),
        message["snapshot_at"],
        None,
    )


def reconcile(project_id: uuid.UUID, canonical_uuid: uuid.UUID) -> bool:
    row_lock = "FOR UPDATE" if owned_projection.CURRENT.get() is None else ""
    messages = (
        _session()
        .execute(
            f"SELECT * FROM bridge_zulip_v5_reaction_messages WHERE project_id=%s AND canonical_message_uuid=%s ORDER BY uuid {row_lock}",
            (project_id, canonical_uuid),
        )
        .fetchall()
    )
    with synchronization.synchronize(
        v2_models.WorkspaceMessageReactionFact,
        project_id,
        canonical_message_uuid=canonical_uuid,
    ):
        return _reconcile_observations(project_id, canonical_uuid, messages)


def _reconcile_observations(
    project_id: uuid.UUID,
    canonical_uuid: uuid.UUID,
    messages: list[dict[str, typing.Any]],
) -> bool:
    observations = (
        _session()
        .execute(
            """SELECT reaction.*,actor.owner_user_uuid,actor.provider_uuid,message.hostname,message.message_id
           FROM bridge_zulip_v5_reactions AS reaction
           JOIN bridge_zulip_v5_reaction_messages AS message ON message.uuid=reaction.reaction_message_uuid
           JOIN messenger_providers AS provider ON provider.hostname=message.hostname
           LEFT JOIN m_external_accounts_v2 AS actor
             ON actor.project_id=message.project_id AND actor.provider='zulip'
            AND actor.provider_uuid=provider.uuid
            AND actor.external_account_id=reaction.user_id::text
           WHERE message.project_id=%s AND message.canonical_message_uuid=%s""",
            (project_id, canonical_uuid),
        )
        .fetchall()
    )
    deferred = any(
        row["owner_user_uuid"] is None
        for row in observations
        if row["reaction_type"] == "unicode_emoji"
    )
    groups: dict[tuple[uuid.UUID, str], list[dict[str, typing.Any]]] = {}
    for row in observations:
        if (
            row["reaction_type"] == "unicode_emoji"
            and row["owner_user_uuid"] is not None
        ):
            name = emoji.name_for_code(row["emoji_code"], row["emoji_name"])
            groups.setdefault((row["owner_user_uuid"], name), []).append(row)
    intents = (
        _session()
        .execute(
            """SELECT DISTINCT ON (data->>'user_uuid',data->>'emoji_name')
            (data->>'user_uuid')::uuid AS user_uuid,data->>'emoji_name' AS emoji_name,
            operation<>'delete' AS present,created_at AS changed_at,status
            FROM zulip_outgoing_intents WHERE project_id=%s AND entity_type='reaction'
              AND data->>'message_uuid'=%s ORDER BY data->>'user_uuid',data->>'emoji_name',id DESC""",
            (project_id, str(canonical_uuid)),
        )
        .fetchall()
    )
    by_key = {(intent["user_uuid"], intent["emoji_name"]): intent for intent in intents}
    facts = v2_models.WorkspaceMessageReactionFact.objects.get_all(
        filters={
            "project_id": filters.EQ(project_id),
            "canonical_message_uuid": filters.EQ(canonical_uuid),
        }
    )
    fact_by_key = {(fact.user_uuid, fact.emoji_name): fact for fact in facts}
    linked = {row["fact_uuid"] for row in observations if row["fact_uuid"] is not None}
    unresolved = {
        row["fact_uuid"] for row in observations if row["owner_user_uuid"] is None
    }
    keys = (
        set(groups)
        | set(by_key)
        | {key for key, fact in fact_by_key.items() if fact.uuid in linked}
    )
    placement = v2_models.WorkspaceMessagePlacement.objects.get_all(
        filters={
            "project_id": filters.EQ(project_id),
            "message_uuid": filters.EQ(canonical_uuid),
        },
        order_by={"uuid": "asc"},
        limit=1,
    )
    if not placement:
        return True
    previous = (
        _session()
        .execute(
            "SELECT current_setting('workspace.zulip_reaction_import',true) AS value"
        )
        .fetchone()["value"]
    )
    _session().execute("SELECT set_config('workspace.zulip_reaction_import','on',true)")
    for key in sorted(keys, key=lambda item: (str(item[0]), item[1])):
        rows = groups.get(key, [])
        intent = by_key.get(key)
        fact = fact_by_key.get(key)
        if fact is not None and fact.uuid in unresolved:
            continue
        present_rows = [row for row in rows if row["present"]]
        candidates = present_rows or rows
        remote_time = max((row["observed_at"] for row in candidates), default=None)
        if not rows:
            remote_time = max(
                (
                    row["snapshot_at"]
                    for row in messages
                    if row["snapshot_at"] is not None
                ),
                default=None,
            )
        if intent is not None and (
            remote_time is None
            or intent["status"] != "done"
            or _naive(intent["changed_at"]) > remote_time
        ):
            present = intent["present"]
        else:
            present = bool(present_rows)
        if present and fact is None:
            fact = v2_models.WorkspaceMessageReactionFact(
                uuid=uuid.uuid5(canonical_uuid, f"{key[0]}:reaction:{key[1]}"),
                project_id=project_id,
                canonical_message_uuid=canonical_uuid,
                placement_uuid=placement[0].uuid,
                user_uuid=key[0],
                emoji_name=key[1],
                source_name="zulip" if rows else "native",
                status="active",
                provider_uuid=rows[0]["provider_uuid"] if rows else None,
                provider_identity=f"{rows[0]['message_id']}:{rows[0]['user_id']}:{key[1]}"
                if rows
                else None,
            )
            fact.insert()
            _publish(fact, "created")
        elif not present and fact is not None:
            fact.delete()
            _publish(fact, "deleted")
            fact = None
        for row in rows:
            _session().execute(
                "UPDATE bridge_zulip_v5_reactions SET fact_uuid=%s WHERE uuid=%s",
                (fact.uuid if fact is not None else None, row["uuid"]),
            )
    _session().execute(
        "SELECT set_config('workspace.zulip_reaction_import',%s,true)",
        (previous or "off",),
    )
    return deferred


class ReactionChange(models.ModelWithID, models.ModelWithProject, orm.SQLStorableMixin):
    __tablename__ = "bridge_zulip_v5_reaction_changes"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    canonical_message_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    revision = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    message_created_at = properties.property(
        types.UTCDateTimeZ(),
        default=datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
        read_only=True,
    )
    message_sequence = properties.property(
        types.Integer(min_value=0), default=0, read_only=True
    )
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[uuid.UUID, uuid.UUID]:
        return self.project_id, self.canonical_message_uuid

    @classmethod
    def reconcile_batch(cls, limit: int) -> int:
        for message in (
            _session()
            .execute(
                """SELECT * FROM bridge_zulip_v5_reaction_messages
               WHERE canonical_message_uuid IS NULL AND (next_retry_at IS NULL OR next_retry_at<=NOW() AT TIME ZONE 'UTC')
               ORDER BY message_id DESC,uuid LIMIT %s FOR UPDATE SKIP LOCKED""",
                (limit,),
            )
            .fetchall()
        ):
            bind(message)
        if mutation_journal_opts.enabled() and owned_projection.CURRENT.get() is None:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc)
        changes = cls.objects.get_all(
            filters=filters.OR(
                {"next_retry_at": filters.Is(None)}, {"next_retry_at": filters.LE(now)}
            ),
            order_by={
                "message_created_at": "desc",
                "message_sequence": "desc",
                "revision": "asc",
            },
            limit=limit,
        )
        for change in changes:
            if reconcile(change.project_id, change.canonical_message_uuid):
                _session().execute(
                    "UPDATE bridge_zulip_v5_reaction_changes SET next_retry_at=%s WHERE project_id=%s AND canonical_message_uuid=%s AND revision=%s",
                    (
                        _naive(now + datetime.timedelta(seconds=5)),
                        change.project_id,
                        change.canonical_message_uuid,
                        change.revision,
                    ),
                )
            else:
                _session().execute(
                    "DELETE FROM bridge_zulip_v5_reaction_changes WHERE project_id=%s AND canonical_message_uuid=%s AND revision=%s",
                    (change.project_id, change.canonical_message_uuid, change.revision),
                )
        return len(changes)
