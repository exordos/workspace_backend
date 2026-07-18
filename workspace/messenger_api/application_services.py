# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Session-oriented Messenger application services.

The caller owns the RESTAlchemy request or worker transaction.  Services in
this module deliberately accept that session instead of opening another one.
"""

import dataclasses
import typing
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters

from workspace.external_bridge_control import sql_state
from workspace.messenger_api import credential_crypto
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models


@dataclasses.dataclass(frozen=True)
class ExternalAccountActor:
    user_uuid: sys_uuid.UUID
    project_id: sys_uuid.UUID | None


def normalize_settings(
    selector: typing.Any,
    value: object,
) -> dict[str, typing.Any]:
    settings = selector.from_simple_type(value)
    return selector.to_simple_type(settings)


def external_credential(
    account: external_models.ExternalAccount,
    session: typing.Any,
) -> external_models.ExternalCredential:
    return external_models.ExternalCredential.objects.get_one(
        filters={"external_account_uuid": dm_filters.EQ(account.uuid)},
        session=session,
    )


def desired_external_account_resource(
    account: external_models.ExternalAccount,
    credential: external_models.ExternalCredential,
    *,
    synchronization_enabled: bool | None = None,
) -> dict[str, typing.Any]:
    if synchronization_enabled is None:
        synchronization_enabled = account.status != (
            external_models.ExternalAccountStatus.DISCONNECTED.value
        )
    settings = {
        key: account.settings[key]
        for key in (
            "kind",
            "server_url",
            "selection_mode",
            "history_depth",
            "default_project_id",
        )
    }
    return {
        "resource_type": "external_account",
        "uuid": str(account.uuid),
        "generation": account.desired_generation,
        "owner_user_uuid": str(account.owner_user_uuid),
        "settings": settings,
        "synchronization_enabled": synchronization_enabled,
        "credential_envelope": credential.envelope,
    }


def append_desired_external_account(
    account: external_models.ExternalAccount,
    credential: external_models.ExternalCredential,
    session: typing.Any,
    *,
    enabled: bool | None = None,
) -> object:
    associated_data = credential.envelope["associated_data"]
    return sql_state.append_upsert(
        session,
        associated_data["bridge_instance_uuid"],
        account.provider,
        desired_external_account_resource(
            account,
            credential,
            synchronization_enabled=enabled,
        ),
    )


def require_external_provider_enabled(
    session: typing.Any,
    owner_user_uuid: object,
    provider: str,
    *,
    creating: bool = False,
) -> external_models.ExternalProviderPolicy:
    policy = external_models.ExternalProviderPolicy.objects.get_one_or_none(
        filters={"provider": dm_filters.EQ(provider)},
        session=session,
    )
    if policy is None or not policy.enabled or policy.emergency_suspended:
        raise messenger_exc.ExternalResourceForbiddenError()
    if creating:
        maximum = policy.limits.get("max_accounts")
        if not isinstance(maximum, int) or maximum < 1:
            raise messenger_exc.ExternalResourceForbiddenError()
        accounts = external_models.ExternalAccount.objects.get_all(
            filters={
                "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
                "provider": dm_filters.EQ(provider),
            },
            session=session,
        )
        if len(accounts) >= maximum:
            raise messenger_exc.ExternalResourceForbiddenError()
    return policy


class ExternalAccountApplicationService:
    @staticmethod
    def create(
        session: typing.Any,
        actor: ExternalAccountActor,
        spec: dict[str, typing.Any],
    ) -> external_models.ExternalAccount:
        if set(spec) != {"uuid", "settings"}:
            raise ra_exc.ValidationErrorException()
        create_settings = normalize_settings(
            external_models.EXTERNAL_ACCOUNT_CREATE_SETTINGS_TYPE,
            spec["settings"],
        )
        provider = create_settings["kind"]
        api_key = create_settings.pop("api_key")
        settings = normalize_settings(
            external_models.EXTERNAL_ACCOUNT_SETTINGS_TYPE,
            create_settings,
        )
        account = external_models.ExternalAccount(
            uuid=spec["uuid"],
            owner_user_uuid=actor.user_uuid,
            provider=provider,
            settings=settings,
            credential_present=True,
        )
        require_external_provider_enabled(
            session,
            actor.user_uuid,
            provider,
            creating=True,
        )
        if external_models.ExternalAccount.objects.get_all(
            filters={
                "owner_user_uuid": dm_filters.EQ(actor.user_uuid),
                "provider": dm_filters.EQ(provider),
            },
            session=session,
        ):
            raise messenger_exc.ExternalAccountConflictError()
        recipient, envelope = credential_crypto.encrypt_for_active_bridge(
            session,
            account.uuid,
            actor.user_uuid,
            provider,
            account.desired_generation,
            {
                "server_url": settings["server_url"],
                "email": settings["email"],
                "api_key": api_key,
            },
        )
        account.insert(session=session)
        credential = external_models.ExternalCredential(
            external_account_uuid=account.uuid,
            key_version=recipient["identity_generation"],
            envelope=envelope,
        )
        credential.insert(session=session)
        append_desired_external_account(account, credential, session)
        if actor.project_id is None:
            raise ra_exc.ValidationErrorException()
        messenger_events.create_external_resource_event(
            actor.project_id,
            actor.user_uuid,
            account,
            messenger_events.EXTERNAL_ACCOUNT_CREATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider"),
            session=session,
        )
        return account


class ExternalChatApplicationService:
    """Session-oriented bridge projection actions shared by API and workers."""

    @staticmethod
    def select_materialized(
        session: typing.Any,
        actor: ExternalAccountActor,
        chat_uuid: object,
    ) -> external_models.ExternalChat:
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(chat_uuid)},
            session=session,
        )
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(chat.external_account_uuid),
                "owner_user_uuid": dm_filters.EQ(actor.user_uuid),
            },
            session=session,
        )
        policy = require_external_provider_enabled(
            session,
            actor.user_uuid,
            account.provider,
        )
        if actor.project_id is None or chat.projection_stream_uuid is None:
            raise ra_exc.ValidationErrorException()
        models.WorkspaceStream.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(actor.project_id),
                "uuid": dm_filters.EQ(chat.projection_stream_uuid),
            },
            session=session,
        )
        if not chat.selected:
            maximum = policy.limits.get("max_selected_chats_per_account")
            selected = external_models.ExternalChat.objects.get_all(
                filters={
                    "external_account_uuid": dm_filters.EQ(account.uuid),
                    "selected": dm_filters.EQ(True),
                },
                session=session,
            )
            if not isinstance(maximum, int) or len(selected) >= maximum:
                raise messenger_exc.ExternalResourceForbiddenError()
        for name, value in {
            "selected": True,
            "project_id": actor.project_id,
            "status": external_models.ExternalChatStatus.SYNCING.value,
            "revision": chat.revision + 1,
        }.items():
            chat.properties[name].set_value_force(value)
        chat.update(session=session)
        credential = external_credential(account, session)
        sql_state.append_upsert(
            session,
            credential.envelope["associated_data"]["bridge_instance_uuid"],
            chat.provider,
            sql_state.external_chat_assignment_desired(chat, session=session),
        )
        messenger_events.create_external_resource_event(
            actor.project_id,
            actor.user_uuid,
            chat,
            messenger_events.EXTERNAL_CHAT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider", "provider_chat_id"),
            session=session,
        )
        return chat
