# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import enum
import typing

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm


class ExternalProvider(str, enum.Enum):
    ZULIP = "zulip"


EXTERNAL_SELECTION_MODES = ("explicit", "all")
EXTERNAL_HISTORY_DEPTHS = ("new", "7_days", "30_days", "90_days", "all")


class ZulipAccountCreateSettings(types_dynamic.AbstractKindModel):
    KIND = ExternalProvider.ZULIP.value

    server_url = properties.property(
        types.String(min_length=1, max_length=2048),
        required=True,
    )
    email = properties.property(
        types.String(min_length=3, max_length=320),
        required=True,
    )
    api_key = properties.property(
        types.String(min_length=1, max_length=4096),
        required=True,
    )
    selection_mode = properties.property(
        types.Enum(EXTERNAL_SELECTION_MODES),
        default="explicit",
    )
    history_depth = properties.property(
        types.Enum(EXTERNAL_HISTORY_DEPTHS),
        default="30_days",
    )
    default_project_id = properties.property(types.UUID(), required=True)


class ZulipAccountSettings(types_dynamic.AbstractKindModel):
    KIND = ExternalProvider.ZULIP.value

    server_url = properties.property(
        types.String(min_length=1, max_length=2048),
        required=True,
    )
    email = properties.property(
        types.String(min_length=3, max_length=320),
        required=True,
    )
    selection_mode = properties.property(
        types.Enum(EXTERNAL_SELECTION_MODES),
        default="explicit",
    )
    history_depth = properties.property(
        types.Enum(EXTERNAL_HISTORY_DEPTHS),
        default="30_days",
    )
    default_project_id = properties.property(types.UUID(), required=True)


class ZulipAccountUpdateSettings(types_dynamic.AbstractKindModel):
    KIND = ExternalProvider.ZULIP.value

    selection_mode = properties.property(
        types.Enum(EXTERNAL_SELECTION_MODES),
        required=True,
    )
    history_depth = properties.property(
        types.Enum(EXTERNAL_HISTORY_DEPTHS),
        required=True,
    )
    default_project_id = properties.property(types.UUID(), required=True)


class ZulipAccountReconnectSettings(types_dynamic.AbstractKindModel):
    KIND = ExternalProvider.ZULIP.value

    server_url = properties.property(
        types.String(min_length=1, max_length=2048),
        required=True,
    )
    email = properties.property(
        types.String(min_length=3, max_length=320),
        required=True,
    )
    api_key = properties.property(
        types.String(min_length=1, max_length=4096),
        required=True,
    )


EXTERNAL_ACCOUNT_CREATE_SETTINGS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipAccountCreateSettings),
)
EXTERNAL_ACCOUNT_SETTINGS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipAccountSettings),
)
EXTERNAL_ACCOUNT_UPDATE_SETTINGS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipAccountUpdateSettings),
)
EXTERNAL_ACCOUNT_RECONNECT_SETTINGS_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(ZulipAccountReconnectSettings),
)


class ExternalAccountStatus(str, enum.Enum):
    CONNECTING = "connecting"
    BACKFILL = "backfill"
    LIVE = "live"
    DEGRADED = "degraded"
    AUTH_REQUIRED = "auth_required"
    DISCONNECTED = "disconnected"
    SUSPENDED = "suspended"


class ExternalChatStatus(str, enum.Enum):
    AVAILABLE = "available"
    SYNCING = "syncing"
    LIVE = "live"
    DEGRADED = "degraded"
    DESELECTED = "deselected"


class ExternalOperationStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"
    DISCARDED = "discarded"


class ExternalReconciliationState(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    DELAYED_CHECK = "delayed_check"
    COMMITTED_MATCH = "committed_match"
    AUTOMATIC_RESEND_QUEUED = "automatic_resend_queued"
    MANUAL_REQUIRED = "manual_required"


class ExternalReconciliationReason(str, enum.Enum):
    PROVIDER_HISTORY_UNAVAILABLE = "provider_history_unavailable"
    NO_MATCH_AFTER_AUTO_RESEND = "no_match_after_auto_resend"
    UNSAFE_PROVIDER_STATE = "unsafe_provider_state"


class ExternalBridgeInstanceStatus(str, enum.Enum):
    ENROLLING = "enrolling"
    ACTIVE = "active"
    DEGRADED = "degraded"
    INCOMPATIBLE = "incompatible"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class RevisionTimestampModel(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
):
    revision = properties.property(
        types.Integer(min_value=1),
        default=1,
        read_only=True,
    )


class ExternalAccount(RevisionTimestampModel, orm.SQLStorableMixin):
    __tablename__ = "m_external_accounts_v2"

    owner_user_uuid = properties.property(types.UUID(), required=True)
    provider = properties.property(types.String(max_length=64), default="zulip")
    settings = properties.property(types.Dict(), required=True)
    credential_present = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )
    status = properties.property(
        types.Enum([value.value for value in ExternalAccountStatus]),
        default=ExternalAccountStatus.CONNECTING.value,
        read_only=True,
    )
    live_ready = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )
    capabilities = properties.property(
        types.Dict(),
        default=dict,
        read_only=True,
    )
    safe_error = properties.property(
        types.AllowNone(types.String(max_length=1024)),
        default=None,
        read_only=True,
    )
    desired_generation = properties.property(
        types.Integer(min_value=1),
        default=1,
        read_only=True,
    )
    applied_generation = properties.property(
        types.Integer(min_value=0),
        default=0,
        read_only=True,
    )
    last_progress_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
        read_only=True,
    )


class ExternalCredential(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "m_external_credentials_v2"

    external_account_uuid = properties.property(types.UUID(), required=True)
    key_version = properties.property(types.Integer(min_value=1), required=True)
    envelope = properties.property(types.Dict(), required=True)


class ExternalChat(RevisionTimestampModel, orm.SQLStorableMixin):
    __tablename__ = "m_external_chats_v2"

    external_account_uuid = properties.property(types.UUID(), required=True)
    owner_user_uuid = properties.property(types.UUID(), required=True)
    provider = properties.property(
        types.Enum([value.value for value in ExternalProvider]),
        required=True,
    )
    provider_chat_id = properties.property(
        types.String(min_length=1, max_length=512),
        required=True,
    )
    source = properties.property(types.Dict(), required=True)
    display_name = properties.property(
        types.String(min_length=1, max_length=512),
        required=True,
    )
    selected = properties.property(types.Boolean(), default=False)
    project_id = properties.property(types.AllowNone(types.UUID()), default=None)
    history_depth = properties.property(
        types.Enum(EXTERNAL_HISTORY_DEPTHS),
        default="30_days",
        read_only=True,
    )
    projection_stream_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
        read_only=True,
    )
    status = properties.property(
        types.Enum([value.value for value in ExternalChatStatus]),
        default=ExternalChatStatus.AVAILABLE.value,
        read_only=True,
    )
    capabilities = properties.property(types.Dict(), default=dict, read_only=True)
    safe_error = properties.property(
        types.AllowNone(types.String(max_length=1024)),
        default=None,
        read_only=True,
    )
    transition_pending = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )


class ExternalOperation(RevisionTimestampModel, orm.SQLStorableMixin):
    __tablename__ = "m_external_operations_v2"

    external_account_uuid = properties.property(types.UUID(), required=True)
    owner_user_uuid = properties.property(types.UUID(), required=True)
    action = properties.property(
        types.String(min_length=1, max_length=128), required=True
    )
    target_type = properties.property(
        types.String(min_length=1, max_length=128), required=True
    )
    target_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    details = properties.property(types.Dict(), default=dict, read_only=True)
    attempt_history = properties.property(types.List(), default=list, read_only=True)
    status = properties.property(
        types.Enum([value.value for value in ExternalOperationStatus]),
        default=ExternalOperationStatus.QUEUED.value,
        read_only=True,
    )
    attempt = properties.property(types.Integer(min_value=0), default=0, read_only=True)
    safe_error = properties.property(
        types.AllowNone(types.String(max_length=1024)),
        default=None,
        read_only=True,
    )
    can_retry = properties.property(types.Boolean(), default=False, read_only=True)
    can_discard = properties.property(types.Boolean(), default=False, read_only=True)
    duplicate_risk = properties.property(types.Boolean(), default=False, read_only=True)
    retry_requires_confirmation = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )
    original_url = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
        read_only=True,
    )
    reconciliation_state = properties.property(
        types.Enum([value.value for value in ExternalReconciliationState]),
        default=ExternalReconciliationState.NOT_REQUIRED.value,
        read_only=True,
    )
    reconciliation_reason = properties.property(
        types.AllowNone(
            types.Enum([value.value for value in ExternalReconciliationReason])
        ),
        default=None,
        read_only=True,
    )
    reconciliation_evidence = properties.property(
        types.Dict(),
        default=dict,
        read_only=True,
    )


class ExternalBridgeInstance(RevisionTimestampModel, orm.SQLStorableMixin):
    __tablename__ = "m_external_bridge_instances_v2"

    provider = properties.property(
        types.Enum([value.value for value in ExternalProvider]),
        required=True,
    )
    identity_generation = properties.property(types.Integer(min_value=1), default=1)
    status = properties.property(
        types.Enum([value.value for value in ExternalBridgeInstanceStatus]),
        default=ExternalBridgeInstanceStatus.ENROLLING.value,
        read_only=True,
    )
    capabilities = properties.property(types.Dict(), default=dict, read_only=True)
    last_heartbeat_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
        read_only=True,
    )
    certificate_not_after = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
        read_only=True,
    )
    safe_error = properties.property(
        types.AllowNone(types.String(max_length=1024)),
        default=None,
        read_only=True,
    )


class ExternalProviderPolicy(RevisionTimestampModel, orm.SQLStorableMixin):
    __tablename__ = "m_external_provider_policies_v1"

    provider = properties.property(types.String(max_length=64), default="zulip")
    enabled = properties.property(types.Boolean(), default=False)
    emergency_suspended = properties.property(
        types.Boolean(), default=False, read_only=True
    )
    limits = properties.property(types.Dict(), default=dict)
    custom_ca_bundle = properties.property(
        types.AllowNone(types.Dict()), default=None, read_only=True
    )
    custom_ca_certificates = properties.property(
        types.AllowNone(types.Dict()), default=None
    )

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"provider": cls.properties.properties["provider"]}


class ExternalProviderHealth(models.Model):
    provider = properties.property(types.String(max_length=64), default="zulip")
    status = properties.property(types.String(), required=True, read_only=True)
    account_counts = properties.property(types.Dict(), default=dict, read_only=True)
    chat_counts = properties.property(types.Dict(), default=dict, read_only=True)
    bridge_counts = properties.property(types.Dict(), default=dict, read_only=True)
    operation_counts = properties.property(types.Dict(), default=dict, read_only=True)
    metrics = properties.property(types.Dict(), default=dict, read_only=True)
    updated_at = properties.property(types.UTCDateTimeZ(), read_only=True)

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"provider": cls.properties.properties["provider"]}
