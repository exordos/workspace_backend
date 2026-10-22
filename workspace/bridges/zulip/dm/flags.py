# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Reconcile personal message flags using their original observation times."""

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

from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import owned_projection

from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import v2_models
from workspace.bridges.zulip import client


if typing.TYPE_CHECKING:
    from workspace.bridges.zulip.dm import models as zulip_models


FLAGS = ("read", "starred")


def prepare_outgoing(
    task: "zulip_models.Task",
) -> (
    tuple[external_models.ExternalAccount, list[dict[str, typing.Any]], str, bool]
    | None
):
    """Read a versioned intent without holding application rows over HTTP."""
    from workspace.bridges.zulip.dm import models as zulip_models

    content = task.content
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
    targets = flag_states(
        task.project_id, content.state_uuid, account.uuid, content.flag
    )
    if not targets:
        return None
    target = targets[0]
    changed_at = content.changed_at.replace(tzinfo=None)
    if (
        target["local_updated_at"] != changed_at
        or target["local_value"] != content.value
        or (
            target["remote_updated_at"] is not None
            and target["remote_updated_at"] >= changed_at
        )
    ):
        return None
    batch = outgoing_batch(
        task.project_id, account.uuid, content.state_uuid, content.flag, content.value
    )
    return (account, batch, content.flag, content.value) if batch else None


def send_prepared(
    prepared: tuple[
        external_models.ExternalAccount, list[dict[str, typing.Any]], str, bool
    ],
) -> None:
    account, batch, flag, value = prepared
    if mutation_journal_opts.enabled():
        from workspace.bridges.zulip.dm import models as zulip_models

        zulip_models.validate_outgoing_account(account)
    zulip = client.ZulipClient(account)
    try:
        result = zulip.update_message_flags(
            sorted({row["message_id"] for row in batch}, reverse=True), flag, value
        )
        if result.get("ignored_because_not_subscribed_channels"):
            raise client.ZulipApiError(
                "NOT_SUBSCRIBED",
                "Cannot mark messages in unsubscribed Zulip channels as unread",
            )
    finally:
        zulip.close()


def confirm_prepared(
    prepared: tuple[
        external_models.ExternalAccount, list[dict[str, typing.Any]], str, bool
    ],
) -> None:
    account, batch, flag, value = prepared
    by_time: dict[datetime.datetime, set[int]] = {}
    for row in batch:
        by_time.setdefault(row["local_updated_at"], set()).add(row["message_id"])
    for observed_at, ids in sorted(by_time.items()):
        record(
            account,
            sorted(ids),
            flag,
            value,
            observed_at.replace(tzinfo=datetime.timezone.utc),
        )


def record(
    account: external_models.ExternalAccount,
    message_ids: list[int],
    flag: str,
    value: bool,
    observed_at: datetime.datetime,
    event_id: int = -1,
) -> None:
    if mutation_journal_opts.enabled():
        _record_owned(
            account,
            [(message_id, flag, value) for message_id in message_ids],
            observed_at,
            event_id,
        )
        return
    session = contexts.Context().get_session()
    session.execute(
        """
        INSERT INTO bridge_zulip_v5_message_flags
            (project_id, external_account_uuid, message_id, flag, value, updated_at, event_id)
        SELECT %s, %s, message_id, %s, %s, %s, %s FROM unnest(%s::bigint[]) AS message_id
        WHERE %s < 0 OR message_id=0 OR NOT EXISTS (
            SELECT 1 FROM bridge_zulip_v5_history_batches AS batch
            WHERE batch.external_account_uuid=%s AND batch.first_id=(message_id-1)/1000*1000+1
              AND message_id<=batch.covered_to AND batch.observed_at >= %s)
        ON CONFLICT (external_account_uuid, message_id, flag) DO UPDATE
            SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at, event_id=EXCLUDED.event_id
            WHERE (bridge_zulip_v5_message_flags.updated_at, bridge_zulip_v5_message_flags.event_id)
                < (EXCLUDED.updated_at, EXCLUDED.event_id)
        """,
        (
            account.project_id,
            account.uuid,
            flag,
            value,
            observed_at.replace(tzinfo=None),
            event_id,
            message_ids,
            event_id,
            account.uuid,
            observed_at.replace(tzinfo=None),
        ),
    )
    # Keep the imported snapshot consistent without changing its observation time.
    session.execute(
        f"""
        UPDATE bridge_zulip_v5_messages AS observed
        SET {flag}=latest.value, updated_at=NOW()
        FROM bridge_zulip_v5_message_flags AS latest
        WHERE observed.external_account_uuid=%s AND observed.project_id=%s
          AND latest.external_account_uuid=observed.external_account_uuid
          AND latest.message_id=observed.message_id AND latest.flag=%s
          AND observed.message_id=ANY(%s::bigint[])
          AND observed.{flag} IS DISTINCT FROM latest.value
        """,
        (account.uuid, account.project_id, flag, message_ids),
    )


def _record_owned(
    account: external_models.ExternalAccount,
    observations: list[tuple[int, str, bool]],
    observed_at: datetime.datetime,
    event_id: int = -1,
) -> None:
    if not observations:
        return
    session = contexts.Context().get_session()
    previous = session.execute(
        "SELECT current_setting('workspace.owned_mutations',true) AS owned, "
        "current_setting('workspace.batch_flags',true) AS batch"
    ).fetchone()
    session.execute(
        "SELECT set_config('workspace.owned_mutations','on',true), "
        "set_config('workspace.batch_flags','on',true)"
    )
    message_ids, names, values = map(list, zip(*observations))
    changed = session.execute(
        """INSERT INTO bridge_zulip_v5_message_flags
             (project_id,external_account_uuid,message_id,flag,value,updated_at,event_id)
           SELECT %s,%s,input.message_id,input.flag,input.value,%s,%s
           FROM unnest(%s::bigint[],%s::text[],%s::boolean[]) AS input(message_id,flag,value)
           WHERE %s < 0 OR input.message_id=0 OR NOT EXISTS (
             SELECT 1 FROM bridge_zulip_v5_history_batches AS batch
             WHERE batch.external_account_uuid=%s
               AND batch.first_id=(input.message_id-1)/1000*1000+1
               AND input.message_id<=batch.covered_to AND batch.observed_at >= %s)
           ORDER BY input.message_id,input.flag
           ON CONFLICT (external_account_uuid,message_id,flag) DO UPDATE
             SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at,event_id=EXCLUDED.event_id
             WHERE (bridge_zulip_v5_message_flags.updated_at,bridge_zulip_v5_message_flags.event_id)
                 < (EXCLUDED.updated_at,EXCLUDED.event_id)
           RETURNING message_id""",
        (
            account.project_id,
            account.uuid,
            observed_at.replace(tzinfo=None),
            event_id,
            message_ids,
            names,
            values,
            event_id,
            account.uuid,
            observed_at.replace(tzinfo=None),
        ),
    ).fetchall()
    mirrored = session.execute(
        """WITH latest AS (
             SELECT fact.message_id,
                    bool_or(fact.value) FILTER (WHERE fact.flag='read') AS read,
                    bool_or(fact.value) FILTER (WHERE fact.flag='starred') AS starred
             FROM bridge_zulip_v5_message_flags AS fact
             JOIN unnest(%s::bigint[],%s::text[]) AS input(message_id,flag)
               ON input.message_id=fact.message_id AND input.flag=fact.flag
             WHERE fact.external_account_uuid=%s GROUP BY fact.message_id
           ) UPDATE bridge_zulip_v5_messages AS observed SET
             read=COALESCE(latest.read,observed.read),
             starred=COALESCE(latest.starred,observed.starred),updated_at=NOW()
           FROM latest WHERE observed.project_id=%s AND observed.external_account_uuid=%s
             AND observed.message_id=latest.message_id
             AND (observed.read IS DISTINCT FROM COALESCE(latest.read,observed.read)
               OR observed.starred IS DISTINCT FROM COALESCE(latest.starred,observed.starred))
           RETURNING observed.message_id""",
        (message_ids, names, account.uuid, account.project_id, account.uuid),
    ).fetchall()
    changed_ids = sorted({row["message_id"] for row in [*changed, *mirrored]})
    if changed_ids:
        targets = """SELECT state.project_id,state.uuid AS state_uuid
            FROM messenger_user_message_states AS state
            JOIN messenger_message_placements AS placement
              ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
            JOIN account ON account.project_id=state.project_id AND account.owner_user_uuid=state.user_uuid
            WHERE placement.message_uuid IN (
              SELECT messenger_uuid_v5(source.project_id,source.message_uuid::text)
              FROM bridge_zulip_v5_messages AS source JOIN account
                ON source.external_account_uuid=account.uuid AND source.project_id=account.project_id
              WHERE source.message_id=ANY(%s::bigint[])
              UNION SELECT delivery.message_uuid FROM bridge_zulip_v5_deliveries AS delivery
              JOIN account ON delivery.project_id=account.project_id AND delivery.hostname=account.hostname
              WHERE delivery.zulip_message_id=ANY(%s::bigint[]))"""
        params = [account.uuid, account.project_id, changed_ids, changed_ids]
        if 0 in changed_ids:
            targets += """ UNION SELECT DISTINCT target.project_id,target.state_uuid
                FROM bridge_zulip_v5_flag_targets AS target
                JOIN account ON target.external_account_uuid=account.uuid
                JOIN messenger_user_message_states AS state
                  ON state.project_id=target.project_id AND state.uuid=target.state_uuid
                WHERE state.read_at IS NULL"""
        session.execute(
            """WITH account AS (
                 SELECT uuid,project_id,owner_user_uuid,
                        regexp_replace(external_account_id, ':[^:]+$', '') AS hostname
                 FROM m_external_accounts_v2 WHERE uuid=%s AND project_id=%s
               ), targets AS ("""
            + targets
            + """ )
               INSERT INTO bridge_zulip_v5_flag_changes(project_id,state_uuid)
               SELECT project_id,state_uuid FROM targets ORDER BY project_id,state_uuid
               ON CONFLICT (project_id,state_uuid) DO UPDATE SET revision=EXCLUDED.revision""",
            tuple(params),
        )
    session.execute(
        "SELECT set_config('workspace.batch_flags',%s,true), "
        "set_config('workspace.owned_mutations',%s,true)",
        (previous["batch"] or "", previous["owned"] or ""),
    )


def observe_snapshot(
    account: external_models.ExternalAccount,
    messages: list[dict],
    observed_at: datetime.datetime,
) -> dict[int, dict[str, bool]]:
    if not messages:
        return {}
    if mutation_journal_opts.enabled():
        _record_owned(
            account,
            [
                (data["id"], flag, flag in data["flags"])
                for data in messages
                for flag in FLAGS
            ],
            observed_at,
        )
    else:
        for flag in FLAGS:
            for value in (False, True):
                message_ids = [
                    data["id"] for data in messages if (flag in data["flags"]) == value
                ]
                if message_ids:
                    record(account, message_ids, flag, value, observed_at)
    rows = (
        contexts.Context()
        .get_session()
        .execute(
            """SELECT message_id, flag, value FROM bridge_zulip_v5_message_flags
           WHERE external_account_uuid=%s AND message_id=ANY(%s::bigint[])""",
            (account.uuid, [data["id"] for data in messages]),
        )
        .fetchall()
    )
    result: dict[int, dict[str, bool]] = {}
    for row in rows:
        result.setdefault(row["message_id"], {})[row["flag"]] = row["value"]
    return result


def flag_states(
    project_id: uuid.UUID,
    state_uuid: uuid.UUID,
    account_uuid: uuid.UUID | None = None,
    flag: str | None = None,
) -> list[dict[str, typing.Any]]:
    return (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT * FROM bridge_zulip_v5_probe_flag_states(%s,%s,%s,%s)",
            (project_id, state_uuid, account_uuid, flag),
        )
        .fetchall()
    )


def lock_state(
    project_id: uuid.UUID, state_uuid: uuid.UUID, account_uuid: uuid.UUID
) -> v2_models.WorkspaceUserMessageState:
    session = contexts.Context().get_session()
    if owned_projection.CURRENT.get() is None:
        session.execute(
            "SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s AND uuid=%s FOR UPDATE",
            (project_id, account_uuid),
        )
    return v2_models.WorkspaceUserMessageState.objects.get_one(
        filters={"project_id": filters.EQ(project_id), "uuid": filters.EQ(state_uuid)},
        locked=owned_projection.CURRENT.get() is None,
    )


def outgoing_batch(
    project_id: uuid.UUID,
    account_uuid: uuid.UUID,
    state_uuid: uuid.UUID,
    flag: str,
    value: bool,
) -> list[dict[str, typing.Any]]:
    candidates = (
        contexts.Context()
        .get_session()
        .execute(
            """SELECT DISTINCT flag_state_uuid AS state_uuid,message_created_at,message_sequence
           FROM bridge_zulip_v5_tasks
           WHERE project_id=%s AND external_account_uuid=%s
             AND content->>'kind'='update_message_flags' AND status IN ('new', 'retry')
             AND content->>'flag'=%s AND (content->>'value')::boolean=%s
           ORDER BY message_created_at DESC,message_sequence DESC,flag_state_uuid LIMIT 100""",
            (project_id, account_uuid, flag, value),
        )
        .fetchall()
    )
    result = []
    for candidate_uuid in sorted(
        {state_uuid, *(row["state_uuid"] for row in candidates)}
    ):
        if not mutation_journal_opts.enabled():
            lock_state(project_id, candidate_uuid, account_uuid)
        for target in flag_states(project_id, candidate_uuid, account_uuid, flag):
            if (
                target["local_value"] == value
                and target["local_updated_at"] is not None
                and (
                    target["remote_updated_at"] is None
                    or target["local_updated_at"] > target["remote_updated_at"]
                )
                and target["remote_value"] != value
            ):
                result.append(target)
    return result


def publish(
    state: v2_models.WorkspaceUserMessageState, target: typing.Mapping[str, typing.Any]
) -> None:
    session = contexts.Context().get_session()
    payload = {
        "source_kind": "message.updated",
        "placement_uuid": str(state.placement_uuid),
        "recipient_uuid": str(state.user_uuid),
    }
    session.execute(
        """INSERT INTO messenger_domain_outbox_events
               (uuid, project_id, event_kind, scope_kind, scope_key, payload)
           VALUES (%s,%s,'delivery_snapshot_event','message',%s,%s::jsonb)""",
        (
            uuid.uuid4(),
            state.project_id,
            f"{state.project_id}:{target['message_uuid']}",
            json.dumps(payload),
        ),
    )
    if target["flag"] != "read":
        return
    changes = owned_projection.CURRENT.get()
    if changes is not None:
        changes.topic(
            state.project_id,
            target["stream_uuid"],
            target["topic_uuid"],
            [state.user_uuid],
        )
        return
    for scope, scope_uuid in (
        ("user-stream", target["stream_uuid"]),
        ("user-topic", target["topic_uuid"]),
    ):
        payload = {
            "source_kind": "zulip.reconciled",
            "user_uuid": str(state.user_uuid),
            "stream_uuid": str(target["stream_uuid"]),
            "topic_uuid": str(target["topic_uuid"]),
        }
        session.execute(
            """INSERT INTO messenger_domain_outbox_events
                   (uuid, project_id, event_kind, scope_kind, scope_key, payload)
               VALUES (%s,%s,'read_counters',%s,%s,%s::jsonb)""",
            (
                uuid.uuid4(),
                state.project_id,
                scope,
                f"{state.project_id}:{state.user_uuid}:{scope_uuid}",
                json.dumps(payload),
            ),
        )


class FlagChange(models.ModelWithID, models.ModelWithProject, orm.SQLStorableMixin):
    __tablename__ = "bridge_zulip_v5_flag_changes"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    state_uuid = properties.property(
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

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[uuid.UUID, uuid.UUID]:
        return self.project_id, self.state_uuid

    @classmethod
    def reconcile_batch(
        cls, limit: int, *, state_ids: list[uuid.UUID] | None = None
    ) -> int:
        from workspace.bridges.zulip.dm import models as zulip_models

        if mutation_journal_opts.enabled() and owned_projection.CURRENT.get() is None:
            return 0
        changes = cls.objects.get_all(
            filters={"state_uuid": filters.In(state_ids)}
            if state_ids is not None
            else {},
            order_by={
                "message_created_at": "desc",
                "message_sequence": "desc",
                "revision": "asc",
            },
            limit=limit,
        )
        owned = owned_projection.CURRENT.get() is not None
        if owned:
            changes.sort(
                key=lambda change: (change.project_id.int, change.state_uuid.int)
            )
            session = contexts.Context().get_session()
            previous_batch = session.execute(
                "SELECT current_setting('workspace.batch_flags',true) AS value"
            ).fetchone()["value"]
            session.execute("SELECT set_config('workspace.batch_flags','on',true)")
        processed = 0
        for change in changes:
            verifying_account = False
            changed_target = None
            targets = flag_states(change.project_id, change.state_uuid)
            for target in targets:
                state = lock_state(
                    change.project_id,
                    change.state_uuid,
                    target["external_account_uuid"],
                )
                current = flag_states(
                    change.project_id,
                    change.state_uuid,
                    target["external_account_uuid"],
                    target["flag"],
                )
                if not current:
                    continue
                target = current[0]
                flag = target["flag"]
                local_time = target["local_updated_at"]
                remote_time = target["remote_updated_at"]
                if remote_time is not None and (
                    local_time is None or remote_time >= local_time
                ):
                    value = target["remote_value"]
                    changed = target["local_value"] != value
                    observed_at = remote_time.replace(tzinfo=datetime.timezone.utc)
                    if flag == "read":
                        state.read_at = (
                            (state.read_at or observed_at) if value else None
                        )
                    else:
                        state.starred = value
                    setattr(state, f"{flag}_updated_at", observed_at)
                    if owned_projection.CURRENT.get() is None:
                        state.update()
                    else:
                        state.validate()
                        # ORM update includes immutable unique-key columns.
                        # Mentioning them upgrades the implicit row lock and
                        # blocks concurrent foreign-key checks from observers.
                        column = "read_at" if flag == "read" else "starred"
                        contexts.Context().get_session().execute(
                            f"""UPDATE messenger_user_message_states
                                SET {column}=%s, {flag}_updated_at=%s::timestamptz AT TIME ZONE 'UTC',
                                    updated_at=NOW() WHERE project_id=%s AND uuid=%s""",
                            (
                                getattr(state, column),
                                observed_at,
                                state.project_id,
                                state.uuid,
                            ),
                        )
                    if changed and local_time == remote_time:
                        # Events from one poll share a timestamp. Preserve it
                        # when the local-write trigger sees an unchanged stamp.
                        contexts.Context().get_session().execute(
                            f"UPDATE messenger_user_message_states SET {flag}_updated_at=%s WHERE project_id=%s AND uuid=%s",
                            (remote_time, state.project_id, state.uuid),
                        )
                    if changed and (changed_target is None or flag == "read"):
                        changed_target = target
                elif (
                    local_time is not None
                    and target["local_value"] != target["remote_value"]
                ):
                    account = external_models.ExternalAccount.objects.get_one(
                        filters={
                            "uuid": filters.EQ(target["external_account_uuid"]),
                            "project_id": filters.EQ(change.project_id),
                        }
                    )
                    if not account.can_stream():
                        verifying_account |= (
                            account.status
                            == external_models.ExternalAccountStatus.NEW.value
                            and bool(account.settings.get("api_key"))
                        )
                        continue
                    account_revision = account.realtime_revision()
                    changed_at = local_time.replace(tzinfo=datetime.timezone.utc)
                    task_uuid = uuid.uuid5(
                        state.uuid,
                        f"zulip-flags:{target['external_account_uuid']}:{flag}:{changed_at.isoformat()}:{account_revision}",
                    )
                    if (
                        zulip_models.Task.objects.get_one_or_none(
                            filters={"uuid": filters.EQ(task_uuid)}
                        )
                        is None
                    ):
                        zulip_models.Task(
                            uuid=task_uuid,
                            project_id=change.project_id,
                            external_account_uuid=target["external_account_uuid"],
                            priority=zulip_models.TASK_PRIORITY_REALTIME_FLAGS,
                            content=zulip_models.UpdateMessageFlagsContent(
                                state_uuid=state.uuid,
                                flag=flag,
                                value=target["local_value"],
                                changed_at=changed_at,
                                account_revision=account_revision,
                            ),
                        ).insert()
            if changed_target is not None:
                publish(state, changed_target)
            if verifying_account:
                continue
            contexts.Context().get_session().execute(
                "DELETE FROM bridge_zulip_v5_flag_changes WHERE project_id=%s AND state_uuid=%s AND revision=%s",
                (change.project_id, change.state_uuid, change.revision),
            )
            processed += 1
        if owned:
            session.execute(
                "SELECT set_config('workspace.batch_flags',%s,true)",
                (previous_batch or "",),
            )
        return processed
