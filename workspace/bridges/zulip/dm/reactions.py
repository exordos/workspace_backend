# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Shared reaction observations and durable local add/remove intentions."""

import datetime
import json
import uuid

import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.bridges.zulip import client
from workspace.common import emoji
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import v2_models


def _session():
    return contexts.Context().get_session()


def _naive(value):
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _enqueue(project_id, message_uuid):
    _session().execute(
        """INSERT INTO bridge_zulip_v5_reaction_changes(project_id, canonical_message_uuid)
           SELECT %s,%s WHERE EXISTS (
               SELECT 1 FROM bridge_zulip_v5_reactions AS reaction
               JOIN bridge_zulip_v5_reaction_messages AS message ON message.uuid=reaction.reaction_message_uuid
               WHERE message.project_id=%s AND message.canonical_message_uuid=%s)
           OR EXISTS (SELECT 1 FROM bridge_zulip_v5_reaction_intents
               WHERE project_id=%s AND canonical_message_uuid=%s)
           ON CONFLICT (project_id, canonical_message_uuid) DO UPDATE
               SET revision=EXCLUDED.revision, next_retry_at=NULL""",
        (project_id, message_uuid, project_id, message_uuid, project_id, message_uuid),
    )


def message_for(account, message_id):
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


def bind(message):
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


def _record(message, data, present, observed_at, event_id=-1, queue_id=None):
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


def record_event(account, data, observed_at, event_id, queue_id):
    message = message_for(account, data["message_id"])
    if (
        message["snapshot_at"] is not None
        and _naive(observed_at) <= message["snapshot_at"]
    ):
        return
    _record(message, data, data["op"] == "add", observed_at, event_id, queue_id)


def observe_snapshot(account, messages, observed_at):
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


def _publish(fact, kind):
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


def _targets(project_id, canonical_uuid, user_uuid):
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


def remote_state(message, user_id, code):
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


def _schedule(intent, observations):
    from workspace.bridges.zulip.dm import models as zulip_models

    code = emoji.CODE_BY_NAME.get(intent.emoji_name)
    if code is None:
        code = next(
            (
                row["emoji_code"]
                for row in observations
                if row["reaction_type"] == "unicode_emoji"
                and row["emoji_name"] == intent.emoji_name
            ),
            None,
        )
    if code is None:
        description = f"Unsupported Unicode emoji: {intent.emoji_name}"
        _session().execute(
            "UPDATE bridge_zulip_v5_reaction_intents SET error_description=%s WHERE uuid=%s AND error_description IS DISTINCT FROM %s",
            (description, intent.uuid, description),
        )
        return False
    targets = _targets(
        intent.project_id, intent.canonical_message_uuid, intent.user_uuid
    )
    for target in targets:
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(target["external_account_uuid"])}
        )
        if (
            not account.can_stream()
            or account.external_account_id.rsplit(":", 1)[0] != account.get_host()
        ):
            continue
        message = message_for(account, target["message_id"])
        user_id = int(account.external_account_id.rsplit(":", 1)[1])
        value, at, _name = remote_state(message, user_id, code)
        if value == intent.present or (
            at is not None and at >= _naive(intent.changed_at)
        ):
            continue
        revision = account.realtime_revision()
        task_uuid = uuid.uuid5(
            intent.uuid,
            f"{message['uuid']}:{intent.changed_at.isoformat()}:{intent.present}:{revision}",
        )
        if (
            zulip_models.Task.objects.get_one_or_none(
                filters={"uuid": filters.EQ(task_uuid)}
            )
            is None
        ):
            zulip_models.Task(
                uuid=task_uuid,
                project_id=intent.project_id,
                external_account_uuid=account.uuid,
                priority=zulip_models.TASK_PRIORITY_REALTIME_FLAGS,
                content=zulip_models.UpdateReactionContent(
                    intent_uuid=intent.uuid,
                    reaction_message_uuid=message["uuid"],
                    changed_at=intent.changed_at,
                    present=intent.present,
                    account_revision=revision,
                ),
            ).insert()
    return not targets and any(
        account.can_stream()
        for account in external_models.ExternalAccount.objects.get_all(
            filters={
                "project_id": filters.EQ(intent.project_id),
                "owner_user_uuid": filters.EQ(intent.user_uuid),
                "provider": filters.EQ("zulip"),
            },
        )
    )


def reconcile(project_id, canonical_uuid):
    # Keep the same account-before-message lock order as outgoing HTTP tasks.
    _session().execute(
        """SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s AND provider='zulip'
           AND (owner_user_uuid IN (SELECT user_uuid FROM bridge_zulip_v5_reaction_intents
                   WHERE project_id=%s AND canonical_message_uuid=%s)
                OR uuid IN (SELECT observer_account_uuid FROM bridge_zulip_v5_reaction_messages
                   WHERE project_id=%s AND canonical_message_uuid=%s))
           ORDER BY uuid FOR UPDATE""",
        (project_id, project_id, canonical_uuid, project_id, canonical_uuid),
    )
    messages = (
        _session()
        .execute(
            "SELECT * FROM bridge_zulip_v5_reaction_messages WHERE project_id=%s AND canonical_message_uuid=%s ORDER BY uuid FOR UPDATE",
            (project_id, canonical_uuid),
        )
        .fetchall()
    )
    observations = (
        _session()
        .execute(
            """SELECT reaction.*,actor.owner_user_uuid,message.hostname
           FROM bridge_zulip_v5_reactions AS reaction
           JOIN bridge_zulip_v5_reaction_messages AS message ON message.uuid=reaction.reaction_message_uuid
           LEFT JOIN m_external_accounts_v2 AS actor
             ON actor.project_id=message.project_id AND actor.provider='zulip'
            AND actor.external_account_id=message.hostname || ':' || reaction.user_id::text
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
    groups = {}
    for row in observations:
        if (
            row["reaction_type"] == "unicode_emoji"
            and row["owner_user_uuid"] is not None
        ):
            name = emoji.name_for_code(row["emoji_code"], row["emoji_name"])
            groups.setdefault((row["owner_user_uuid"], name), []).append(row)
    intents = ReactionIntent.objects.get_all(
        filters={
            "project_id": filters.EQ(project_id),
            "canonical_message_uuid": filters.EQ(canonical_uuid),
        }
    )
    by_key = {(intent.user_uuid, intent.emoji_name): intent for intent in intents}
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
            remote_time is None or _naive(intent.changed_at) > remote_time
        ):
            present = intent.present
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
    for intent in intents:
        deferred = _schedule(intent, observations) or deferred
    return deferred


def send(content, task):
    from workspace.bridges.zulip.dm import models as zulip_models

    _session().execute(
        "SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s AND uuid=%s FOR UPDATE",
        (task.project_id, task.external_account_uuid),
    )
    account = external_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": filters.EQ(task.external_account_uuid),
            "project_id": filters.EQ(task.project_id),
        }
    )
    if (
        not account.can_stream()
        or account.realtime_revision() != content.account_revision
    ):
        raise zulip_models.SubscriptionChangedError()
    message = (
        _session()
        .execute(
            "SELECT * FROM bridge_zulip_v5_reaction_messages WHERE uuid=%s AND project_id=%s FOR UPDATE",
            (content.reaction_message_uuid, task.project_id),
        )
        .fetchone()
    )
    intent = ReactionIntent.objects.get_one(
        filters={
            "uuid": filters.EQ(content.intent_uuid),
            "project_id": filters.EQ(task.project_id),
        }
    )
    if intent.changed_at != content.changed_at or intent.present != content.present:
        return
    if (
        account.owner_user_uuid != intent.user_uuid
        or message["hostname"] != account.get_host()
        or account.external_account_id.rsplit(":", 1)[0] != message["hostname"]
        or message["canonical_message_uuid"] != intent.canonical_message_uuid
    ):
        raise zulip_models.SubscriptionChangedError()
    if not any(
        row["external_account_uuid"] == account.uuid
        and row["message_id"] == message["message_id"]
        for row in _targets(
            task.project_id, intent.canonical_message_uuid, intent.user_uuid
        )
    ):
        raise zulip_models.SubscriptionChangedError()
    code = emoji.CODE_BY_NAME.get(intent.emoji_name)
    if code is None:
        code = (
            _session()
            .execute(
                "SELECT emoji_code FROM bridge_zulip_v5_reactions WHERE reaction_message_uuid=%s AND reaction_type='unicode_emoji' AND emoji_name=%s LIMIT 1",
                (message["uuid"], intent.emoji_name),
            )
            .fetchone()["emoji_code"]
        )
    user_id = int(account.external_account_id.rsplit(":", 1)[1])
    value, at, name = remote_state(message, user_id, code)
    if value == intent.present or (at is not None and at >= _naive(intent.changed_at)):
        return
    zulip = client.ZulipClient(account)
    name = name or emoji.ZULIP_NAME_BY_CODE.get(code, intent.emoji_name)
    try:
        try:
            zulip.update_reaction(
                message["message_id"],
                name,
                code,
                "unicode_emoji",
                intent.present,
            )
        except (requests.HTTPError, client.ZulipApiError) as error:
            if (
                isinstance(error, requests.HTTPError)
                and error.response.status_code != 400
            ):
                raise
            code_error = (
                error.code
                if isinstance(error, client.ZulipApiError)
                else error.response.json()["code"]
            )
            expected = (
                "REACTION_ALREADY_EXISTS"
                if intent.present
                else "REACTION_DOES_NOT_EXIST"
            )
            if code_error != expected:
                raise
        _record(
            message,
            {
                "user_id": user_id,
                "emoji_code": code,
                "emoji_name": name,
                "reaction_type": "unicode_emoji",
            },
            intent.present,
            intent.changed_at,
        )
    finally:
        zulip.close()


class ReactionIntent(
    models.ModelWithUUID, models.ModelWithProject, orm.SQLStorableMixin
):
    __tablename__ = "bridge_zulip_v5_reaction_intents"

    canonical_message_uuid = properties.property(
        types.UUID(), required=True, read_only=True
    )
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    emoji_name = properties.property(types.String(max_length=128), required=True)
    present = properties.property(types.Boolean(), required=True)
    changed_at = properties.property(types.UTCDateTimeZ(), required=True)
    error_description = properties.property(
        types.AllowNone(types.String()), default=None
    )


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
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )

    @classmethod
    def get_id_property(cls):
        return cls.id_properties.copy()

    def get_id(self):
        return self.project_id, self.canonical_message_uuid

    @classmethod
    def reconcile_batch(cls, limit):
        for message in (
            _session()
            .execute(
                """SELECT * FROM bridge_zulip_v5_reaction_messages
               WHERE canonical_message_uuid IS NULL AND (next_retry_at IS NULL OR next_retry_at<=NOW() AT TIME ZONE 'UTC')
               ORDER BY next_retry_at NULLS FIRST,uuid LIMIT %s FOR UPDATE SKIP LOCKED""",
                (limit,),
            )
            .fetchall()
        ):
            bind(message)
        now = datetime.datetime.now(datetime.timezone.utc)
        changes = cls.objects.get_all(
            filters=filters.OR(
                {"next_retry_at": filters.Is(None)}, {"next_retry_at": filters.LE(now)}
            ),
            order_by={"revision": "asc"},
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
