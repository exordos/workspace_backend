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
from restalchemy.storage.sql import utils

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import flags as message_flags
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
TASK_PRIORITY_REALTIME_FLAGS = 550_000
TASK_PRIORITY_RECONCILIATION = 200_000
TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS = 700_000
TASK_RETRY_DELAY = datetime.timedelta(minutes=1)
TASK_CHANNEL_SYNC_INTERVAL = datetime.timedelta(minutes=15)
TASK_USER_SYNC_INTERVAL = datetime.timedelta(minutes=15)


class ExternalAccountIdPendingError(Exception):
    pass


class ExternalAccountAlreadyConnectedError(Exception):
    pass


class SubscriptionChangedError(Exception):
    pass


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
        projects = {}
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
    def reconcile_batch(cls, limit: int) -> int:
        changes = {}
        change_models = (ZulipMessageChange, v2_models.WorkspaceMessageChange)
        for model in change_models:
            for change in model.objects.get_all(limit=limit):
                changes.setdefault(change.get_id(), []).append(change)
        batch = dict(list(changes.items())[:limit])
        if not batch:
            return 0
        projects = {}
        for project_id, message_uuid in batch:
            projects.setdefault(project_id, []).append(message_uuid)
        counter_scopes = set()
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
        unreconciled = set()
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
    def _workspace_messages(cls, project_id, message_uuids):
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
    def _publish_message_events(cls, project_id, before, after, session):
        # Read projections include each recipient's flags and access. Publish
        # only after content, placement and viewer state agree, and retain the
        # previous projection for rows removed by cascading deletes.
        created, updated, deleted = {}, {}, {}
        for key, message in after.items():
            previous = before.get(key)
            if previous is None:
                created.setdefault(message.uuid, []).append(message)
            elif dict(previous) != dict(message):
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
        session.execute(
            "SELECT uuid FROM messenger_messages "
            "WHERE project_id = %s AND uuid = %s AND source_name = 'zulip' "
            "FOR UPDATE",
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
            "ORDER BY content.uuid FOR UPDATE OF content",
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
            if content.workspace_content_updated_at > edited_at:
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
            locked=True,
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
            binding.update()
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
            state.update()


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
        for index, first_message_id in enumerate(
            reversed(
                range(first_message_id, self.last_message_id + 1, HISTORY_BATCH_SIZE)
            )
        ):
            Task(
                project_id=task.project_id,
                external_account_uuid=task.external_account_uuid,
                priority=max(
                    TASK_PRIORITY_IMPORT_HISTORY_MIN,
                    TASK_PRIORITY_IMPORT_HISTORY - index,
                ),
                backfill_uuid=task.backfill_uuid,
                content=ImportHistoryContent(
                    first_message_id=first_message_id,
                    last_message_id=min(
                        first_message_id + HISTORY_BATCH_SIZE - 1,
                        self.last_message_id,
                    ),
                    since=self.since,
                ),
            ).insert()


class ActualizeBackfillContent(FullBackfillContent):
    KIND = "actualize_backfill"

    since = properties.property(types.UTCDateTimeZ(), required=True, read_only=True)

    def execute(self, task: "Task") -> None:
        super().execute(task)
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            realtime_event_uuid=task.realtime_event_uuid or task.backfill_uuid,
            priority=TASK_PRIORITY_IMPORT_HISTORY_MIN,
            content=RefreshMessageFlagsContent(last_message_id=self.last_message_id),
        ).insert()


class ImportUsersContent(AbstractZulipTaskContent):
    KIND = "import_users"

    periodic = properties.property(types.Boolean(), default=True)

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
        for existing in external_models.ExternalAccount.objects.get_all(
            filters={
                "project_id": filters.EQ(task.project_id),
                "provider": filters.EQ("zulip"),
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
            for data in zulip.get_users():
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

    def on_done(self, task: "Task") -> None:
        super().on_done(task)
        if not self.periodic:
            return
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=TASK_PRIORITY_IMPORT_USERS,
            not_before_at=(
                datetime.datetime.now(datetime.timezone.utc) + TASK_USER_SYNC_INTERVAL
            ),
            content=ImportUsersContent(),
        ).insert()


class ImportChannelsContent(AbstractZulipTaskContent):
    KIND = "import_channels"

    periodic = properties.property(types.Boolean(), default=True)

    def execute(self, task: "Task") -> None:
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": filters.EQ(task.external_account_uuid),
                "project_id": filters.EQ(task.project_id),
            },
        )
        hostname = account.get_host()
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            topic_notifications = {}
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

    def on_done(self, task: "Task") -> None:
        super().on_done(task)
        if not self.periodic:
            return
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

    since = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)

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
        hostname = account.get_host()
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            observed_at = datetime.datetime.now(datetime.timezone.utc)
            messages = zulip.get_messages(self.first_message_id, self.last_message_id)
            observed_flags = message_flags.observe_snapshot(
                account, messages, observed_at
            )
            message_reactions.observe_snapshot(account, messages, observed_at)
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
            pending_senders = (
                MessageDelivery.pending_sender_ids(account) if messages else set()
            )
            for data in messages:
                if (
                    self.since is not None
                    and data["timestamp"] < self.since.timestamp()
                ):
                    continue
                delivery = deliveries.get(data["id"])
                if delivery is not None:
                    local = v2_models.WorkspaceMessage.objects.get_one(
                        filters={
                            "project_id": filters.EQ(task.project_id),
                            "uuid": filters.EQ(delivery.message_uuid),
                        }
                    )
                    if local.source_name == "native":
                        delivery.observe(data)
                        continue
                elif data["sender_id"] in pending_senders:
                    raise DeliveryPendingError(
                        "Waiting for the outgoing message identity before import"
                    )
                if data["type"] == "stream":
                    channel = ZulipChannel.objects.get_one_or_none(
                        filters={
                            "external_account_uuid": filters.EQ(account.uuid),
                            "channel_type": filters.EQ(ZulipChannelType.CHANNEL.value),
                            "stream_id": filters.EQ(data["stream_id"]),
                        },
                    )
                    if channel is None:
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
                topic = ZulipTopic.objects.get_one_or_none(
                    filters={"uuid": filters.EQ(topic_uuid)},
                )
                if topic is None:
                    topic = ZulipTopic(
                        uuid=topic_uuid,
                        stream_id=channel.stream_id,
                        name=topic_name,
                        max_id=data["id"],
                    )
                    topic.insert()
                elif data["id"] > topic.max_id or topic.name != topic_name:
                    topic.name = topic_name
                    topic.max_id = max(topic.max_id, data["id"])
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
                    content.update()
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
                    **observed_flags[data["id"]],
                }
                if message is None:
                    ZulipMessage(
                        project_id=task.project_id,
                        external_account_uuid=account.uuid,
                        **values,
                    ).insert()
                else:
                    if content_updated_at <= message.placement_updated_at:
                        for field in ("channel", "topic", "placement_updated_at"):
                            del values[field]
                    message.update_dm(values=values)
                    message.update()
        finally:
            zulip.close()

    def on_error(self, task: "Task", error: Exception) -> None:
        if isinstance(error, DeliveryPendingError):
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
        message_ids = sorted({row["message_id"] for row in batch})
        zulip = client.ZulipClient(account)
        try:
            result = zulip.update_message_flags(message_ids, self.flag, self.value)
            if result.get("ignored_because_not_subscribed_channels"):
                raise client.ZulipApiError(
                    "NOT_SUBSCRIBED",
                    "Cannot mark messages in unsubscribed Zulip channels as unread",
                )
            by_time = {}
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
                    ORDER BY message_id LIMIT 100)
                   UNION
                   (SELECT delivery.zulip_message_id AS message_id
                    FROM bridge_zulip_v5_deliveries AS delivery
                    JOIN bridge_zulip_v5_channels AS channel
                      ON channel.external_account_uuid=%s AND channel.stream_uuid=delivery.stream_uuid
                    WHERE delivery.project_id=%s AND delivery.hostname=%s
                      AND delivery.zulip_message_id > %s AND delivery.zulip_message_id <= %s
                    ORDER BY delivery.zulip_message_id LIMIT 100)
               ) AS known ORDER BY message_id LIMIT 100""",
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
        first = rows[0]["message_id"]
        last = min(rows[-1]["message_id"], first + HISTORY_BATCH_SIZE - 1)
        wanted = {row["message_id"] for row in rows if row["message_id"] <= last}
        zulip = client.ZulipClient(account)
        try:
            observed_at = datetime.datetime.now(datetime.timezone.utc)
            data = zulip.get_messages(first, last)
            message_flags.observe_snapshot(
                account, [row for row in data if row["id"] in wanted], observed_at
            )
            message_reactions.observe_snapshot(
                account, [row for row in data if row["id"] in wanted], observed_at
            )
        finally:
            zulip.close()
        Task(
            uuid=sys_uuid.uuid5(task.uuid, str(last)),
            project_id=task.project_id,
            external_account_uuid=account.uuid,
            priority=task.priority,
            realtime_event_uuid=task.realtime_event_uuid,
            content=RefreshMessageFlagsContent(
                after_message_id=last, last_message_id=self.last_message_id
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
            error, (DeliveryPendingError, requests.Timeout, requests.ConnectionError)
        ) or (
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


class SendMessageContent(OutboundMessageContent):
    KIND = "send_message"

    def prepare(self, task: "Task") -> bool:
        delivery = self.delivery(task)
        now = datetime.datetime.now(datetime.timezone.utc)
        row = (
            contexts.Context()
            .get_session()
            .execute(
                "SELECT status, lease_expires_at FROM bridge_zulip_v5_tasks WHERE uuid=%s FOR UPDATE",
                (task.uuid,),
            )
            .fetchone()
        )
        if row["status"] not in ("new", "waiting", "retry") or (
            row["lease_expires_at"] is not None
            and row["lease_expires_at"] > now.replace(tzinfo=None)
        ):
            return False
        self._submit = not delivery.sending and delivery.zulip_message_id is None
        if self._submit:
            subscription = RealtimeSubscription.for_account(delivery.account())
            if subscription is None:
                raise DeliveryPendingError(
                    "Waiting for the sender's realtime subscription"
                )
            delivery.sending = True
            delivery.update()
        task.lease_owner = sys_uuid.uuid4()
        task.lease_expires_at = now + datetime.timedelta(minutes=2)
        task.update()
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
                self._submit = False
                delivery.zulip_message_id = zulip.send_message(
                    delivery.recipient["to"],
                    self.content,
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
            task.next_retry_at = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(seconds=3)


class EditMessageContent(OutboundMessageContent):
    KIND = "edit_message"

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
        zulip = client.ZulipClient(delivery.account())
        try:
            zulip.edit_message(delivery.zulip_message_id, self.content)
            (data,) = zulip.get_messages(
                delivery.zulip_message_id, delivery.zulip_message_id
            )
            delivery.observe(data)
        finally:
            zulip.close()


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
        message_flags.FlagChange.reconcile_batch(self.batch_size)
        message_reactions.ReactionChange.reconcile_batch(self.batch_size)
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
    types_dynamic.KindModelType(UpdateReactionContent),
    types_dynamic.KindModelType(UpdateMessageFlagsContent),
    types_dynamic.KindModelType(RefreshMessageFlagsContent),
    types_dynamic.KindModelType(SendMessageContent),
    types_dynamic.KindModelType(EditMessageContent),
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
            contexts.Context().get_session().execute(
                "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s AND project_id=%s FOR UPDATE",
                (self.external_account_uuid, self.project_id),
            )
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
            "account_revision": account.realtime_revision(),
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
            or account.realtime_revision() != subscription.account_revision
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
                for message_id in sorted(set(message_ids))
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
                    for message_id in sorted(set(message_ids) - known - {0})
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
                contents.append(ImportUsersContent(periodic=False))
            remote = message_reactions.message_for(account, self.payload["message_id"])
            if remote["canonical_message_uuid"] is None:
                contents.append(
                    ImportHistoryContent(
                        first_message_id=self.payload["message_id"],
                        last_message_id=self.payload["message_id"],
                    )
                )
        elif kind == "realm_user":
            contents = [ImportUsersContent(periodic=False)]
            priority = TASK_PRIORITY_IMPORT_USERS
        elif kind in ("subscription", "stream", "user_topic"):
            contents = [ImportChannelsContent(periodic=False)]
            priority = TASK_PRIORITY_IMPORT_CHANNELS
        for index, content in enumerate(contents):
            Task(
                uuid=sys_uuid.uuid5(self.uuid, f"task:{index}"),
                project_id=self.project_id,
                external_account_uuid=self.external_account_uuid,
                priority=priority,
                backfill_uuid=backfill_uuid,
                realtime_event_uuid=self.uuid,
                content=content,
            ).insert()

    @classmethod
    def dispatch_one(cls) -> bool:
        row = (
            contexts.Context()
            .get_session()
            .execute(
                """
            SELECT uuid FROM bridge_zulip_v5_events
            WHERE processed_at IS NULL AND (next_retry_at IS NULL OR next_retry_at <= NOW() AT TIME ZONE 'UTC')
            ORDER BY created_at, event_id NULLS FIRST, uuid LIMIT 1 FOR UPDATE SKIP LOCKED
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
    def pending_sender_ids(cls, account: external_models.ExternalAccount) -> set[int]:
        return {
            int(delivery.external_account_id.rsplit(":", 1)[1])
            for delivery in cls.objects.get_all(
                filters={
                    "project_id": filters.EQ(account.project_id),
                    "hostname": filters.EQ(account.get_host()),
                    "zulip_message_id": filters.Is(None),
                    "sending": filters.EQ(True),
                }
            )
        }

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

    def observe(self, data: dict) -> None:
        account = self.account(sending=False)
        if data["sender_id"] != int(account.external_account_id.rsplit(":", 1)[1]):
            raise SubscriptionChangedError()
        if self.zulip_message_id is not None and self.zulip_message_id != data["id"]:
            raise SubscriptionChangedError()
        self.zulip_message_id = data["id"]
        self.delivered_at = datetime.datetime.fromtimestamp(
            data["timestamp"], datetime.timezone.utc
        )
        self.update()
        session = contexts.Context().get_session()
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={
                "project_id": filters.EQ(self.project_id),
                "uuid": filters.EQ(self.message_uuid),
            }
        )
        if message.source_name != "native":
            return
        edited_at = datetime.datetime.fromtimestamp(
            data.get("last_edit_timestamp", data["timestamp"]), datetime.timezone.utc
        )
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
        replace_content = message.payload.content == data["content"] or (
            not pending_edits
            and (
                message.content_updated_at is None
                or edited_at >= message.content_updated_at
            )
        )
        changed = message.created_at != self.delivered_at or (
            replace_content
            and (
                message.payload.content != data["content"]
                or message.content_updated_at != edited_at
            )
        )
        if not changed:
            return
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
                json.dumps({"kind": "markdown", "content": data["content"]}),
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
