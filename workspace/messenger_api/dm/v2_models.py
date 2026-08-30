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

import typing

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

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
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_messages"

    author_uuid = properties.property(types.UUID(), required=True, read_only=True)
    payload = properties.property(
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE,
        required=True,
    )
    source_name = properties.property(
        types.Enum([source.value for source in base.SourceName]),
        default=base.SourceName.NATIVE.value,
    )
    source = properties.property(types.Dict(), default=base.native_source)
    provider_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    provider_external_id = properties.property(
        types.AllowNone(types.String(max_length=2048)), default=None, read_only=True
    )
    provider_realm_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True
    )
    provider_message_id = properties.property(
        types.AllowNone(types.String(max_length=32)), default=None, read_only=True
    )
    provider = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery = properties.property(types.AllowNone(types.Dict()), default=None)
    reactions = properties.property(types.Dict(), default=dict, read_only=True)
    reaction_users = properties.property(types.Dict(), default=dict, read_only=True)
    ingest_sequence = properties.property(
        types.Integer(min_value=1), required=False, read_only=True
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
    membership_generation = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    relation_role = properties.property(types.String(max_length=64), required=True)
    visibility = properties.property(types.String(max_length=64), required=True)
    permissions = properties.property(types.Dict(), required=True)


class WorkspaceUserMessageState(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_user_message_states"

    placement_uuid = properties.property(types.UUID(), required=True, read_only=True)
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    membership_generation = properties.property(
        types.Integer(min_value=1), required=True, read_only=True
    )
    read_at = properties.property(types.AllowNone(types.UTCDateTimeZ()), default=None)
    mentioned = properties.property(types.Boolean(), default=False)
    starred = properties.property(types.Boolean(), default=False)
    pinned = properties.property(types.Boolean(), default=False)


class WorkspaceUserMessage(
    base.WorkspaceUserMessageBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_messages_v1"

    binding_uuid = properties.property(types.UUID(), required=True, read_only=True)
    canonical_message_uuid = properties.property(
        types.UUID(), required=True, read_only=True
    )
    provider = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    delivery = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"binding_uuid": cls.properties.properties["binding_uuid"]}


class WorkspaceStream(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_streams"

    owner_uuid = properties.property(types.UUID(), required=True, read_only=True)
    source_name = properties.property(
        types.Enum([source.value for source in base.SourceName]),
        default=base.SourceName.NATIVE.value,
    )
    source = properties.property(types.Dict(), default=base.native_source)
    invite_only = properties.property(types.Boolean(), default=False)
    announce = properties.property(types.Boolean(), default=False)
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
    provider = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery = properties.property(types.AllowNone(types.Dict()), default=None)


class WorkspaceStreamBinding(
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
    last_message_uuid = properties.property(types.AllowNone(types.UUID()), default=None)


class WorkspaceUserStream(
    base.WorkspaceUserStreamBase,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_streams_v1"

    provider = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    delivery = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)


class WorkspaceStreamBindingView(
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
    base.WorkspaceSourceBase,
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
    provider = properties.property(types.AllowNone(types.Dict()), default=None)
    delivery = properties.property(types.AllowNone(types.Dict()), default=None)
    is_done = properties.property(types.Boolean(), default=False)
    version = properties.property(types.Integer(min_value=0), default=0, read_only=True)


class WorkspaceUserTopicBinding(
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
    last_message_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    summary_has_new_messages = properties.property(
        types.AllowNone(types.Boolean()), default=None
    )


class WorkspaceUserTopic(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    base.WorkspaceSourceBase,
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
    provider = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    delivery = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    deleted_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()), default=None, read_only=True
    )
    visible = properties.property(types.Boolean(), default=True, read_only=True)


class WorkspaceMessageReactionFact(
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
    provider = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    delivery = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
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
