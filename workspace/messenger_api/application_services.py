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


from workspace.bridges.zulip.dm import models as zulip_models
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import external_models


@dataclasses.dataclass(frozen=True)
class ExternalAccountActor:
    user_uuid: sys_uuid.UUID
    project_id: sys_uuid.UUID | None


class ExternalAccountApplicationService:
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
        if account.provider == "zulip":
            task = zulip_models.Task(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                priority=zulip_models.TASK_PRIORITY_INITIALIZE_CONNECTION,
                content=zulip_models.InitializeConnectionContent(),
            )
            task.cancel_pending_tasks(exclude_current=True)
            task.insert(session=session)
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
        account.update_dm(values=spec)
        status = account.status
        error_description = account.error_description
        if account.provider == "zulip":
            status = external_models.ExternalAccountStatus.NEW.value
            error_description = None
            task = zulip_models.Task(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                priority=zulip_models.TASK_PRIORITY_INITIALIZE_CONNECTION,
                content=zulip_models.InitializeConnectionContent(),
            )
            task.cancel_pending_tasks(exclude_current=True)
            task.insert(session=session)
        account.update_status(
            status,
            error_description=error_description,
        )
        return account
