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
