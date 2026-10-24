# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import enum
import typing
import urllib.parse
import uuid as sys_uuid

from restalchemy.common import exceptions
from restalchemy.dm import filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.messenger_api import events as messenger_events


class Provider(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "messenger_providers"

    hostname = properties.property(
        types.String(min_length=1, max_length=255), required=True
    )

    @classmethod
    def register(cls, hostname: str, session: typing.Any = None) -> "Provider":
        provider = cls(
            uuid=sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname), hostname=hostname
        )
        with cls._get_engine().session_manager(session=session) as current_session:
            current_session.execute(
                "INSERT INTO messenger_providers (uuid, hostname) VALUES (%s, %s) "
                "ON CONFLICT (hostname) DO NOTHING",
                (provider.uuid, provider.hostname),
            )
            return cls.objects.get_one(
                filters={"hostname": filters.EQ(hostname)}, session=current_session
            )


class ExternalAccountStatus(str, enum.Enum):
    NEW = "new"
    SYNCING = "syncing"
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
    provider_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
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

    @classmethod
    def restore_row(cls, row: typing.Any, pour: typing.Any = None) -> "ExternalAccount":
        account = super().restore_row(row, pour=pour)
        account._persisted_provider_uuid = account.provider_uuid
        return account

    def _register_provider(self, session: typing.Any = None) -> None:
        hostname = urllib.parse.urlsplit(self.settings["server_url"]).hostname
        if hostname is None:
            raise exceptions.ValidationErrorException()
        provider = Provider.register(hostname, session=session)
        self.provider_uuid = provider.uuid
        self._provider = provider

    def insert(self, session: typing.Any = None) -> None:
        with self._get_engine().session_manager(session=session) as current_session:
            if self.provider_uuid is None and "server_url" in self.settings:
                self._register_provider(session=current_session)
            elif self.provider_uuid is not None:
                self._normalize_endpoint_host()
            super().insert(session=current_session)
            self._persisted_provider_uuid = self.provider_uuid

    def get_host(self) -> str:
        if not hasattr(self, "_provider") or self._provider.uuid != self.provider_uuid:
            self._provider = Provider.objects.get_one(
                filters={"uuid": filters.EQ(self.provider_uuid)}
            )
        return self._provider.hostname

    def update(self, session: typing.Any = None, force: bool = False) -> None:
        with self._get_engine().session_manager(session=session) as current_session:
            if self.provider_uuid != self._persisted_provider_uuid:
                self._normalize_endpoint_host()
            elif (
                "server_url" in self.settings and self.properties["settings"].is_dirty()
            ):
                hostname = urllib.parse.urlsplit(self.settings["server_url"]).hostname
                if self.provider_uuid is None or hostname != self.get_host():
                    self._register_provider(session=current_session)
            super().update(session=current_session, force=force)
            self._persisted_provider_uuid = self.provider_uuid

    def _normalize_endpoint_host(self) -> None:
        if "server_url" not in self.settings:
            return
        endpoint = urllib.parse.urlsplit(self.settings["server_url"])
        hostname = self.get_host()
        if endpoint.hostname != hostname:
            netloc = f"[{hostname}]" if ":" in hostname else hostname
            if endpoint.port is not None:
                netloc += f":{endpoint.port}"
            self.settings = {
                **self.settings,
                "server_url": endpoint._replace(netloc=netloc).geturl(),
            }

    def connection_settings(self) -> dict:
        return {
            name: self.settings.get(name) for name in ("server_url", "email", "api_key")
        }

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
