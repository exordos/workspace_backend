#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import datetime
import json
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exc
from restalchemy.storage.sql import sessions

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import message_payloads, models, schema, v2_models

ALL_CHATS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000000")
PERSONAL_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
CHANNELS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
TOPIC_NOTIFICATION_MODES = {
    models.WorkspaceTopicNotificationMode.MUTE.value,
    models.WorkspaceTopicNotificationMode.DEFAULT.value,
    models.WorkspaceTopicNotificationMode.FOLLOW.value,
}
MUTED_STREAM_TOPIC_NOTIFICATION_MODES = TOPIC_NOTIFICATION_MODES | {
    models.WorkspaceTopicNotificationMode.UNMUTE.value,
}
WORKSPACE_USER_OFFLINE_TIMEOUT = datetime.timedelta(minutes=3)
WORKSPACE_USER_PRESENCE_EVENT_FIELDS = (
    "status",
    "status_emoji",
    "status_text",
)
DIRECT_STREAM_NAMESPACE = sys_uuid.UUID("26f42131-4503-5f59-8aa6-32b20f551751")
STREAM_SOURCE_IDENTITY_FIELDS = frozenset({"source_name", "source"})
DIRECT_STREAM_IDENTITY_FIELDS = frozenset(
    {"direct_user_uuid", "private_index", "private"}
)


@contextlib.contextmanager
def _workspace_session(
    session: typing.Any = None,
) -> typing.Iterator[typing.Any]:
    yield session if session is not None else contexts.Context().get_session()


def _lock_workspace_project_event_writes(
    project_id: object,
    session: typing.Any,
) -> None:
    """Serialize project event writes before mutating message flags."""
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return
    schema.lock_shared(session)
    execute(
        """
        SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
        """,
        (project_id,),
    )


def _database_timestamp(value: datetime.datetime) -> datetime.datetime:
    """Normalize UTC model timestamps for PostgreSQL columns without timezone."""
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _get_workspace_user_event_recipients(
    project_id: object,
    session: typing.Any = None,
) -> typing.Any:
    """Return users with materialized state in this project."""
    with _workspace_session(session) as current_session:
        rows = current_session.execute(
            """
            SELECT user_uuid FROM messenger_project_users
            WHERE project_id = %s ORDER BY user_uuid
            """,
            (project_id,),
        ).fetchall()
    return [row["user_uuid"] for row in rows]


def _get_workspace_event_project_ids() -> typing.Any:
    projects = models.WorkspaceProject.objects.get_all(
        order_by={"project_id": "asc"},
    )
    return [project.project_id for project in projects]


def _create_workspace_user_updated_events(
    project_id: object, user: typing.Any, session: typing.Any = None
) -> typing.Any:
    recipient_user_uuids = _get_workspace_user_event_recipients(
        project_id,
        session=session,
    )
    return messenger_events.create_user_updated_events(
        user=user,
        project_id=project_id,
        recipient_user_uuids=recipient_user_uuids,
        session=session,
    )


def _get_stale_workspace_users(cutoff: typing.Any) -> typing.Any:
    return models.WorkspaceUser.objects.get_all(
        filters=dm_filters.AND(
            {"source": dm_filters.EQ(models.WorkspaceUserSource.IAM.value)},
            {"status": dm_filters.NE(models.WorkspaceUserStatus.OFFLINE.value)},
            {"last_ping_at": dm_filters.LE(cutoff)},
        ),
        order_by={"uuid": "asc"},
    )


def _should_create_workspace_user_presence_event(
    user: typing.Any, values: typing.Any
) -> typing.Any:
    for field_name in WORKSPACE_USER_PRESENCE_EVENT_FIELDS:
        if field_name in values and getattr(user, field_name) != values[field_name]:
            return True
    return False


def get_workspace_own_user(
    user_uuid: object, current_user_uuid: object, session: typing.Any = None
) -> typing.Any:
    if user_uuid != current_user_uuid:
        raise storage_exc.RecordNotFound(
            model=models.WorkspaceUser.__name__,
            filters={"uuid": user_uuid},
        )
    return models.WorkspaceUser.objects.get_one(
        filters={"uuid": dm_filters.EQ(user_uuid)},
        session=session,
    )


def _normalize_workspace_draft_payload(payload: typing.Any) -> typing.Any:
    if isinstance(payload, dict):
        if payload["kind"] != message_payloads.MarkdownPayload.KIND:
            raise ra_exc.ValidationErrorException()
        content = payload["content"]
    else:
        if not isinstance(payload, message_payloads.MarkdownPayload):
            raise ra_exc.ValidationErrorException()
        content = payload.content
    content = content.strip()
    if not content or len(content) > message_payloads.MARKDOWN_CONTENT_MAX_LENGTH:
        raise ra_exc.ValidationErrorException()
    return message_payloads.MarkdownPayload(content=content)


def _lock_workspace_draft_scope(
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    session: typing.Any,
) -> typing.Any:
    stream = session.execute(
        """
        SELECT "uuid"
        FROM "messenger_streams"
        WHERE "uuid" = %s AND "project_id" = %s AND deleted_at IS NULL
        FOR KEY SHARE
        """,
        (stream_uuid, project_id),
    ).fetchone()
    if stream is None:
        return False
    topic = session.execute(
        """
        SELECT "uuid"
        FROM "messenger_topics"
        WHERE "uuid" = %s
            AND "project_id" = %s
            AND "stream_uuid" = %s AND deleted_at IS NULL
        FOR KEY SHARE
        """,
        (topic_uuid, project_id, stream_uuid),
    ).fetchone()
    if topic is None:
        return False
    binding = session.execute(
        """
        SELECT "uuid"
        FROM "messenger_stream_bindings"
        WHERE "project_id" = %s
            AND "stream_uuid" = %s
            AND "user_uuid" = %s AND active
        FOR KEY SHARE
        """,
        (project_id, stream_uuid, user_uuid),
    ).fetchone()
    return binding is not None


def _get_workspace_draft_scope(
    project_id: object,
    user_uuid: object,
    draft_uuid: object,
    session: typing.Any,
) -> typing.Any:
    return session.execute(
        """
        SELECT "stream_uuid", "topic_uuid"
        FROM "m_workspace_drafts"
        WHERE "uuid" = %s
            AND "project_id" = %s
            AND "user_uuid" = %s
        """,
        (draft_uuid, project_id, user_uuid),
    ).fetchone()


def _lock_workspace_stream(
    project_id: object, stream_uuid: object, session: typing.Any
) -> typing.Any:
    return session.execute(
        """
        SELECT "uuid"
        FROM "messenger_streams"
        WHERE "uuid" = %s AND "project_id" = %s
        FOR UPDATE
        """,
        (stream_uuid, project_id),
    ).fetchone()


def _lock_workspace_topic(
    project_id: object, topic_uuid: object, session: typing.Any
) -> typing.Any:
    scope = session.execute(
        """
        SELECT "stream_uuid"
        FROM "messenger_topics"
        WHERE "uuid" = %s AND "project_id" = %s
        """,
        (topic_uuid, project_id),
    ).fetchone()
    if scope is None:
        return None
    _lock_workspace_stream(project_id, scope["stream_uuid"], session)
    return session.execute(
        """
        SELECT "uuid", "stream_uuid"
        FROM "messenger_topics"
        WHERE "uuid" = %s AND "project_id" = %s
        FOR UPDATE
        """,
        (topic_uuid, project_id),
    ).fetchone()


def _lock_workspace_stream_binding(
    project_id: object, binding_uuid: object, session: typing.Any
) -> typing.Any:
    scope = session.execute(
        """
        SELECT "stream_uuid"
        FROM "messenger_stream_bindings"
        WHERE "uuid" = %s AND "project_id" = %s
        """,
        (binding_uuid, project_id),
    ).fetchone()
    if scope is None:
        return None
    _lock_workspace_stream(project_id, scope["stream_uuid"], session)
    return session.execute(
        """
        SELECT "uuid", "stream_uuid"
        FROM "messenger_stream_bindings"
        WHERE "uuid" = %s AND "project_id" = %s
        FOR UPDATE
        """,
        (binding_uuid, project_id),
    ).fetchone()


def get_workspace_draft(
    project_id: object,
    user_uuid: object,
    draft_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    return models.WorkspaceVisibleDraft.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(draft_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def create_workspace_draft(
    project_id: object,
    user_uuid: object,
    draft_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    payload: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    with _workspace_session(session=session) as s:
        payload = _normalize_workspace_draft_payload(payload)
        if not _lock_workspace_draft_scope(
            project_id,
            user_uuid,
            stream_uuid,
            topic_uuid,
            s,
        ):
            raise ra_exc.ValidationErrorException()
        result = s.execute(
            """
            INSERT INTO "m_workspace_drafts"
                ("uuid", "project_id", "user_uuid", "stream_uuid",
                 "topic_uuid", "payload")
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT ("uuid") DO NOTHING
            RETURNING "uuid"
            """,
            (
                draft_uuid,
                project_id,
                user_uuid,
                stream_uuid,
                topic_uuid,
                json.dumps(
                    {
                        "kind": message_payloads.MarkdownPayload.KIND,
                        "content": payload.content,
                    }
                ),
            ),
        )
        if result.fetchone() is not None:
            draft = get_workspace_draft(
                project_id,
                user_uuid,
                draft_uuid,
                session=s,
            )
            return draft, True
        existing = models.WorkspaceDraft.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(draft_uuid)},
            session=s,
        )
        if existing is not None:
            same_create = (
                existing.project_id == project_id
                and existing.user_uuid == user_uuid
                and existing.stream_uuid == stream_uuid
                and existing.topic_uuid == topic_uuid
                and existing.payload.content == payload.content
            )
            if not same_create:
                raise messenger_exc.DraftConflictError()
            return existing, False
        raise messenger_exc.DraftConflictError()


def update_workspace_draft(
    project_id: object,
    user_uuid: object,
    draft_uuid: object,
    payload: typing.Any,
    expected_revision: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    with _workspace_session(session=session) as s:
        payload = _normalize_workspace_draft_payload(payload)
        scope = _get_workspace_draft_scope(
            project_id,
            user_uuid,
            draft_uuid,
            s,
        )
        if scope is None:
            return (
                get_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    session=s,
                ),
                False,
            )
        if not _lock_workspace_draft_scope(
            project_id,
            user_uuid,
            scope["stream_uuid"],
            scope["topic_uuid"],
            s,
        ):
            return (
                get_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    session=s,
                ),
                False,
            )
        locked = s.execute(
            """
            SELECT "uuid"
            FROM "m_workspace_drafts"
            WHERE "uuid" = %s
                AND "project_id" = %s
                AND "user_uuid" = %s
            FOR UPDATE
            """,
            (draft_uuid, project_id, user_uuid),
        ).fetchone()
        if locked is None:
            return (
                get_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    session=s,
                ),
                False,
            )
        result = s.execute(
            """
            UPDATE "m_workspace_drafts"
            SET "payload" = %s::jsonb,
                "revision" = "revision" + 1,
                "updated_at" = NOW()
            WHERE "uuid" = %s
                AND "project_id" = %s
                AND "user_uuid" = %s
                AND "revision" = %s
            RETURNING "uuid"
            """,
            (
                json.dumps(
                    {
                        "kind": message_payloads.MarkdownPayload.KIND,
                        "content": payload.content,
                    }
                ),
                draft_uuid,
                project_id,
                user_uuid,
                expected_revision,
            ),
        )
        if result.fetchone() is None:
            draft = get_workspace_draft(
                project_id,
                user_uuid,
                draft_uuid,
                session=s,
            )
            return draft, False
        draft = get_workspace_draft(
            project_id,
            user_uuid,
            draft_uuid,
            session=s,
        )
        return draft, True


def delete_workspace_draft(
    project_id: object,
    user_uuid: object,
    draft_uuid: object,
    expected_revision: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    with _workspace_session(session=session) as s:
        scope = _get_workspace_draft_scope(
            project_id,
            user_uuid,
            draft_uuid,
            s,
        )
        if scope is None:
            return (
                get_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    session=s,
                ),
                False,
            )
        if not _lock_workspace_draft_scope(
            project_id,
            user_uuid,
            scope["stream_uuid"],
            scope["topic_uuid"],
            s,
        ):
            return (
                get_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    session=s,
                ),
                False,
            )
        locked = s.execute(
            """
            SELECT "uuid"
            FROM "m_workspace_drafts"
            WHERE "uuid" = %s
                AND "project_id" = %s
                AND "user_uuid" = %s
            FOR UPDATE
            """,
            (draft_uuid, project_id, user_uuid),
        ).fetchone()
        if locked is None:
            return (
                get_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    session=s,
                ),
                False,
            )
        draft = get_workspace_draft(
            project_id,
            user_uuid,
            draft_uuid,
            session=s,
        )
        if draft.revision != expected_revision:
            return draft, False
        models.WorkspaceDraft.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(user_uuid),
                "uuid": dm_filters.EQ(draft_uuid),
            },
            session=s,
        ).delete(session=s)
        return None, True


def update_workspace_user_presence(
    project_id: object,
    user_uuid: object,
    current_user_uuid: object,
    values: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    user = get_workspace_own_user(user_uuid, current_user_uuid, session=session)
    values = dict(values)
    should_create_event = _should_create_workspace_user_presence_event(
        user=user,
        values=values,
    )
    values["last_ping_at"] = datetime.datetime.now(datetime.timezone.utc)
    user.update_dm(values=values)
    user.update(session=session)
    if should_create_event:
        _create_workspace_user_updated_events(
            project_id=project_id,
            user=user,
            session=session,
        )
    return user


def update_workspace_user_avatar(
    user_uuid: object,
    current_user_uuid: object,
    avatar: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    user = get_workspace_own_user(user_uuid, current_user_uuid, session=session)
    if user.avatar == avatar:
        return user
    user.update_dm(values={"avatar": avatar})
    user.update(session=session)
    for project_id in _get_workspace_event_project_ids():
        _create_workspace_user_updated_events(
            project_id=project_id,
            user=user,
            session=session,
        )
    return user


def mark_stale_workspace_users_offline(
    now: typing.Any = None, session: typing.Any = None
) -> typing.Any:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - WORKSPACE_USER_OFFLINE_TIMEOUT
    users = list(_get_stale_workspace_users(cutoff))
    if not users:
        return users

    project_ids = _get_workspace_event_project_ids()
    for user in users:
        user.update_dm(
            values={"status": models.WorkspaceUserStatus.OFFLINE.value},
        )
        user.update(session=session)
        for project_id in project_ids:
            _create_workspace_user_updated_events(
                project_id=project_id,
                user=user,
                session=session,
            )
    return users


def get_workspace_user_folder_item(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    item_uuid: sys_uuid.UUID,
    session: sessions.PgSQLSession | None = None,
) -> v2_models.WorkspaceUserFolderItem:
    return v2_models.WorkspaceUserFolderItem.objects.get_one(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "uuid": dm_filters.EQ(item_uuid),
        },
        session=session,
    )


def get_workspace_owned_file(
    project_id: object, user_uuid: object, file_uuid: object
) -> typing.Any:
    return models.WorkspaceFile.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(file_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
    )


def get_or_create_workspace_file_access(
    project_id: object,
    file_uuid: object,
    user_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    access = models.WorkspaceFileAccess.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "file_uuid": dm_filters.EQ(file_uuid),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    if access is not None:
        return access

    access = models.WorkspaceFileAccess(
        project_id=project_id,
        file_uuid=file_uuid,
        user_uuid=user_uuid,
    )
    access.insert(session=session)
    return access


def _get_workspace_stream_files(project_id: object, stream_uuid: object) -> typing.Any:
    return models.WorkspaceFile.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
        },
    )


def _create_workspace_stream_binding_file_accesses(
    project_id: object,
    stream_uuid: object,
    user_uuid: object,
    session: typing.Any = None,
) -> None:
    for file in _get_workspace_stream_files(
        project_id=project_id,
        stream_uuid=stream_uuid,
    ):
        get_or_create_workspace_file_access(
            project_id=project_id,
            file_uuid=file.uuid,
            user_uuid=user_uuid,
            session=session,
        )


def create_workspace_file(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    uuid: sys_uuid.UUID,
    session: typing.Any = None,
    emit_events: bool = True,
    **values: typing.Any,
) -> typing.Any:
    if (
        "storage_type" not in values
        or "storage_id" not in values
        or "storage_object_id" not in values
    ):
        storage_info = file_storage.get_workspace_file_storage_info(
            file_uuid=uuid,
            storage_type=values.get("storage_type"),
            storage_object_id=values.get("storage_object_id"),
        )
        values.setdefault("storage_type", storage_info.storage_type)
        values.setdefault("storage_id", storage_info.storage_id)
        values.setdefault("storage_object_id", storage_info.storage_object_id)

    file = models.WorkspaceFile(
        uuid=uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        **values,
    )
    file.insert(session=session)
    recipient_user_uuids = _get_workspace_file_event_recipients(
        project_id,
        file,
        session=session,
    )
    if file.stream_uuid is not None:
        for recipient_user_uuid in recipient_user_uuids:
            get_or_create_workspace_file_access(
                project_id=project_id,
                file_uuid=file.uuid,
                user_uuid=recipient_user_uuid,
                session=session,
            )
    if emit_events:
        messenger_events.create_file_created_events(
            file,
            recipient_user_uuids,
            session=session,
        )
    return file


def create_workspace_avatar_file(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    uuid: sys_uuid.UUID,
    session: typing.Any = None,
    **values: typing.Any,
) -> typing.Any:
    file = models.WorkspaceFile(
        uuid=uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=None,
        acl_mode="public",
        **values,
    )
    file.insert(session=session)
    recipient_user_uuids = _get_workspace_file_event_recipients(
        project_id,
        file,
        session=session,
    )
    messenger_events.create_file_created_events(
        file,
        recipient_user_uuids,
        session=session,
    )
    return file


def _get_workspace_file_event_recipients(
    project_id: sys_uuid.UUID,
    file: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    if file.stream_uuid is not None:
        return models.get_stream_recipients(
            project_id=project_id, stream_uuid=file.stream_uuid, session=session
        )
    with _workspace_session(session) as current_session:
        rows = current_session.execute(
            "SELECT user_uuid FROM messenger_project_users WHERE project_id=%s ORDER BY user_uuid",
            (project_id,),
        ).fetchall()
    return sorted({file.user_uuid, *(row["user_uuid"] for row in rows)}, key=str)


def delete_workspace_avatar_file(
    user_uuid: object, file_uuid: object, session: typing.Any = None
) -> None:
    file = models.WorkspaceFile.objects.get_one_or_none(
        filters={
            "uuid": dm_filters.EQ(file_uuid),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    if file is not None and file.stream_uuid is None:
        recipient_user_uuids = _get_workspace_file_event_recipients(
            file.project_id,
            file,
            session=session,
        )
        messenger_events.create_file_deleted_events(
            file.project_id,
            file.stream_uuid,
            file.uuid,
            recipient_user_uuids,
            session=session,
        )
        file.delete(session=session)


def update_workspace_file(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    file_uuid: sys_uuid.UUID,
    values: typing.Any,
    session: typing.Any = None,
) -> typing.Any:
    file = get_workspace_owned_file(
        project_id=project_id,
        user_uuid=user_uuid,
        file_uuid=file_uuid,
    )
    file.update_dm(values=values)
    file.update(session=session)
    recipient_user_uuids = _get_workspace_file_event_recipients(
        project_id,
        file,
        session=session,
    )
    messenger_events.create_file_updated_events(
        file,
        recipient_user_uuids,
        session=session,
    )
    return file


def delete_workspace_file(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    file_uuid: sys_uuid.UUID,
    session: typing.Any = None,
) -> None:
    file = get_workspace_owned_file(
        project_id=project_id,
        user_uuid=user_uuid,
        file_uuid=file_uuid,
    )
    recipient_user_uuids = _get_workspace_file_event_recipients(
        project_id,
        file,
        session=session,
    )
    messenger_events.create_file_deleted_events(
        project_id,
        file.stream_uuid,
        file.uuid,
        recipient_user_uuids,
        session=session,
    )
    file.delete(session=session)


def deterministic_direct_stream_uuid(
    project_uuid: str | sys_uuid.UUID,
    first_user_uuid: str | sys_uuid.UUID,
    second_user_uuid: str | sys_uuid.UUID,
) -> sys_uuid.UUID:
    """Build the stable project-scoped UUID used by the existing UI contract."""
    user_values = sorted((str(first_user_uuid), str(second_user_uuid)))
    return sys_uuid.uuid5(
        DIRECT_STREAM_NAMESPACE,
        f"{project_uuid}:{user_values[0]}:{user_values[1]}",
    )


# These bounded projections reproduce the public collection rows without
# expanding the global visibility and unread views. They select legacy flags or
# compact bitmaps per project mode, so event snapshots cannot drift from a
# subsequent client read during cutover.


# Folder payloads include every item and are user-specific. Repeating those
# snapshots for each message would merely move the old amplification into a
# large JSON delta. The UI projects folder unread aggregates from the
# authoritative stream.updated snapshot; folder.updated remains reserved
# for actual folder mutations and explicit read reconciliation.


def delete_workspace_stream_binding(
    project_id: sys_uuid.UUID,
    binding_uuid: sys_uuid.UUID,
    session: typing.Any = None,
    defer_folder_events: bool = False,
) -> None:
    with _workspace_session(session) as current_session:
        _lock_workspace_project_event_writes(project_id, current_session)
        _lock_workspace_stream_binding(project_id, binding_uuid, current_session)
        binding = v2_models.WorkspaceStreamBinding.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "uuid": dm_filters.EQ(binding_uuid),
            },
            session=current_session,
        )
        stream = v2_models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "uuid": dm_filters.EQ(binding.stream_uuid),
            },
            session=current_session,
        )
        messenger_events.create_stream_deleted_event(
            project_id=project_id,
            user_uuid=binding.user_uuid,
            stream_uuid=stream.uuid,
            source_name=stream.source_name,
            source=stream.source,
            session=current_session,
        )
        current_session.execute(
            "UPDATE messenger_stream_bindings SET active=false, "
            "membership_generation=membership_generation+1, updated_at=NOW() "
            "WHERE project_id=%s AND uuid=%s AND active",
            (project_id, binding.uuid),
        )
        messenger_events.create_stream_binding_deleted_events(
            binding,
            models.get_stream_recipients(
                project_id, stream.uuid, session=current_session
            ),
            session=current_session,
        )
        _enqueue_stream_folders(
            project_id, binding.user_uuid, stream.uuid, stream.private, current_session
        )


def _validate_stream_binding_roles_payload(
    role_user_uuids: dict[str, object],
) -> None:
    allowed_roles = {role.value for role in models.WorkspaceStreamRole}
    for role, user_uuids in role_user_uuids.items():
        if role not in allowed_roles:
            raise messenger_exc.InvalidStreamBindingRoleError(role=role)
        if not isinstance(user_uuids, list):
            raise messenger_exc.StreamBindingUsersPayloadError()


def get_or_create_workspace_stream_bindings(
    project_id: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    who_uuid: object,
    role_user_uuids: typing.Any,
    session: typing.Any = None,
    binding_uuids: typing.Any = None,
    emit_events: bool = True,
    defer_folder_events: bool = False,
) -> typing.Any:
    _validate_stream_binding_roles_payload(role_user_uuids)
    with _workspace_session(session) as current_session:
        _lock_workspace_project_event_writes(project_id, current_session)
        _lock_workspace_stream(project_id, stream_uuid, current_session)
        now = current_session.execute("SELECT NOW() AS value").fetchone()["value"]
        bindings = []
        created = []
        for role, user_uuids in role_user_uuids.items():
            for user_uuid in user_uuids:
                current_session.execute(
                    "SELECT messenger_v2_register_project_user(%s,%s)",
                    (project_id, user_uuid),
                )
                binding = v2_models.WorkspaceStreamBinding.objects.get_one_or_none(
                    filters={
                        "project_id": dm_filters.EQ(project_id),
                        "stream_uuid": dm_filters.EQ(stream_uuid),
                        "user_uuid": dm_filters.EQ(user_uuid),
                    },
                    session=current_session,
                )
                changed = binding is None or not binding.active
                if binding is None:
                    binding = v2_models.WorkspaceStreamBinding(
                        uuid=(binding_uuids or {}).get(
                            str(user_uuid), sys_uuid.uuid4()
                        ),
                        project_id=project_id,
                        stream_uuid=stream_uuid,
                        user_uuid=user_uuid,
                        who_uuid=who_uuid,
                        role=role,
                        notification_updated_at=now.astimezone(datetime.timezone.utc),
                    )
                    binding.insert(session=current_session)
                elif not binding.active:
                    current_session.execute(
                        "UPDATE messenger_stream_bindings SET active=true, "
                        "membership_generation=membership_generation+1, "
                        "membership_started_at=NOW(), role=%s, who_uuid=%s, updated_at=NOW() "
                        "WHERE project_id=%s AND uuid=%s",
                        (role, who_uuid, project_id, binding.uuid),
                    )
                    binding = v2_models.WorkspaceStreamBinding.objects.get_one(
                        filters={
                            "project_id": dm_filters.EQ(project_id),
                            "uuid": dm_filters.EQ(binding.uuid),
                        },
                        session=current_session,
                    )
                bindings.append(binding)
                if not changed:
                    continue
                created.append(binding)
                current_session.execute(
                    "INSERT INTO messenger_user_topic_bindings "
                    "(uuid, project_id, user_uuid, topic_uuid, notification_mode) "
                    "SELECT messenger_uuid_v5(uuid, %s), project_id, %s, uuid, 'default' "
                    "FROM messenger_topics WHERE project_id=%s AND stream_uuid=%s AND deleted_at IS NULL "
                    "ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING",
                    (str(user_uuid), user_uuid, project_id, stream_uuid),
                )
                current_session.execute(
                    "INSERT INTO messenger_domain_outbox_events "
                    "(uuid, project_id, event_kind, scope_kind, scope_key, payload) "
                    "SELECT gen_random_uuid(), topic.project_id, "
                    "'topic_membership_policy_rebuild', 'topic', "
                    "topic.project_id::text || ':' || topic.uuid::text, "
                    "jsonb_build_object('source_kind', 'stream_binding.created', "
                    "'user_uuid', membership.user_uuid, "
                    "'membership_generation', membership.membership_generation, "
                    "'membership_started_at', membership.membership_started_at, "
                    "'stream_uuid', topic.stream_uuid, 'topic_uuid', topic.uuid) "
                    "FROM messenger_topics AS topic "
                    "JOIN messenger_stream_bindings AS membership "
                    "ON membership.project_id=topic.project_id "
                    "AND membership.stream_uuid=topic.stream_uuid "
                    "WHERE topic.project_id=%s AND topic.stream_uuid=%s "
                    "AND topic.deleted_at IS NULL AND membership.user_uuid=%s "
                    "AND membership.active",
                    (project_id, stream_uuid, user_uuid),
                )
                _create_workspace_stream_binding_file_accesses(
                    project_id,
                    stream_uuid,
                    user_uuid,
                    session=current_session,
                )
                if emit_events:
                    stream = v2_models.WorkspaceUserStream.objects.get_one(
                        filters={
                            "project_id": dm_filters.EQ(project_id),
                            "uuid": dm_filters.EQ(stream_uuid),
                            "user_uuid": dm_filters.EQ(user_uuid),
                        },
                        session=current_session,
                    )
                    messenger_events.create_stream_event(
                        stream, session=current_session
                    )
                    _enqueue_stream_folders(
                        project_id,
                        user_uuid,
                        stream_uuid,
                        stream.private,
                        current_session,
                    )
        if emit_events and created:
            recipients = models.get_stream_recipients(
                project_id, stream_uuid, session=current_session
            )
            for recipient in recipients:
                messenger_events.create_stream_bindings_created_event(
                    created,
                    recipient,
                    session=current_session,
                )
        return bindings


def get_workspace_user_stream_topic(
    project_id: object,
    user_uuid: object,
    topic_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    return v2_models.WorkspaceUserTopic.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(topic_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def _create_workspace_stream_topic_updated_events(
    project_id: object,
    topic_uuid: object,
    session: typing.Any = None,
) -> None:
    topics = v2_models.WorkspaceUserTopic.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(topic_uuid),
            "visible": dm_filters.EQ(True),
        },
        session=session,
    )
    messenger_events.create_topic_updated_events(
        project_id, topics, session=session, compact=True
    )


def _get_workspace_stream_topic_for_user(
    project_id: object,
    user_uuid: object,
    topic_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    user_topic = get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    return v2_models.WorkspaceStreamTopic.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(user_topic.uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )


def _get_workspace_topic_message(
    project_id: object,
    topic: typing.Any,
    message_uuid: object,
    session: typing.Any,
) -> typing.Any:
    row = session.execute(
        "SELECT placement.uuid, message.created_at FROM messenger_message_placements AS placement "
        "JOIN messenger_messages AS message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid "
        "WHERE placement.project_id=%s AND placement.topic_uuid=%s AND placement.uuid=%s "
        "AND message.deleted_at IS NULL",
        (project_id, topic.uuid, message_uuid),
    ).fetchone()
    if row is None:
        raise messenger_exc.InvalidTopicSummaryBoundaryError()
    return row


def set_workspace_user_stream_topic_summary(
    project_id: object,
    user_uuid: object,
    topic_uuid: object,
    summary: typing.Any,
    summary_last_message_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    if summary is None and summary_last_message_uuid is not None:
        raise messenger_exc.InvalidTopicSummaryStateError()

    with _workspace_session(session=session) as current_session:
        _lock_workspace_topic(project_id, topic_uuid, current_session)
        topic = _get_workspace_stream_topic_for_user(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
            session=current_session,
        )

        incoming_boundary = None
        if summary_last_message_uuid is not None:
            try:
                summary_last_message_uuid = sys_uuid.UUID(
                    str(summary_last_message_uuid)
                )
            except (TypeError, ValueError) as exc:
                raise messenger_exc.InvalidTopicSummaryBoundaryError() from exc
            incoming_boundary = _get_workspace_topic_message(
                project_id,
                topic,
                summary_last_message_uuid,
                current_session,
            )

        if summary is not None and topic.summary_last_message_uuid is not None:
            if incoming_boundary is None:
                raise messenger_exc.TopicSummaryConflictError()
            current_boundary = _get_workspace_topic_message(
                project_id,
                topic,
                topic.summary_last_message_uuid,
                current_session,
            )
            incoming_order = (
                incoming_boundary["created_at"],
                str(incoming_boundary["uuid"]),
            )
            current_order = (
                current_boundary["created_at"],
                str(current_boundary["uuid"]),
            )
            if incoming_order < current_order:
                raise messenger_exc.TopicSummaryConflictError()

        topic.update_dm(
            values={
                "summary": summary,
                "summary_last_message_uuid": summary_last_message_uuid,
            }
        )

        if summary is not None:
            if incoming_boundary is None:
                raise messenger_exc.InvalidTopicSummaryBoundaryError()
            current_session.execute(
                """
                INSERT INTO m_workspace_topic_summary_journal (
                    uuid, topic_uuid, project_id, summary,
                    boundary_message_uuid, boundary_message_created_at,
                    generated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    sys_uuid.uuid4(),
                    topic_uuid,
                    project_id,
                    summary,
                    incoming_boundary["uuid"],
                    _database_timestamp(incoming_boundary["created_at"]),
                ),
            )

        topic.update(session=current_session)
        _create_workspace_stream_topic_updated_events(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=current_session,
        )
        return get_workspace_user_stream_topic(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
            session=current_session,
        )


def _get_workspace_stream_topics(
    project_id: object,
    stream_uuid: object,
    session: typing.Any = None,
) -> typing.Any:
    return v2_models.WorkspaceStreamTopic.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
        },
        order_by={"created_at": "asc", "uuid": "asc"},
        session=session,
    )


def _validate_topic_notification_mode(
    stream_notification_mode: typing.Any, notification_mode: typing.Any
) -> None:
    if stream_notification_mode == models.WorkspaceStreamNotificationMode.MUTED.value:
        allowed_modes = MUTED_STREAM_TOPIC_NOTIFICATION_MODES
    else:
        allowed_modes = TOPIC_NOTIFICATION_MODES
    if notification_mode not in allowed_modes:
        raise messenger_exc.InvalidTopicNotificationModeError(mode=notification_mode)


def _enqueue_stream_folders(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    private: bool,
    session: sessions.PgSQLSession,
) -> None:
    folder_uuids = {
        ALL_CHATS_FOLDER_UUID,
        PERSONAL_FOLDER_UUID if private else CHANNELS_FOLDER_UUID,
    }
    folder_uuids.update(
        row["folder_uuid"]
        for row in session.execute(
            "SELECT DISTINCT folder_uuid FROM messenger_folder_items WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (project_id, user_uuid, stream_uuid),
        ).fetchall()
    )
    for folder_uuid in sorted(folder_uuids, key=str):
        v2_models.WorkspaceDomainOutboxEvent(
            project_id=project_id,
            event_kind="folder_projection",
            scope_kind="user-folder",
            scope_key=f"{project_id}:{user_uuid}:{folder_uuid}",
            payload={
                "source_kind": "zulip.reconciled",
                "user_uuid": str(user_uuid),
                "folder_uuid": str(folder_uuid),
            },
        ).insert(session=session)
