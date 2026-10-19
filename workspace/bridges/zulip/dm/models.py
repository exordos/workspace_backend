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
import json
import logging
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
from restalchemy.storage.sql import orm
from restalchemy.storage.sql import utils

from workspace.bridges.zulip import client
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
MAX_HISTORY_MESSAGES = 5000
TASK_PRIORITY_IMPORT_HISTORY = 10_000
TASK_PRIORITY_IMPORT_HISTORY_MIN = 10
TASK_PRIORITY_IMPORT_CHANNELS = 50_000
TASK_PRIORITY_IMPORT_USERS = 500_000
TASK_PRIORITY_FULL_BACKFILL = 100_000
TASK_PRIORITY_INITIALIZE_CONNECTION = 600_000
TASK_PRIORITY_REALTIME_MESSAGE = 200_000
TASK_PRIORITY_RECONCILIATION = 400_000
TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS = 700_000
TASK_RETRY_DELAY = datetime.timedelta(minutes=1)
TASK_CHANNEL_SYNC_INTERVAL = datetime.timedelta(minutes=15)
TASK_USER_SYNC_INTERVAL = datetime.timedelta(minutes=15)


class ExternalAccountIdPendingError(Exception):
    pass


class ExternalAccountAlreadyConnectedError(Exception):
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
        messenger_models.WorkspaceStream(
            uuid=self.stream_uuid,
            project_id=self.project_id,
            user_uuid=owner_uuid,
            name=channel.name,
            description=channel.description,
            source_name=messenger_models.StreamSourceName.ZULIP.value,
            source=messenger_models.ZulipSource(),
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
        ).insert()

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
        messenger_models.WorkspaceStreamTopic(
            uuid=self.topic_uuid,
            project_id=self.project_id,
            stream_uuid=self.stream_uuid,
            name=topic.name,
            source_name=messenger_models.TopicSourceName.ZULIP.value,
            source=messenger_models.ZulipSource(),
            created_at=topic.created_at,
        ).insert()

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
        projects = {}
        for project_id, message_uuid in batch:
            projects.setdefault(project_id, []).append(message_uuid)
        counter_scopes = set()
        for project_id, message_uuids in projects.items():
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
                    ZulipMessageCounterChange.message_scopes(project_id, changed_uuids)
                )
                for state in states:
                    state.do()
                counter_scopes.update(
                    ZulipMessageCounterChange.message_scopes(project_id, changed_uuids)
                )
        ZulipMessageCounterChange.mark_many(counter_scopes)
        for model in change_models:
            pending = [
                change
                for changes in batch.values()
                for change in changes
                if type(change) is model
            ]
            if pending:
                model.acknowledge_many(pending)
        return len(batch)

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
            }
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
            if (
                new_generation
                or state.zulip_source_uuid != self.source_uuid
                or state.zulip_flags_updated_at is None
                or state.zulip_flags_updated_at < self.flags_updated_at
            ):
                state.read_at = (
                    (state.read_at or self.flags_updated_at) if self.read else None
                )
                state.starred = self.starred
                state.mentioned = self.mentioned
                if new_generation:
                    state.pinned = False
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
            priority=TASK_PRIORITY_IMPORT_USERS,
            content=ImportUsersContent(),
        ).insert()
        Task(
            project_id=task.project_id,
            external_account_uuid=task.external_account_uuid,
            priority=TASK_PRIORITY_IMPORT_CHANNELS,
            content=ImportChannelsContent(),
        ).insert()
        for index, first_message_id in enumerate(
            reversed(range(1, last_message_id + 1, MAX_HISTORY_MESSAGES))
        ):
            Task(
                project_id=task.project_id,
                external_account_uuid=task.external_account_uuid,
                priority=max(
                    TASK_PRIORITY_IMPORT_HISTORY_MIN,
                    TASK_PRIORITY_IMPORT_HISTORY - index,
                ),
                content=ImportHistoryContent(
                    first_message_id=first_message_id,
                    last_message_id=min(
                        first_message_id + MAX_HISTORY_MESSAGES - 1,
                        last_message_id,
                    ),
                ),
            ).insert()


class ImportUsersContent(AbstractZulipTaskContent):
    KIND = "import_users"

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
            priority=TASK_PRIORITY_IMPORT_USERS,
            not_before_at=(
                datetime.datetime.now(datetime.timezone.utc) + TASK_USER_SYNC_INTERVAL
            ),
            content=ImportUsersContent(),
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
        hostname = account.get_host()
        server_namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        zulip = client.ZulipClient(account)
        try:
            for data in zulip.get_messages(self.first_message_id, self.last_message_id):
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
                if message is not None:
                    contexts.Context().get_session().execute(
                        "SELECT uuid FROM bridge_zulip_v5_messages "
                        "WHERE uuid = %s FOR UPDATE",
                        (message.uuid,),
                    )
                    message = ZulipMessage.objects.get_one(
                        filters={"uuid": filters.EQ(message.uuid)}
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
                    contexts.Context().get_session().execute(
                        "SELECT uuid FROM bridge_zulip_v5_message_contents "
                        "WHERE uuid = %s FOR UPDATE",
                        (message.content.uuid,),
                    )
                    content = MessageContent.objects.get_one(
                        filters={"uuid": filters.EQ(message.content.uuid)}
                    )
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
        if isinstance(error, ExternalAccountIdPendingError):
            task.status = TaskStatus.RETRY.value
            task.next_retry_at = (
                datetime.datetime.now(datetime.timezone.utc) + TASK_RETRY_DELAY
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
        types.Integer(min_value=1), default=1000, read_only=True
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
    types_dynamic.KindModelType(InitializeConnectionContent),
    types_dynamic.KindModelType(FullBackfillContent),
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
