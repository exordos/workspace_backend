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

from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import v2_models


FLAGS = ("read", "starred")


def record(
    account: external_models.ExternalAccount,
    message_ids: list[int],
    flag: str,
    value: bool,
    observed_at: datetime.datetime,
    event_id: int = -1,
) -> None:
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


def observe_snapshot(
    account: external_models.ExternalAccount,
    messages: list[dict],
    observed_at: datetime.datetime,
) -> dict[int, dict[str, bool]]:
    if not messages:
        return {}
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
            """SELECT * FROM bridge_zulip_v5_flag_states
           WHERE project_id=%s AND state_uuid=%s
             AND (%s::uuid IS NULL OR external_account_uuid=%s)
             AND (%s::text IS NULL OR flag=%s)
           ORDER BY external_account_uuid, flag""",
            (project_id, state_uuid, account_uuid, account_uuid, flag, flag),
        )
        .fetchall()
    )


def lock_state(
    project_id: uuid.UUID, state_uuid: uuid.UUID, account_uuid: uuid.UUID
) -> v2_models.WorkspaceUserMessageState:
    session = contexts.Context().get_session()
    session.execute(
        "SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s AND uuid=%s FOR UPDATE",
        (project_id, account_uuid),
    )
    return v2_models.WorkspaceUserMessageState.objects.get_one(
        filters={"project_id": filters.EQ(project_id), "uuid": filters.EQ(state_uuid)},
        locked=True,
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
    def reconcile_batch(cls, limit: int) -> int:
        from workspace.bridges.zulip.dm import models as zulip_models

        changes = cls.objects.get_all(
            order_by={
                "message_created_at": "desc",
                "message_sequence": "desc",
                "revision": "asc",
            },
            limit=limit,
        )
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
                    state.update()
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
        return processed
