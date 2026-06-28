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
from workspace.messenger_api.dm import models


ALL_CHATS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000000")
PERSONAL_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
CHANNELS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
SYSTEM_FOLDER_ITEM_MODELS = {
    "00": models.AllFolderItem,
    "11": models.PersonalFolderItem,
    "22": models.ChannelFolderItem,
}
TOPIC_NOTIFICATION_MODES = {
    models.WorkspaceTopicNotificationMode.MUTE.value,
    models.WorkspaceTopicNotificationMode.DEFAULT.value,
    models.WorkspaceTopicNotificationMode.FOLLOW.value,
}
MUTED_STREAM_TOPIC_NOTIFICATION_MODES = TOPIC_NOTIFICATION_MODES | {
    models.WorkspaceTopicNotificationMode.UNMUTE.value,
}


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
    for user_topic in _get_workspace_user_stream_topics(
        project_id=project_id,
        topic_uuid=topic.uuid,
        session=session,
    ):
        if user_topic.user_uuid == user_uuid:
            result = user_topic
        messenger_events.create_topic_event(
            topic=user_topic,
            session=session,
        )
    return result


def update_workspace_user_stream_topic(project_id, user_uuid, topic_uuid,
                                       values, session=None):
    topic = _get_workspace_stream_topic_for_user(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )
    topic.update_dm(values={"name": values["name"]})
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

    create_workspace_stream_topic_with_flags(
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

    create_workspace_stream_topic_with_flags(
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

    for target_user_uuid, folder_uuid in folder_targets:
        folder = get_workspace_user_folder(
            project_id=project_id,
            user_uuid=target_user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        messenger_events.create_folder_updated_event(
            folder=folder,
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
    if message_uuids:
        messenger_events.create_messages_read_event(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuids=message_uuids,
            session=session,
        )
    return stream_uuid, topic_uuids, message_uuids


def create_workspace_user_message(project_id, user_uuid, session=None,
                                  **kwargs):
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
    _, topic_uuids, _ = _read_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        messages=unread_messages,
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
    return get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        session=session,
    )


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
    _, topic_uuids, _ = _read_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        messages=unread_messages,
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
    return get_workspace_user_stream_topic(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        session=session,
    )


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
    stream_uuid, topic_uuids, _ = _read_workspace_user_messages(
        project_id=project_id,
        user_uuid=user_uuid,
        messages=unread_messages,
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
    return get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        session=session,
    )


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
        messenger_events.create_messages_read_event(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuids=[message_uuid],
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
