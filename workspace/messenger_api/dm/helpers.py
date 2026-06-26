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

from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import models


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
    return models.UserFolderItem.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(item_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )


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


def create_workspace_user_stream(project_id, user_uuid, session=None,
                                 **kwargs):
    stream_uuid = kwargs.pop("uuid", None) or sys_uuid.uuid4()
    stream = models.WorkspaceStream(
        uuid=stream_uuid,
        project_id=project_id,
        user_uuid=user_uuid,
        **kwargs,
    )
    stream.insert(session=session)

    binding = models.WorkspaceStreamBinding(
        project_id=project_id,
        stream_uuid=stream.uuid,
        user_uuid=user_uuid,
        who_uuid=user_uuid,
        role=models.WorkspaceStreamRole.OWNER.value,
    )
    binding.insert(session=session)

    create_workspace_stream_topic_with_flags(
        project_id=project_id,
        stream_uuid=stream.uuid,
        name="General Topic",
        default_for_stream_uuid=stream.uuid,
        session=session,
    )

    user_stream = get_workspace_user_stream(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream.uuid,
        session=session,
    )
    messenger_events.create_stream_event(
        stream=user_stream,
        session=session,
    )
    return user_stream


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
    item = models.FolderItem.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(item_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    item.pinned_at = datetime.datetime.now(datetime.timezone.utc)
    item.save(session=session)
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
        item_uuid=item_uuid,
        session=session,
    )


def unpin_workspace_user_folder_item(project_id, user_uuid, item_uuid,
                                     session=None):
    item = models.FolderItem.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(item_uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        },
        session=session,
    )
    item.pinned_at = None
    item.save(session=session)
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
    return get_workspace_user_message(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message.uuid,
        session=session,
    )
