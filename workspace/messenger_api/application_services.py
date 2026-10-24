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


from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models as messenger_models


SELECT_PROVIDER_MEMBERSHIPS_SQL = """
    SELECT member.uuid FROM messenger_stream_bindings member
    JOIN messenger_streams stream
      ON stream.project_id = member.project_id AND stream.uuid = member.stream_uuid
    WHERE member.project_id = %s AND member.user_uuid = %s AND member.active
      AND stream.source_name = 'zulip' AND stream.provider_uuid = %s
"""


@dataclasses.dataclass(frozen=True)
class ExternalAccountActor:
    user_uuid: sys_uuid.UUID
    project_id: sys_uuid.UUID | None


class ExternalAccountApplicationService:
    @staticmethod
    def _detach_streams(
        session: typing.Any,
        account: external_models.ExternalAccount,
        provider_uuid: object,
    ) -> None:
        bindings = session.execute(
            SELECT_PROVIDER_MEMBERSHIPS_SQL,
            (
                account.project_id,
                account.owner_user_uuid,
                provider_uuid,
            ),
        ).fetchall()
        previous = session.execute(
            "SELECT current_setting('workspace.outgoing_actor', true) AS actor"
        ).fetchone()["actor"]
        session.execute("SELECT set_config('workspace.outgoing_actor', '', true)")
        for binding in bindings:
            helpers.delete_workspace_stream_binding(
                account.project_id,
                binding["uuid"],
                session=session,
            )
        session.execute(
            "SELECT set_config('workspace.outgoing_actor', %s, true)", (previous or "",)
        )

    @staticmethod
    def delete(
        session: typing.Any,
        actor: ExternalAccountActor,
        account: external_models.ExternalAccount,
    ) -> None:
        messenger_events.create_external_resource_event(
            actor.project_id,
            actor.user_uuid,
            account,
            messenger_events.EXTERNAL_ACCOUNT_DELETED_EVENT,
            hidden_fields=("owner_user_uuid",),
            session=session,
        )
        if account.provider != "zulip" or account.external_account_id is None:
            account.delete(session=session)
            return
        ExternalAccountApplicationService._detach_streams(
            session, account, account.provider_uuid
        )
        hostname, user_id = account.get_host(), account.external_account_id
        namespace = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
        user_uuid = sys_uuid.uuid5(namespace, f"user:{user_id}")
        user = messenger_models.WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": user_uuid},
            session=session,
        )
        if user is None:
            user = messenger_models.WorkspaceUser(
                uuid=user_uuid,
                username=f"zulip-{user_uuid}",
                source=messenger_models.WorkspaceUserSource.ZULIP.value,
                status=messenger_models.WorkspaceUserStatus.OFFLINE.value,
                disabled=account.disabled,
                email=account.settings.get("email"),
            )
            user.insert(session=session)
        else:
            user.source = messenger_models.WorkspaceUserSource.ZULIP.value
            user.update(session=session)
        account.properties["owner_user_uuid"].set_value_force(user.uuid)
        account.settings = {
            name: account.settings[name]
            for name in ("server_url", "email")
            if name in account.settings
        }
        account.status = external_models.ExternalAccountStatus.NEW.value
        account.error_description = None
        account.update(session=session)

    @staticmethod
    def create(
        session: typing.Any,
        actor: ExternalAccountActor,
        spec: dict[str, typing.Any],
    ) -> external_models.ExternalAccount:
        spec = {**spec, "project_id": actor.project_id}
        account = external_models.ExternalAccount(
            owner_user_uuid=actor.user_uuid,
            **spec,
        )
        account.insert(session=session)
        messenger_events.create_external_resource_event(
            actor.project_id,
            actor.user_uuid,
            account,
            messenger_events.EXTERNAL_ACCOUNT_CREATED_EVENT,
            hidden_fields=("owner_user_uuid",),
            session=session,
        )
        return account

    @staticmethod
    def update(
        session: typing.Any,
        actor: ExternalAccountActor,
        account: external_models.ExternalAccount,
        spec: dict[str, typing.Any],
    ) -> external_models.ExternalAccount:
        connection = (
            account.provider,
            account.provider_uuid,
            account.connection_settings(),
        )
        account.update_dm(values=spec)
        verify_connection = account.provider == "zulip" and connection != (
            account.provider,
            account.provider_uuid,
            account.connection_settings(),
        )
        status = account.status
        error_description = account.error_description
        if verify_connection:
            status = external_models.ExternalAccountStatus.NEW.value
            error_description = None
        account.update_status(
            status,
            error_description=error_description,
        )
        if connection[1] != account.provider_uuid:
            ExternalAccountApplicationService._detach_streams(
                session, account, connection[1]
            )
        return account
