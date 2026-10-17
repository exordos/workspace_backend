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
import datetime
import enum
import hashlib
import logging
import urllib.parse
import uuid as sys_uuid

import requests
from restalchemy.common import contexts
from restalchemy.common import exceptions
from restalchemy.dm import filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm
from restalchemy.storage.sql import utils

from workspace.bridges.zulip import client
from workspace.messenger_api.dm import external_models


LOG = logging.getLogger(__name__)
MAX_HISTORY_MESSAGES = 5000
TASK_PRIORITY_IMPORT_HISTORY = 20
TASK_PRIORITY_IMPORT_CHANNELS = 50
TASK_PRIORITY_FULL_BACKFILL = 100
TASK_PRIORITY_INITIALIZE_CONNECTION = 100
TASK_PRIORITY_REALTIME_MESSAGE = 200
TASK_RETRY_DELAY = datetime.timedelta(minutes=1)
TASK_CHANNEL_SYNC_INTERVAL = datetime.timedelta(minutes=15)


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

    external_account_uuid = properties.property(types.UUID(), required=True)
    stream_uuid = properties.property(types.UUID(), required=True)
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
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """A channel message with account-specific flags."""

    __tablename__ = "bridge_zulip_v5_messages"

    external_account_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    message_uuid = properties.property(types.UUID(), required=True)
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
    def execute(self, task: "Task") -> None:
        raise NotImplementedError()

    def on_error(self, task: "Task", error: Exception) -> None:
        pass

    def on_done(self, task: "Task") -> None:
        pass


class AbstractZulipTaskContent(AbstractTaskContent):
    def on_error(self, task: "Task", error: Exception) -> None:
        response = error.response if isinstance(error, requests.HTTPError) else None
        if isinstance(error, (requests.ConnectionError, requests.Timeout)) or (
            response is not None
            and (response.status_code in (408, 429) or response.status_code >= 500)
        ):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = (
                datetime.datetime.now(datetime.timezone.utc) + TASK_RETRY_DELAY
            )
            description = f"{error}\nNext retry at: {task.next_retry_at.isoformat()}"
        elif response is not None:
            result = response.json()
            description = (
                f"HTTP {response.status_code} {result['code']}: {result['msg']}"
            )
            task.status = TaskStatus.ERROR.value
            task.error_description = description
        else:
            return
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        account.update_status(
            external_models.ExternalAccountStatus.ERROR.value,
            error_description=description,
        )


class InitializeConnectionContent(AbstractZulipTaskContent):
    KIND = "initialize_connection"

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        zulip = client.ZulipClient(account)
        try:
            zulip.check_connection()
        finally:
            zulip.close()

    def on_done(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        account.update_status(external_models.ExternalAccountStatus.BACKFILL.value)
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=TASK_PRIORITY_FULL_BACKFILL,
            content=FullBackfillContent(),
        ).insert()


class FullBackfillContent(AbstractZulipTaskContent):
    KIND = "full_backfill"

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        zulip = client.ZulipClient(account)
        try:
            last_message_id = zulip.get_last_message_id()
        finally:
            zulip.close()
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=TASK_PRIORITY_IMPORT_CHANNELS,
            content=ImportChannelsContent(),
        ).insert()
        for first_message_id in reversed(
            range(1, last_message_id + 1, MAX_HISTORY_MESSAGES)
        ):
            Task(
                project_id=task.project_id,
                external_account_uuid=task.external_account_uuid,
                priority=TASK_PRIORITY_IMPORT_HISTORY,
                content=ImportHistoryContent(
                    first_message_id=first_message_id,
                    last_message_id=min(
                        first_message_id + MAX_HISTORY_MESSAGES - 1,
                        last_message_id,
                    ),
                ),
            ).insert()


class ImportChannelsContent(AbstractZulipTaskContent):
    KIND = "import_channels"

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        hostname = urllib.parse.urlsplit(account.settings["server_url"]).hostname
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            stream_ids = set()
            for data in zulip.get_channels():
                values = {
                    name: data[name]
                    for name in (
                        "stream_id",
                        "name",
                        "description",
                        "rendered_description",
                        "date_created",
                        "creator_id",
                        "invite_only",
                        "is_web_public",
                        "history_public_to_subscribers",
                        "is_archived",
                        "message_retention_days",
                    )
                }
                values["stream_uuid"] = sys_uuid.uuid5(
                    server_namespace, str(values["stream_id"])
                )
                channel = ZulipChannel.objects.get_one_or_none(
                    filters={
                        "external_account_uuid": filters.EQ(account.uuid),
                        "stream_id": filters.EQ(values["stream_id"]),
                    },
                )
                if channel is None:
                    ZulipChannel(
                        external_account_uuid=account.uuid,
                        **values,
                    ).insert()
                else:
                    channel.update_dm(values=values)
                    channel.update()
                stream_ids.add(values["stream_id"])
            for channel in ZulipChannel.objects.get_all(
                filters={"external_account_uuid": filters.EQ(account.uuid)},
            ):
                if channel.stream_id not in stream_ids:
                    channel.delete()
        finally:
            zulip.close()

    def on_done(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        account.update_status(external_models.ExternalAccountStatus.BACKFILL.value)
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=TASK_PRIORITY_IMPORT_CHANNELS,
            not_before_at=(
                datetime.datetime.now(datetime.timezone.utc)
                + TASK_CHANNEL_SYNC_INTERVAL
            ),
            content=ImportChannelsContent(),
        ).insert()


class ImportHistoryContent(AbstractZulipTaskContent):
    KIND = "import_history"

    first_message_id = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    last_message_id = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )

    def validate(self) -> None:
        count = self.last_message_id - self.first_message_id + 1
        if not 1 <= count <= MAX_HISTORY_MESSAGES:
            raise exceptions.ValidationErrorException()

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        hostname = urllib.parse.urlsplit(account.settings["server_url"]).hostname
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            for data in zulip.get_messages(self.first_message_id, self.last_message_id):
                if data["type"] != "stream":
                    continue
                channel = ZulipChannel.objects.get_one(
                    filters={
                        "external_account_uuid": filters.EQ(account.uuid),
                        "stream_id": filters.EQ(data["stream_id"]),
                    },
                )
                topic_uuid = sys_uuid.uuid5(channel.uuid, data["subject"])
                topic = ZulipTopic.objects.get_one_or_none(
                    filters={"uuid": filters.EQ(topic_uuid)},
                )
                if topic is None:
                    topic = ZulipTopic(
                        uuid=topic_uuid,
                        stream_id=data["stream_id"],
                        name=data["subject"],
                        max_id=data["id"],
                    )
                    topic.insert()
                elif data["id"] > topic.max_id:
                    topic.max_id = data["id"]
                    topic.update()
                message = ZulipMessage.objects.get_one_or_none(
                    filters={
                        "external_account_uuid": filters.EQ(account.uuid),
                        "project_id": filters.EQ(task.project_id),
                        "message_id": filters.EQ(data["id"]),
                    },
                )
                content_hash = hashlib.sha256(
                    data["content"].encode("utf-8")
                ).hexdigest()
                content_updated_at = datetime.datetime.fromtimestamp(
                    data.get("last_edit_timestamp", data["timestamp"]),
                    tz=datetime.timezone.utc,
                )
                content_values = {
                    "zulip_content": data["content"],
                    "workspace_content": data["content"],
                    "zulip_content_hash": content_hash,
                    "workspace_content_hash": content_hash,
                    "zulip_content_updated_at": content_updated_at,
                    "workspace_content_updated_at": content_updated_at,
                }
                if message is None:
                    content = MessageContent(**content_values)
                    content.insert()
                else:
                    content = message.content
                    content.update_dm(values=content_values)
                    content.update()
                values = {
                    "message_uuid": sys_uuid.uuid5(server_namespace, str(data["id"])),
                    "message_id": data["id"],
                    "sender_id": data["sender_id"],
                    "timestamp": data["timestamp"],
                    "channel": channel,
                    "topic": topic,
                    "content": content,
                    "is_me_message": data["is_me_message"],
                    **{
                        flag: flag in data["flags"]
                        for flag in (
                            "read",
                            "starred",
                            "collapsed",
                            "hide_link_previews",
                            "mentioned",
                            "stream_wildcard_mentioned",
                            "topic_wildcard_mentioned",
                            "has_alert_word",
                            "historical",
                        )
                    },
                }
                if message is None:
                    ZulipMessage(
                        project_id=task.project_id,
                        external_account_uuid=account.uuid,
                        **values,
                    ).insert()
                else:
                    message.update_dm(values=values)
                    message.update()
        finally:
            zulip.close()

    def on_done(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        account.update_status(external_models.ExternalAccountStatus.BACKFILL.value)


TASK_CONTENT_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(InitializeConnectionContent),
    types_dynamic.KindModelType(FullBackfillContent),
    types_dynamic.KindModelType(ImportChannelsContent),
    types_dynamic.KindModelType(ImportHistoryContent),
)


class TaskStatus(str, enum.Enum):
    NEW = "new"
    RETRY = "retry"
    ERROR = "error"
    DONE = "done"
    CANCEL = "cancel"


class Task(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_tasks"

    external_account_uuid = properties.property(types.UUID(), required=True)
    priority = properties.property(
        types.Integer(min_value=0), default=TASK_PRIORITY_IMPORT_HISTORY
    )
    status = properties.property(
        types.Enum([status.value for status in TaskStatus]),
        default=TaskStatus.NEW.value,
    )
    content = properties.property(TASK_CONTENT_TYPE, required=True, read_only=True)
    error_description = properties.property(
        types.AllowNone(types.String()), default=None
    )
    not_before_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    lease_owner = properties.property(types.AllowNone(types.UUID()), default=None)
    lease_expires_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )

    def cancel_pending_tasks(self, exclude_current: bool = False) -> int:
        now = datetime.datetime.now(datetime.timezone.utc)
        session = contexts.Context().get_session()
        cancelled = session.execute(
            """
            UPDATE bridge_zulip_v5_tasks
            SET status = %s, next_retry_at = NULL,
                lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
            WHERE external_account_uuid = %s AND project_id = %s
                AND status IN (%s, %s)
                AND (NOT %s OR uuid <> %s)
            RETURNING uuid
            """,
            (
                TaskStatus.CANCEL.value,
                now.replace(tzinfo=None),
                self.external_account_uuid,
                self.project_id,
                TaskStatus.NEW.value,
                TaskStatus.RETRY.value,
                exclude_current,
                self.uuid,
            ),
        ).fetchall()
        if any(row["uuid"] == self.uuid for row in cancelled):
            self.status = TaskStatus.CANCEL.value
            self.next_retry_at = None
            self.lease_owner = None
            self.lease_expires_at = None
            self.properties["updated_at"].set_value_force(now)
        return len(cancelled)

    def execute(self) -> None:
        self.error_description = None
        self.next_retry_at = None
        try:
            with utils.savepoint():
                self.content.execute(self)
        except Exception as error:
            LOG.exception("Task %s failed", self.uuid)
            self.on_error(error)
        else:
            self.on_done()

    def on_error(self, error: Exception) -> None:
        self.status = TaskStatus.ERROR.value
        self.error_description = str(error)
        self.next_retry_at = None
        try:
            with utils.savepoint():
                self.content.on_error(self, error)
        except Exception as handler_error:
            LOG.exception("Task %s error handler failed", self.uuid)
            self.status = TaskStatus.ERROR.value
            self.error_description = f"{error}\nError handler failed: {handler_error}"
            self.next_retry_at = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.update()

    def on_done(self) -> None:
        self.status = TaskStatus.DONE.value
        self.error_description = None
        self.next_retry_at = None
        try:
            with utils.savepoint():
                self.content.on_done(self)
        except Exception as error:
            LOG.exception("Task %s completion handler failed", self.uuid)
            self.on_error(error)
        else:
            self.lease_owner = None
            self.lease_expires_at = None
            self.update()
