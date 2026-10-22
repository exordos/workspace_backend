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
import contextlib
import datetime
import enum
import hashlib
import json
import logging
import time
import typing
import uuid as sys_uuid

import psycopg.errors
import requests
from restalchemy.common import contexts
from restalchemy.common import exceptions
from restalchemy.dm import filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage import exceptions as storage_exceptions
from restalchemy.storage.sql import orm
from restalchemy.storage.sql import sessions
from restalchemy.storage.sql import utils

from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import owned_projection

from workspace.bridges.zulip import client
from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip import message_conversion
from workspace.bridges.zulip import history_fingerprints
from workspace.bridges.zulip.dm import flags as message_flags
from workspace.bridges.zulip.dm import history as history_snapshots
from workspace.bridges.zulip.dm import reactions as message_reactions
from workspace.common import models as common_models
from workspace.common import types as common_types
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import base as messenger_base
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers as messenger_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import v2_models


LOG = logging.getLogger(__name__)
# Preserve the validation bound for archived task payloads.
MAX_HISTORY_MESSAGES = 5000
HISTORY_BATCH_SIZE = 100
TASK_PRIORITY_IMPORT_HISTORY = 10_000
TASK_PRIORITY_IMPORT_HISTORY_MIN = 10
TASK_PRIORITY_IMPORT_CHANNELS = 450_000
TASK_PRIORITY_IMPORT_USERS = 500_000
TASK_PRIORITY_FULL_BACKFILL = 100_000
TASK_PRIORITY_INITIALIZE_CONNECTION = 600_000
TASK_PRIORITY_REALTIME_MESSAGE = 300_000
TASK_PRIORITY_REALTIME_EDIT = 250_000
REALTIME_MESSAGE_RETRY_DELAY = datetime.timedelta(milliseconds=250)
TASK_PRIORITY_REALTIME_FLAGS = 550_000
TASK_PRIORITY_RECONCILIATION = 200_000
TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS = 700_000
TASK_RETRY_DELAY = datetime.timedelta(minutes=1)


class ExternalAccountIdPendingError(Exception):
    pass


class ExternalAccountAlreadyConnectedError(Exception):
    pass


class SubscriptionChangedError(Exception):
    pass


def validate_outgoing_account(account: external_models.ExternalAccount) -> None:
    """Authorize this send with a fresh account snapshot, then close the DB hop.

    Revocation after this check cannot cancel an HTTP request already authorized.
    The completion hop checks the revision again before recording its ACK.
    """
    with contexts.Context().session_manager():
        current = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(account.uuid),
                "project_id": filters.EQ(account.project_id),
            }
        )
        if (
            not current.can_stream()
            or current.realtime_revision() != account.realtime_revision()
        ):
            raise SubscriptionChangedError()


class MessageContent(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_message_contents"

    source_timestamp = properties.property(types.Integer(min_value=0), default=0)
    source_message_id = properties.property(types.Integer(min_value=0), default=0)

    conversion_version = properties.property(types.Integer(min_value=0), default=0)

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

    def convert(
        self,
        converter: content_conversion.ConversionContext,
        data: dict[str, typing.Any],
        stream_uuid: sys_uuid.UUID,
        zulip: client.ZulipClient | None,
    ) -> None:
        delivered = converter.delivered_content(data["id"])
        if delivered is not None:
            self.workspace_content = delivered["content"]
            self.workspace_content_updated_at = delivered["content_updated_at"].replace(
                tzinfo=datetime.timezone.utc
            )
        elif self.workspace_content_updated_at <= self.zulip_content_updated_at:
            self.workspace_content = converter.to_workspace(
                {**data, "content": self.zulip_content}, stream_uuid, zulip
            )
            self.workspace_content_updated_at = self.zulip_content_updated_at
        self.conversion_version = content_conversion.VERSION
        self.update()


class ZulipChannelType(str, enum.Enum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class ZulipProjectUserState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Zulip account owners missing from the Workspace project registry."""

    __tablename__ = "bridge_zulip_v5_project_user_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    user_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.user_uuid

    def do(self) -> None:
        contexts.Context().get_session().execute(
            "SELECT messenger_v2_register_project_user(%s, %s)",
            (self.project_id, self.user_uuid),
        )


class ZulipStreamState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Stream differences to reconcile between Zulip and Workspace."""

    __tablename__ = "bridge_zulip_v5_stream_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    stream_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    action = properties.property(
        types.Enum(["create", "delete"]), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.stream_uuid

    def do(self) -> None:
        {
            "create": self.create_workspace_stream,
            "delete": self.delete_workspace_stream,
        }[self.action]()

    def create_workspace_stream(self) -> None:
        channels = ZulipChannel.objects.get_all(
            filters={"stream_uuid": filters.EQ(self.stream_uuid)},
            order_by={"updated_at": "desc", "uuid": "asc"},
        )
        accounts = {
            account.uuid: account
            for account in external_models.ExternalAccount.objects.get_all(
                filters={
                    "uuid": filters.In(
                        [channel.external_account_uuid for channel in channels]
                    ),
                    "project_id": filters.EQ(self.project_id),
                    "provider": filters.EQ("zulip"),
                },
            )
        }
        channel = next(
            channel for channel in channels if channel.external_account_uuid in accounts
        )
        account = accounts[channel.external_account_uuid]
        {
            ZulipChannelType.CHANNEL.value: self._create_channel_stream,
            ZulipChannelType.GROUP.value: self._create_group_stream,
            ZulipChannelType.PRIVATE.value: self._create_private_stream,
        }[channel.channel_type](channel, account)

    def _create_channel_stream(
        self, channel: "ZulipChannel", account: external_models.ExternalAccount
    ) -> None:
        if channel.creator_id is not None:
            account = external_models.ExternalAccount.objects.get_one(
                filters={
                    "project_id": filters.EQ(self.project_id),
                    "provider": filters.EQ("zulip"),
                    "external_account_id": filters.EQ(
                        f"{account.get_host()}:{channel.creator_id}"
                    ),
                },
            )
        self._create_stream(channel, account.owner_user_uuid)

    def _create_group_stream(
        self, channel: "ZulipChannel", account: external_models.ExternalAccount
    ) -> None:
        self._create_stream(channel, account.owner_user_uuid)

    def _create_private_stream(
        self, channel: "ZulipChannel", account: external_models.ExternalAccount
    ) -> None:
        peer = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(channel.direct_user_uuid),
                "project_id": filters.EQ(self.project_id),
                "provider": filters.EQ("zulip"),
            },
        )
        self._create_stream(channel, account.owner_user_uuid, peer.owner_user_uuid)

    def _create_stream(
        self,
        channel: "ZulipChannel",
        owner_uuid: sys_uuid.UUID,
        direct_user_uuid: sys_uuid.UUID | None = None,
    ) -> None:
        session = contexts.Context().get_session()
        session.execute(
            "SELECT messenger_v2_register_project_user(%s,%s)",
            (self.project_id, owner_uuid),
        )
        if direct_user_uuid is not None:
            session.execute(
                "SELECT messenger_v2_register_project_user(%s,%s)",
                (self.project_id, direct_user_uuid),
            )
        stream = v2_models.WorkspaceStream(
            uuid=self.stream_uuid,
            project_id=self.project_id,
            owner_uuid=owner_uuid,
            name=channel.name,
            description=channel.description,
            source_name=messenger_models.StreamSourceName.ZULIP.value,
            source={"kind": "zulip"},
            invite_only=channel.invite_only,
            is_archived=channel.is_archived,
            private=channel.channel_type == ZulipChannelType.PRIVATE.value,
            direct_user_uuid=direct_user_uuid,
            private_index=(
                f"zulip:{self.stream_uuid}" if direct_user_uuid is not None else None
            ),
            created_at=(
                datetime.datetime.fromtimestamp(
                    channel.date_created, datetime.timezone.utc
                )
                if channel.date_created is not None
                else channel.created_at
            ),
        )
        restored = session.execute(
            "INSERT INTO messenger_streams (uuid,project_id,owner_uuid,name,description,source_name,source,invite_only,is_archived,private,direct_user_uuid,private_index,color,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (project_id,uuid) DO UPDATE SET owner_uuid=EXCLUDED.owner_uuid, name=EXCLUDED.name,description=EXCLUDED.description, "
            "source_name=EXCLUDED.source_name,source=EXCLUDED.source,invite_only=EXCLUDED.invite_only,is_archived=EXCLUDED.is_archived, "
            "private=EXCLUDED.private,direct_user_uuid=EXCLUDED.direct_user_uuid,private_index=EXCLUDED.private_index,color=EXCLUDED.color, "
            "deleted_at=NULL,updated_at=EXCLUDED.updated_at WHERE messenger_streams.deleted_at IS NOT NULL RETURNING uuid",
            (
                stream.uuid,
                stream.project_id,
                stream.owner_uuid,
                stream.name,
                stream.description,
                stream.source_name,
                json.dumps(stream.source),
                stream.invite_only,
                stream.is_archived,
                stream.private,
                stream.direct_user_uuid,
                stream.private_index,
                stream.color,
                messenger_helpers._database_timestamp(stream.created_at),
                messenger_helpers._database_timestamp(stream.updated_at),
            ),
        ).fetchone()
        if restored is None:
            raise storage_exceptions.ConflictRecords(
                model=stream, msg="Stream already exists"
            )

    def delete_workspace_stream(self) -> None:
        stream = v2_models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.stream_uuid),
                "source_name": filters.EQ(
                    messenger_models.StreamSourceName.ZULIP.value
                ),
            },
        )
        stream.delete()


class ZulipStreamBindingState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Stream membership differences between Zulip and Workspace."""

    __tablename__ = "bridge_zulip_v5_stream_binding_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    stream_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    user_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    binding_uuid = properties.property(types.UUID(), required=True, read_only=True)
    notification_mode = properties.property(
        types.AllowNone(
            types.Enum(
                [
                    mode.value
                    for mode in messenger_models.WorkspaceStreamNotificationMode
                ]
            )
        ),
        default=None,
        read_only=True,
    )
    action = properties.property(
        types.Enum(["create", "update", "delete"]), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.stream_uuid, self.user_uuid

    def do(self) -> None:
        {
            "create": self.create_workspace_binding,
            "update": self.update_workspace_binding,
            "delete": self.delete_workspace_binding,
        }[self.action]()

    def create_workspace_binding(self) -> None:
        messenger_helpers.get_or_create_workspace_stream_bindings(
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            who_uuid=self.user_uuid,
            role_user_uuids={
                messenger_models.WorkspaceStreamRole.MEMBER.value: [self.user_uuid]
            },
            binding_uuids={str(self.user_uuid): self.binding_uuid},
            session=contexts.Context().get_session(),
            defer_folder_events=True,
        )
        if self.notification_mode is not None:
            self.update_workspace_binding()

    def update_workspace_binding(self) -> None:
        session = contexts.Context().get_session()
        messenger_helpers._lock_workspace_project_event_writes(self.project_id, session)
        messenger_helpers._lock_workspace_stream_binding(
            self.project_id, self.binding_uuid, session
        )
        binding = v2_models.WorkspaceStreamBinding.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.binding_uuid),
            }
        )
        if binding.notification_mode == self.notification_mode:
            return
        session.execute(
            "UPDATE messenger_stream_bindings SET notification_mode=%s, "
            "notification_updated_at=NOW(), updated_at=NOW() "
            "WHERE project_id=%s AND uuid=%s AND active",
            (self.notification_mode, self.project_id, self.binding_uuid),
        )
        topic_uuids = [
            topic.uuid
            for topic in messenger_helpers._get_workspace_stream_topics(
                project_id=self.project_id,
                stream_uuid=self.stream_uuid,
                session=session,
            )
        ]
        ZulipMessageCounterChange.mark_many(
            {
                (self.project_id, self.user_uuid, self.stream_uuid, None),
                *(
                    (self.project_id, self.user_uuid, self.stream_uuid, topic)
                    for topic in topic_uuids
                ),
            }
        )
        stream = v2_models.WorkspaceUserStream.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.stream_uuid),
                "user_uuid": filters.EQ(self.user_uuid),
            }
        )
        messenger_events.create_stream_updated_event(stream, session=session)

    def delete_workspace_binding(self) -> None:
        messenger_helpers.delete_workspace_stream_binding(
            project_id=self.project_id,
            binding_uuid=self.binding_uuid,
            session=contexts.Context().get_session(),
            defer_folder_events=True,
        )


class ZulipTopicChange(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_topic_changes"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    topic_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    revision = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.topic_uuid

    @classmethod
    def acknowledge_many(cls, changes: typing.Sequence["ZulipTopicChange"]) -> None:
        contexts.Context().get_session().execute(
            "DELETE FROM bridge_zulip_v5_topic_changes AS pending "
            "USING unnest(%s::uuid[], %s::uuid[], %s::bigint[]) "
            "AS checked(project_id, topic_uuid, revision) "
            "WHERE pending.project_id = checked.project_id "
            "AND pending.topic_uuid = checked.topic_uuid "
            "AND pending.revision = checked.revision",
            (
                [change.project_id for change in changes],
                [change.topic_uuid for change in changes],
                [change.revision for change in changes],
            ),
        )


class ZulipTopicState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Topic differences to reconcile between Zulip and Workspace."""

    __tablename__ = "bridge_zulip_v5_topic_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    topic_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    action = properties.property(
        types.Enum(["create", "delete"]), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.topic_uuid

    @classmethod
    def reconcile_batch(cls, limit: int) -> int:
        changes = ZulipTopicChange.objects.get_all(limit=limit)
        projects: dict[sys_uuid.UUID, list[sys_uuid.UUID]] = {}
        for change in changes:
            projects.setdefault(change.project_id, []).append(change.topic_uuid)
        for project_id, topic_uuids in projects.items():
            for state in cls.objects.get_all(
                filters={
                    "project_id": filters.EQ(project_id),
                    "topic_uuid": filters.In(topic_uuids),
                },
            ):
                state.do()
            for state in ZulipTopicNotificationState.objects.get_all(
                filters={
                    "project_id": filters.EQ(project_id),
                    "topic_uuid": filters.In(topic_uuids),
                },
            ):
                state.do()
        if changes:
            ZulipTopicChange.acknowledge_many(changes)
        return len(changes)

    def do(self) -> None:
        {
            "create": self.create_workspace_topic,
            "delete": self.delete_workspace_topic,
        }[self.action]()

    def create_workspace_topic(self) -> None:
        topic = ZulipTopic.objects.get_one(
            filters={"uuid": filters.EQ(self.topic_uuid)},
        )
        workspace_topic = v2_models.WorkspaceStreamTopic(
            uuid=self.topic_uuid,
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            name=topic.name,
            source_name=messenger_models.TopicSourceName.ZULIP.value,
            source=messenger_models.ZulipSource(),
            created_at=topic.created_at,
        )
        session = contexts.Context().get_session()
        restored = session.execute(
            "INSERT INTO messenger_topics (uuid,project_id,stream_uuid,name,color,source_name,source,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'zulip','{\"kind\":\"zulip\"}'::jsonb,%s,%s) "
            "ON CONFLICT (project_id,uuid) DO UPDATE SET name=EXCLUDED.name,color=EXCLUDED.color,source_name=EXCLUDED.source_name,source=EXCLUDED.source,deleted_at=NULL,updated_at=EXCLUDED.updated_at WHERE messenger_topics.deleted_at IS NOT NULL RETURNING uuid",
            (
                workspace_topic.uuid,
                workspace_topic.project_id,
                workspace_topic.stream_uuid,
                workspace_topic.name,
                workspace_topic.color,
                messenger_helpers._database_timestamp(workspace_topic.created_at),
                messenger_helpers._database_timestamp(workspace_topic.updated_at),
            ),
        ).fetchone()
        if restored is None:
            raise storage_exceptions.ConflictRecords(
                model=workspace_topic, msg="Topic already exists"
            )
        session.execute(
            "INSERT INTO messenger_user_topic_bindings (uuid,project_id,user_uuid,topic_uuid,notification_mode) "
            "SELECT messenger_uuid_v5(%s,user_uuid::text),project_id,user_uuid,%s,'default' "
            "FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND active "
            "ON CONFLICT (project_id,user_uuid,topic_uuid) DO NOTHING",
            (self.topic_uuid, self.topic_uuid, self.project_id, self.stream_uuid),
        )
        v2_models.WorkspaceDomainOutboxEvent(
            project_id=self.project_id,
            event_kind="topic_state_projection",
            scope_kind="topic",
            scope_key=f"{self.project_id}:{self.topic_uuid}",
            payload={
                "source_kind": "topic.created",
                "topic_uuid": str(self.topic_uuid),
                "emit_public_event": False,
            },
        ).insert(session=session)

    def delete_workspace_topic(self) -> None:
        topic = v2_models.WorkspaceStreamTopic.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.topic_uuid),
                "stream_uuid": filters.EQ(self.stream_uuid),
                "source_name": filters.EQ(messenger_models.TopicSourceName.ZULIP.value),
            },
        )
        topic.delete()


class ZulipTopicNotificationState(models.ModelWithID, orm.SQLStorableMixin):
    __tablename__ = "bridge_zulip_v5_topic_notification_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    user_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    topic_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    notification_mode = properties.property(
        types.Enum(
            [mode.value for mode in messenger_models.WorkspaceTopicNotificationMode]
        ),
        required=True,
        read_only=True,
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.user_uuid, self.topic_uuid

    def do(self) -> None:
        session = contexts.Context().get_session()
        messenger_helpers._lock_workspace_project_event_writes(self.project_id, session)
        session.execute(
            "UPDATE messenger_user_topic_bindings SET notification_mode=%s, updated_at=NOW() "
            "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
            (self.notification_mode, self.project_id, self.user_uuid, self.topic_uuid),
        )
        ZulipMessageCounterChange.mark_many(
            {(self.project_id, self.user_uuid, self.stream_uuid, self.topic_uuid)}
        )
        topic = v2_models.WorkspaceUserTopic.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "user_uuid": filters.EQ(self.user_uuid),
                "uuid": filters.EQ(self.topic_uuid),
            }
        )
        messenger_events.create_topic_updated_event(topic, session=session)


class ZulipMessageChange(v2_models.WorkspaceMessageChange):
    __tablename__ = "bridge_zulip_v5_message_changes"


class ZulipMessageCounterChange(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_message_counter_changes"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    user_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    scope_kind = properties.property(
        types.Enum(["user-stream", "user-topic"]),
        required=True,
        read_only=True,
        id_property=True,
    )
    scope_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    revision = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID, str, sys_uuid.UUID]:
        return self.project_id, self.user_uuid, self.scope_kind, self.scope_uuid

    @classmethod
    def message_scopes(
        cls, project_id: sys_uuid.UUID, message_uuids: typing.Sequence[sys_uuid.UUID]
    ) -> set[tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]]:
        rows = (
            contexts.Context()
            .get_session()
            .execute(
                """
            SELECT placement.project_id, audience.user_uuid,
                   placement.stream_uuid, placement.topic_uuid
            FROM messenger_message_placements AS placement
            JOIN messenger_messages AS message
              ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
            CROSS JOIN LATERAL (
                SELECT user_uuid FROM messenger_user_message_bindings
                WHERE project_id = placement.project_id AND placement_uuid = placement.uuid
                UNION
                SELECT user_uuid FROM messenger_user_message_states
                WHERE project_id = placement.project_id AND placement_uuid = placement.uuid
            ) AS audience
            WHERE placement.project_id = %s AND placement.message_uuid = ANY(%s::uuid[])
              AND message.source_name = 'zulip'
            """,
                (project_id, message_uuids),
            )
            .fetchall()
        )
        return {
            (row["project_id"], row["user_uuid"], row["stream_uuid"], row["topic_uuid"])
            for row in rows
        }

    @classmethod
    def mark_many(
        cls,
        scopes: set[
            tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]
        ],
    ) -> None:
        changes = owned_projection.CURRENT.get()
        if changes is not None:
            for project, user, stream, topic in scopes:
                changes.topic(project, stream, topic, [user])
            return
        if not scopes:
            return
        rows = {
            (
                project,
                user,
                kind,
                scope,
                stream,
                topic if kind == "user-topic" else None,
            )
            for project, user, stream, topic in scopes
            for kind, scope in (("user-stream", stream), ("user-topic", topic))
            if scope is not None
        }
        columns = list(zip(*rows))
        contexts.Context().get_session().execute(
            """
            INSERT INTO bridge_zulip_v5_message_counter_changes
                (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
            SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::text[], %s::uuid[], %s::uuid[], %s::uuid[])
            ON CONFLICT (project_id, user_uuid, scope_kind, scope_uuid)
            DO UPDATE SET revision = EXCLUDED.revision,
                          stream_uuid = EXCLUDED.stream_uuid, topic_uuid = EXCLUDED.topic_uuid
            """,
            tuple(list(column) for column in columns),
        )

    @classmethod
    def publish_batch(cls, limit: int) -> int:
        changes = cls.objects.get_all(limit=limit)
        if not changes:
            return 0
        session = contexts.Context().get_session()
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events
                (uuid, project_id, event_kind, scope_kind, scope_key, payload)
            SELECT event_uuid, project_id, 'read_counters', scope_kind, scope_key, payload::jsonb
            FROM unnest(%s::uuid[], %s::uuid[], %s::text[], %s::text[], %s::text[])
                AS pending(event_uuid, project_id, scope_kind, scope_key, payload)
            """,
            (
                [sys_uuid.uuid4() for change in changes],
                [change.project_id for change in changes],
                [change.scope_kind for change in changes],
                [
                    f"{change.project_id}:{change.user_uuid}:{change.scope_uuid}"
                    for change in changes
                ],
                [
                    json.dumps(
                        {
                            "source_kind": "zulip.reconciled",
                            "user_uuid": str(change.user_uuid),
                            "stream_uuid": str(change.stream_uuid),
                            "topic_uuid": str(change.topic_uuid)
                            if change.topic_uuid is not None
                            else None,
                        }
                    )
                    for change in changes
                ],
            ),
        )
        cls.acknowledge_many(changes)
        return len(changes)

    @classmethod
    def acknowledge_many(
        cls, changes: typing.Sequence["ZulipMessageCounterChange"]
    ) -> None:
        contexts.Context().get_session().execute(
            """
            DELETE FROM bridge_zulip_v5_message_counter_changes AS pending
            USING unnest(%s::uuid[], %s::uuid[], %s::text[], %s::uuid[], %s::bigint[])
                AS checked(project_id, user_uuid, scope_kind, scope_uuid, revision)
            WHERE pending.project_id = checked.project_id AND pending.user_uuid = checked.user_uuid
              AND pending.scope_kind = checked.scope_kind AND pending.scope_uuid = checked.scope_uuid
              AND pending.revision = checked.revision
            """,
            (
                [change.project_id for change in changes],
                [change.user_uuid for change in changes],
                [change.scope_kind for change in changes],
                [change.scope_uuid for change in changes],
                [change.revision for change in changes],
            ),
        )


class ZulipMessageState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Message existence and content differences, without viewer state."""

    __tablename__ = "bridge_zulip_v5_message_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    message_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    source_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    action = properties.property(
        types.Enum(["create", "delete", "update"]), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.message_uuid

    @classmethod
    def reconcile_batch(
        cls,
        limit: int,
        *,
        project_id: sys_uuid.UUID | None = None,
        message_uuids: list[sys_uuid.UUID] | None = None,
    ) -> int:
        if mutation_journal_opts.enabled() and owned_projection.CURRENT.get() is None:
            return 0
        changes: dict[
            tuple[sys_uuid.UUID, sys_uuid.UUID],
            list[ZulipMessageChange | v2_models.WorkspaceMessageChange],
        ] = {}
        scope = (
            {
                "project_id": filters.EQ(project_id),
                "message_uuid": filters.In(message_uuids),
            }
            if message_uuids is not None
            else {}
        )
        change_models = (ZulipMessageChange, v2_models.WorkspaceMessageChange)
        for model in change_models:
            for change in model.objects.get_all(
                filters=scope,
                order_by={
                    "message_created_at": "desc",
                    "message_sequence": "desc",
                    "revision": "asc",
                },
                limit=limit,
            ):
                changes.setdefault(change.get_id(), []).append(change)
        batch = dict(
            sorted(
                changes.items(),
                key=lambda item: max(
                    (change.message_created_at, change.message_sequence)
                    for change in item[1]
                ),
                reverse=True,
            )[:limit]
        )
        if not batch:
            return 0
        projects: dict[sys_uuid.UUID, list[sys_uuid.UUID]] = {}
        for project_id, message_uuid in batch:
            projects.setdefault(project_id, []).append(message_uuid)
        counter_scopes: set[
            tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID | None]
        ] = set()
        # Suppress only our own marks for this batch. Other connections still
        # advance revisions, so acknowledgement below cannot consume their work.
        with utils.savepoint(name="zulip_message_reconciliation") as session:
            previous_batch = session.execute(
                "SELECT current_setting('workspace.zulip_reconciliation_batch', true) AS value"
            ).fetchone()["value"]
            session.execute(
                "SELECT set_config('workspace.zulip_reconciliation_batch', %s, true)",
                (json.dumps([f"{project}:{message}" for project, message in batch]),),
            )
            for project_id, message_uuids in sorted(projects.items()):
                messenger_helpers._lock_workspace_project_event_writes(
                    project_id, session
                )
                before = cls._workspace_messages(project_id, message_uuids)
                for model in (cls, ZulipMessagePlacementState, ZulipUserMessageState):
                    states = model.objects.get_all(
                        filters={
                            "project_id": filters.EQ(project_id),
                            "message_uuid": filters.In(message_uuids),
                        },
                    )
                    if not states:
                        continue
                    rank = {
                        message: index for index, message in enumerate(message_uuids)
                    }
                    if (
                        model is ZulipUserMessageState
                        and owned_projection.CURRENT.get() is not None
                    ):
                        states.sort(key=lambda state: state.state_uuid.int)
                    else:
                        states.sort(key=lambda state: rank[state.message_uuid])
                    changed_uuids = list({state.message_uuid for state in states})
                    counter_scopes.update(
                        ZulipMessageCounterChange.message_scopes(
                            project_id, changed_uuids
                        )
                    )
                    for state in states:
                        state.do()
                    counter_scopes.update(
                        ZulipMessageCounterChange.message_scopes(
                            project_id, changed_uuids
                        )
                    )
                cls._publish_message_events(
                    project_id,
                    before,
                    cls._workspace_messages(project_id, message_uuids),
                    session,
                )
            session.execute(
                "SELECT set_config('workspace.zulip_reconciliation_batch', %s, true)",
                (previous_batch or "",),
            )
        ZulipMessageCounterChange.mark_many(counter_scopes)
        # A write can affect another message sharing source data. Keep its
        # original mark if any reconciliation stage still has a difference.
        unreconciled: set[tuple[sys_uuid.UUID, sys_uuid.UUID]] = set()
        for project_id, message_uuids in projects.items():
            for model in (cls, ZulipMessagePlacementState, ZulipUserMessageState):
                unreconciled.update(
                    (state.project_id, state.message_uuid)
                    for state in model.objects.get_all(
                        filters={
                            "project_id": filters.EQ(project_id),
                            "message_uuid": filters.In(message_uuids),
                        },
                    )
                )
        for model in change_models:
            pending = [
                change
                for changes in batch.values()
                for change in changes
                if type(change) is model and change.get_id() not in unreconciled
            ]
            if pending:
                model.acknowledge_many(pending)
        return len(batch)

    @classmethod
    def _workspace_messages(
        cls, project_id: sys_uuid.UUID, message_uuids: list[sys_uuid.UUID]
    ) -> dict[tuple[sys_uuid.UUID, sys_uuid.UUID], v2_models.WorkspaceUserMessage]:
        return {
            (message.uuid, message.user_uuid): message
            for message in v2_models.WorkspaceUserMessage.objects.get_all(
                filters={
                    "project_id": filters.EQ(project_id),
                    "canonical_message_uuid": filters.In(message_uuids),
                    "visible": filters.EQ(True),
                },
                order_by={"uuid": "asc", "user_uuid": "asc"},
            )
        }

    @classmethod
    def _publish_message_events(
        cls,
        project_id: sys_uuid.UUID,
        before: dict[
            tuple[sys_uuid.UUID, sys_uuid.UUID], v2_models.WorkspaceUserMessage
        ],
        after: dict[
            tuple[sys_uuid.UUID, sys_uuid.UUID], v2_models.WorkspaceUserMessage
        ],
        session: sessions.PgSQLSession,
    ) -> None:
        # Read projections include each recipient's flags and access. Publish
        # only after content, placement and viewer state agree, and retain the
        # previous projection for rows removed by cascading deletes.
        created: dict[sys_uuid.UUID, list[v2_models.WorkspaceUserMessage]] = {}
        updated: dict[sys_uuid.UUID, list[v2_models.WorkspaceUserMessage]] = {}
        deleted: dict[sys_uuid.UUID, list[v2_models.WorkspaceUserMessage]] = {}
        for key, message in after.items():
            if not messenger_events.is_recent_message(message.created_at):
                continue
            previous = before.get(key)
            if previous is None:
                created.setdefault(message.uuid, []).append(message)
            elif messenger_events.message_snapshot_changed(previous, message):
                updated.setdefault(message.uuid, []).append(message)
        for key, message in before.items():
            if key not in after:
                deleted.setdefault(message.uuid, []).append(message)
        for messages in deleted.values():
            message = messages[0]
            messenger_events.create_message_deleted_events(
                project_id,
                [row.user_uuid for row in messages],
                message.uuid,
                message.stream_uuid,
                message.topic_uuid,
                message.author_uuid,
                message.source_name,
                message.source,
                session=session,
                compact=True,
            )
        for messages in created.values():
            messenger_events.create_compact_message_events(
                project_id, messages, session=session
            )
        for messages in updated.values():
            messenger_events.create_message_updated_events(
                project_id, messages, session=session, compact=True
            )

    def do(self) -> None:
        {
            "create": self.create_workspace_message,
            "delete": self.delete_workspace_message,
            "update": self.update_content,
        }[self.action]()

    def create_workspace_message(self) -> None:
        source = ZulipMessage.objects.get_one(
            filters={"uuid": filters.EQ(self.source_uuid)},
        )
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(source.external_account_uuid)},
        )
        author = external_models.ExternalAccount.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "provider": filters.EQ("zulip"),
                "external_account_id": filters.EQ(
                    f"{account.get_host()}:{source.sender_id}"
                ),
            },
        )
        message = v2_models.WorkspaceMessage.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.message_uuid),
                "source_name": filters.EQ(messenger_base.SourceName.ZULIP.value),
            },
        )
        created_at = datetime.datetime.fromtimestamp(
            source.timestamp, tz=datetime.timezone.utc
        )
        if message is None:
            sequence = (
                contexts.Context()
                .get_session()
                .execute(
                    "SELECT nextval('messenger_messages_ingest_sequence_seq') AS value"
                )
                .fetchone()["value"]
            )
            message = v2_models.WorkspaceMessage(
                uuid=self.message_uuid,
                project_id=self.project_id,
                author_uuid=author.owner_user_uuid,
                payload=message_payloads.MarkdownPayload(
                    content=source.content.workspace_content,
                ),
                source_name=messenger_base.SourceName.ZULIP.value,
                source={"kind": "zulip"},
                ingest_sequence=sequence,
                created_at=created_at,
                updated_at=source.content.workspace_content_updated_at,
                content_updated_at=source.content.workspace_content_updated_at,
            )
            message.insert()
        else:
            message.properties["deleted_at"].set_value_force(None)
            message.update()
        self.update_content()

    def update_content(self) -> None:
        session = contexts.Context().get_session()
        content_lock = "FOR UPDATE" if owned_projection.CURRENT.get() is None else ""
        session.execute(
            "SELECT uuid FROM messenger_messages "
            "WHERE project_id = %s AND uuid = %s AND source_name = 'zulip' "
            + content_lock,
            (self.project_id, self.message_uuid),
        )
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.message_uuid),
                "source_name": filters.EQ(messenger_base.SourceName.ZULIP.value),
            },
        )
        source = ZulipMessage.objects.get_one(
            filters={"uuid": filters.EQ(self.source_uuid)}
        )
        observer = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(source.external_account_uuid)}
        )
        author = external_models.ExternalAccount.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "provider": filters.EQ("zulip"),
                "external_account_id": filters.EQ(
                    f"{observer.get_host()}:{source.sender_id}"
                ),
            }
        )
        author_changed = message.author_uuid != author.owner_user_uuid
        if author_changed:
            message.properties["author_uuid"].set_value_force(author.owner_user_uuid)
        rows = session.execute(
            "SELECT content.uuid FROM bridge_zulip_v5_messages AS message "
            "JOIN m_external_accounts_v2 AS account "
            "ON account.uuid = message.external_account_uuid "
            "AND account.project_id = message.project_id "
            "JOIN bridge_zulip_v5_message_contents AS content "
            "ON content.uuid = message.content "
            "WHERE message.project_id = %s AND "
            "messenger_uuid_v5(message.project_id, message.message_uuid::text) = %s "
            "AND account.provider = 'zulip' "
            "ORDER BY content.uuid "
            + (
                "FOR UPDATE OF content"
                if owned_projection.CURRENT.get() is None
                else ""
            ),
            (self.project_id, self.message_uuid),
        ).fetchall()
        contents = MessageContent.objects.get_all(
            filters={"uuid": filters.In([row["uuid"] for row in rows])},
            order_by={"uuid": "asc"},
        )
        text = message.payload.content
        content_hash = message.content_hash
        edited_at = message.content_updated_at
        for content in contents:
            if content.workspace_content_updated_at > edited_at or (
                content.workspace_content_updated_at == edited_at
                and content.conversion_version == content_conversion.VERSION
            ):
                text = content.workspace_content
                content_hash = content.workspace_content_hash
                edited_at = content.workspace_content_updated_at
        if author_changed or (message.content_hash, message.content_updated_at) != (
            content_hash,
            edited_at,
        ):
            message.payload = message_payloads.MarkdownPayload(content=text)
            message.properties["content_updated_at"].set_value_force(edited_at)
            message.properties["updated_at"].set_value_force(
                max(message.updated_at, edited_at)
            )
            # Preserve the edit time instead of dating the synchronization itself.
            with content_conversion.preserve_source_edit_time(
                session, self.project_id, self.message_uuid
            ):
                orm.SQLStorableMixin.update(message)
        for content in contents:
            if (
                content.workspace_content_hash,
                content.workspace_content_updated_at,
            ) != (content_hash, edited_at):
                content.workspace_content = text
                content.workspace_content_hash = content_hash
                content.workspace_content_updated_at = edited_at
                content.update()

    def delete_workspace_message(self) -> None:
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.message_uuid),
                "source_name": filters.EQ(messenger_base.SourceName.ZULIP.value),
            },
        )
        message.delete()


class ZulipMessagePlacementState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Zulip-owned placements missing or differing from the latest observation."""

    __tablename__ = "bridge_zulip_v5_message_placement_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    message_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    placement_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    topic_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    action = properties.property(
        types.Enum(["create", "update", "delete"]), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.message_uuid

    def do(self) -> None:
        {
            "create": self.reconcile_workspace_placement,
            "update": self.reconcile_workspace_placement,
            "delete": self.delete_workspace_placement,
        }[self.action]()

    def reconcile_workspace_placement(self) -> None:
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.placement_uuid),
            },
        )
        if placement is None:
            message = v2_models.WorkspaceMessage.objects.get_one(
                filters={
                    "project_id": filters.EQ(self.project_id),
                    "uuid": filters.EQ(self.message_uuid),
                    "source_name": filters.EQ(messenger_base.SourceName.ZULIP.value),
                },
            )
            v2_models.WorkspaceMessagePlacement(
                uuid=self.placement_uuid,
                project_id=self.project_id,
                message_uuid=self.message_uuid,
                stream_uuid=self.stream_uuid,
                topic_uuid=self.topic_uuid,
                created_at=message.created_at,
                updated_at=message.updated_at,
            ).insert()
        else:
            for field in ("message_uuid", "stream_uuid", "topic_uuid"):
                placement.properties[field].set_value_force(getattr(self, field))
            placement.update()

    def delete_workspace_placement(self) -> None:
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.placement_uuid),
            },
        )
        placement.delete()


class ZulipUserMessageState(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    """Access and imported flags for owners of observed Zulip messages."""

    __tablename__ = "bridge_zulip_v5_user_message_states"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    message_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    user_uuid = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    placement_uuid = properties.property(types.UUID(), required=True, read_only=True)
    binding_uuid = properties.property(types.UUID(), required=True, read_only=True)
    state_uuid = properties.property(types.UUID(), required=True, read_only=True)
    source_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    flags_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    membership_generation = properties.property(
        types.AllowNone(types.Integer(min_value=1)), default=None, read_only=True
    )
    relation_role = properties.property(
        types.AllowNone(types.String()), default=None, read_only=True
    )
    read = properties.property(
        types.AllowNone(types.Boolean()), default=None, read_only=True
    )
    starred = properties.property(
        types.AllowNone(types.Boolean()), default=None, read_only=True
    )
    mentioned = properties.property(
        types.AllowNone(types.Boolean()), default=None, read_only=True
    )
    action = properties.property(
        types.Enum(["create", "update", "delete"]), required=True, read_only=True
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return cls.id_properties.copy()

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.message_uuid, self.user_uuid

    def do(self) -> None:
        binding = v2_models.WorkspaceUserMessageBinding.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.binding_uuid),
            }
        )
        state = v2_models.WorkspaceUserMessageState.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.state_uuid),
            },
            locked=owned_projection.CURRENT.get() is None,
        )
        if self.action == "delete":
            if binding is not None:
                binding.delete()
            elif state is not None:
                state.delete()
            return
        permissions = {"read": True, "react": True, "star": True, "pin": True}
        if binding is None:
            binding = v2_models.WorkspaceUserMessageBinding(
                uuid=self.binding_uuid,
                project_id=self.project_id,
                placement_uuid=self.placement_uuid,
                user_uuid=self.user_uuid,
                membership_generation=self.membership_generation,
                relation_role=self.relation_role,
                visibility="visible",
                permissions=permissions,
            )
            binding.insert()
        else:
            binding.properties["membership_generation"].set_value_force(
                self.membership_generation
            )
            binding.relation_role = self.relation_role
            binding.visibility = "visible"
            binding.permissions = permissions
            if owned_projection.CURRENT.get() is None:
                binding.update()
            else:
                binding.validate()
                contexts.Context().get_session().execute(
                    """UPDATE messenger_user_message_bindings SET membership_generation=%s,
                       relation_role=%s,visibility='visible',permissions=%s::jsonb,updated_at=NOW()
                       WHERE project_id=%s AND uuid=%s""",
                    (
                        binding.membership_generation,
                        binding.relation_role,
                        json.dumps(binding.permissions),
                        binding.project_id,
                        binding.uuid,
                    ),
                )
        if state is None:
            v2_models.WorkspaceUserMessageState(
                uuid=self.state_uuid,
                project_id=self.project_id,
                placement_uuid=self.placement_uuid,
                user_uuid=self.user_uuid,
                binding_uuid=binding.uuid,
                membership_generation=self.membership_generation,
                zulip_source_uuid=self.source_uuid,
                zulip_flags_updated_at=self.flags_updated_at,
                read_at=self.flags_updated_at if self.read else None,
                starred=self.starred,
                mentioned=self.mentioned,
            ).insert()
        else:
            new_generation = state.membership_generation != self.membership_generation
            if new_generation:
                state.read_updated_at = None
                state.starred_updated_at = None
                state.pinned = False
            # Notification preferences can change without a new flags snapshot.
            state.mentioned = self.mentioned
            state.properties["binding_uuid"].set_value_force(binding.uuid)
            state.properties["membership_generation"].set_value_force(
                self.membership_generation
            )
            state.properties["zulip_source_uuid"].set_value_force(self.source_uuid)
            state.properties["zulip_flags_updated_at"].set_value_force(
                self.flags_updated_at
            )
            if owned_projection.CURRENT.get() is None:
                state.update()
            else:
                state.validate()
                contexts.Context().get_session().execute(
                    """UPDATE messenger_user_message_states SET mentioned=%s,binding_uuid=%s,
                       membership_generation=%s,zulip_source_uuid=%s,
                       zulip_flags_updated_at=%s::timestamptz AT TIME ZONE 'UTC',
                       read_updated_at=%s::timestamptz AT TIME ZONE 'UTC',
                       starred_updated_at=%s::timestamptz AT TIME ZONE 'UTC',pinned=%s,updated_at=NOW()
                       WHERE project_id=%s AND uuid=%s""",
                    (
                        state.mentioned,
                        state.binding_uuid,
                        state.membership_generation,
                        state.zulip_source_uuid,
                        state.zulip_flags_updated_at,
                        state.read_updated_at,
                        state.starred_updated_at,
                        state.pinned,
                        state.project_id,
                        state.uuid,
                    ),
                )


class ZulipChannel(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_channels"

    topic_notifications_synced = properties.property(types.Boolean(), default=False)

    notification_mode = properties.property(
        types.AllowNone(
            types.Enum(
                [
                    mode.value
                    for mode in messenger_models.WorkspaceStreamNotificationMode
                ]
            )
        ),
        default=None,
    )

    external_account_uuid = properties.property(types.UUID(), required=True)
    channel_type = properties.property(
        types.Enum([channel_type.value for channel_type in ZulipChannelType]),
        default=ZulipChannelType.CHANNEL.value,
    )
    direct_user_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    stream_uuid = properties.property(types.UUID(), required=True)
    stream_id = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
    )
    name = properties.property(
        types.String(min_length=1),
        required=True,
    )
    description = properties.property(
        types.String(max_length=common_models.DESCRIPTION_MAX_LENGTH),
        required=True,
    )
    rendered_description = properties.property(
        types.String(),
        required=True,
    )
    date_created = properties.property(
        types.AllowNone(types.Integer(min_value=0)),
        default=None,
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

    def validate(self) -> None:
        super().validate()
        if (self.channel_type == ZulipChannelType.CHANNEL.value) != (
            self.stream_id is not None
        ):
            raise exceptions.ValidationErrorException()
        if (self.channel_type == ZulipChannelType.PRIVATE.value) != (
            self.direct_user_uuid is not None
        ):
            raise exceptions.ValidationErrorException()


class ZulipTopicNotification(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "bridge_zulip_v5_topic_notifications"

    channel_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_name = properties.property(types.String(), required=True, read_only=True)
    visibility_policy = properties.property(
        types.Integer(min_value=0, max_value=3), required=True
    )
    last_updated = properties.property(types.Integer(min_value=0), required=True)

    @classmethod
    def import_snapshot(cls, channel: "ZulipChannel", values: list[dict]) -> None:
        previous = {
            row.topic_name: row
            for row in cls.objects.get_all(
                filters={"channel_uuid": filters.EQ(channel.uuid)}
            )
        }
        for data in values:
            name = data["topic_name"].lower()
            row = previous.pop(name, None)
            fields = {
                "visibility_policy": data["visibility_policy"],
                "last_updated": data["last_updated"],
            }
            if row is None:
                cls(channel_uuid=channel.uuid, topic_name=name, **fields).insert()
            else:
                row.update_dm(values=fields)
                row.update()
        for row in previous.values():
            row.delete()
        channel.topic_notifications_synced = True
        channel.update()


class ZulipTopic(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_topics"

    stream_id = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
    )
    name = properties.property(
        types.String(),
        required=True,
    )
    max_id = properties.property(
        types.Integer(min_value=1),
        required=True,
    )

    @classmethod
    def observe(cls, **values: typing.Any) -> "ZulipTopic":
        topic = cls(**values)
        contexts.Context().get_session().execute(
            """INSERT INTO bridge_zulip_v5_topics (uuid,stream_id,name,max_id,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (uuid) DO UPDATE SET name=EXCLUDED.name,
                   max_id=GREATEST(bridge_zulip_v5_topics.max_id,EXCLUDED.max_id),
                   updated_at=EXCLUDED.updated_at
               WHERE bridge_zulip_v5_topics.name IS DISTINCT FROM EXCLUDED.name
                  OR bridge_zulip_v5_topics.max_id<EXCLUDED.max_id""",
            (
                topic.uuid,
                topic.stream_id,
                topic.name,
                topic.max_id,
                messenger_helpers._database_timestamp(topic.created_at),
                messenger_helpers._database_timestamp(topic.updated_at),
            ),
        )
        return cls.objects.get_one(filters={"uuid": filters.EQ(topic.uuid)})


class ZulipMessage(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """A message with account-specific flags."""

    __tablename__ = "bridge_zulip_v5_messages"

    created_at = properties.property(
        common_types.UTCDateTimeWithOffset(),
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = properties.property(
        common_types.UTCDateTimeWithOffset(),
        read_only=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

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
    placement_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
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
    def on_done(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        if account.status != external_models.ExternalAccountStatus.ERROR.value:
            return
        self._restore_account_status(account)

    @staticmethod
    def _restore_account_status(account: external_models.ExternalAccount) -> None:
        status = (
            external_models.ExternalAccountStatus.ACTIVE.value
            if account.has_completed_backfill
            else external_models.ExternalAccountStatus.BACKFILL.value
        )
        subscription = RealtimeSubscription.for_account(account)
        if subscription is not None:
            event_uuid = sys_uuid.uuid5(subscription.generation, "backfill")
            unfinished = Task.objects.get_all(
                filters={
                    "backfill_uuid": filters.EQ(event_uuid),
                    "status": filters.NE(TaskStatus.DONE.value),
                },
                limit=1,
            )
            event = RealtimeEvent.objects.get_one(
                filters={"uuid": filters.EQ(event_uuid)}
            )
            if event.processed_at is None or unfinished:
                status = (
                    external_models.ExternalAccountStatus.FAST_BACKFILL.value
                    if event.payload["type"] == "actualize_backfill"
                    else external_models.ExternalAccountStatus.BACKFILL.value
                )
        account.update_status(status)

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(
            error, (storage_exceptions.DeadLock, psycopg.errors.DeadlockDetected)
        ):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = (
                datetime.datetime.now(datetime.timezone.utc) + TASK_RETRY_DELAY
            )
            return
        response = error.response if isinstance(error, requests.HTTPError) else None
        if isinstance(
            error,
            (
                requests.ConnectionError,
                requests.Timeout,
            ),
        ) or (
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

    previous_revision = properties.property(
        types.AllowNone(types.String()), default=None, read_only=True
    )

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
            locked=True,
        )
        zulip = client.ZulipClient(account)
        try:
            data = zulip.check_connection()
        finally:
            zulip.close()
        external_account_id = f"{account.get_host()}:{data['user_id']}"
        if (
            account.external_account_id is not None
            and account.external_account_id != external_account_id
        ):
            account.properties["has_completed_backfill"].set_value_force(False)
        existing = external_models.ExternalAccount.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(task.project_id),
                "provider": filters.EQ(account.provider),
                "external_account_id": filters.EQ(external_account_id),
            },
        )
        if existing is not None and existing.uuid != account.uuid:
            owner = messenger_models.WorkspaceUser.objects.get_one(
                filters={"uuid": filters.EQ(existing.owner_user_uuid)},
            )
            if (
                owner.source
                not in (
                    messenger_models.WorkspaceUserSource.ZULIP.value,
                    messenger_models.WorkspaceUserSource.HIDDEN.value,
                )
                or "api_key" in existing.settings
            ):
                raise ExternalAccountAlreadyConnectedError(
                    f"External account {external_account_id} is already connected "
                    "to another Workspace user."
                )
        namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, account.get_host())
        user_uuid = sys_uuid.uuid5(namespace, f"user:{data['user_id']}")
        user = messenger_models.WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": filters.EQ(user_uuid)},
        )
        if user is None:
            user = messenger_models.WorkspaceUser(
                uuid=user_uuid,
                username=f"zulip-{user_uuid}",
                source=messenger_models.WorkspaceUserSource.HIDDEN.value,
                status=messenger_models.WorkspaceUserStatus.OFFLINE.value,
                first_name=data["full_name"],
                email=data["email"],
                disabled=account.disabled,
            )
            user.insert()
        else:
            user.source = messenger_models.WorkspaceUserSource.HIDDEN.value
            user.update()
        if existing is None or existing.uuid == account.uuid:
            account.properties["external_account_id"].set_value_force(
                external_account_id
            )
            account.update()
            return
        try:
            task.external_account_uuid = existing.uuid
            task.update()
            account.delete()
            existing.properties["owner_user_uuid"].set_value_force(
                account.owner_user_uuid
            )
            existing.settings = account.settings
            existing.update()
            messenger_events.create_external_resource_event(
                account.project_id,
                account.owner_user_uuid,
                account,
                messenger_events.EXTERNAL_ACCOUNT_DELETED_EVENT,
                hidden_fields=("owner_user_uuid",),
            )
            messenger_events.create_external_resource_event(
                existing.project_id,
                existing.owner_user_uuid,
                existing,
                messenger_events.EXTERNAL_ACCOUNT_CREATED_EVENT,
                hidden_fields=("owner_user_uuid",),
            )
        except Exception:
            task.external_account_uuid = account.uuid
            raise

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, ExternalAccountAlreadyConnectedError):
            account = external_models.ExternalAccount.objects.get_one(
                filters={
                    "uuid": filters.EQ(task.external_account_uuid),
                    "project_id": filters.EQ(task.project_id),
                },
            )
            account.update_status(
                external_models.ExternalAccountStatus.ERROR.value,
                error_description=str(error),
            )
        else:
            super().on_error(task, error)

    def on_done(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        subscription = RealtimeSubscription.for_account(account)
        if (
            subscription is not None
            and subscription.account_revision == account.subscription_revision()
        ):
            if self.previous_revision is not None:
                contexts.Context().get_session().execute(
                    "UPDATE bridge_zulip_v5_tasks "
                    "SET content=jsonb_set(content,'{account_revision}',to_jsonb(%s::text)) "
                    "WHERE external_account_uuid=%s AND project_id=%s "
                    "AND status IN ('new','waiting','retry') AND content->>'account_revision'=%s",
                    (
                        account.realtime_revision(),
                        account.uuid,
                        account.project_id,
                        self.previous_revision,
                    ),
                )
            self._restore_account_status(account)
        else:
            account.update_status(external_models.ExternalAccountStatus.BACKFILL.value)


class FullBackfillContent(AbstractZulipTaskContent):
    KIND = "full_backfill"

    last_message_id = properties.property(
        types.Integer(min_value=0), required=True, read_only=True
    )
    since = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        first_message_id = 1
        last_message_id = self.last_message_id
        if self.since is None:
            last_message_id = max(
                last_message_id,
                history_snapshots.retained_upper_bound(account, task.created_at),
            )
        if self.since is not None:
            zulip = client.ZulipClient(account)
            try:
                first_since = zulip.get_first_message_id_since(self.since)
            finally:
                zulip.close()
            first_message_id = (
                self.last_message_id + 1 if first_since is None else first_since
            )
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=TASK_PRIORITY_IMPORT_USERS,
            backfill_uuid=task.backfill_uuid,
            content=ImportUsersContent(),
        ).insert()
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=(
                TASK_PRIORITY_IMPORT_USERS
                if task.backfill_uuid is not None
                else TASK_PRIORITY_IMPORT_CHANNELS
            ),
            backfill_uuid=task.backfill_uuid,
            content=ImportChannelsContent(),
        ).insert()
        if first_message_id <= last_message_id:
            Task(
                project_id=task.project_id,
                external_account_uuid=task.external_account_uuid,
                priority=TASK_PRIORITY_IMPORT_HISTORY,
                backfill_uuid=task.backfill_uuid,
                content=ScanHistoryContent(
                    first_message_id=first_message_id,
                    last_message_id=last_message_id,
                    since=self.since,
                ),
            ).insert()


class ActualizeBackfillContent(FullBackfillContent):
    KIND = "actualize_backfill"

    since = properties.property(types.UTCDateTimeZ(), required=True, read_only=True)

    def execute(self, task: "Task") -> None:
        # Queue loss can hide old edits and reactions as well as old flags.
        # Scan the complete visible history, including native deliveries.
        FullBackfillContent(last_message_id=self.last_message_id).execute(task)


class ImportUsersContent(AbstractZulipTaskContent):
    KIND = "import_users"

    periodic = properties.property(types.Boolean(), default=False)
    event = properties.property(types.AllowNone(types.Dict()), default=None)

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        hostname = account.get_host()
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        accounts_by_id = {}
        person = self.event["person"] if self.event is not None else None
        for existing in external_models.ExternalAccount.objects.get_all(
            filters={
                "project_id": filters.EQ(task.project_id),
                "provider": filters.EQ("zulip"),
                **(
                    {
                        "external_account_id": filters.EQ(
                            f"{hostname}:{person['user_id']}"
                        )
                    }
                    if person is not None
                    else {}
                ),
            },
        ):
            if existing.external_account_id is None:
                raise ExternalAccountIdPendingError(
                    f"External account {existing.uuid} has no external_account_id; "
                    "waiting for account verification."
                )
            accounts_by_id[existing.external_account_id] = existing
        zulip = client.ZulipClient(account)
        try:
            if person is None or self.event["op"] == "remove":
                users = zulip.get_users()
                if person is not None:
                    users = [
                        data for data in users if data["user_id"] == person["user_id"]
                    ]
            else:
                stored = messenger_models.WorkspaceUser.objects.get_one_or_none(
                    filters={
                        "uuid": filters.EQ(
                            sys_uuid.uuid5(
                                server_namespace, f"user:{person['user_id']}"
                            )
                        )
                    }
                )
                if stored is not None:
                    data = {
                        "user_id": person["user_id"],
                        "full_name": stored.first_name,
                        "email": stored.email,
                        "is_active": not stored.disabled,
                        **person,
                    }
                elif (
                    "full_name" in person
                    and "email" in person
                    and "is_active" in person
                ):
                    data = person
                else:
                    data = {**zulip.get_user(person["user_id"]), **person}
                if "new_email" in person:
                    data = {**data, "email": person["new_email"]}
                users = [data]
            for data in users:
                user_id = data["user_id"]
                disabled = not data["is_active"]
                external_account_id = f"{hostname}:{user_id}"
                user_uuid = sys_uuid.uuid5(server_namespace, f"user:{user_id}")
                external_account = accounts_by_id.get(external_account_id)
                user = messenger_models.WorkspaceUser.objects.get_one_or_none(
                    filters={"uuid": filters.EQ(user_uuid)},
                )
                if user is None:
                    source = (
                        messenger_models.WorkspaceUserSource.HIDDEN.value
                        if external_account is not None
                        and external_account.owner_user_uuid != user_uuid
                        else messenger_models.WorkspaceUserSource.ZULIP.value
                    )
                    user = messenger_models.WorkspaceUser(
                        uuid=user_uuid,
                        username=f"zulip-{user_uuid}",
                        source=source,
                        disabled=disabled,
                        status=messenger_models.WorkspaceUserStatus.OFFLINE.value,
                        first_name=data["full_name"],
                        email=data["email"],
                    )
                    user.insert()
                if external_account is None:
                    external_account = external_models.ExternalAccount(
                        uuid=sys_uuid.uuid5(task.project_id, f"zulip:{user_uuid}"),
                        project_id=task.project_id,
                        owner_user_uuid=user.uuid,
                        provider="zulip",
                        external_account_id=external_account_id,
                        disabled=disabled,
                        settings={
                            "server_url": account.settings["server_url"],
                            "email": data["email"],
                        },
                    )
                    external_account.insert()
                settings = dict(external_account.settings)
                if "api_key" not in settings:
                    settings["email"] = data["email"]
                external_account.settings = settings
                external_account.disabled = disabled
                external_account.update()
                accounts_by_id[external_account_id] = external_account
                user.first_name = data["full_name"]
                user.email = data["email"]
                if user.source != messenger_models.WorkspaceUserSource.HIDDEN.value:
                    user.source = messenger_models.WorkspaceUserSource.ZULIP.value
                user.disabled = disabled
                user.update()
        finally:
            zulip.close()

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, ExternalAccountIdPendingError):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = datetime.datetime.now(datetime.timezone.utc)
        else:
            super().on_error(task, error)


class ImportChannelsContent(AbstractZulipTaskContent):
    KIND = "import_channels"

    periodic = properties.property(types.Boolean(), default=False)
    event = properties.property(types.AllowNone(types.Dict()), default=None)

    def apply_event(self, account: external_models.ExternalAccount) -> bool:
        if self.event is None:
            return False
        if self.event["type"] == "user_topic":
            channel = ZulipChannel.objects.get_one_or_none(
                filters={
                    "external_account_uuid": filters.EQ(account.uuid),
                    "stream_id": filters.EQ(self.event["stream_id"]),
                }
            )
            if channel is None:
                return False
            name = self.event["topic_name"].lower()
            row = ZulipTopicNotification.objects.get_one_or_none(
                filters={
                    "channel_uuid": filters.EQ(channel.uuid),
                    "topic_name": filters.EQ(name),
                }
            )
            if row is not None and row.last_updated >= self.event["last_updated"]:
                return True
            if self.event["visibility_policy"] == 0:
                if row is not None:
                    row.delete()
            else:
                values = {
                    key: self.event[key]
                    for key in ("visibility_policy", "last_updated")
                }
                if row is None:
                    ZulipTopicNotification(
                        channel_uuid=channel.uuid, topic_name=name, **values
                    ).insert()
                else:
                    row.update_dm(values=values)
                    row.update()
            return True
        if self.event["op"] != "update":
            return False
        changes = self.event.get("subscription_data", [self.event])
        for change in changes:
            channel = ZulipChannel.objects.get_one_or_none(
                filters={
                    "external_account_uuid": filters.EQ(account.uuid),
                    "stream_id": filters.EQ(change["stream_id"]),
                }
            )
            if channel is None:
                return False
            name = change["property"]
            if name == "is_muted":
                values = {
                    "notification_mode": (
                        "muted"
                        if channel.notification_mode == "muted"
                        else "mentions_only"
                    )
                    if change["value"]
                    else "all_messages"
                }
            elif name in (
                "name",
                "description",
                "rendered_description",
                "invite_only",
                "is_web_public",
                "history_public_to_subscribers",
                "is_archived",
                "message_retention_days",
            ):
                values = {name: change["value"]}
                if name == "description":
                    if "rendered_description" not in self.event:
                        return False
                    values["rendered_description"] = self.event["rendered_description"]
            else:
                continue
            channel.update_dm(values=values)
            channel.update()
        return True

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        if self.apply_event(account):
            return
        hostname = account.get_host()
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            topic_notifications: dict[int, list[dict[str, typing.Any]]] = {}
            for data in zulip.get_topic_notifications():
                topic_notifications.setdefault(data["stream_id"], []).append(data)
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
                        "notification_mode",
                    )
                }
                values["stream_uuid"] = sys_uuid.uuid5(
                    server_namespace, str(values["stream_id"])
                )
                values["channel_type"] = ZulipChannelType.CHANNEL.value
                channel = ZulipChannel.objects.get_one_or_none(
                    filters={
                        "external_account_uuid": filters.EQ(account.uuid),
                        "channel_type": filters.EQ(ZulipChannelType.CHANNEL.value),
                        "stream_id": filters.EQ(values["stream_id"]),
                    },
                )
                if channel is None:
                    channel = ZulipChannel(
                        external_account_uuid=account.uuid,
                        **values,
                    )
                    channel.insert()
                else:
                    if (
                        values["notification_mode"] == "mentions_only"
                        and channel.notification_mode == "muted"
                    ):
                        values["notification_mode"] = "muted"
                    channel.update_dm(values=values)
                    channel.update()
                ZulipTopicNotification.import_snapshot(
                    channel, topic_notifications.get(values["stream_id"], [])
                )
                stream_ids.add(values["stream_id"])
            for channel in ZulipChannel.objects.get_all(
                filters={
                    "external_account_uuid": filters.EQ(account.uuid),
                    "channel_type": filters.EQ(ZulipChannelType.CHANNEL.value),
                },
            ):
                if channel.stream_id not in stream_ids:
                    channel.delete()
        finally:
            zulip.close()


class HistorySnapshotChangedError(Exception):
    pass


class ScanHistoryContent(AbstractZulipTaskContent):
    KIND = "scan_history"

    first_message_id = properties.property(types.Integer(min_value=1), default=1)
    last_message_id = properties.property(types.Integer(min_value=1), required=True)
    since = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)

    def fetch(self, zulip: client.ZulipClient) -> dict[str, typing.Any]:
        first_id = history_fingerprints.range_start(self.last_message_id)
        page = zulip.get_history_batch(first_id, self.last_message_id)
        # Production workers prepare HTTP and hashes outside account/task locks.
        page["fingerprints"] = {
            data["id"]: history_fingerprints.message_hashes(data)
            for data in page["messages"]
        }
        page["hashes"] = history_fingerprints.batch_hashes_from_entries(
            sorted(page["fingerprints"].items()), first_id, self.last_message_id
        )
        return page

    def execute(self, task: "Task") -> None:
        if getattr(self, "_prepared_error", None) is not None:
            raise self._prepared_error
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        if account.external_account_id is None:
            raise ExternalAccountIdPendingError(
                "Waiting for the importing account's Zulip identity"
            )
        prepared = getattr(self, "_prepared_import", None)
        if prepared is not None:
            if account.realtime_revision() != prepared[0]:
                raise SubscriptionChangedError()
            observed_at, page = prepared[1:]
        else:
            observed_at = datetime.datetime.now(datetime.timezone.utc)
            zulip = client.ZulipClient(account)
            try:
                page = self.fetch(zulip)
            finally:
                zulip.close()
        first_id = history_fingerprints.range_start(self.last_message_id)
        # Incomplete source history is never a successful recovery checkpoint.
        if page["history_limited"]:
            raise client.ZulipApiError(
                "HISTORY_LIMITED", "Zulip history is limited for this account"
            )
        messages = page["messages"]
        compared = history_snapshots.compare(
            account,
            first_id,
            self.last_message_id,
            messages,
            observed_at,
            fingerprints=page["fingerprints"],
            hashes=page["hashes"],
        )
        if compared is None:
            raise HistorySnapshotChangedError()
        changed, batch_revision = compared
        children = []
        for index in range(0, len(changed), HISTORY_BATCH_SIZE):
            snapshot = changed[index : index + HISTORY_BATCH_SIZE]
            child = Task(
                uuid=sys_uuid.uuid5(task.uuid, f"delta:{index}"),
                project_id=task.project_id,
                external_account_uuid=account.uuid,
                priority=task.priority + 1,
                backfill_uuid=task.backfill_uuid,
                realtime_event_uuid=task.realtime_event_uuid,
                queue=task.queue,
                content=ImportHistoryContent(
                    first_message_id=min(item["message"]["id"] for item in snapshot),
                    last_message_id=max(item["message"]["id"] for item in snapshot),
                    snapshot=snapshot,
                    since=self.since,
                    snapshot_at=observed_at,
                    account_revision=account.realtime_revision(),
                    batch_first_id=first_id,
                    batch_revision=batch_revision,
                ),
            )
            child.insert()
            children.append(child.uuid)
        if children:
            contexts.Context().get_session().execute(
                "UPDATE bridge_zulip_v5_history_batches SET pending=%s::uuid[] WHERE external_account_uuid=%s AND first_id=%s",
                (children, account.uuid, first_id),
            )
        next_id = min(
            first_id - 1,
            max(page["next_id"], history_snapshots.next_known_id(account, first_id)),
        )
        if next_id >= self.first_message_id:
            Task(
                uuid=sys_uuid.uuid5(task.uuid, "next"),
                project_id=task.project_id,
                external_account_uuid=account.uuid,
                priority=task.priority,
                backfill_uuid=task.backfill_uuid,
                realtime_event_uuid=task.realtime_event_uuid,
                queue=task.queue,
                content=ScanHistoryContent(
                    first_message_id=self.first_message_id,
                    last_message_id=next_id,
                    since=self.since,
                ),
            ).insert()
        LOG.info(
            "Zulip history batch account=%s first=%s last=%s observed=%s changed=%s processing_tasks=%s",
            account.uuid,
            first_id,
            self.last_message_id,
            len(messages),
            len(changed),
            len(children),
        )

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, HistorySnapshotChangedError):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = (
                datetime.datetime.now(datetime.timezone.utc)
                + REALTIME_MESSAGE_RETRY_DELAY
            )
        elif isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        else:
            super().on_error(task, error)


class ImportHistoryContent(AbstractZulipTaskContent):
    KIND = "import_history"

    since = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)

    first_message_id = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    last_message_id = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    snapshot = properties.property(
        types.AllowNone(types.TypedList(types.Dict())), default=None
    )
    snapshot_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    account_revision = properties.property(
        types.AllowNone(types.String()), default=None
    )
    batch_first_id = properties.property(
        types.AllowNone(types.Integer(min_value=1)), default=None
    )
    batch_revision = properties.property(
        types.AllowNone(types.Integer(min_value=1)), default=None
    )

    def fetch(self, zulip: client.ZulipClient) -> list[dict[str, typing.Any]]:
        if self.snapshot is not None:
            return [item["message"] for item in self.snapshot]
        return zulip.get_messages(self.first_message_id, self.last_message_id)

    def validate(self) -> None:
        count = self.last_message_id - self.first_message_id + 1
        if not 1 <= count <= MAX_HISTORY_MESSAGES:
            raise exceptions.ValidationErrorException()

    def execute(self, task: "Task") -> None:
        if getattr(self, "_prepared_error", None) is not None:
            raise self._prepared_error
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        if account.external_account_id is None:
            raise ExternalAccountIdPendingError(
                "Waiting for the importing account's Zulip identity"
            )
        prepared = getattr(self, "_prepared_import", None)
        if prepared is not None and account.realtime_revision() != prepared[0]:
            raise SubscriptionChangedError()
        if self.snapshot is not None:
            current = typing.cast(
                dict[str, typing.Any],
                history_snapshots.batch(account, self.batch_first_id),
            )
            if current["revision"] != self.batch_revision or current[
                "source_revision"
            ] != history_snapshots.revision(account):
                session = contexts.Context().get_session()
                remaining = session.execute(
                    "UPDATE bridge_zulip_v5_history_batches SET pending=array_remove(pending,%s) "
                    "WHERE external_account_uuid=%s AND first_id=%s AND %s=ANY(pending) RETURNING pending,covered_to",
                    (task.uuid, account.uuid, self.batch_first_id, task.uuid),
                ).fetchone()
                if remaining is not None and not remaining["pending"]:
                    Task(
                        project_id=task.project_id,
                        external_account_uuid=account.uuid,
                        backfill_uuid=task.backfill_uuid,
                        realtime_event_uuid=task.realtime_event_uuid,
                        priority=task.priority,
                        content=ScanHistoryContent(
                            first_message_id=self.batch_first_id,
                            last_message_id=remaining["covered_to"],
                            since=self.since,
                        ),
                    ).insert()
                return
            if account.realtime_revision() != self.account_revision:
                raise SubscriptionChangedError()
            self._accepted: dict[int, dict[str, typing.Any]] = {}
            self._complete_snapshot = True
            with history_snapshots.applying(account, self.batch_first_id):
                self._execute_import(task, account, prepared)
        else:
            self._execute_import(task, account, prepared)

    def _execute_import(
        self,
        task: "Task",
        account: external_models.ExternalAccount,
        prepared: tuple[str, datetime.datetime, list[dict[str, typing.Any]]] | None,
    ) -> None:
        hostname = typing.cast(str, account.get_host())
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            observed_at = (
                self.snapshot_at
                if self.snapshot is not None
                else prepared[1]
                if prepared is not None
                else datetime.datetime.now(datetime.timezone.utc)
            )
            messages = sorted(
                prepared[2] if prepared is not None else self.fetch(zulip),
                key=lambda data: data["id"],
                reverse=True,
            )
            self._missing_ids = []
            if self.snapshot is not None:
                self._missing_ids = [
                    item["message"]["id"]
                    for item in self.snapshot
                    if item["components"] == ["visibility"]
                ]
                if self._missing_ids:
                    for observed in ZulipMessage.objects.get_all(
                        filters={
                            "external_account_uuid": filters.EQ(account.uuid),
                            "project_id": filters.EQ(account.project_id),
                            "message_id": filters.In(self._missing_ids),
                        }
                    ):
                        observed.delete()
                    messages = [
                        data for data in messages if data["id"] not in self._missing_ids
                    ]
            components = (
                {item["message"]["id"]: item["components"] for item in self.snapshot}
                if self.snapshot is not None
                else {data["id"]: history_fingerprints.COMPONENTS for data in messages}
            )
            observed_flags = message_flags.observe_snapshot(
                account,
                [data for data in messages if "flags" in components[data["id"]]],
                observed_at,
            )
            message_reactions.observe_snapshot(
                account,
                [data for data in messages if "reactions" in components[data["id"]]],
                observed_at,
            )
            deliveries = (
                {
                    delivery.zulip_message_id: delivery
                    for delivery in MessageDelivery.objects.get_all(
                        filters={
                            "project_id": filters.EQ(task.project_id),
                            "hostname": filters.EQ(account.get_host()),
                            "zulip_message_id": filters.In(
                                [data["id"] for data in messages]
                            ),
                        }
                    )
                }
                if messages
                else {}
            )
            pending_messages = (
                MessageDelivery.pending_import_ids(account, messages)
                if messages
                else set()
            )
            imported_contents = []
            deferred_messages = []
            topics = {}
            for data in messages:
                changed = components[data["id"]]
                if self.snapshot is not None:
                    self._accepted[data["id"]] = data
                    if "flags" in changed and any(
                        value != (flag in data["flags"])
                        for flag, value in observed_flags[data["id"]].items()
                    ):
                        self._accepted.pop(data["id"], None)
                        self._complete_snapshot = False
                if not {"text", "metadata"}.intersection(changed):
                    if "flags" in changed:
                        contexts.Context().get_session().execute(
                            "UPDATE bridge_zulip_v5_messages SET "
                            + ",".join(
                                f"{flag}=%s" for flag in history_fingerprints.FLAGS
                            )
                            + " WHERE external_account_uuid=%s AND project_id=%s AND message_id=%s",
                            tuple(
                                observed_flags[data["id"]].get(
                                    flag, flag in data["flags"]
                                )
                                for flag in history_fingerprints.FLAGS
                            )
                            + (account.uuid, account.project_id, data["id"]),
                        )
                    continue
                if (
                    self.since is not None
                    and data["timestamp"] < self.since.timestamp()
                ):
                    if self.snapshot is not None:
                        self._accepted.pop(data["id"], None)
                        self._complete_snapshot = False
                    continue
                delivery = deliveries.get(data["id"])
                if delivery is not None:
                    if (
                        self.snapshot is not None
                        and delivery.content_observed_at is not None
                        and delivery.content_observed_at.timestamp()
                        > data.get("last_edit_timestamp", data["timestamp"])
                    ):
                        self._accepted.pop(data["id"], None)
                        self._complete_snapshot = False
                    local = v2_models.WorkspaceMessage.objects.get_one(
                        filters={
                            "project_id": filters.EQ(task.project_id),
                            "uuid": filters.EQ(delivery.message_uuid),
                        }
                    )
                    delivery.observe(
                        data,
                        observer=account,
                        zulip=zulip,
                        convert_text="text" in changed,
                    )
                    if local.source_name == "native":
                        continue
                elif data["id"] in pending_messages:
                    if self.snapshot is not None:
                        self._accepted.pop(data["id"], None)
                        self._complete_snapshot = False
                    if self.first_message_id == self.last_message_id:
                        raise DeliveryPendingError(
                            "Waiting for the outgoing message identity before import"
                        )
                    deferred_messages.append(data["id"])
                    continue
                if data["type"] == "stream":
                    channel = ZulipChannel.objects.get_one_or_none(
                        filters={
                            "external_account_uuid": filters.EQ(account.uuid),
                            "channel_type": filters.EQ(ZulipChannelType.CHANNEL.value),
                            "stream_id": filters.EQ(data["stream_id"]),
                        },
                    )
                    if channel is None:
                        # Catalog removal owns withdrawal. Cache the handled
                        # observation; a subscription change invalidates it.
                        continue
                else:
                    if account.external_account_id is None:
                        raise ExternalAccountIdPendingError(
                            f"External account {account.uuid} is not verified yet."
                        )
                    user_id = int(account.external_account_id.rsplit(":", 1)[1])
                    recipients = sorted(
                        data["display_recipient"], key=lambda recipient: recipient["id"]
                    )
                    peers = [user for user in recipients if user["id"] != user_id]
                    channel_type = (
                        ZulipChannelType.PRIVATE.value
                        if len(peers) <= 1
                        else ZulipChannelType.GROUP.value
                    )
                    direct_user_uuid = None
                    if channel_type == ZulipChannelType.PRIVATE.value:
                        peer_id = (peers or recipients)[0]["id"]
                        peer = external_models.ExternalAccount.objects.get_one_or_none(
                            filters={
                                "project_id": filters.EQ(task.project_id),
                                "provider": filters.EQ("zulip"),
                                "external_account_id": filters.EQ(
                                    f"{hostname}:{peer_id}"
                                ),
                            },
                        )
                        if peer is None:
                            raise ExternalAccountIdPendingError(
                                f"External account {hostname}:{peer_id} "
                                "has not been imported yet."
                            )
                        direct_user_uuid = peer.uuid
                    members = ",".join(str(user["id"]) for user in recipients)
                    stream_uuid = sys_uuid.uuid5(server_namespace, f"dm:{members}")
                    values = {
                        "channel_type": channel_type,
                        "direct_user_uuid": direct_user_uuid,
                        "stream_id": None,
                        "name": ", ".join(
                            user["full_name"] for user in peers or recipients
                        ),
                        "description": "",
                        "rendered_description": "",
                        "date_created": None,
                        "invite_only": True,
                        "is_web_public": False,
                        "history_public_to_subscribers": False,
                        "is_archived": False,
                    }
                    channel = ZulipChannel.objects.get_one_or_none(
                        filters={
                            "external_account_uuid": filters.EQ(account.uuid),
                            "stream_uuid": filters.EQ(stream_uuid),
                        },
                    )
                    if channel is None:
                        channel = ZulipChannel(
                            external_account_uuid=account.uuid,
                            stream_uuid=stream_uuid,
                            **values,
                        )
                        channel.insert()
                    else:
                        channel.update_dm(values=values)
                        channel.update()
                topic_uuid = sys_uuid.uuid5(channel.stream_uuid, data["subject"])
                topic_name = (
                    data["subject"] or "General Topic"
                    if channel.channel_type == ZulipChannelType.CHANNEL.value
                    else "Zulip"
                )
                if topic_uuid not in topics:
                    topics[topic_uuid] = ZulipTopic.observe(
                        uuid=topic_uuid,
                        stream_id=channel.stream_id,
                        name=topic_name,
                        max_id=data["id"],
                    )
                topic = topics[topic_uuid]
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
                if (
                    self.snapshot is not None
                    and message is not None
                    and content_updated_at < message.content.zulip_content_updated_at
                ):
                    self._accepted.pop(data["id"], None)
                    self._complete_snapshot = False
                content_values = {
                    "source_timestamp": data["timestamp"],
                    "source_message_id": data["id"],
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
                elif "text" in changed:
                    content = message.content
                    source_changed = content_hash != content.zulip_content_hash
                    if content_updated_at < content.zulip_content_updated_at:
                        for field in (
                            "zulip_content",
                            "zulip_content_hash",
                            "zulip_content_updated_at",
                        ):
                            del content_values[field]
                    if content_updated_at <= content.workspace_content_updated_at:
                        for field in (
                            "workspace_content",
                            "workspace_content_hash",
                            "workspace_content_updated_at",
                        ):
                            del content_values[field]
                    content.update_dm(values=content_values)
                    if source_changed and "zulip_content" in content_values:
                        content.conversion_version = 0
                    content.update()
                else:
                    content = message.content
                    if content_updated_at > content.zulip_content_updated_at:
                        content.zulip_content_updated_at = content_updated_at
                        if content.workspace_content_updated_at < content_updated_at:
                            content.workspace_content_updated_at = content_updated_at
                        content.update()
                if "text" in changed:
                    imported_contents.append((content, data, channel.stream_uuid))
                values = {
                    "message_uuid": sys_uuid.uuid5(server_namespace, str(data["id"])),
                    "message_id": data["id"],
                    "sender_id": data["sender_id"],
                    "timestamp": data["timestamp"],
                    "placement_updated_at": content_updated_at,
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
                    **observed_flags.get(data["id"], {}),
                }
                if message is None:
                    ZulipMessage(
                        project_id=task.project_id,
                        external_account_uuid=account.uuid,
                        **values,
                    ).insert()
                else:
                    if "flags" not in changed:
                        for flag in history_fingerprints.FLAGS:
                            del values[flag]
                    if content_updated_at < message.placement_updated_at or (
                        self.snapshot is None
                        and content_updated_at == message.placement_updated_at
                    ):
                        for field in ("channel", "topic", "placement_updated_at"):
                            del values[field]
                    message.update_dm(values=values)
                    message.update()
            if imported_contents:
                file_streams = {
                    stream_uuid
                    for content, data, stream_uuid in imported_contents
                    if message_conversion.get_zulip_files(
                        {**data, "content": content.zulip_content},
                        {},
                        include_quotes=True,
                    )
                }
                if file_streams:
                    for state in ZulipStreamState.objects.get_all(
                        filters={
                            "project_id": filters.EQ(task.project_id),
                            "stream_uuid": filters.In(list(file_streams)),
                            "action": filters.EQ("create"),
                        }
                    ):
                        state.do()
                converter = content_conversion.ConversionContext(account)
                converter.prefetch(
                    [
                        {**data, "content": content.zulip_content}
                        for content, data, _stream in imported_contents
                    ]
                )
                for content, data, stream_uuid in imported_contents:
                    content.convert(converter, data, stream_uuid, zulip)
                if task.queue == "realtime":
                    Task(
                        project_id=task.project_id,
                        external_account_uuid=account.uuid,
                        realtime_event_uuid=task.realtime_event_uuid,
                        priority=TASK_PRIORITY_REALTIME_MESSAGE,
                        content=ReconcileRealtimeContent(
                            message_ids=[
                                data["id"]
                                for _content, data, _stream in imported_contents
                            ]
                        ),
                    ).insert()
            for message_id in deferred_messages:
                Task(
                    uuid=sys_uuid.uuid5(task.uuid, f"pending-delivery:{message_id}"),
                    project_id=task.project_id,
                    external_account_uuid=task.external_account_uuid,
                    backfill_uuid=task.backfill_uuid,
                    realtime_event_uuid=task.realtime_event_uuid,
                    queue=task.queue,
                    priority=(
                        min(task.priority, TASK_PRIORITY_IMPORT_HISTORY - 1)
                        if task.queue == "background"
                        else task.priority
                    ),
                    not_before_at=datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=30),
                    content=ImportHistoryContent(
                        first_message_id=message_id,
                        last_message_id=message_id,
                        since=self.since,
                    ),
                ).insert()
        finally:
            zulip.close()

    def on_done(self, task: "Task") -> None:
        if self.snapshot is not None and hasattr(self, "_accepted"):
            account = external_models.ExternalAccount.objects.get_one(
                filters={
                    "uuid": filters.EQ(task.external_account_uuid),
                    "project_id": filters.EQ(task.project_id),
                }
            )
            history_snapshots.complete(
                account,
                self.batch_first_id,
                self.batch_revision,
                task.uuid,
                list(self._accepted.values()),
                self._complete_snapshot,
                self._missing_ids,
            )
        super().on_done(task)

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        elif isinstance(
            error, (DeliveryPendingError, content_conversion.ConversionPendingError)
        ):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(seconds=3)
        elif isinstance(error, ExternalAccountIdPendingError):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = (
                datetime.datetime.now(datetime.timezone.utc) + TASK_RETRY_DELAY
            )
        else:
            super().on_error(task, error)


class DeliveryPendingError(Exception):
    pass


class UpdateNotificationsContent(AbstractZulipTaskContent):
    KIND = "update_notifications"

    channel_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_name = properties.property(
        types.AllowNone(types.String()), default=None, read_only=True
    )
    notification_mode = properties.property(
        types.Enum(
            [mode.value for mode in messenger_models.WorkspaceTopicNotificationMode]
            + [mode.value for mode in messenger_models.WorkspaceStreamNotificationMode]
        ),
        required=True,
        read_only=True,
    )

    account_revision = properties.property(
        types.String(), required=True, read_only=True
    )

    def remember(self, channel: "ZulipChannel") -> None:
        # Reconciliation reads this same cache. Remember the local choice in
        # the API transaction so the unchanged cache does not immediately undo it.
        if self.topic_name is None:
            channel.notification_mode = self.notification_mode
            channel.update()
            return
        name = self.topic_name.lower()
        row = ZulipTopicNotification.objects.get_one_or_none(
            filters={
                "channel_uuid": filters.EQ(channel.uuid),
                "topic_name": filters.EQ(name),
            }
        )
        policy = {"default": 0, "mute": 1, "unmute": 2, "follow": 3}[
            self.notification_mode
        ]
        if policy == 0:
            if row is not None:
                row.delete()
        elif row is None:
            ZulipTopicNotification(
                channel_uuid=channel.uuid,
                topic_name=name,
                visibility_policy=policy,
                last_updated=0,
            ).insert()
        else:
            row.visibility_policy = policy
            row.last_updated = 0
            row.update()

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        if (
            not account.can_stream()
            or account.realtime_revision() != self.account_revision
        ):
            raise SubscriptionChangedError()
        channel = ZulipChannel.objects.get_one_or_none(
            filters={
                "uuid": filters.EQ(self.channel_uuid),
                "external_account_uuid": filters.EQ(account.uuid),
                "channel_type": filters.EQ("channel"),
            }
        )
        if channel is None:
            return
        zulip = client.ZulipClient(account)
        try:
            if self.topic_name is None:
                zulip.update_stream_notifications(
                    channel.stream_id, self.notification_mode != "all_messages"
                )
            else:
                zulip.update_topic_notifications(
                    channel.stream_id,
                    self.topic_name,
                    {"default": 0, "mute": 1, "unmute": 2, "follow": 3}[
                        self.notification_mode
                    ],
                )
        finally:
            zulip.close()
        self.remember(channel)

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        else:
            super().on_error(task, error)


class UpdateMessageFlagsContent(AbstractZulipTaskContent):
    KIND = "update_message_flags"

    state_uuid = properties.property(types.UUID(), required=True, read_only=True)
    flag = properties.property(
        types.Enum(message_flags.FLAGS), required=True, read_only=True
    )
    value = properties.property(types.Boolean(), required=True, read_only=True)
    changed_at = properties.property(
        types.UTCDateTimeZ(), required=True, read_only=True
    )
    account_revision = properties.property(
        types.String(), required=True, read_only=True
    )

    def execute(self, task: "Task") -> None:
        if not mutation_journal_opts.enabled():
            message_flags.lock_state(
                task.project_id, self.state_uuid, task.external_account_uuid
            )
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        if (
            not account.can_stream()
            or account.realtime_revision() != self.account_revision
        ):
            raise SubscriptionChangedError()
        targets = message_flags.flag_states(
            task.project_id, self.state_uuid, account.uuid, self.flag
        )
        if not targets:
            return
        target = targets[0]
        changed_at = self.changed_at.replace(tzinfo=None)
        if (
            target["local_updated_at"] != changed_at
            or target["local_value"] != self.value
            or (
                target["remote_updated_at"] is not None
                and target["remote_updated_at"] >= changed_at
            )
        ):
            return
        batch = message_flags.outgoing_batch(
            task.project_id, account.uuid, self.state_uuid, self.flag, self.value
        )
        if not batch:
            return
        message_ids = sorted({row["message_id"] for row in batch}, reverse=True)
        zulip = client.ZulipClient(account)
        try:
            result = zulip.update_message_flags(message_ids, self.flag, self.value)
            if result.get("ignored_because_not_subscribed_channels"):
                raise client.ZulipApiError(
                    "NOT_SUBSCRIBED",
                    "Cannot mark messages in unsubscribed Zulip channels as unread",
                )
            by_time: dict[datetime.datetime, set[int]] = {}
            for row in batch:
                by_time.setdefault(row["local_updated_at"], set()).add(
                    row["message_id"]
                )
            for observed_at, ids in sorted(by_time.items()):
                message_flags.record(
                    account,
                    sorted(ids),
                    self.flag,
                    self.value,
                    observed_at.replace(tzinfo=datetime.timezone.utc),
                )
        finally:
            zulip.close()

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        else:
            super().on_error(task, error)


class UpdateReactionContent(AbstractZulipTaskContent):
    KIND = "update_reaction"

    intent_uuid = properties.property(types.UUID(), required=True, read_only=True)
    reaction_message_uuid = properties.property(
        types.UUID(), required=True, read_only=True
    )
    changed_at = properties.property(
        types.UTCDateTimeZ(), required=True, read_only=True
    )
    present = properties.property(types.Boolean(), required=True, read_only=True)
    account_revision = properties.property(
        types.String(), required=True, read_only=True
    )

    def execute(self, task: "Task") -> None:
        message_reactions.send(self, task)

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        else:
            super().on_error(task, error)


class RefreshMessageFlagsContent(AbstractZulipTaskContent):
    """Refresh flags and reactions on known messages, including old history."""

    KIND = "refresh_message_flags"

    after_message_id = properties.property(types.Integer(min_value=0), default=0)
    last_message_id = properties.property(types.Integer(min_value=0), required=True)

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "project_id": filters.EQ(task.project_id),
                "uuid": filters.EQ(task.external_account_uuid),
            }
        )
        if not account.can_stream():
            raise SubscriptionChangedError()
        rows = (
            contexts.Context()
            .get_session()
            .execute(
                """SELECT message_id FROM (
                   (SELECT DISTINCT message_id FROM bridge_zulip_v5_message_flags
                    WHERE project_id=%s AND external_account_uuid=%s
                      AND message_id > %s AND message_id <= %s
                    ORDER BY message_id DESC LIMIT 100)
                   UNION
                   (SELECT delivery.zulip_message_id AS message_id
                    FROM bridge_zulip_v5_deliveries AS delivery
                    JOIN bridge_zulip_v5_channels AS channel
                      ON channel.external_account_uuid=%s AND channel.stream_uuid=delivery.stream_uuid
                    WHERE delivery.project_id=%s AND delivery.hostname=%s
                      AND delivery.zulip_message_id > %s AND delivery.zulip_message_id <= %s
                    ORDER BY delivery.zulip_message_id DESC LIMIT 100)
               ) AS known ORDER BY message_id DESC LIMIT 100""",
                (
                    task.project_id,
                    account.uuid,
                    self.after_message_id,
                    self.last_message_id,
                    account.uuid,
                    task.project_id,
                    account.get_host(),
                    self.after_message_id,
                    self.last_message_id,
                ),
            )
            .fetchall()
        )
        if not rows:
            return
        last = rows[0]["message_id"]
        first = max(rows[-1]["message_id"], last - HISTORY_BATCH_SIZE + 1)
        wanted = {row["message_id"] for row in rows if row["message_id"] >= first}
        zulip = client.ZulipClient(account)
        try:
            observed_at = datetime.datetime.now(datetime.timezone.utc)
            data = sorted(
                zulip.get_messages(first, last), key=lambda row: row["id"], reverse=True
            )
            message_flags.observe_snapshot(
                account, [row for row in data if row["id"] in wanted], observed_at
            )
            message_reactions.observe_snapshot(
                account, [row for row in data if row["id"] in wanted], observed_at
            )
        finally:
            zulip.close()
        Task(
            uuid=sys_uuid.uuid5(task.uuid, str(first)),
            project_id=task.project_id,
            external_account_uuid=account.uuid,
            priority=task.priority,
            realtime_event_uuid=task.realtime_event_uuid,
            content=RefreshMessageFlagsContent(
                after_message_id=self.after_message_id, last_message_id=first - 1
            ),
        ).insert()

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        else:
            super().on_error(task, error)


class OutboundMessageContent(AbstractTaskContent):
    delivery_uuid = properties.property(types.UUID(), required=True, read_only=True)
    content = properties.property(
        types.String(max_length=40000), required=True, read_only=True
    )

    def prepare_content(self, task: "Task", delivery: "MessageDelivery") -> str:
        session = contexts.Context().get_session()
        snapshot = session.execute(
            "SELECT zulip_content FROM bridge_zulip_v5_content_snapshots WHERE uuid=%s",
            (task.uuid,),
        ).fetchone()
        if snapshot is not None:
            return snapshot["zulip_content"]
        account = delivery.account()
        zulip = client.ZulipClient(account)
        try:
            converter = content_conversion.ConversionContext(account, session=session)
            text = converter.to_zulip(
                {"kind": "markdown", "content": self.content},
                delivery.stream_uuid,
                zulip,
            )
        finally:
            zulip.close()
        if len(text) > 10000:
            raise ValueError("Converted Zulip message exceeds 10000 characters")
        session.execute(
            """INSERT INTO bridge_zulip_v5_content_snapshots
                   (uuid, delivery_uuid, workspace_content, zulip_content)
               VALUES (%s,%s,%s,%s)""",
            (task.uuid, delivery.uuid, self.content, text),
        )
        return text

    def ready_to_prepare(self, task: "Task") -> bool:
        row = (
            contexts.Context()
            .get_session()
            .execute(
                "SELECT status, lease_owner, lease_expires_at FROM bridge_zulip_v5_tasks WHERE uuid=%s FOR UPDATE",
                (task.uuid,),
            )
            .fetchone()
        )
        return row["status"] in ("new", "waiting", "retry") and (
            (
                getattr(task, "_worker_owner", None) is not None
                and row["lease_owner"] == task._worker_owner
            )
            or row["lease_expires_at"] is None
            or row["lease_expires_at"]
            <= datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        )

    def claim(self, task: "Task") -> None:
        task.lease_owner = getattr(task, "_worker_owner", None) or sys_uuid.uuid4()
        task.lease_expires_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(minutes=2)
        task.update()

    def delivery(self, task: "Task") -> "MessageDelivery":
        session = contexts.Context().get_session()
        session.execute(
            "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s AND project_id=%s FOR UPDATE",
            (task.external_account_uuid, task.project_id),
        )
        session.execute(
            "SELECT uuid FROM bridge_zulip_v5_deliveries WHERE uuid=%s FOR UPDATE",
            (self.delivery_uuid,),
        )
        delivery = MessageDelivery.objects.get_one(
            filters={
                "uuid": filters.EQ(self.delivery_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        delivery.account()
        if delivery.external_account_uuid != task.external_account_uuid:
            raise SubscriptionChangedError()
        return delivery

    def on_error(self, task: "Task", error: Exception) -> None:
        if (
            isinstance(self, SendMessageContent)
            and isinstance(error, requests.HTTPError)
            and 400 <= error.response.status_code < 500
        ):
            delivery = MessageDelivery.objects.get_one(
                filters={"uuid": filters.EQ(self.delivery_uuid)}
            )
            if delivery.zulip_message_id is None:
                delivery.sending = False
                delivery.update()
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        elif isinstance(
            error,
            (
                DeliveryPendingError,
                content_conversion.ConversionPendingError,
                requests.Timeout,
                requests.ConnectionError,
            ),
        ) or (
            isinstance(error, requests.HTTPError)
            and (
                error.response.status_code in (408, 429)
                or error.response.status_code >= 500
            )
        ):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = datetime.datetime.now(datetime.timezone.utc) + (
                REALTIME_MESSAGE_RETRY_DELAY
                if isinstance(
                    error,
                    (DeliveryPendingError, content_conversion.ConversionPendingError),
                )
                else datetime.timedelta(seconds=3)
            )


class SendMessageContent(OutboundMessageContent):
    KIND = "send_message"

    def prepare(self, task: "Task") -> bool:
        delivery = self.delivery(task)
        if not self.ready_to_prepare(task):
            return False
        self._submit = not delivery.sending and delivery.zulip_message_id is None
        if self._submit:
            subscription = RealtimeSubscription.for_account(delivery.account())
            if subscription is None:
                raise DeliveryPendingError(
                    "Waiting for the sender's realtime subscription"
                )
            self.prepare_content(task, delivery)
            delivery.sending = True
            delivery.update()
        self.claim(task)
        return True

    def execute(self, task: "Task") -> None:
        delivery = self.delivery(task)
        if delivery.delivered_at is not None:
            return
        if delivery.zulip_message_id is None and not getattr(self, "_submit", False):
            # The attempt marker commits before HTTP. After a lost response or
            # worker crash, wait for local echo instead of posting a duplicate.
            raise DeliveryPendingError(
                "Waiting for the Zulip send result or local echo"
            )
        account = delivery.account()
        zulip = client.ZulipClient(account)
        try:
            if delivery.zulip_message_id is None:
                subscription = RealtimeSubscription.for_account(account)
                if subscription is None:
                    raise DeliveryPendingError(
                        "Waiting for the sender's realtime subscription"
                    )
                text = (
                    contexts.Context()
                    .get_session()
                    .execute(
                        "SELECT zulip_content FROM bridge_zulip_v5_content_snapshots WHERE uuid=%s",
                        (task.uuid,),
                    )
                    .fetchone()["zulip_content"]
                )
                self._submit = False
                delivery.zulip_message_id = zulip.send_message(
                    delivery.recipient["to"],
                    text,
                    delivery.topic_name,
                    subscription.queue_id,
                    str(delivery.uuid),
                )
                delivery.update()
            else:
                (data,) = zulip.get_messages(
                    delivery.zulip_message_id, delivery.zulip_message_id
                )
                delivery.observe(data)
        finally:
            zulip.close()

    def on_done(self, task: "Task") -> None:
        delivery = MessageDelivery.objects.get_one(
            filters={"uuid": filters.EQ(self.delivery_uuid)}
        )
        if delivery.delivered_at is None:
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = (
                datetime.datetime.now(datetime.timezone.utc)
                + REALTIME_MESSAGE_RETRY_DELAY
            )


class EditMessageContent(OutboundMessageContent):
    KIND = "edit_message"

    def prepare(self, task: "Task") -> bool:
        delivery = self.delivery(task)
        if not self.ready_to_prepare(task):
            return False
        if delivery.zulip_message_id is None or delivery.delivered_at is None:
            raise DeliveryPendingError("Waiting for message delivery before editing")
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": filters.EQ(delivery.message_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        if message.payload.content == self.content:
            self.prepare_content(task, delivery)
        self.claim(task)
        return True

    def execute(self, task: "Task") -> None:
        delivery = self.delivery(task)
        if delivery.zulip_message_id is None or delivery.delivered_at is None:
            raise DeliveryPendingError("Waiting for message delivery before editing")
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": filters.EQ(delivery.message_uuid),
                "project_id": filters.EQ(task.project_id),
            }
        )
        if message.payload.content != self.content:
            return
        text = self.prepare_content(task, delivery)
        zulip = client.ZulipClient(delivery.account())
        try:
            zulip.edit_message(delivery.zulip_message_id, text)
            (data,) = zulip.get_messages(
                delivery.zulip_message_id, delivery.zulip_message_id
            )
            delivery.observe(data)
        finally:
            zulip.close()


class DeleteMessageContent(AbstractTaskContent):
    KIND = "delete_message"

    message_id = properties.property(types.Integer(min_value=1), required=True)
    hostname = properties.property(types.String(), required=True)
    external_account_id = properties.property(types.String(), required=True)
    author_uuid = properties.property(types.UUID(), required=True)

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "project_id": filters.EQ(task.project_id),
                "uuid": filters.EQ(task.external_account_uuid),
            }
        )
        if (
            account.provider != "zulip"
            or account.get_host() != self.hostname
            or account.external_account_id != self.external_account_id
            or account.owner_user_uuid != self.author_uuid
            or not account.can_stream()
        ):
            raise SubscriptionChangedError()
        zulip = client.ZulipClient(account)
        try:
            zulip.delete_message(self.message_id)
        finally:
            zulip.close()

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, SubscriptionChangedError):
            task.status = TaskStatus.CANCEL.value
        elif isinstance(error, (requests.Timeout, requests.ConnectionError)) or (
            isinstance(error, requests.HTTPError)
            and (
                error.response.status_code in (408, 429)
                or error.response.status_code >= 500
            )
        ):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(seconds=3)


class ConvertMessageContent(AbstractTaskContent):
    KIND = "convert_message_content"

    batch_size = properties.property(
        types.Integer(min_value=1, max_value=100), default=100
    )
    # Retain the old cursor field when loading tasks serialized before 0242.
    after_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    before_timestamp = properties.property(
        types.AllowNone(types.Integer(min_value=0)), default=None
    )
    before_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    before_message_id = properties.property(
        types.AllowNone(types.Integer(min_value=0)), default=None
    )
    content_uuid = properties.property(types.AllowNone(types.UUID()), default=None)

    def execute(self, task: "Task") -> None:
        rows = (
            contexts.Context()
            .get_session()
            .execute(
                """SELECT content.uuid, content.source_timestamp, content.source_message_id, observed.*
               FROM (
                   SELECT content.uuid, content.source_timestamp, content.source_message_id
                   FROM bridge_zulip_v5_message_contents AS content
                   WHERE content.conversion_version < %s
                     AND (%s::bigint IS NULL OR (content.source_timestamp, content.source_message_id, content.uuid) < (%s, %s, %s))
                     AND (%s::uuid IS NULL OR content.uuid = %s)
                     AND EXISTS (SELECT 1 FROM bridge_zulip_v5_messages AS candidate WHERE candidate.content=content.uuid)
                   ORDER BY content.source_timestamp DESC, content.source_message_id DESC, content.uuid DESC LIMIT %s
               ) AS content
               JOIN LATERAL (
                   SELECT observed.external_account_uuid, observed.message_id,
                          observed.sender_id, observed.timestamp, channel.stream_uuid
                   FROM bridge_zulip_v5_messages AS observed
                   JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=observed.channel
                   WHERE observed.content=content.uuid
                   ORDER BY observed.timestamp DESC, observed.message_id DESC, observed.uuid
                   LIMIT 1
               ) AS observed ON true
               ORDER BY content.source_timestamp DESC, content.source_message_id DESC, content.uuid DESC""",
                (
                    content_conversion.VERSION,
                    self.before_timestamp,
                    self.before_timestamp,
                    self.before_message_id,
                    self.before_uuid,
                    self.content_uuid,
                    self.content_uuid,
                    self.batch_size,
                ),
            )
            .fetchall()
        )
        contents = (
            {
                content.uuid: content
                for content in MessageContent.objects.get_all(
                    filters={"uuid": filters.In([row["uuid"] for row in rows])}
                )
            }
            if rows
            else {}
        )
        converters: dict[
            sys_uuid.UUID, content_conversion.ConversionContext | None
        ] = {}
        for account_uuid in dict.fromkeys(row["external_account_uuid"] for row in rows):
            converters[account_uuid] = None
            try:
                with utils.savepoint():
                    account = external_models.ExternalAccount.objects.get_one(
                        filters={"uuid": filters.EQ(account_uuid)}
                    )
                    prefetched = content_conversion.ConversionContext(account)
                    prefetched.prefetch(
                        [
                            {
                                "id": row["message_id"],
                                "content": contents[row["uuid"]].zulip_content,
                            }
                            for row in rows
                            if row["external_account_uuid"] == account_uuid
                        ]
                    )
                    converters[account_uuid] = prefetched
            except Exception:
                LOG.exception(
                    "Failed to prefetch Zulip content; resolving messages individually"
                )
        for row in rows:
            try:
                with utils.savepoint():
                    account_uuid = row["external_account_uuid"]
                    content = contents[row["uuid"]]
                    converter = converters[account_uuid]
                    if converter is None:
                        account = external_models.ExternalAccount.objects.get_one(
                            filters={"uuid": filters.EQ(account_uuid)}
                        )
                        converter = content_conversion.ConversionContext(account)
                    content.convert(
                        converter,
                        {
                            "id": row["message_id"],
                            "sender_id": row["sender_id"],
                            "timestamp": row["timestamp"],
                        },
                        row["stream_uuid"],
                        None,
                    )
            except Exception:
                if self.content_uuid is not None:
                    raise
                LOG.exception(
                    "Failed to convert stored Zulip message content %s", row["uuid"]
                )
                Task(
                    priority=150000,
                    not_before_at=datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=10),
                    content=ConvertMessageContent(content_uuid=row["uuid"]),
                ).insert()
        if len(rows) == self.batch_size:
            Task(
                priority=150000,
                content=ConvertMessageContent(
                    batch_size=self.batch_size,
                    before_timestamp=rows[-1]["source_timestamp"],
                    before_message_id=rows[-1]["source_message_id"],
                    before_uuid=rows[-1]["uuid"],
                ),
            ).insert()

    def on_error(self, task: "Task", error: Exception) -> None:
        task.status = TaskStatus.RETRY.value
        task.next_retry_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=10)


class ReconcileRealtimeContent(AbstractTaskContent):
    """Publish only the messages touched by a realtime import, in bounded steps."""

    KIND = "reconcile_realtime"
    message_ids = properties.property(
        types.TypedList(types.Integer(min_value=1)), required=True
    )

    def execute(self, task: "Task") -> None:
        session = contexts.Context().get_session()
        rows = session.execute(
            """SELECT messenger_uuid_v5(observed.project_id,observed.message_uuid::text) AS message_uuid,
                      channel.stream_uuid,observed.topic AS topic_uuid
               FROM bridge_zulip_v5_messages AS observed
               JOIN bridge_zulip_v5_channels AS channel ON channel.uuid=observed.channel
               WHERE observed.project_id=%s AND observed.external_account_uuid=%s
                 AND observed.message_id=ANY(%s)""",
            (task.project_id, task.external_account_uuid, self.message_ids),
        ).fetchall()
        if not rows:
            return
        streams = list({row["stream_uuid"] for row in rows})
        topics = list({row["topic_uuid"] for row in rows})
        messages = list({row["message_uuid"] for row in rows})
        processed = 0
        for model, scope in (
            (ZulipStreamState, {"stream_uuid": filters.In(streams)}),
            (ZulipStreamBindingState, {"stream_uuid": filters.In(streams)}),
            (ZulipTopicState, {"topic_uuid": filters.In(topics)}),
        ):
            if processed == 16:
                break
            states = model.objects.get_all(
                filters={"project_id": filters.EQ(task.project_id), **scope},
                limit=16 - processed,
            )
            for state in states:
                state.do()
            processed += len(states)
        if processed < 16:
            processed += ZulipMessageState.reconcile_batch(
                16 - processed, project_id=task.project_id, message_uuids=messages
            )
        if processed:
            Task(
                project_id=task.project_id,
                external_account_uuid=task.external_account_uuid,
                realtime_event_uuid=task.realtime_event_uuid,
                priority=task.priority,
                content=self,
            ).insert()

    def on_error(self, task: "Task", error: Exception) -> None:
        task.status = TaskStatus.RETRY.value
        task.next_retry_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=3)


class RefreshMessageCountersContent(AbstractTaskContent):
    KIND = "refresh_message_counters"

    batch_size = properties.property(
        types.Integer(min_value=1), default=1000, read_only=True
    )
    interval = properties.property(
        types.Integer(min_value=1), default=30, read_only=True
    )

    def execute(self, task: "Task") -> None:
        ZulipMessageCounterChange.publish_batch(self.batch_size)
        Task(
            priority=TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS,
            not_before_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=self.interval),
            content=self,
        ).insert()

    def on_error(self, task: "Task", error: Exception) -> None:
        task.status = TaskStatus.RETRY.value
        task.next_retry_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=self.interval)


class ReconciliationContent(AbstractTaskContent):
    KIND = "reconciliation"

    batch_size = properties.property(
        types.Integer(min_value=1), default=16, read_only=True
    )
    interval = properties.property(
        types.Integer(min_value=0), default=10, read_only=True
    )

    def execute(self, task: "Task") -> None:
        states = ZulipProjectUserState.objects.get_all(
            limit=self.batch_size,
        )
        for state in states:
            state.do()
        processed = len(states)
        if processed < self.batch_size:
            stream_states = ZulipStreamState.objects.get_all(
                limit=self.batch_size - processed,
            )
            for stream_state in stream_states:
                stream_state.do()
            processed += len(stream_states)
        if processed < self.batch_size:
            binding_states = ZulipStreamBindingState.objects.get_all(
                limit=self.batch_size - processed,
            )
            for binding_state in binding_states:
                binding_state.do()
            processed += len(binding_states)
        if processed < self.batch_size:
            processed += ZulipTopicState.reconcile_batch(self.batch_size - processed)
        if processed < self.batch_size:
            processed += ZulipMessageState.reconcile_batch(self.batch_size - processed)
        # Realtime owns flag/reaction queues. Taking their account locks here,
        # after projection/event locks, can deadlock with imports and realtime.
        not_before_at = None
        if processed < self.batch_size:
            not_before_at = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(seconds=self.interval)
        Task(
            priority=TASK_PRIORITY_RECONCILIATION,
            not_before_at=not_before_at,
            content=self,
        ).insert()

    def on_error(self, task: "Task", error: Exception) -> None:
        task.status = TaskStatus.RETRY.value
        task.next_retry_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=self.interval)


TASK_CONTENT_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ScanHistoryContent),
    types_dynamic.KindModelType(ReconcileRealtimeContent),
    types_dynamic.KindModelType(ConvertMessageContent),
    types_dynamic.KindModelType(UpdateReactionContent),
    types_dynamic.KindModelType(UpdateMessageFlagsContent),
    types_dynamic.KindModelType(UpdateNotificationsContent),
    types_dynamic.KindModelType(RefreshMessageFlagsContent),
    types_dynamic.KindModelType(SendMessageContent),
    types_dynamic.KindModelType(EditMessageContent),
    types_dynamic.KindModelType(DeleteMessageContent),
    types_dynamic.KindModelType(InitializeConnectionContent),
    types_dynamic.KindModelType(FullBackfillContent),
    types_dynamic.KindModelType(ActualizeBackfillContent),
    types_dynamic.KindModelType(ImportUsersContent),
    types_dynamic.KindModelType(ImportChannelsContent),
    types_dynamic.KindModelType(ImportHistoryContent),
    types_dynamic.KindModelType(ReconciliationContent),
    types_dynamic.KindModelType(RefreshMessageCountersContent),
)


class TaskStatus(str, enum.Enum):
    NEW = "new"
    WAITING = "waiting"
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

    queue = properties.property(
        types.Enum(["realtime", "background"]), default="background"
    )
    concurrency_key = properties.property(types.String(), default="", read_only=True)

    message_created_at = properties.property(
        types.UTCDateTimeZ(),
        default=datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
        read_only=True,
    )
    message_sequence = properties.property(
        types.Integer(min_value=0), default=0, read_only=True
    )

    project_id = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    backfill_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    realtime_event_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
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
    execution_time = properties.property(
        types.AllowNone(types.Float(min_value=0.0)), default=None
    )
    queue_wait_time = properties.property(
        types.AllowNone(types.Float(min_value=0.0)), default=None
    )

    def __init__(self, **kwargs: object) -> None:
        if "queue" not in kwargs and typing.cast(
            AbstractTaskContent, kwargs["content"]
        ).kind in (
            "send_message",
            "edit_message",
            "delete_message",
            "update_message_flags",
            "update_notifications",
            "update_reaction",
            "initialize_connection",
            "reconcile_realtime",
        ):
            kwargs["queue"] = "realtime"
        if "concurrency_key" not in kwargs:
            content = typing.cast(AbstractTaskContent, kwargs["content"])
            account_uuid = kwargs.get("external_account_uuid")
            kwargs["concurrency_key"] = (
                f"{kwargs.get('project_id')}:catalog"
                if content.kind in ("import_users", "import_channels")
                else f"{kwargs.get('project_id')}:{account_uuid}"
                if account_uuid is not None
                else content.kind
            )
        super().__init__(**kwargs)
        if (
            self.status == TaskStatus.NEW.value
            and self.not_before_at is not None
            and self.not_before_at > datetime.datetime.now(datetime.timezone.utc)
        ):
            self.status = TaskStatus.WAITING.value

    def validate(self) -> None:
        super().validate()
        if self.external_account_uuid is not None and self.project_id is None:
            raise exceptions.ValidationErrorException()

    def cancel_pending_tasks(self, exclude_current: bool = False) -> int:
        now = datetime.datetime.now(datetime.timezone.utc)
        session = contexts.Context().get_session()
        cancelled = session.execute(
            """
            UPDATE bridge_zulip_v5_tasks
            SET status = %s, next_retry_at = NULL,
                lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
            WHERE external_account_uuid = %s AND project_id = %s
                AND status IN (%s, %s, %s)
                AND (NOT %s OR uuid <> %s)
            RETURNING uuid
            """,
            (
                TaskStatus.CANCEL.value,
                now.replace(tzinfo=None),
                self.external_account_uuid,
                self.project_id,
                TaskStatus.NEW.value,
                TaskStatus.WAITING.value,
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

    @contextlib.contextmanager
    def track_execution(self) -> typing.Iterator[None]:
        now = datetime.datetime.now(datetime.timezone.utc)
        queued_at = self.created_at if self.execution_time is None else self.updated_at
        ready_at = max(
            value
            for value in (
                queued_at,
                self.not_before_at,
                self.next_retry_at,
                self.lease_expires_at,
            )
            if value is not None
        )
        if getattr(self, "_worker_owner", None) is None:
            self.queue_wait_time = (self.queue_wait_time or 0.0) + max(
                0.0, (now - ready_at).total_seconds()
            )
        started = time.monotonic()
        try:
            yield
        finally:
            self.execution_time = (
                (self.execution_time or 0.0) + time.monotonic() - started
            )
            finished = datetime.datetime.now(datetime.timezone.utc)
            self.properties["updated_at"].set_value_force(finished)
            # Persist only timing fields: a failed send claim must not overwrite
            # a task status changed by another transaction.
            contexts.Context().get_session().execute(
                "UPDATE bridge_zulip_v5_tasks SET execution_time=%s, queue_wait_time=%s, "
                "updated_at=%s WHERE uuid=%s",
                (
                    self.execution_time,
                    self.queue_wait_time,
                    finished.replace(tzinfo=None),
                    self.uuid,
                ),
            )

    def execute(self) -> None:
        with self.track_execution():
            self._execute()

    def _execute(self) -> None:
        source_event_uuid = self.backfill_uuid or self.realtime_event_uuid
        if source_event_uuid is not None:
            # Match API and subscription registration lock order: account first,
            # then task. This also fences writes against a concurrent rebind.
            account = (
                contexts.Context()
                .get_session()
                .execute(
                    "SELECT uuid,status FROM m_external_accounts_v2 WHERE uuid=%s AND project_id=%s FOR UPDATE",
                    (self.external_account_uuid, self.project_id),
                )
                .fetchone()
            )
            if account["status"] == external_models.ExternalAccountStatus.NEW.value:
                self.status = TaskStatus.RETRY.value
                self.next_retry_at = datetime.datetime.now(
                    datetime.timezone.utc
                ) + datetime.timedelta(seconds=3)
                self.lease_owner = None
                self.lease_expires_at = None
                self.update()
                return
            event = RealtimeEvent.objects.get_one(
                filters={"uuid": filters.EQ(source_event_uuid)}
            )
            if event.current_subscription() is None:
                self.status = TaskStatus.CANCEL.value
                self.update()
                return
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
            self.error_description = f"{error}\nError handler failed: {handler_error}"
            # A failed account-status update must not discard a scheduled retry.
            if self.status != TaskStatus.RETRY.value or not isinstance(
                handler_error,
                (storage_exceptions.DeadLock, psycopg.errors.DeadlockDetected),
            ):
                self.status = TaskStatus.ERROR.value
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
            if (
                isinstance(self.content, ImportHistoryContent)
                and self.content.snapshot is not None
            ):
                # Preserve retry payloads, then release bodies only after the
                # completed task has been saved by the ORM.
                contexts.Context().get_session().execute(
                    "UPDATE bridge_zulip_v5_tasks SET content=content-ARRAY["
                    "'snapshot','snapshot_at','account_revision','batch_first_id','batch_revision'] WHERE uuid=%s",
                    (self.uuid,),
                )
            if self.backfill_uuid is not None:
                RealtimeEvent.complete_backfill(self.backfill_uuid)


class RealtimeSubscription(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_subscriptions"

    external_account_uuid = properties.property(types.UUID(), required=True)
    generation = properties.property(types.UUID(), required=True)
    queue_id = properties.property(types.String(), required=True)
    last_event_id = properties.property(types.Integer(min_value=-1), required=True)
    account_revision = properties.property(types.String(), required=True)
    longpoll_timeout = properties.property(types.Integer(min_value=1), required=True)

    @classmethod
    def for_account(
        cls, account: external_models.ExternalAccount
    ) -> "RealtimeSubscription | None":
        return cls.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(account.project_id),
                "external_account_uuid": filters.EQ(account.uuid),
            }
        )

    @staticmethod
    def lock_account(
        expected: external_models.ExternalAccount,
    ) -> external_models.ExternalAccount:
        contexts.Context().get_session().execute(
            "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s AND project_id=%s FOR UPDATE",
            (expected.uuid, expected.project_id),
        )
        account = external_models.ExternalAccount.objects.get_one_or_none(
            filters={
                "uuid": filters.EQ(expected.uuid),
                "project_id": filters.EQ(expected.project_id),
            }
        )
        if (
            account is None
            or not account.can_stream()
            or account.realtime_revision() != expected.realtime_revision()
        ):
            raise SubscriptionChangedError()
        return account

    @classmethod
    def register(
        cls,
        expected: external_models.ExternalAccount,
        data: dict,
        started_at: datetime.datetime,
    ) -> "RealtimeSubscription":
        account = cls.lock_account(expected)
        generation = sys_uuid.uuid4()
        contexts.Context().get_session().execute(
            """
            UPDATE bridge_zulip_v5_tasks
            SET status='cancel', next_retry_at=NULL, updated_at=NOW() AT TIME ZONE 'UTC'
            WHERE external_account_uuid=%s AND project_id=%s
                AND status IN ('new', 'waiting', 'retry')
                AND (backfill_uuid IS NOT NULL OR realtime_event_uuid IS NOT NULL
                     OR content->>'kind' IN ('import_users', 'import_channels'))
        """,
            (account.uuid, account.project_id),
        )
        values = {
            "generation": generation,
            "queue_id": data["queue_id"],
            "last_event_id": data["last_event_id"],
            "account_revision": account.subscription_revision(),
            "longpoll_timeout": data.get("event_queue_longpoll_timeout_seconds", 90),
        }
        subscription = cls.for_account(account)
        if subscription is None:
            subscription = cls(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                **values,
            )
            subscription.insert()
        else:
            subscription.update_dm(values=values)
            subscription.update()
        imported = (
            account.has_completed_backfill
            or account.status == external_models.ExternalAccountStatus.ACTIVE.value
        )
        payload = {
            "type": "actualize_backfill" if imported else "full_backfill",
            "last_message_id": max(0, data["max_message_id"]),
        }
        if imported:
            account.properties["has_completed_backfill"].set_value_force(True)
            payload["since"] = (started_at - datetime.timedelta(days=30)).isoformat()
        RealtimeEvent(
            uuid=sys_uuid.uuid5(generation, "backfill"),
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            generation=generation,
            queue_id=subscription.queue_id,
            source="internal",
            payload=payload,
        ).insert()
        account.update_status(
            external_models.ExternalAccountStatus.FAST_BACKFILL.value
            if imported
            else external_models.ExternalAccountStatus.BACKFILL.value
        )
        return subscription

    @classmethod
    def append_events(
        cls,
        expected: external_models.ExternalAccount,
        generation: sys_uuid.UUID,
        events: list[dict],
        received_at: datetime.datetime | None = None,
    ) -> int:
        account = cls.lock_account(expected)
        subscription = cls.for_account(account)
        if subscription is None or subscription.generation != generation:
            raise SubscriptionChangedError()
        received_at = received_at or datetime.datetime.now(datetime.timezone.utc)
        for event in events:
            if event["id"] <= subscription.last_event_id:
                continue
            RealtimeEvent(
                uuid=sys_uuid.uuid5(generation, str(event["id"])),
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                generation=generation,
                queue_id=subscription.queue_id,
                event_id=event["id"],
                created_at=received_at,
                source="zulip",
                payload=event,
            ).insert()
            subscription.last_event_id = event["id"]
        subscription.update()
        return subscription.last_event_id


class RealtimeEvent(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "bridge_zulip_v5_events"

    external_account_uuid = properties.property(types.UUID(), required=True)
    generation = properties.property(types.UUID(), required=True)
    queue_id = properties.property(types.String(), required=True)
    event_id = properties.property(
        types.AllowNone(types.Integer(min_value=0)), default=None
    )
    source = properties.property(types.Enum(["zulip", "internal"]), required=True)
    payload = properties.property(types.Dict(), required=True)
    processed_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    attempts = properties.property(types.Integer(min_value=0), default=0)
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    error_description = properties.property(
        types.AllowNone(types.String()), default=None
    )

    def current_subscription(self) -> RealtimeSubscription | None:
        subscription = RealtimeSubscription.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(self.project_id),
                "external_account_uuid": filters.EQ(self.external_account_uuid),
                "generation": filters.EQ(self.generation),
            }
        )
        if subscription is None:
            return None
        account = external_models.ExternalAccount.objects.get_one_or_none(
            filters={
                "uuid": filters.EQ(self.external_account_uuid),
                "project_id": filters.EQ(self.project_id),
            }
        )
        if (
            account is None
            or not account.can_stream()
            or account.subscription_revision() != subscription.account_revision
        ):
            return None
        return subscription

    def dispatch(self) -> None:
        if self.current_subscription() is None:
            return
        kind = self.payload["type"]
        contents: list[AbstractTaskContent] = []
        backfill_uuid = None
        priority = TASK_PRIORITY_REALTIME_MESSAGE
        if self.source == "internal":
            values = {
                name: value for name, value in self.payload.items() if name != "type"
            }
            contents = [TASK_CONTENT_TYPE.from_simple_type({"kind": kind, **values})]
            backfill_uuid = self.uuid
            priority = TASK_PRIORITY_INITIALIZE_CONNECTION
        elif kind in ("message", "update_message"):
            if kind == "message":
                if "reactions" in self.payload["message"]:
                    account = external_models.ExternalAccount.objects.get_one(
                        filters={
                            "uuid": filters.EQ(self.external_account_uuid),
                            "project_id": filters.EQ(self.project_id),
                        }
                    )
                    RealtimeSubscription.lock_account(account)
                    if self.current_subscription() is None:
                        return
                    message_reactions.observe_snapshot(
                        account, [self.payload["message"]], self.created_at
                    )
                if "flags" in self.payload:
                    account = external_models.ExternalAccount.objects.get_one(
                        filters={
                            "uuid": filters.EQ(self.external_account_uuid),
                            "project_id": filters.EQ(self.project_id),
                        }
                    )
                    for flag in message_flags.FLAGS:
                        message_flags.record(
                            account,
                            [self.payload["message"]["id"]],
                            flag,
                            flag in self.payload["flags"],
                            self.created_at,
                            self.event_id,
                        )
                if "local_message_id" in self.payload:
                    delivery = MessageDelivery.objects.get_one_or_none(
                        filters={
                            "uuid": filters.EQ(
                                sys_uuid.UUID(self.payload["local_message_id"])
                            ),
                            "project_id": filters.EQ(self.project_id),
                            "external_account_uuid": filters.EQ(
                                self.external_account_uuid
                            ),
                        }
                    )
                    if delivery is not None:
                        delivery.observe(self.payload["message"])
                        for task in Task.objects.get_all(
                            filters={
                                "project_id": filters.EQ(self.project_id),
                                "content": filters.JSONFields(
                                    {
                                        "kind": "send_message",
                                        "delivery_uuid": str(delivery.uuid),
                                    }
                                ),
                                "status": filters.In(["error", "retry"]),
                            }
                        ):
                            task.status = TaskStatus.RETRY.value
                            task.next_retry_at = None
                            task.update()
                message_ids = [self.payload["message"]["id"]]
            elif "message_ids" in self.payload:
                message_ids = self.payload["message_ids"]
            else:
                message_ids = [self.payload["message_id"]]
            if kind == "update_message":
                priority = TASK_PRIORITY_REALTIME_EDIT
            contents = [
                ImportHistoryContent(
                    first_message_id=message_id, last_message_id=message_id
                )
                for message_id in sorted(set(message_ids), reverse=True)
            ]
        elif kind == "delete_message":
            account = external_models.ExternalAccount.objects.get_one(
                filters={
                    "uuid": filters.EQ(self.external_account_uuid),
                    "project_id": filters.EQ(self.project_id),
                }
            )
            RealtimeSubscription.lock_account(account)
            if self.current_subscription() is None:
                return
            message_ids = (
                self.payload["message_ids"]
                if "message_ids" in self.payload
                else [self.payload["message_id"]]
            )
            for message in ZulipMessage.objects.get_all(
                filters={
                    "project_id": filters.EQ(self.project_id),
                    "external_account_uuid": filters.EQ(account.uuid),
                    "message_id": filters.In(message_ids),
                }
            ):
                message.delete()
        elif kind == "update_message_flags":
            flag = self.payload["flag"]
            if flag in message_flags.FLAGS:
                account = external_models.ExternalAccount.objects.get_one(
                    filters={
                        "uuid": filters.EQ(self.external_account_uuid),
                        "project_id": filters.EQ(self.project_id),
                    }
                )
                message_ids = (
                    [0] if self.payload.get("all", False) else self.payload["messages"]
                )
                message_flags.record(
                    account,
                    message_ids,
                    flag,
                    self.payload["op"] == "add",
                    self.created_at,
                    self.event_id,
                )
                known = {
                    row.message_id
                    for row in ZulipMessage.objects.get_all(
                        filters={
                            "project_id": filters.EQ(self.project_id),
                            "external_account_uuid": filters.EQ(account.uuid),
                            "message_id": filters.In(message_ids),
                        }
                    )
                }
                known.update(
                    row.zulip_message_id
                    for row in MessageDelivery.objects.get_all(
                        filters={
                            "project_id": filters.EQ(self.project_id),
                            "hostname": filters.EQ(account.get_host()),
                            "zulip_message_id": filters.In(message_ids),
                        }
                    )
                )
                contents = [
                    ImportHistoryContent(
                        first_message_id=message_id, last_message_id=message_id
                    )
                    for message_id in sorted(
                        set(message_ids) - known - {0}, reverse=True
                    )
                ]
        elif kind == "reaction":
            account = external_models.ExternalAccount.objects.get_one(
                filters={
                    "project_id": filters.EQ(self.project_id),
                    "uuid": filters.EQ(self.external_account_uuid),
                }
            )
            RealtimeSubscription.lock_account(account)
            if self.current_subscription() is None:
                return
            message_reactions.record_event(
                account, self.payload, self.created_at, self.event_id, self.queue_id
            )
            actor = external_models.ExternalAccount.objects.get_one_or_none(
                filters={
                    "project_id": filters.EQ(self.project_id),
                    "provider": filters.EQ("zulip"),
                    "external_account_id": filters.EQ(
                        f"{account.get_host()}:{self.payload['user_id']}"
                    ),
                }
            )
            if actor is None:
                contents.append(
                    ImportUsersContent(
                        event={
                            "op": "update",
                            "person": {"user_id": self.payload["user_id"]},
                        }
                    )
                )
            remote = message_reactions.message_for(account, self.payload["message_id"])
            if remote["canonical_message_uuid"] is None:
                contents.append(
                    ImportHistoryContent(
                        first_message_id=self.payload["message_id"],
                        last_message_id=self.payload["message_id"],
                    )
                )
        elif kind == "realm_user":
            contents = [ImportUsersContent(event=self.payload)]
            priority = TASK_PRIORITY_IMPORT_USERS
        elif kind in ("subscription", "stream", "user_topic"):
            contents = [ImportChannelsContent(event=self.payload)]
            priority = TASK_PRIORITY_IMPORT_CHANNELS
        for index, content in enumerate(contents):
            Task(
                uuid=sys_uuid.uuid5(self.uuid, f"task:{index}"),
                project_id=self.project_id,
                external_account_uuid=self.external_account_uuid,
                priority=priority,
                backfill_uuid=backfill_uuid,
                realtime_event_uuid=self.uuid,
                queue="background" if backfill_uuid is not None else "realtime",
                content=content,
            ).insert()

    @classmethod
    def dispatch_one(cls) -> bool:
        row = (
            contexts.Context()
            .get_session()
            .execute(
                """
            SELECT event.uuid FROM bridge_zulip_v5_events AS event
            JOIN m_external_accounts_v2 AS account ON account.uuid=event.external_account_uuid
            WHERE event.processed_at IS NULL AND account.status<>'new'
              AND (event.next_retry_at IS NULL OR event.next_retry_at <= NOW() AT TIME ZONE 'UTC')
            ORDER BY event.created_at,event.event_id NULLS FIRST,event.uuid
            LIMIT 1 FOR UPDATE OF event SKIP LOCKED
        """,
                (),
            )
            .fetchone()
        )
        if row is None:
            return False
        event = cls.objects.get_one(filters={"uuid": filters.EQ(row["uuid"])})
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            with utils.savepoint():
                event.dispatch()
        except Exception as error:
            LOG.exception("Failed to dispatch Zulip event %s", event.uuid)
            event.attempts += 1
            event.error_description = str(error)
            event.next_retry_at = now + datetime.timedelta(
                seconds=min(2 ** min(event.attempts, 6), 60)
            )
        else:
            event.processed_at = now
            event.error_description = None
            event.next_retry_at = None
        event.update()
        return True

    @classmethod
    def complete_backfill(cls, event_uuid: sys_uuid.UUID) -> None:
        event = cls.objects.get_one(filters={"uuid": filters.EQ(event_uuid)})
        if event.current_subscription() is None:
            return
        unfinished = Task.objects.get_all(
            filters={
                "backfill_uuid": filters.EQ(event_uuid),
                "status": filters.NE(TaskStatus.DONE.value),
            },
            limit=1,
        )
        if unfinished:
            return
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(event.external_account_uuid),
                "project_id": filters.EQ(event.project_id),
            }
        )
        RealtimeSubscription.lock_account(account)
        account.update_status(external_models.ExternalAccountStatus.ACTIVE.value)


class MessageDelivery(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """Bind a Workspace message and destination to the author's Zulip account."""

    __tablename__ = "bridge_zulip_v5_deliveries"

    message_uuid = properties.property(types.UUID(), required=True)
    stream_uuid = properties.property(types.UUID(), required=True)
    external_account_uuid = properties.property(types.UUID(), required=True)
    hostname = properties.property(types.String(), required=True)
    external_account_id = properties.property(types.String(), required=True)
    recipient = properties.property(types.Dict(), required=True)
    topic_name = properties.property(types.AllowNone(types.String()), default=None)
    sending = properties.property(types.Boolean(), default=False)
    zulip_message_id = properties.property(
        types.AllowNone(types.Integer(min_value=1)), default=None
    )
    delivered_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    content_observed_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    observed_zulip_content = properties.property(
        types.AllowNone(types.String(max_length=10000)), default=None
    )
    observed_workspace_content = properties.property(
        types.AllowNone(types.String(max_length=40000)), default=None
    )

    def account(self, sending: bool = True) -> external_models.ExternalAccount:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(self.external_account_uuid),
                "project_id": filters.EQ(self.project_id),
            }
        )
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": filters.EQ(self.message_uuid),
                "project_id": filters.EQ(self.project_id),
            }
        )
        if (
            account.owner_user_uuid != message.author_uuid
            or message.deleted_at is not None
            or account.provider != "zulip"
            or account.get_host() != self.hostname
            or account.external_account_id != self.external_account_id
            or (sending and not account.can_stream())
        ):
            raise SubscriptionChangedError()
        return account

    @classmethod
    def pending_import_ids(
        cls, account: external_models.ExternalAccount, messages: list[dict]
    ) -> set[int]:
        pending = (
            contexts.Context()
            .get_session()
            .execute(
                """SELECT delivery.external_account_id, delivery.recipient,
                          delivery.topic_name, snapshot.zulip_content
                   FROM bridge_zulip_v5_deliveries AS delivery
                   LEFT JOIN bridge_zulip_v5_content_snapshots AS snapshot
                     ON snapshot.delivery_uuid=delivery.uuid
                   WHERE delivery.project_id=%s AND delivery.hostname=%s
                     AND delivery.sending AND delivery.zulip_message_id IS NULL""",
                (account.project_id, account.get_host()),
            )
            .fetchall()
        )
        blocked = set()
        for data in messages:
            for delivery in pending:
                sender = int(delivery["external_account_id"].rsplit(":", 1)[1])
                if data["sender_id"] != sender:
                    continue
                # Edits may have changed both content and destination. Without
                # an immutable snapshot, retain the conservative identity fence.
                if (
                    data.get("last_edit_timestamp") is not None
                    or delivery["zulip_content"] is None
                ):
                    blocked.add(data["id"])
                    break
                # Zulip normalizes leading newlines and trailing whitespace.
                text = delivery["zulip_content"].rstrip().lstrip("\n")
                if data["content"] != text and not (
                    data["content"].endswith("\n[message truncated]")
                    and text.startswith(
                        data["content"].removesuffix("\n[message truncated]")
                    )
                ):
                    continue
                if delivery["topic_name"] is None:
                    matches = data["type"] == "private" and {
                        user["id"] for user in data["display_recipient"]
                    } == set(delivery["recipient"]["to"]) | {sender}
                else:
                    matches = (
                        data["type"] == "stream"
                        and data["stream_id"] == delivery["recipient"]["to"]
                        and (
                            data["subject"] == delivery["topic_name"].strip()
                            or (
                                data["subject"].endswith("...")
                                and delivery["topic_name"]
                                .strip()
                                .startswith(data["subject"][:-3])
                            )
                            or (
                                not data["subject"]
                                and delivery["topic_name"] == "(no topic)"
                            )
                        )
                    )
                if matches:
                    # Matching content is only a reason to defer. Never infer
                    # a delivery identity from text or repost an unknown send.
                    blocked.add(data["id"])
                    break
        return blocked

    @classmethod
    def route(
        cls,
        project_id: sys_uuid.UUID,
        placement: v2_models.WorkspaceMessagePlacement,
        message: v2_models.WorkspaceMessage,
    ) -> "MessageDelivery":
        session = contexts.Context().get_session()
        # Serialize all events for this local message without borrowing another
        # observer's credentials when several accounts see the same channel.
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"zulip-delivery:{project_id}:{message.uuid}:{placement.stream_uuid}",),
        )
        delivery = cls.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(project_id),
                "message_uuid": filters.EQ(message.uuid),
                "stream_uuid": filters.EQ(placement.stream_uuid),
            }
        )
        if delivery is not None:
            delivery.account()
            return delivery
        accounts = external_models.ExternalAccount.objects.get_all(
            filters={
                "project_id": filters.EQ(project_id),
                "owner_user_uuid": filters.EQ(message.author_uuid),
                "provider": filters.EQ("zulip"),
                "disabled": filters.EQ(False),
            }
        )
        channels = (
            ZulipChannel.objects.get_all(
                filters={
                    "external_account_uuid": filters.In(
                        [account.uuid for account in accounts]
                    ),
                    "stream_uuid": filters.EQ(placement.stream_uuid),
                }
            )
            if accounts
            else []
        )
        routes = [
            (account, channel)
            for account in accounts
            for channel in channels
            if channel.external_account_uuid == account.uuid and account.can_stream()
        ]
        if len(routes) != 1:
            raise DeliveryPendingError(
                "Waiting for an unambiguous sender account and Zulip channel"
            )
        account, channel = routes[0]
        topic = v2_models.WorkspaceStreamTopic.objects.get_one(
            filters={
                "project_id": filters.EQ(project_id),
                "uuid": filters.EQ(placement.topic_uuid),
            }
        )
        if channel.channel_type == ZulipChannelType.CHANNEL.value:
            recipient = channel.stream_id
            topic_name = (
                ""
                if topic.uuid == sys_uuid.uuid5(channel.stream_uuid, "")
                else topic.name
            )
        else:
            topic_name = None
            if channel.channel_type == ZulipChannelType.PRIVATE.value:
                peer = external_models.ExternalAccount.objects.get_one(
                    filters={
                        "project_id": filters.EQ(project_id),
                        "uuid": filters.EQ(channel.direct_user_uuid),
                    }
                )
                if peer.provider != "zulip" or peer.get_host() != account.get_host():
                    raise SubscriptionChangedError()
                recipient = [int(peer.external_account_id.rsplit(":", 1)[1])]
            else:
                members = session.execute(
                    "SELECT user_uuid FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND active",
                    (project_id, placement.stream_uuid),
                ).fetchall()
                peers = external_models.ExternalAccount.objects.get_all(
                    filters={
                        "project_id": filters.EQ(project_id),
                        "provider": filters.EQ("zulip"),
                        "owner_user_uuid": filters.In(
                            [row["user_uuid"] for row in members]
                        ),
                    }
                )
                recipient = sorted(
                    {
                        int(peer.external_account_id.rsplit(":", 1)[1])
                        for peer in peers
                        if peer.external_account_id is not None
                        and peer.get_host() == account.get_host()
                    }
                )
                namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, account.get_host())
                if (
                    sys_uuid.uuid5(namespace, "dm:" + ",".join(map(str, recipient)))
                    != channel.stream_uuid
                ):
                    raise DeliveryPendingError(
                        "Waiting for the complete Zulip group recipient list"
                    )
        delivery = cls(
            project_id=project_id,
            message_uuid=message.uuid,
            stream_uuid=placement.stream_uuid,
            external_account_uuid=account.uuid,
            hostname=account.get_host(),
            external_account_id=account.external_account_id,
            recipient={"to": recipient},
            topic_name=topic_name,
        )
        if message.source_name == "zulip":
            source = session.execute(
                """
                SELECT observed.message_id, observed.timestamp, observed.sender_id
                FROM bridge_zulip_v5_messages observed JOIN m_external_accounts_v2 account
                  ON account.uuid=observed.external_account_uuid AND account.project_id=observed.project_id
                WHERE observed.project_id=%s AND messenger_uuid_v5(observed.project_id, observed.message_uuid::text)=%s
                  AND rtrim(account.settings->>'server_url','/')=%s ORDER BY observed.updated_at DESC LIMIT 1
            """,
                (project_id, message.uuid, account.settings["server_url"].rstrip("/")),
            ).fetchone()
            if source is None:
                raise DeliveryPendingError(
                    "Waiting for the imported Zulip message identity"
                )
            if source["sender_id"] != int(
                account.external_account_id.rsplit(":", 1)[1]
            ):
                raise SubscriptionChangedError()
            delivery.zulip_message_id = source["message_id"]
            delivery.delivered_at = datetime.datetime.fromtimestamp(
                source["timestamp"], datetime.timezone.utc
            )
        delivery.insert()
        return delivery

    def observe(
        self,
        data: dict,
        observer: external_models.ExternalAccount | None = None,
        zulip: client.ZulipClient | None = None,
        convert_text: bool = True,
    ) -> None:
        account = self.account(sending=False)
        if data["sender_id"] != int(account.external_account_id.rsplit(":", 1)[1]):
            raise SubscriptionChangedError()
        if self.zulip_message_id is not None and self.zulip_message_id != data["id"]:
            raise SubscriptionChangedError()
        self.zulip_message_id = data["id"]
        self.delivered_at = datetime.datetime.fromtimestamp(
            data["timestamp"], datetime.timezone.utc
        )
        session = contexts.Context().get_session()
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.message_uuid),
            }
        )
        edited_at = datetime.datetime.fromtimestamp(
            data.get("last_edit_timestamp", data["timestamp"]), datetime.timezone.utc
        )
        if (
            self.content_observed_at is not None
            and edited_at < self.content_observed_at
        ):
            return
        previous_content = self.observed_workspace_content
        self.content_observed_at = edited_at
        self.observed_zulip_content = data["content"]
        self.update()
        snapshot = session.execute(
            """SELECT workspace_content FROM bridge_zulip_v5_content_snapshots
               WHERE delivery_uuid=%s AND zulip_content=%s
               ORDER BY created_at DESC, uuid DESC LIMIT 1""",
            (self.uuid, data["content"]),
        ).fetchone()
        if snapshot is not None:
            text = snapshot["workspace_content"]
        elif not convert_text:
            text = self.observed_workspace_content
        else:
            text = content_conversion.ConversionContext(
                observer or account
            ).to_workspace(data, self.stream_uuid, zulip)
        self.observed_workspace_content = text
        self.update()
        pending_edits = Task.objects.get_all(
            filters={
                "project_id": filters.EQ(self.project_id),
                "content": filters.JSONFields(
                    {"kind": "edit_message", "delivery_uuid": str(self.uuid)}
                ),
                "status": filters.In(["new", "waiting", "retry"]),
            },
            limit=1,
        )
        replace_content = message.payload.content == text or (
            (snapshot is None or previous_content == message.payload.content)
            and not pending_edits
            and (
                message.content_updated_at is None
                or edited_at >= message.content_updated_at
            )
        )
        changed = message.created_at != self.delivered_at or (
            replace_content
            and (
                message.payload.content != text
                or message.content_updated_at != edited_at
            )
        )
        if not changed:
            return
        with content_conversion.preserve_source_edit_time(
            session, self.project_id, self.message_uuid
        ):
            self._update_observed_message(session, replace_content, text, edited_at)

    def _update_observed_message(
        self,
        session: sessions.PgSQLSession,
        replace_content: bool,
        text: str,
        edited_at: datetime.datetime,
    ) -> None:
        session.execute(
            """
            UPDATE messenger_messages SET created_at=%s, updated_at=NOW() AT TIME ZONE 'UTC',
                payload=CASE WHEN %s THEN %s::jsonb ELSE payload END,
                content_updated_at=CASE WHEN %s THEN %s ELSE content_updated_at END
            WHERE project_id=%s AND uuid=%s
        """,
            (
                messenger_helpers._database_timestamp(self.delivered_at),
                replace_content,
                json.dumps({"kind": "markdown", "content": text}),
                replace_content,
                messenger_helpers._database_timestamp(edited_at),
                self.project_id,
                self.message_uuid,
            ),
        )
        session.execute(
            "UPDATE messenger_message_placements SET created_at=%s, updated_at=NOW() AT TIME ZONE 'UTC' WHERE project_id=%s AND message_uuid=%s",
            (
                messenger_helpers._database_timestamp(self.delivered_at),
                self.project_id,
                self.message_uuid,
            ),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events(uuid, project_id, event_kind, scope_kind, scope_key, payload)
            VALUES (%s,%s,'delivery_snapshot_event','message',%s,%s::jsonb)
        """,
            (
                sys_uuid.uuid4(),
                self.project_id,
                f"{self.project_id}:{self.message_uuid}",
                json.dumps(
                    {
                        "source_kind": "message.updated",
                        "canonical_message_uuid": str(self.message_uuid),
                    }
                ),
            ),
        )


class WorkspaceEvent(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """Independent delivery receipts for the existing Messenger domain outbox."""

    __tablename__ = "bridge_zulip_v5_workspace_events"

    processed_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    attempts = properties.property(types.Integer(min_value=0), default=0)
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    error_description = properties.property(
        types.AllowNone(types.String()), default=None
    )

    def dispatch(self) -> None:
        outbox = v2_models.WorkspaceDomainOutboxEvent.objects.get_one(
            filters={
                "uuid": filters.EQ(self.uuid),
                "project_id": filters.EQ(self.project_id),
            }
        )
        if outbox.payload["source_kind"] == "message.deleted":
            for snapshot in outbox.payload.get("zulip_deletions", []):
                task_uuid = sys_uuid.uuid5(self.uuid, snapshot["delivery_uuid"])
                if (
                    Task.objects.get_one_or_none(
                        filters={"uuid": filters.EQ(task_uuid)}
                    )
                    is None
                ):
                    Task(
                        uuid=task_uuid,
                        project_id=self.project_id,
                        external_account_uuid=sys_uuid.UUID(
                            snapshot["external_account_uuid"]
                        ),
                        priority=TASK_PRIORITY_REALTIME_EDIT,
                        content=TASK_CONTENT_TYPE.from_simple_type(snapshot["content"]),
                    ).insert()
            return
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one_or_none(
            filters={
                "uuid": filters.EQ(sys_uuid.UUID(outbox.payload["placement_uuid"])),
                "project_id": filters.EQ(self.project_id),
            }
        )
        if placement is None:
            return
        stream = v2_models.WorkspaceStream.objects.get_one(
            filters={
                "uuid": filters.EQ(placement.stream_uuid),
                "project_id": filters.EQ(self.project_id),
            }
        )
        if stream.source_name != "zulip":
            return
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": filters.EQ(placement.message_uuid),
                "project_id": filters.EQ(self.project_id),
            }
        )
        if message.deleted_at is not None or message.payload.kind != "markdown":
            return
        delivery = MessageDelivery.route(self.project_id, placement, message)
        creating = outbox.payload["source_kind"] == "message.created"
        content = (
            SendMessageContent(
                delivery_uuid=delivery.uuid, content=message.payload.content
            )
            if creating
            else EditMessageContent(
                delivery_uuid=delivery.uuid, content=message.payload.content
            )
        )
        task_uuid = sys_uuid.uuid5(
            delivery.uuid, "send" if creating else f"edit:{message.content_updated_at}"
        )
        if (
            Task.objects.get_one_or_none(filters={"uuid": filters.EQ(task_uuid)})
            is None
        ):
            Task(
                uuid=task_uuid,
                project_id=self.project_id,
                external_account_uuid=delivery.external_account_uuid,
                priority=TASK_PRIORITY_REALTIME_MESSAGE
                if creating
                else TASK_PRIORITY_REALTIME_EDIT,
                content=content,
            ).insert()

    @classmethod
    def dispatch_one(cls) -> bool:
        row = (
            contexts.Context()
            .get_session()
            .execute("""
            SELECT uuid FROM bridge_zulip_v5_workspace_events
            WHERE processed_at IS NULL AND (next_retry_at IS NULL OR next_retry_at<=NOW() AT TIME ZONE 'UTC')
            ORDER BY created_at, uuid LIMIT 1 FOR UPDATE SKIP LOCKED
        """)
            .fetchone()
        )
        if row is None:
            return False
        event = cls.objects.get_one(filters={"uuid": filters.EQ(row["uuid"])})
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            with utils.savepoint():
                event.dispatch()
        except Exception as error:
            LOG.exception("Failed to dispatch Workspace event %s to Zulip", event.uuid)
            event.attempts += 1
            event.error_description = str(error)
            event.next_retry_at = now + datetime.timedelta(seconds=3)
        else:
            event.processed_at = now
            event.error_description = None
            event.next_retry_at = None
        event.update()
        return True


def enqueue_notifications(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    topic_uuid: sys_uuid.UUID | None,
    notification_mode: str,
    session: sessions.PgSQLSession,
) -> None:
    targets = session.execute(
        "SELECT channel.uuid, account.uuid AS account_uuid, topic.name FROM bridge_zulip_v5_channels AS channel "
        "JOIN m_external_accounts_v2 AS account ON account.uuid=channel.external_account_uuid "
        "JOIN messenger_streams AS stream ON stream.project_id=account.project_id AND stream.uuid=channel.stream_uuid "
        "LEFT JOIN messenger_topics AS topic ON topic.project_id=stream.project_id AND topic.stream_uuid=stream.uuid AND topic.uuid=%s "
        "WHERE account.project_id=%s AND account.owner_user_uuid=%s AND account.provider='zulip' "
        "AND stream.uuid=%s AND stream.source_name='zulip' AND channel.channel_type='channel'",
        (topic_uuid, project_id, user_uuid, stream_uuid),
    ).fetchall()
    for target in targets:
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(target["account_uuid"])}, session=session
        )
        if not account.can_stream():
            continue
        channel = ZulipChannel.objects.get_one(
            filters={"uuid": filters.EQ(target["uuid"])}, session=session
        )
        content = UpdateNotificationsContent(
            channel_uuid=channel.uuid,
            account_revision=account.realtime_revision(),
            topic_name=(
                "" if topic_uuid == sys_uuid.uuid5(stream_uuid, "") else target["name"]
            )
            if topic_uuid is not None
            else None,
            notification_mode=notification_mode,
        )
        content.remember(channel)
        Task(
            project_id=project_id,
            external_account_uuid=account.uuid,
            priority=TASK_PRIORITY_REALTIME_FLAGS,
            content=content,
        ).insert(session=session)
