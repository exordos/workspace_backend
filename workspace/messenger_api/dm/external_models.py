# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import enum
import urllib.parse

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.messenger_api import events as messenger_events


class ExternalAccountStatus(str, enum.Enum):
    NEW = "new"
    BACKFILL = "backfill"
    FAST_BACKFILL = "fast_backfill"
    ACTIVE = "active"
    ERROR = "error"


class ExternalAccount(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_external_accounts_v2"

    owner_user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    provider = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    external_account_id = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=255)),
        default=None,
        read_only=True,
    )
    disabled = properties.property(types.Boolean(), default=False)
    settings = properties.property(types.Dict(), required=True)
    status = properties.property(
        types.Enum([status.value for status in ExternalAccountStatus]),
        default=ExternalAccountStatus.NEW.value,
    )
    error_description = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )

    def get_host(self) -> str | None:
        return urllib.parse.urlsplit(self.settings["server_url"]).hostname

    def update_status(
        self,
        status: str,
        error_description: str | None = None,
    ) -> None:
        self.status = status
        self.error_description = error_description
        if not self.is_dirty():
            return
        self.update()
        messenger_events.create_external_resource_event(
            self.project_id,
            self.owner_user_uuid,
            self,
            messenger_events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid",),
        )
