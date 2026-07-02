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

import datetime
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exc
from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import base as messenger_dm_base
from workspace.messenger_api.dm import models


ALL_CHATS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000000")
PERSONAL_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
CHANNELS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
SYSTEM_FOLDER_ITEM_MODELS = {
    "00": models.AllFolderItem,
    "11": models.PersonalFolderItem,
    "22": models.ChannelFolderItem,
}
SYSTEM_FOLDER_UUIDS = {
    ALL_CHATS_FOLDER_UUID,
    PERSONAL_FOLDER_UUID,
    CHANNELS_FOLDER_UUID,
}
TOPIC_NOTIFICATION_MODES = {
    models.WorkspaceTopicNotificationMode.MUTE.value,
    models.WorkspaceTopicNotificationMode.DEFAULT.value,
    models.WorkspaceTopicNotificationMode.FOLLOW.value,
}
MUTED_STREAM_TOPIC_NOTIFICATION_MODES = TOPIC_NOTIFICATION_MODES | {
    models.WorkspaceTopicNotificationMode.UNMUTE.value,
}
WORKSPACE_USER_OFFLINE_TIMEOUT = datetime.timedelta(minutes=1)


def _random_color():
    return messenger_dm_base.random_color()


def _ensure_color(values):
    if (
        "color" not in values or
        values["color"] is None
    ):
        values["color"] = _random_color()


def _get_workspace_user_event_recipients(session=None):
    users = models.WorkspaceUser.objects.get_all(
        order_by={"uuid": "asc"},
        session=session,
    )
    return [user.uuid for user in users]


def _get_workspace_event_project_ids(session=None):
    bindings = models.WorkspaceStreamBinding.objects.get_all(
        order_by={"project_id": "asc"},
        session=session,
    )
    events = models.WorkspaceEvent.objects.get_all(
        order_by={"project_id": "asc"},
        session=session,
    )
    project_ids = []
    for item in list(bindings) + list(events):
        if item.project_id not in project_ids:
            project_ids.append(item.project_id)
    return project_ids


def _create_workspace_user_updated_events(project_id, user, session=None):
    recipient_user_uuids = _get_workspace_user_event_recipients(
        session=session,
    )
    return messenger_events.create_user_updated_events(
        user=user,
        project_id=project_id,
        recipient_user_uuids=recipient_user_uuids,
        session=session,
    )


def _get_stale_workspace_users(cutoff, session=None):
    return models.WorkspaceUser.objects.get_all(
        filters=dm_filters.AND(
            {"status": dm_filters.NE(models.WorkspaceUserStatus.OFFLINE.value)},
            {"last_ping_at": dm_filters.LE(cutoff)},
        ),
        order_by={"uuid": "asc"},
        session=session,
    )


def update_workspace_user_presence(project_id, user_uuid, current_user_uuid,
                                   values, session=None):
    if user_uuid != current_user_uuid:
        raise storage_exc.RecordNotFound(
            model=models.WorkspaceUser.__name__,
            filters={"uuid": user_uuid},
        )

    user = models.WorkspaceUser.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    values = dict(values)
    values["last_ping_at"] = datetime.datetime.now(datetime.timezone.utc)
    user.update_dm(values=values)
    user.update(session=session)
    _create_workspace_user_updated_events(
        project_id=project_id,
        user=user,
        session=session,
    )
    return user


def mark_stale_workspace_users_offline(now=None, session=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - WORKSPACE_USER_OFFLINE_TIMEOUT
    users = _get_stale_workspace_users(cutoff, session=session)
    project_ids = _get_workspace_event_project_ids(session=session)
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


def get_workspace_user_folder(project_id, user_uuid, folder_uuid,
                              session=None):
    return models.UserFolder.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(folder_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def get_workspace_user_folder_item(project_id, user_uuid, item_uuid,
                                   session=None):
    filters = {
        "uuid": dm_filters.EQ(item_uuid),
        "project_id": dm_filters.EQ(project_id),
        "user_uuid": dm_filters.EQ(user_uuid),
    }
    try:
        return models.UserFolderItem.objects.get_one(
            filters=filters,
            session=session,
        )
    except storage_exc.RecordNotFound:
        system_item_model = SYSTEM_FOLDER_ITEM_MODELS.get(str(item_uuid)[:2])
        if system_item_model is None:
            raise
        return system_item_model.objects.get_one(
            filters=filters,
            session=session,
        )


def _get_workspace_user_folder_item_for_update(project_id, user_uuid,
                                               item_uuid, session=None):
    return models.FolderItem.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(item_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def _get_workspace_user_folder_item_for_stream_folder(project_id, user_uuid,
                                                      item, session=None):
    return models.FolderItem.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "folder_uuid": dm_filters.EQ(item.folder_uuid),
            "stream_uuid": dm_filters.EQ(item.stream_uuid),
        },
        session=session,
    )


def _create_workspace_user_folder_item_from_view(project_id, user_uuid, item,
                                                 pinned_at, session=None):
    folder_item = models.FolderItem(
        uuid=item.uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=item.folder_uuid,
        stream_uuid=item.stream_uuid,
        order_index=item.order_index,
        pinned_at=pinned_at,
        chat_type=item.chat_type,
    )
    folder_item.insert(session=session)
    return folder_item


def get_workspace_user_stream(project_id, user_uuid, stream_uuid,
                              session=None):
    return models.WorkspaceUserStream.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(stream_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def get_workspace_file(project_id, file_uuid, session=None):
    return models.WorkspaceFile.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(file_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )


def get_workspace_user_file(project_id, user_uuid, file_uuid, session=None):
    models.WorkspaceFileAccess.objects.get_one(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "file_uuid": dm_filters.EQ(file_uuid),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    return get_workspace_file(
        project_id=project_id,
        file_uuid=file_uuid,
        session=session,
    )


def get_workspace_owned_file(project_id, user_uuid, file_uuid, session=None):
    return models.WorkspaceFile.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(file_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def get_workspace_user_file_uuids(project_id, user_uuid, session=None):
    accesses = models.WorkspaceFileAccess.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    return [access.file_uuid for access in accesses]


def get_or_create_workspace_file_access(project_id, file_uuid, user_uuid,
                                        session=None):
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


def _get_workspace_stream_files(project_id, stream_uuid, session=None):
    return models.WorkspaceFile.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
        },
        session=session,
    )


def _create_workspace_stream_binding_file_accesses(project_id, stream_uuid,
                                                    user_uuid, session=None):
    for file in _get_workspace_stream_files(
        project_id=project_id,
        stream_uuid=stream_uuid,
        session=session,
    ):
        get_or_create_workspace_file_access(
            project_id=project_id,
            file_uuid=file.uuid,
            user_uuid=user_uuid,
            session=session,
        )


def _delete_workspace_stream_binding_file_accesses(project_id, stream_uuid,
                                                    user_uuid, session=None):
    for file in _get_workspace_stream_files(
        project_id=project_id,
        stream_uuid=stream_uuid,
        session=session,
    ):
        access = models.WorkspaceFileAccess.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "file_uuid": dm_filters.EQ(file.uuid),
                "user_uuid": dm_filters.EQ(user_uuid),
            },
            session=session,
        )
        if access is not None:
            access.delete(session=session)


def create_workspace_file(project_id, user_uuid, uuid, session=None, **values):
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
    stream_user_uuids = models.get_stream_recipients(
        project_id=project_id,
        stream_uuid=values["stream_uuid"],
        session=session,
    )
    for stream_user_uuid in stream_user_uuids:
        get_or_create_workspace_file_access(
            project_id=project_id,
            file_uuid=file.uuid,
            user_uuid=stream_user_uuid,
            session=session,
        )
    return file


def update_workspace_file(project_id, user_uuid, file_uuid, values,
                          session=None):
    file = get_workspace_owned_file(
        project_id=project_id,
        user_uuid=user_uuid,
        file_uuid=file_uuid,
        session=session,
    )
    file.update_dm(values=values)
    file.update(session=session)
    return file


def delete_workspace_file(project_id, user_uuid, file_uuid, session=None):
    file = get_workspace_owned_file(
        project_id=project_id,
        user_uuid=user_uuid,
        file_uuid=file_uuid,
        session=session,
    )
    file.delete(session=session)


def build_private_stream_index(user_uuid, direct_user_uuid):
    if user_uuid == direct_user_uuid:
        raise messenger_exc.DirectStreamSelfChatError()
    return ":".join(sorted([str(user_uuid), str(direct_user_uuid)]))


def _create_owner_binding(project_id, stream_uuid, user_uuid, who_uuid,
                          session=None):
    binding = models.WorkspaceStreamBinding(
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_uuid=user_uuid,
        who_uuid=who_uuid,
        role=models.WorkspaceStreamRole.OWNER.value,
    )
    binding.insert(session=session)
    return binding


def _add_folder_event_target(targets, user_uuid, folder_uuid):
    target = (user_uuid, folder_uuid)
    if target not in targets:
        targets.append(target)


def _create_available_folder_updated_events(project_id, folder_targets,
                                            session=None):
    for target_user_uuid, folder_uuid in folder_targets:
        try:
            folder = get_workspace_user_folder(
                project_id=project_id,
                user_uuid=target_user_uuid,
                folder_uuid=folder_uuid,
                session=session,
            )
        except storage_exc.RecordNotFound:
            if folder_uuid in SYSTEM_FOLDER_UUIDS:
                continue
            raise
        messenger_events.create_folder_updated_event(
            folder=folder,
            session=session,
        )


def _get_stream_folder_event_targets(project_id, stream_uuid, user_streams,
                                     session=None):
    targets = []
    for user_stream in user_streams:
        _add_folder_event_target(
            targets,
            user_stream.user_uuid,
            ALL_CHATS_FOLDER_UUID,
        )
        _add_folder_event_target(
            targets,
            user_stream.user_uuid,
            PERSONAL_FOLDER_UUID if user_stream.private else CHANNELS_FOLDER_UUID,
        )

    for item in models.FolderItem.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
        },
        session=session,
    ):
        _add_folder_event_target(targets, item.user_uuid, item.folder_uuid)
    return targets


def _create_stream_folder_updated_events(project_id, user_uuid, private,
                                         session=None):
    folder_uuids = (
        ALL_CHATS_FOLDER_UUID,
        PERSONAL_FOLDER_UUID if private else CHANNELS_FOLDER_UUID,
    )
    for folder_uuid in folder_uuids:
        user_folder = get_workspace_user_folder(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        messenger_events.create_folder_updated_event(
            folder=user_folder,
            session=session,
        )


def _get_user_stream_folder_event_targets(project_id, user_uuid, stream_uuid,
                                          private, session=None):
    targets = []
    _add_folder_event_target(targets, user_uuid, ALL_CHATS_FOLDER_UUID)
    _add_folder_event_target(
        targets,
        user_uuid,
        PERSONAL_FOLDER_UUID if private else CHANNELS_FOLDER_UUID,
    )
    for item in models.FolderItem.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "stream_uuid": dm_filters.EQ(stream_uuid),
        },
        session=session,
    ):
        _add_folder_event_target(targets, item.user_uuid, item.folder_uuid)
    return targets


def _create_message_unread_updated_events(project_id, user_uuid, stream_uuid,
                                          topic_uuid, session=None):
    _create_unread_updated_events(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        topic_uuids=[topic_uuid],
        session=session,
    )


def _create_unread_updated_events(project_id, user_uuid, stream_uuid,
                                  topic_uuids, session=None):
    for topic_uuid in topic_uuids:
        user_topic = get_workspace_user_stream_topic(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )
        messenger_events.create_topic_updated_event(
            topic=user_topic,
            session=session,
        )
    user_stream = get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    messenger_events.create_stream_updated_event(
        stream=user_stream,
        session=session,
    )

    folder_targets = _get_user_stream_folder_event_targets(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        private=user_stream.private,
        session=session,
    )
    for target_user_uuid, folder_uuid in folder_targets:
        user_folder = get_workspace_user_folder(
            project_id=project_id,
            user_uuid=target_user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        messenger_events.create_folder_updated_event(
            folder=user_folder,
            session=session,
        )


def _create_messages_unread_updated_events(project_id, user_uuids,
                                           stream_uuid, topic_uuid,
                                           session=None):
    for user_uuid in user_uuids:
        _create_message_unread_updated_events(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )


def fetch_existing_private_workspace_user_stream(project_id, user_uuid, stream,
                                                 fields, session=None):
    stream.update_dm(values=fields)
    should_send_event = stream.is_dirty()
    stream.update(session=session)
    result = None
    for user_stream in models.WorkspaceUserStream.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(stream.uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    ):
        if user_stream.user_uuid == user_uuid:
            result = user_stream
        if should_send_event:
            messenger_events.create_stream_updated_event(
                stream=user_stream,
                session=session,
            )
    return result


def create_workspace_stream_binding_events(binding, session=None):
    user_stream = get_workspace_user_stream(
        project_id=binding.project_id,
        user_uuid=binding.user_uuid,
        stream_uuid=binding.stream_uuid,
        session=session,
    )
    messenger_events.create_stream_event(
        stream=user_stream,
        session=session,
    )
    _create_stream_folder_updated_events(
        project_id=binding.project_id,
        user_uuid=binding.user_uuid,
        private=user_stream.private,
        session=session,
    )


def create_workspace_stream_bindings_created_events(bindings, session=None):
    if not bindings:
        return
    added_user_uuids = {binding.user_uuid for binding in bindings}
    binding = bindings[0]
    for stream_binding in models.WorkspaceStreamBinding.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(binding.project_id),
            "stream_uuid": dm_filters.EQ(binding.stream_uuid),
        },
        session=session,
    ):
        if stream_binding.user_uuid in added_user_uuids:
            continue
        messenger_events.create_stream_bindings_created_event(
            bindings=bindings,
            user_uuid=stream_binding.user_uuid,
            session=session,
        )


def delete_workspace_stream_binding(project_id, binding_uuid, session=None):
    binding = models.WorkspaceStreamBinding.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(binding_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )
    user_stream = get_workspace_user_stream(
        project_id=project_id,
        user_uuid=binding.user_uuid,
        stream_uuid=binding.stream_uuid,
        session=session,
    )
    folder_targets = _get_user_stream_folder_event_targets(
        project_id=project_id,
        user_uuid=binding.user_uuid,
        stream_uuid=binding.stream_uuid,
        private=user_stream.private,
        session=session,
    )

    messenger_events.create_stream_deleted_event(
        project_id=project_id,
        user_uuid=binding.user_uuid,
        stream_uuid=binding.stream_uuid,
        session=session,
    )
    _delete_workspace_stream_binding_file_accesses(
        project_id=project_id,
        stream_uuid=binding.stream_uuid,
        user_uuid=binding.user_uuid,
        session=session,
    )
    binding.delete(session=session)

    _create_available_folder_updated_events(
        project_id=project_id,
        folder_targets=folder_targets,
        session=session,
    )


def _create_workspace_stream_binding_message_flags(project_id, stream_uuid,
                                                   user_uuid, session=None):
    statement = """
        INSERT INTO "m_workspace_user_message_flags"
            ("uuid", "user_uuid", "project_id", "read")
        SELECT
            m."uuid",
            %s::uuid,
            m."project_id",
            m."user_uuid" = %s::uuid
        FROM "m_workspace_messages" AS m
        WHERE m."project_id" = %s::uuid
            AND m."stream_uuid" = %s::uuid
        ON CONFLICT ("uuid", "user_uuid") DO NOTHING;
    """
    values = (
        str(user_uuid),
        str(user_uuid),
        str(project_id),
        str(stream_uuid),
    )
    if session is not None:
        session.execute(statement, values)
        return

    engine = models.WorkspaceUserMessageFlags._get_engine()
    with engine.session_manager() as s:
        s.execute(statement, values)


def _get_or_create_workspace_stream_binding(project_id, stream_uuid, user_uuid,
                                            who_uuid, role, session=None):
    for existing in models.WorkspaceStreamBinding.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        limit=1,
        session=session,
    ):
        return existing, False

    binding = models.WorkspaceStreamBinding(
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_uuid=user_uuid,
        who_uuid=who_uuid,
        role=role,
    )
    binding.insert(session=session)
    _create_workspace_stream_binding_message_flags(
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_uuid=user_uuid,
        session=session,
    )
    _create_workspace_stream_binding_file_accesses(
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_uuid=user_uuid,
        session=session,
    )
    create_workspace_stream_binding_events(binding, session=session)
    return binding, True


def get_or_create_workspace_stream_binding(project_id, stream_uuid, user_uuid,
                                           who_uuid, role, session=None):
    binding, _created = _get_or_create_workspace_stream_binding(
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_uuid=user_uuid,
        who_uuid=who_uuid,
        role=role,
        session=session,
    )
    return binding


def _validate_stream_binding_roles_payload(role_user_uuids):
    allowed_roles = {role.value for role in models.WorkspaceStreamRole}
    for role, user_uuids in role_user_uuids.items():
        if role not in allowed_roles:
            raise messenger_exc.InvalidStreamBindingRoleError(role=role)
        if not isinstance(user_uuids, list):
            raise messenger_exc.StreamBindingUsersPayloadError()


def get_or_create_workspace_stream_bindings(project_id, stream_uuid, who_uuid,
                                            role_user_uuids, session=None):
    _validate_stream_binding_roles_payload(role_user_uuids)
    result = []
    created_bindings = []
    for role, user_uuids in role_user_uuids.items():
        for user_uuid in user_uuids:
            user_uuid = sys_uuid.UUID(str(user_uuid))
            binding, created = _get_or_create_workspace_stream_binding(
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
                who_uuid=who_uuid,
                role=role,
                session=session,
            )
            result.append(binding)
            if created:
                created_bindings.append(binding)
    create_workspace_stream_bindings_created_events(
        bindings=created_bindings,
        session=session,
    )
    return result


def create_workspace_stream_topic_with_flags(project_id, session=None,
                                             **kwargs):
    topic_uuid = kwargs.pop("uuid", None) or sys_uuid.uuid4()
    _ensure_color(kwargs)
    topic = models.WorkspaceStreamTopic(
        uuid=topic_uuid,
        project_id=project_id,
        **kwargs,
    )
    topic.insert(session=session)

    bindings = models.WorkspaceStreamBinding.objects.get_all(
        filters={
            "stream_uuid": dm_filters.EQ(topic.stream_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )
    for binding in bindings:
        flags = models.WorkspaceUserTopicFlags(
            uuid=topic.uuid,
            user_uuid=binding.user_uuid,
            project_id=project_id,
            is_done=False,
        )
        flags.insert(session=session)

    return topic


def get_workspace_user_stream_topic(project_id, user_uuid, topic_uuid,
                                    session=None):
    return models.WorkspaceUserTopic.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(topic_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def _get_workspace_user_stream_topics(project_id, topic_uuid, session=None):
    return models.WorkspaceUserTopic.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(topic_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )


def _create_workspace_stream_topic_events(project_id, topic_uuid, session=None):
    user_topics = _get_workspace_user_stream_topics(
        project_id=project_id,
        topic_uuid=topic_uuid,
        session=session,
    )
    for user_topic in user_topics:
        messenger_events.create_topic_event(
            topic=user_topic,
            session=session,
        )
    return user_topics


def _get_workspace_stream_topic_for_user(project_id, user_uuid, topic_uuid,
                                         session=None):
    topic = models.WorkspaceStreamTopic.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(topic_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )
    models.WorkspaceStreamBinding.objects.get_one(
        filters={
            "stream_uuid": dm_filters.EQ(topic.stream_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    return topic


def create_workspace_user_stream_topic(project_id, user_uuid, values,
                                       session=None):
    stream_uuid = values["stream_uuid"]
    models.WorkspaceStreamBinding.objects.get_one(
        filters={
            "stream_uuid": dm_filters.EQ(stream_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    topic = create_workspace_stream_topic_with_flags(
        project_id=project_id,
        session=session,
        **values,
    )
    result = None
    for user_topic in _create_workspace_stream_topic_events(
        project_id=project_id,
        topic_uuid=topic.uuid,
        session=session,
    ):
        if user_topic.user_uuid == user_uuid:
            result = user_topic
    return result


def update_workspace_user_stream_topic(project_id, user_uuid, topic_uuid,
                                       values, session=None):
    topic = _get_workspace_stream_topic_for_user(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    topic.update_dm(values=values)
    topic.update(session=session)
    result = None
    for user_topic in _get_workspace_user_stream_topics(
        project_id=project_id,
        topic_uuid=topic_uuid,
        session=session,
    ):
        if user_topic.user_uuid == user_uuid:
            result = user_topic
        messenger_events.create_topic_updated_event(
            topic=user_topic,
            session=session,
        )
    return result


def delete_workspace_user_stream_topic(project_id, user_uuid, topic_uuid,
                                       session=None):
    topic = _get_workspace_stream_topic_for_user(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    for user_topic in _get_workspace_user_stream_topics(
        project_id=project_id,
        topic_uuid=topic_uuid,
        session=session,
    ):
        messenger_events.create_topic_deleted_event(
            project_id=project_id,
            user_uuid=user_topic.user_uuid,
            topic_uuid=topic_uuid,
            stream_uuid=topic.stream_uuid,
            session=session,
        )
    topic.delete(session=session)


def _set_workspace_user_topic_done(project_id, user_uuid, topic_uuid,
                                   is_done, session=None):
    for flags in models.WorkspaceUserTopicFlags.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(topic_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        limit=1,
        session=session,
    ):
        flags.is_done = is_done
        flags.update(session=session)
        return

    flags = models.WorkspaceUserTopicFlags(
        uuid=topic_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        is_done=is_done,
    )
    flags.insert(session=session)


def _set_workspace_user_topic_notification_mode(
    project_id,
    user_uuid,
    topic_uuid,
    notification_mode,
    session=None,
):
    for flags in models.WorkspaceUserTopicFlags.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(topic_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        limit=1,
        session=session,
    ):
        flags.notification_mode = notification_mode
        flags.update(session=session)
        return

    flags = models.WorkspaceUserTopicFlags(
        uuid=topic_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        notification_mode=notification_mode,
    )
    flags.insert(session=session)


def _validate_topic_notification_mode(stream, notification_mode):
    if (
        stream.notification_mode ==
        models.WorkspaceStreamNotificationMode.MUTED.value
    ):
        allowed_modes = MUTED_STREAM_TOPIC_NOTIFICATION_MODES
    else:
        allowed_modes = TOPIC_NOTIFICATION_MODES
    if notification_mode not in allowed_modes:
        raise messenger_exc.InvalidTopicNotificationModeError(
            mode=notification_mode
        )


def toggle_workspace_user_stream_topic_done(project_id, user_uuid, topic_uuid,
                                            session=None):
    topic = get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    is_done = not topic.is_done
    for user_topic in _get_workspace_user_stream_topics(
        project_id=project_id,
        topic_uuid=topic_uuid,
        session=session,
    ):
        _set_workspace_user_topic_done(
            project_id=project_id,
            user_uuid=user_topic.user_uuid,
            topic_uuid=topic_uuid,
            is_done=is_done,
            session=session,
        )

    result = None
    for user_topic in _get_workspace_user_stream_topics(
        project_id=project_id,
        topic_uuid=topic_uuid,
        session=session,
    ):
        if user_topic.user_uuid == user_uuid:
            result = user_topic
        messenger_events.create_topic_updated_event(
            topic=user_topic,
            session=session,
        )
    return result


def update_workspace_user_stream_topic_notifications(
    project_id,
    user_uuid,
    topic_uuid,
    notification_mode,
    session=None,
):
    topic = get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    stream = get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=topic.stream_uuid,
        session=session,
    )
    _validate_topic_notification_mode(
        stream=stream,
        notification_mode=notification_mode,
    )
    _set_workspace_user_topic_notification_mode(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        notification_mode=notification_mode,
        session=session,
    )
    result = get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    messenger_events.create_topic_updated_event(
        topic=result,
        session=session,
    )
    return result


def _get_or_create_private_workspace_user_stream(project_id, user_uuid,
                                                 direct_user_uuid, stream_uuid,
                                                 session=None, **kwargs):
    private_index = build_private_stream_index(user_uuid, direct_user_uuid)
    for existing in models.WorkspaceStream.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "private_index": dm_filters.EQ(private_index),
        },
        limit=1,
        session=session,
    ):
        return fetch_existing_private_workspace_user_stream(
            project_id=project_id,
            user_uuid=user_uuid,
            stream=existing,
            fields=kwargs,
            session=session,
        )

    _ensure_color(kwargs)
    stream = models.WorkspaceStream(
        uuid=stream_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        private=True,
        direct_user_uuid=direct_user_uuid,
        private_index=private_index,
        **kwargs,
    )
    stream.insert(session=session)

    participant_uuids = (user_uuid, direct_user_uuid)
    for participant_uuid in participant_uuids:
        _create_owner_binding(
            project_id=project_id,
            stream_uuid=stream.uuid,
            user_uuid=participant_uuid,
            who_uuid=user_uuid,
            session=session,
        )

    default_topic = create_workspace_stream_topic_with_flags(
        project_id=project_id,
        stream_uuid=stream.uuid,
        name="General Topic",
        default_for_stream_uuid=stream.uuid,
        session=session,
    )

    result = None
    for user_stream in models.WorkspaceUserStream.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "private_index": dm_filters.EQ(private_index),
        },
        session=session,
    ):
        if user_stream.user_uuid == user_uuid:
            result = user_stream
        messenger_events.create_stream_event(
            stream=user_stream,
            session=session,
        )
        _create_stream_folder_updated_events(
            project_id=project_id,
            user_uuid=user_stream.user_uuid,
            private=True,
            session=session,
        )
    _create_workspace_stream_topic_events(
        project_id=project_id,
        topic_uuid=default_topic.uuid,
        session=session,
    )
    return result


def get_or_create_workspace_user_stream(project_id, user_uuid, session=None,
                                        **kwargs):
    stream_uuid = kwargs.pop("uuid", None) or sys_uuid.uuid4()
    direct_user_uuid = kwargs.pop("direct_user_uuid", None)
    kwargs.pop("private", None)
    if kwargs.pop("private_index", None) is not None:
        raise messenger_exc.PrivateIndexIsTechnicalFieldError()
    if direct_user_uuid is not None:
        return _get_or_create_private_workspace_user_stream(
            project_id=project_id,
            user_uuid=user_uuid,
            direct_user_uuid=direct_user_uuid,
            stream_uuid=stream_uuid,
            session=session,
            **kwargs,
        )

    _ensure_color(kwargs)
    stream = models.WorkspaceStream(
        uuid=stream_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        **kwargs,
    )
    stream.insert(session=session)

    _create_owner_binding(
        project_id=project_id,
        stream_uuid=stream.uuid,
        user_uuid=user_uuid,
        who_uuid=user_uuid,
        session=session,
    )

    default_topic = create_workspace_stream_topic_with_flags(
        project_id=project_id,
        stream_uuid=stream.uuid,
        name="General Topic",
        default_for_stream_uuid=stream.uuid,
        session=session,
    )

    result = None
    for user_stream in models.WorkspaceUserStream.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(stream.uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    ):
        if user_stream.user_uuid == user_uuid:
            result = user_stream
        messenger_events.create_stream_event(
            stream=user_stream,
            session=session,
        )
        _create_stream_folder_updated_events(
            project_id=project_id,
            user_uuid=user_stream.user_uuid,
            private=False,
            session=session,
        )
    _create_workspace_stream_topic_events(
        project_id=project_id,
        topic_uuid=default_topic.uuid,
        session=session,
    )
    return result


def update_workspace_user_stream(project_id, user_uuid, stream_uuid, values,
                                 session=None):
    get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    stream = models.WorkspaceStream.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(stream_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )
    stream.update_dm(values=values)
    stream.update(session=session)

    result = None
    for user_stream in models.WorkspaceUserStream.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(stream_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    ):
        if user_stream.user_uuid == user_uuid:
            result = user_stream
        messenger_events.create_stream_updated_event(
            stream=user_stream,
            session=session,
        )
    return result


def update_workspace_user_stream_notifications(project_id, user_uuid,
                                               stream_uuid,
                                               notification_mode,
                                               session=None):
    binding = models.WorkspaceStreamBinding.objects.get_one(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    binding.update_dm(values={"notification_mode": notification_mode})
    binding.update(session=session)
    stream = get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    messenger_events.create_stream_updated_event(
        stream=stream,
        session=session,
    )
    return stream


def delete_workspace_user_stream(project_id, user_uuid, stream_uuid,
                                 session=None):
    get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    stream = models.WorkspaceStream.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(stream_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )
    user_streams = models.WorkspaceUserStream.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(stream_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )
    folder_targets = _get_stream_folder_event_targets(
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_streams=user_streams,
        session=session,
    )

    for user_stream in user_streams:
        messenger_events.create_stream_deleted_event(
            project_id=project_id,
            user_uuid=user_stream.user_uuid,
            stream_uuid=stream_uuid,
            session=session,
        )

    stream.delete(session=session)

    _create_available_folder_updated_events(
        project_id=project_id,
        folder_targets=folder_targets,
        session=session,
    )


def create_workspace_user_folder(project_id, user_uuid, session=None,
                                 **kwargs):
    folder = models.Folder(
        uuid=kwargs.pop("uuid", None) or sys_uuid.uuid4(),
        project_id=project_id,
        user_uuid=user_uuid,
        **kwargs,
    )
    folder.insert(session=session)
    user_folder = get_workspace_user_folder(
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=folder.uuid,
        session=session,
    )
    messenger_events.create_folder_event(
        folder=user_folder,
        session=session,
    )
    return user_folder


def create_workspace_user_folder_item(project_id, user_uuid, session=None,
                                      **kwargs):
    item = models.FolderItem(
        uuid=kwargs.pop("uuid", None) or sys_uuid.uuid4(),
        project_id=project_id,
        user_uuid=user_uuid,
        **kwargs,
    )
    item.insert(session=session)
    user_folder = get_workspace_user_folder(
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=item.folder_uuid,
        session=session,
    )
    messenger_events.create_folder_updated_event(
        folder=user_folder,
        session=session,
    )
    return get_workspace_user_folder_item(
        project_id=project_id,
        user_uuid=user_uuid,
        item_uuid=item.uuid,
        session=session,
    )


def delete_workspace_user_folder_item(project_id, user_uuid, item_uuid,
                                      session=None):
    item = models.FolderItem.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(item_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    item.delete(session=session)
    messenger_events.create_folder_item_deleted_event(
        project_id=project_id,
        user_uuid=user_uuid,
        item_uuid=item_uuid,
        session=session,
    )


def pin_workspace_user_folder_item(project_id, user_uuid, item_uuid,
                                   session=None):
    pinned_at = datetime.datetime.now(datetime.timezone.utc)
    try:
        item = _get_workspace_user_folder_item_for_update(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )
        item.pinned_at = pinned_at
        item.save(session=session)
        folder_uuid = item.folder_uuid
    except storage_exc.RecordNotFound:
        view_item = get_workspace_user_folder_item(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )
        item = _get_workspace_user_folder_item_for_stream_folder(
            project_id=project_id,
            user_uuid=user_uuid,
            item=view_item,
            session=session,
        )
        if item is None:
            item = _create_workspace_user_folder_item_from_view(
                project_id=project_id,
                user_uuid=user_uuid,
                item=view_item,
                pinned_at=pinned_at,
                session=session,
            )
        else:
            item.pinned_at = pinned_at
            item.save(session=session)
        folder_uuid = item.folder_uuid
    user_folder = get_workspace_user_folder(
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=folder_uuid,
        session=session,
    )
    messenger_events.create_folder_updated_event(
        folder=user_folder,
        session=session,
    )
    return get_workspace_user_folder_item(
        project_id=project_id,
        user_uuid=user_uuid,
        item_uuid=item_uuid,
        session=session,
    )


def unpin_workspace_user_folder_item(project_id, user_uuid, item_uuid,
                                     session=None):
    try:
        item = _get_workspace_user_folder_item_for_update(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )
        item.pinned_at = None
        item.save(session=session)
        folder_uuid = item.folder_uuid
    except storage_exc.RecordNotFound:
        view_item = get_workspace_user_folder_item(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )
        item = _get_workspace_user_folder_item_for_stream_folder(
            project_id=project_id,
            user_uuid=user_uuid,
            item=view_item,
            session=session,
        )
        if item is not None:
            item.pinned_at = None
            item.save(session=session)
            folder_uuid = item.folder_uuid
        else:
            folder_uuid = view_item.folder_uuid
    user_folder = get_workspace_user_folder(
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=folder_uuid,
        session=session,
    )
    messenger_events.create_folder_updated_event(
        folder=user_folder,
        session=session,
    )
    return get_workspace_user_folder_item(
        project_id=project_id,
        user_uuid=user_uuid,
        item_uuid=item_uuid,
        session=session,
    )


def update_workspace_user_folder(project_id, user_uuid, folder_uuid,
                                 session=None, **values):
    folder = models.Folder.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(folder_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    folder.update_dm(values=values)
    folder.update(session=session)

    user_folder = get_workspace_user_folder(
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=folder_uuid,
        session=session,
    )
    messenger_events.create_folder_updated_event(
        folder=user_folder,
        session=session,
    )
    return user_folder


def delete_workspace_user_folder(project_id, user_uuid, folder_uuid,
                                 session=None):
    folder = models.Folder.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(folder_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    folder.delete(session=session)
    messenger_events.create_folder_deleted_event(
        project_id=project_id,
        user_uuid=user_uuid,
        folder_uuid=folder_uuid,
        session=session,
    )


def get_workspace_user_message(project_id, user_uuid, message_uuid,
                               session=None):
    return models.WorkspaceUserMessage.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(message_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def get_workspace_user_message_uuids(project_id, user_uuid, session=None):
    messages = models.WorkspaceUserMessage.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    return [message.uuid for message in messages]


def get_workspace_message_reaction(project_id, user_uuid, reaction_uuid,
                                   session=None):
    return models.WorkspaceMessageReactions.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(reaction_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def _create_workspace_message_reaction_updated_events(project_id,
                                                      message_uuid,
                                                      session=None):
    for user_message in _get_workspace_user_messages(
        project_id=project_id,
        message_uuid=message_uuid,
        session=session,
    ):
        messenger_events.create_message_updated_event(
            message=user_message,
            session=session,
        )


def create_workspace_message_reaction(project_id, user_uuid, session=None,
                                      **kwargs):
    message_uuid = kwargs["message_uuid"]
    get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    reaction = models.WorkspaceMessageReactions(
        uuid=kwargs.pop("uuid", None) or sys_uuid.uuid4(),
        project_id=project_id,
        user_uuid=user_uuid,
        **kwargs,
    )
    reaction.insert(session=session)
    _create_workspace_message_reaction_updated_events(
        project_id=project_id,
        message_uuid=message_uuid,
        session=session,
    )
    return reaction


def update_workspace_message_reaction(project_id, user_uuid, reaction_uuid,
                                      values, session=None):
    reaction = get_workspace_message_reaction(
        project_id=project_id,
        user_uuid=user_uuid,
        reaction_uuid=reaction_uuid,
        session=session,
    )
    old_message_uuid = reaction.message_uuid
    if "message_uuid" in values:
        get_workspace_user_message(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=values["message_uuid"],
            session=session,
        )
    values.pop("project_id", None)
    values.pop("user_uuid", None)
    values.pop("uuid", None)
    reaction.update_dm(values=values)
    reaction.update(session=session)
    new_message_uuid = values.get("message_uuid", old_message_uuid)
    _create_workspace_message_reaction_updated_events(
        project_id=project_id,
        message_uuid=old_message_uuid,
        session=session,
    )
    if new_message_uuid != old_message_uuid:
        _create_workspace_message_reaction_updated_events(
            project_id=project_id,
            message_uuid=new_message_uuid,
            session=session,
        )
    return reaction


def delete_workspace_message_reaction(project_id, user_uuid, reaction_uuid,
                                      session=None):
    reaction = get_workspace_message_reaction(
        project_id=project_id,
        user_uuid=user_uuid,
        reaction_uuid=reaction_uuid,
        session=session,
    )
    message_uuid = reaction.message_uuid
    reaction.delete(session=session)
    _create_workspace_message_reaction_updated_events(
        project_id=project_id,
        message_uuid=message_uuid,
        session=session,
    )


def _get_workspace_stream_default_topic(project_id, stream_uuid, session=None):
    return models.WorkspaceStreamTopic.objects.get_one(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(stream_uuid),
            "default_for_stream_uuid": dm_filters.EQ(stream_uuid),
        },
        session=session,
    )


def _get_unread_workspace_user_messages(project_id, user_uuid,
                                        stream_uuid=None, topic_uuid=None,
                                        created_at=None, session=None):
    filters = {
        "project_id": dm_filters.EQ(project_id),
        "user_uuid": dm_filters.EQ(user_uuid),
        "read": dm_filters.EQ(False),
    }
    if stream_uuid is not None:
        filters["stream_uuid"] = dm_filters.EQ(stream_uuid)
    if topic_uuid is not None:
        filters["topic_uuid"] = dm_filters.EQ(topic_uuid)
    if created_at is not None:
        filters["created_at"] = dm_filters.LE(created_at)
    return models.WorkspaceUserMessage.objects.get_all(
        filters=filters,
        order_by={"created_at": "asc", "uuid": "asc"},
        session=session,
    )


def _get_workspace_user_messages(project_id, message_uuid, session=None):
    return models.WorkspaceUserMessage.objects.get_all(
        filters={
            "uuid": dm_filters.EQ(message_uuid),
            "project_id": dm_filters.EQ(project_id),
        },
        session=session,
    )


def _get_workspace_message_for_author(project_id, user_uuid, message_uuid,
                                      session=None):
    return models.WorkspaceMessage.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(message_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


def create_message_flags(project_id, message_uuid, author_uuid, recipients,
                         session=None):
    for recipient_uuid in recipients:
        flags = models.WorkspaceUserMessageFlags(
            uuid=message_uuid,
            user_uuid=recipient_uuid,
            project_id=project_id,
            read=recipient_uuid == author_uuid,
        )
        flags.insert(session=session)


def _read_workspace_user_messages(project_id, user_uuid, messages,
                                  session=None):
    topic_uuids = []
    message_uuids = []
    stream_uuid = None
    for message in messages:
        flags = models.WorkspaceUserMessageFlags.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(message.uuid),
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(user_uuid),
            },
            session=session,
        )
        flags.update_dm(values={"read": True})
        flags.update(session=session)
        message_uuids.append(message.uuid)
        stream_uuid = message.stream_uuid
        if message.topic_uuid not in topic_uuids:
            topic_uuids.append(message.topic_uuid)
    return stream_uuid, topic_uuids, message_uuids


def create_workspace_user_message(project_id, user_uuid, session=None,
                                  **kwargs):
    if "topic_uuid" not in kwargs or kwargs["topic_uuid"] is None:
        topic = _get_workspace_stream_default_topic(
            project_id=project_id,
            stream_uuid=kwargs["stream_uuid"],
            session=session,
        )
        kwargs["topic_uuid"] = topic.uuid

    message = models.WorkspaceMessage(
        uuid=kwargs.pop("uuid", None) or sys_uuid.uuid4(),
        project_id=project_id,
        user_uuid=user_uuid,
        **kwargs,
    )
    message.insert(session=session)
    recipients = message.get_recipients(session=session)
    create_message_flags(
        project_id=project_id,
        message_uuid=message.uuid,
        author_uuid=message.user_uuid,
        recipients=recipients,
        session=session,
    )
    messenger_events.create_message_events(
        project_id=project_id,
        message=message,
        recipients=recipients,
        session=session,
    )
    unread_user_uuids = [
        recipient_uuid for recipient_uuid in recipients
        if recipient_uuid != message.user_uuid
    ]
    _create_messages_unread_updated_events(
        project_id=project_id,
        user_uuids=unread_user_uuids,
        stream_uuid=message.stream_uuid,
        topic_uuid=message.topic_uuid,
        session=session,
    )
    return get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message.uuid,
        session=session,
    )


def update_workspace_user_message(project_id, user_uuid, message_uuid, values,
                                  session=None):
    get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    message = _get_workspace_message_for_author(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    message.update_dm(values={"payload": values["payload"]})
    message.update(session=session)

    result = None
    for user_message in _get_workspace_user_messages(
        project_id=project_id,
        message_uuid=message_uuid,
        session=session,
    ):
        if user_message.user_uuid == user_uuid:
            result = user_message
        messenger_events.create_message_updated_event(
            message=user_message,
            session=session,
        )
    return result


def read_workspace_user_stream_messages(project_id, user_uuid, stream_uuid,
                                        session=None):
    get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    unread_messages = _get_unread_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    _, topic_uuids, message_uuids = _read_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        messages=unread_messages,
        session=session,
    )
    result = get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )
    if message_uuids:
        messenger_events.create_stream_read_event(
            stream=result,
            session=session,
        )
    if topic_uuids:
        _create_unread_updated_events(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuids=topic_uuids,
            session=session,
        )
    return result


def read_workspace_user_stream_topic_messages(project_id, user_uuid,
                                              topic_uuid, session=None):
    topic = get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    unread_messages = _get_unread_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=topic.stream_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    _, topic_uuids, message_uuids = _read_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        messages=unread_messages,
        session=session,
    )
    result = get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    if message_uuids:
        messenger_events.create_topic_read_event(
            topic=result,
            session=session,
        )
    if topic_uuids:
        _create_unread_updated_events(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=topic.stream_uuid,
            topic_uuids=topic_uuids,
            session=session,
        )
    return result


def read_workspace_user_topic_messages_to_message(project_id, user_uuid,
                                                  message_uuid, session=None):
    current_message = get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    unread_messages = _get_unread_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=current_message.stream_uuid,
        topic_uuid=current_message.topic_uuid,
        created_at=current_message.created_at,
        session=session,
    )
    stream_uuid, topic_uuids, message_uuids = _read_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        messages=unread_messages,
        session=session,
    )
    result = get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    if message_uuids:
        messenger_events.create_message_read_event(
            message=result,
            session=session,
        )
    if topic_uuids:
        _create_unread_updated_events(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuids=topic_uuids,
            session=session,
        )
    return result


def read_workspace_user_message(project_id, user_uuid, message_uuid,
                                session=None):
    current_message = get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    flags = models.WorkspaceUserMessageFlags.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(message_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    was_read = flags.read
    flags.update_dm(values={"read": True})
    flags.update(session=session)

    result = get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    if not was_read:
        messenger_events.create_message_read_event(
            message=result,
            session=session,
        )
        _create_message_unread_updated_events(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=current_message.stream_uuid,
            topic_uuid=current_message.topic_uuid,
            session=session,
        )
    return result


def delete_workspace_user_message(project_id, user_uuid, message_uuid,
                                  session=None):
    get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    message = _get_workspace_message_for_author(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )
    user_messages = _get_workspace_user_messages(
        project_id=project_id,
        message_uuid=message_uuid,
        session=session,
    )
    unread_user_uuids = [
        user_message.user_uuid for user_message in user_messages
        if not user_message.read
    ]
    for user_message in user_messages:
        messenger_events.create_message_deleted_event(
            project_id=project_id,
            user_uuid=user_message.user_uuid,
            message_uuid=message_uuid,
            stream_uuid=message.stream_uuid,
            topic_uuid=message.topic_uuid,
            session=session,
        )

    message.delete(session=session)
    _create_messages_unread_updated_events(
        project_id=project_id,
        user_uuids=unread_user_uuids,
        stream_uuid=message.stream_uuid,
        topic_uuid=message.topic_uuid,
        session=session,
    )
