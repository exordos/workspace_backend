# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Messenger v2 canonical models and public read projections.

The v2 write model separates canonical content, placement, access, and
viewer state.  Public models deliberately retain the Workspace v1 JSON shape;
the public message UUID is the placement UUID while the canonical UUID stays
internal.
"""

import datetime
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.common import models as common_models
from workspace.common import types as common_types
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.dm import base
from workspace.messenger_api.dm import message_payloads


TASK_KINDS = (
    "fanout",
    "content_mentions",
    "reaction_snapshot",
    "read_counters",
    "folder_projection",
    "delivery_snapshot_event",
    "topic_state_projection",
    "topic_membership_policy_rebuild",
)


class WorkspaceMutationRequest(
    models.ModelWithUUID, models.ModelWithProject, orm.SQLStorableMixin
):
    """Owner-scoped progress; applied means state and topic counters committed."""

    __tablename__ = "messenger_mutation_requests"
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    status = properties.property(
        types.Enum(["pending", "applied", "rejected"]), required=True, read_only=True
    )
    command_count = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    applied_count = properties.property(
        types.Integer(min_value=0), required=True, read_only=True
    )
    rejected_count = properties.property(
        types.Integer(min_value=0), required=True, read_only=True
    )
    created_at = properties.property(
        common_types.UTCDateTimeWithOffset(), required=True, read_only=True
    )
    finished_at = properties.property(
        types.AllowNone(common_types.UTCDateTimeWithOffset()),
        default=None,
        read_only=True,
    )


class WorkspaceFolder(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_folders"

    title = properties.property(
        types.String(min_length=1, max_length=64), required=True
    )
    background_color_value = properties.property(
        types.AllowNone(types.Integer(min_value=0, max_value=2**32 - 1)),
        default=None,
    )
    system_type = properties.property(
        types.AllowNone(types.Enum(base.FOLDER_SYSTEM_TYPES)),
        default=base.FOLDER_SYSTEM_TYPE_CREATED,
        read_only=True,
    )


class WorkspaceUserFolderBinding(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_user_folder_bindings"

    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    folder_uuid = properties.property(types.UUID(), required=True, read_only=True)
    rule = properties.property(
        types.String(max_length=32), required=True, read_only=True
    )
    unread_count = properties.property(types.Integer(min_value=0), default=0)
    mention_count = properties.property(types.Integer(min_value=0), default=0)
    folder_items_snapshot = properties.property(types.List(), default=list)
    snapshot_version = properties.property(
        types.Integer(min_value=0), default=0, read_only=True
    )
    snapshot_updated_at = properties.property(types.UTCDateTimeZ(), required=True)


class WorkspaceFolderItem(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_folder_items"

    folder_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    order_index = properties.property(
        types.AllowNone(types.Integer(max_value=2**31 - 1)), default=None
    )
    pinned_at = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)
    chat_type = properties.property(
        types.Enum(["stream", "group", "private"]), required=True
    )
    automatic = properties.property(types.Boolean(), default=False, read_only=True)


class WorkspaceUserFolder(base.WorkspaceUserFolderBase, orm.SQLStorableMixin):
    __tablename__ = "messenger_api_user_folders_v1"


class WorkspaceUserFolderItem(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_folder_items_v1"

    folder_uuid = properties.property(types.UUID(), required=True)
    stream_uuid = properties.property(types.UUID(), required=True)
    order_index = properties.property(
        types.AllowNone(types.Integer(max_value=2**31 - 1)), default=None
    )
    pinned_at = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)
    chat_type = properties.property(
        types.Enum(["stream", "group", "private"]), required=True
    )
    unread_count = properties.property(types.Integer(min_value=0), default=0)
    active_unread_count = properties.property(types.Integer(min_value=0), default=0)
    passive_unread_count = properties.property(types.Integer(min_value=0), default=0)


class WorkspaceMessage(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_messages"

    author_uuid = properties.property(types.UUID(), required=True, read_only=True)
    content_uuid = properties.property(types.UUID(), required=True)
    content_revision = properties.property(
        types.Integer(min_value=1), default=1, read_only=True
    )
    source_name = properties.property(
        types.Enum([source.value for source in base.SourceName]),
        default=base.SourceName.NATIVE.value,
    )
    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    provider_identity = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=255)), default=None
    )
    content_updated_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    reactions = properties.property(types.Dict(), default=dict, read_only=True)
    reaction_users = properties.property(types.Dict(), default=dict, read_only=True)
    ingest_sequence = properties.property(
        types.Integer(min_value=1), required=False, read_only=True
    )
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )


class WorkspaceMessageChange(
    models.ModelWithID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_message_changes"

    project_id = properties.property(
        types.UUID(), required=True, read_only=True, id_property=True
    )
    message_uuid = properties.property(
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

    def get_id(self) -> tuple[sys_uuid.UUID, sys_uuid.UUID]:
        return self.project_id, self.message_uuid

    def acknowledge(self) -> None:
        self.acknowledge_many([self])

    @classmethod
    def acknowledge_many(
        cls, changes: typing.Sequence["WorkspaceMessageChange"]
    ) -> None:
        contexts.Context().get_session().execute(
            f"DELETE FROM {cls.__tablename__} AS pending "
            "USING unnest(%s::uuid[], %s::uuid[], %s::bigint[]) "
            "AS checked(project_id, message_uuid, revision) "
            "WHERE pending.project_id = checked.project_id "
            "AND pending.message_uuid = checked.message_uuid "
            "AND pending.revision = checked.revision",
            (
                [change.project_id for change in changes],
                [change.message_uuid for change in changes],
                [change.revision for change in changes],
            ),
        )


class WorkspaceMessagePlacement(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_message_placements"

    message_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_uuid = properties.property(types.UUID(), required=True, read_only=True)


class WorkspaceUserMessageBinding(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_user_message_bindings"

    placement_uuid = properties.property(types.UUID(), required=True, read_only=True)
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)


class WorkspaceUserMessage(
    base.WorkspaceUserMessageBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_messages_v1"

    membership_generation = properties.property(
        types.Integer(min_value=1), read_only=True
    )
    binding_uuid = properties.property(types.UUID(), required=True, read_only=True)
    canonical_message_uuid = properties.property(
        types.UUID(), required=True, read_only=True
    )
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"binding_uuid": cls.properties.properties["binding_uuid"]}

    def validate_payload_update(
        self, payload: typing.Mapping[str, typing.Any], session: typing.Any
    ) -> None:
        self.validate_key_announcement_update(payload["kind"])
        if payload["kind"] != message_payloads.E2EEPayload.KIND:
            return
        streams = session.execute(
            """
            SELECT DISTINCT stream.source_name
            FROM messenger_message_placements AS placement
            JOIN messenger_streams AS stream
              ON stream.project_id = placement.project_id
             AND stream.uuid = placement.stream_uuid
            WHERE placement.project_id = %s AND placement.message_uuid = %s
            """,
            (self.project_id, self.canonical_message_uuid),
        ).fetchall()
        for stream in streams:
            base.WorkspaceUserStreamBase.validate_message_kind(
                stream["source_name"], payload["kind"]
            )


class WorkspaceStream(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    common_models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_streams"

    owner_uuid = properties.property(types.UUID(), required=True, read_only=True)
    encryption = properties.property(types.Boolean(), default=False)
    current_encryption_key_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    source_name = properties.property(
        types.Enum([source.value for source in base.StreamSourceName]),
        default=base.SourceName.NATIVE.value,
    )
    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    provider_identity = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=255)), default=None
    )
    invite_only = properties.property(types.Boolean(), default=False)
    announce = properties.property(types.Boolean(), default=False)
    announcement_only = properties.property(types.Boolean(), default=False)
    direct_user_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    private = properties.property(types.Boolean(), default=False)
    is_archived = properties.property(types.Boolean(), default=False)
    private_index = properties.property(
        types.AllowNone(types.String(max_length=73)), default=None
    )
    color = properties.property(base.Color(), default=base.random_color)
    default_topic_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )

    def validate(self) -> None:
        super().validate()
        base.WorkspaceStreamBase.validate_private_identity(
            self.private, self.direct_user_uuid
        )
        if self.announcement_only and self.encryption:
            raise ra_exceptions.ValidationErrorException()


class WorkspaceStreamBinding(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_stream_bindings"

    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    who_uuid = properties.property(types.UUID(), required=True, read_only=True)
    active = properties.property(types.Boolean(), default=True, read_only=True)
    membership_generation = properties.property(
        types.Integer(min_value=1), default=1, read_only=True
    )
    role = properties.property(
        types.Enum([role.value for role in base.WorkspaceStreamRole]),
        default=base.WorkspaceStreamRole.MEMBER.value,
    )
    notification_mode = properties.property(
        types.Enum([mode.value for mode in base.WorkspaceStreamNotificationMode]),
        default=base.WorkspaceStreamNotificationMode.ALL_MESSAGES.value,
    )
    notification_updated_at = properties.property(types.UTCDateTimeZ(), required=True)
    unread_count = properties.property(types.Integer(min_value=0), default=0)
    active_unread_count = properties.property(types.Integer(min_value=0), default=0)
    passive_unread_count = properties.property(types.Integer(min_value=0), default=0)


class WorkspaceUserStream(
    base.WorkspaceUserStreamBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_streams_v1"

    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)


class WorkspaceStreamBindingView(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_stream_bindings_v1"

    viewer_user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    who_uuid = properties.property(types.UUID(), required=True, read_only=True)
    role = properties.property(types.String(max_length=32), required=True)
    notification_mode = properties.property(types.String(max_length=32), required=True)
    notification_updated_at = properties.property(types.UTCDateTimeZ(), required=True)
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)


class WorkspaceStreamTopic(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    base.WorkspaceTopicSourceBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_topics"

    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    name = properties.property(types.String(max_length=128), required=True)
    color = properties.property(base.Color(), default=base.random_color)
    summary = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=4096)), default=None
    )
    summary_last_message_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None
    )
    summary_enabled = properties.property(types.Boolean(), default=True)
    summary_system_prompt = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=16384)), default=None
    )
    summary_reasoning_effort = properties.property(
        types.AllowNone(types.String(max_length=16)), default=None
    )
    is_done = properties.property(types.Boolean(), default=False)
    version = properties.property(types.Integer(min_value=0), default=0, read_only=True)


class WorkspaceUserTopicBinding(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_user_topic_bindings"

    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_uuid = properties.property(types.UUID(), required=True, read_only=True)
    notification_mode = properties.property(
        types.Enum([mode.value for mode in base.WorkspaceTopicNotificationMode]),
        default=base.WorkspaceTopicNotificationMode.DEFAULT.value,
    )
    unread_count = properties.property(types.Integer(min_value=0), default=0)
    active_unread_count = properties.property(types.Integer(min_value=0), default=0)
    passive_unread_count = properties.property(types.Integer(min_value=0), default=0)
    summary_has_new_messages = properties.property(
        types.AllowNone(types.Boolean()), default=None
    )


class WorkspaceUserTopic(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    base.WorkspaceTopicSourceBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_topics_v1"

    name = properties.property(types.String(max_length=128), required=True)
    stream_uuid = properties.property(types.UUID(), required=True)
    color = properties.property(base.Color(), default=base.random_color)
    last_message_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    unread_count = properties.property(types.Integer(min_value=0), default=0)
    active_unread_count = properties.property(types.Integer(min_value=0), default=0)
    passive_unread_count = properties.property(types.Integer(min_value=0), default=0)
    is_default = properties.property(types.Boolean(), default=False)
    is_done = properties.property(types.Boolean(), default=False)
    notification_mode = properties.property(
        types.Enum([mode.value for mode in base.WorkspaceTopicNotificationMode]),
        default=base.WorkspaceTopicNotificationMode.DEFAULT.value,
    )
    summary = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=4096)),
        default=None,
        read_only=True,
    )
    summary_last_message_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    summary_has_new_messages = properties.property(
        types.AllowNone(types.Boolean()), default=None, read_only=True
    )
    summary_enabled = properties.property(types.Boolean(), default=True, read_only=True)
    summary_system_prompt = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=16384)),
        default=None,
        read_only=True,
    )
    summary_reasoning_effort = properties.property(
        types.AllowNone(types.String(max_length=16)), default=None, read_only=True
    )
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)

    def validate_summary_prompt_update(self, session: typing.Any) -> None:
        binding = WorkspaceStreamBinding.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self.project_id),
                "stream_uuid": dm_filters.EQ(self.stream_uuid),
                "user_uuid": dm_filters.EQ(self.user_uuid),
                "active": dm_filters.EQ(True),
            },
            session=session,
        )
        if binding.role not in {
            base.WorkspaceStreamRole.OWNER.value,
            base.WorkspaceStreamRole.ADMINISTRATOR.value,
        }:
            raise messenger_exceptions.TopicSummaryPromptForbiddenError()


class WorkspaceMessageReactionFact(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_message_reaction_facts"

    canonical_message_uuid = properties.property(
        types.UUID(), required=True, read_only=True
    )
    placement_uuid = properties.property(types.UUID(), required=True, read_only=True)
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    emoji_name = properties.property(types.String(max_length=128), required=True)


class WorkspaceMessageReactionView(
    base.WorkspaceProviderBase,
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_message_reactions_v1"

    viewer_user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    message_uuid = properties.property(types.UUID(), required=True)
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    emoji_name = properties.property(types.String(max_length=128), required=True)
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)


class WorkspaceDomainOutboxEvent(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_domain_outbox_events"

    event_kind = properties.property(types.Enum(TASK_KINDS), required=True)
    scope_kind = properties.property(types.String(max_length=64), required=True)
    scope_key = properties.property(types.String(max_length=512), required=True)
    payload = properties.property(types.Dict(), required=True)


class WorkspaceProjectionTask(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_projection_tasks"

    outbox_event_uuid = properties.property(types.UUID(), required=True, read_only=True)
    task_kind = properties.property(types.Enum(TASK_KINDS), required=True)
    scope_kind = properties.property(types.String(max_length=64), required=True)
    scope_key = properties.property(types.String(max_length=512), required=True)
    payload = properties.property(types.Dict(), required=True)
    execution_stats = properties.property(types.Dict(), default=dict, read_only=True)
    status = properties.property(types.String(max_length=32), default="pending")
    lease_owner = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )
    fencing_token = properties.property(types.Integer(min_value=0), default=0)
    lease_expires_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    attempts = properties.property(types.Integer(min_value=0), default=0)
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    last_error = properties.property(
        types.AllowNone(types.String(max_length=4096)), default=None
    )


class WorkspaceProjectionScopeLease(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_projection_scope_leases"

    scope_kind = properties.property(types.String(max_length=64), required=True)
    scope_key = properties.property(types.String(max_length=512), required=True)
    owner = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )
    fencing_token = properties.property(types.Integer(min_value=0), default=0)
    lease_expires_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )


class WorkspaceFanoutRoot(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_fanout_roots"

    outbox_event_uuid = properties.property(types.UUID(), required=True)
    placement_uuid = properties.property(types.UUID(), required=True)
    next_user_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    processed_count = properties.property(types.Integer(min_value=0), default=0)
    status = properties.property(types.String(max_length=32), default="pending")


class WorkspaceFanoutBatchTask(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_fanout_batch_tasks"

    fanout_root_uuid = properties.property(types.UUID(), required=True)
    batch_no = properties.property(types.Integer(min_value=0), required=True)
    start_user_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    end_user_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    batch_size = properties.property(types.Integer(min_value=1, max_value=5000))
    status = properties.property(types.String(max_length=32), default="pending")
    lease_owner = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )
    fencing_token = properties.property(types.Integer(min_value=0), default=0)
    lease_expires_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    attempts = properties.property(types.Integer(min_value=0), default=0)
    next_retry_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None
    )
    last_error = properties.property(
        types.AllowNone(types.String(max_length=4096)), default=None
    )
