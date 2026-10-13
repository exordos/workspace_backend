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

import abc
import enum

from restalchemy.common import exceptions
from restalchemy.dm import filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm

from workspace.bridges.zulip import client
from workspace.messenger_api.dm import external_models


MAX_HISTORY_MESSAGES = 5000


class MessageContent(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_message_contents"

    zulip_content = properties.property(
        types.String(max_length=10000),
        required=True,
    )
    workspace_content = properties.property(
        types.String(max_length=64536),
        required=True,
    )
    zulip_content_hash = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )
    workspace_content_hash = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )
    zulip_content_updated_at = properties.property(
        types.UTCDateTimeZ(),
        required=True,
    )
    workspace_content_updated_at = properties.property(
        types.UTCDateTimeZ(),
        required=True,
    )


class ZulipChannel(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_channels"

    stream_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )
    name = properties.property(
        types.String(min_length=1),
        required=True,
    )
    description = properties.property(
        types.String(),
        required=True,
    )
    rendered_description = properties.property(
        types.String(),
        required=True,
    )
    date_created = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
    creator_id = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
    )
    invite_only = properties.property(
        types.Boolean(),
        required=True,
    )
    is_web_public = properties.property(
        types.Boolean(),
        required=True,
    )
    history_public_to_subscribers = properties.property(
        types.Boolean(),
        required=True,
    )
    is_archived = properties.property(
        types.Boolean(),
        required=True,
    )
    message_retention_days = properties.property(
        types.AllowNone(types.Integer(min_value=-1)),
        default=None,
    )


class ZulipTopic(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_topics"

    stream_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )
    name = properties.property(
        types.String(),
        required=True,
    )
    max_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )


class ZulipMessage(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """A channel message with account-specific flags."""

    __tablename__ = "bridge_zulip_v5_messages"

    external_account_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    message_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )
    sender_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )
    timestamp = properties.property(
        types.Integer(min_value=0),
        required=True,
    )
    content = relationships.relationship(
        MessageContent,
        prefetch=True,
        required=True,
    )
    topic = relationships.relationship(
        ZulipTopic,
        prefetch=True,
        required=True,
    )
    channel = relationships.relationship(
        ZulipChannel,
        prefetch=True,
        required=True,
    )
    read = properties.property(
        types.Boolean(),
        default=False,
    )
    starred = properties.property(
        types.Boolean(),
        default=False,
    )
    collapsed = properties.property(
        types.Boolean(),
        default=False,
    )
    hide_link_previews = properties.property(
        types.Boolean(),
        default=False,
    )
    mentioned = properties.property(
        types.Boolean(),
        default=False,
    )
    stream_wildcard_mentioned = properties.property(
        types.Boolean(),
        default=False,
    )
    topic_wildcard_mentioned = properties.property(
        types.Boolean(),
        default=False,
    )
    has_alert_word = properties.property(
        types.Boolean(),
        default=False,
    )
    historical = properties.property(
        types.Boolean(),
        default=False,
    )
    is_me_message = properties.property(
        types.Boolean(),
        default=False,
    )


class ZulipFile(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_files"

    zulip_file_url = properties.property(
        types.String(min_length=1, max_length=2048),
        required=True,
    )
    workspace_file_url = properties.property(
        types.String(min_length=1, max_length=2048),
        required=True,
    )
    zulip_author_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )
    workspace_author_uuid = properties.property(
        types.UUID(),
        required=True,
    )


class AbstractTaskContent(types_dynamic.AbstractKindModel):
    @abc.abstractmethod
    def execute(self) -> None:
        raise NotImplementedError()


class CheckConnectionContent(AbstractTaskContent):
    KIND = "check_connection"

    external_account_uuid = properties.property(types.UUID(), required=True)

    def execute(self) -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(self.external_account_uuid)},
        )
        zulip = client.ZulipClient(account)
        try:
            zulip.check_connection()
        finally:
            zulip.close()


class ImportHistoryContent(AbstractTaskContent):
    KIND = "import_history"

    external_account_uuid = properties.property(types.UUID(), required=True)
    first_message_id = properties.property(types.Integer(min_value=1), required=True)
    last_message_id = properties.property(types.Integer(min_value=1), required=True)

    def validate(self) -> None:
        count = self.last_message_id - self.first_message_id + 1
        if not 1 <= count <= MAX_HISTORY_MESSAGES:
            raise exceptions.ValidationErrorException()

    def execute(self) -> None:
        raise NotImplementedError("History import execution is not implemented yet")


TASK_CONTENT_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(CheckConnectionContent),
    types_dynamic.KindModelType(ImportHistoryContent),
)


class TaskStatus(str, enum.Enum):
    NEW = "new"
    RETRY = "retry"
    ERROR = "error"
    DONE = "done"


class Task(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_tasks"

    priority = properties.property(types.Integer(min_value=0), default=0)
    status = properties.property(
        types.Enum([status.value for status in TaskStatus]),
        default=TaskStatus.NEW.value,
    )
    content = properties.property(TASK_CONTENT_TYPE, required=True)
    error_description = properties.property(
        types.AllowNone(types.String()), default=None
    )
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    lease_owner = properties.property(types.AllowNone(types.UUID()), default=None)
    lease_expires_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )

    def execute(self) -> None:
        return self.content.execute()
