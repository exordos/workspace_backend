# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import hashlib
import re
import typing
import uuid

from restalchemy.dm import filters

from workspace.calendar_api import models as calendar_models
from workspace.mail_api import events as mail_events
from workspace.mail_api.dm import models as mail_models
from workspace.messenger_api import events as workspace_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.services import groupware_caldav_write
from workspace.services import groupware_calendar
from workspace.services import groupware_content
from workspace.services import groupware_mail


_OUTBOUND_MESSAGE_ID = re.compile(r"^<([0-9a-f-]{36})@([^>]+)>$")


class AccountProjection:
    """Apply a provider read to the canonical resources for one external account."""

    def __init__(
        self, session: typing.Any, account: external_models.ExternalAccount
    ) -> None:
        self._session = session
        self._account = account

    def _owned(self, resource_uuid: uuid.UUID) -> dict[str, typing.Any]:
        return {
            "uuid": filters.EQ(resource_uuid),
            "project_id": filters.EQ(self._account.project_id),
            "owner_user_uuid": filters.EQ(self._account.owner_user_uuid),
        }

    def _values(self, resource_uuid: uuid.UUID) -> dict[str, typing.Any]:
        return {
            "uuid": resource_uuid,
            "project_id": self._account.project_id,
            "owner_user_uuid": self._account.owner_user_uuid,
        }

    def mail_folder(self, folder: groupware_mail.Folder) -> mail_models.MailFolder:
        identity = folder.mailbox.decode("ascii")
        resource_uuid = uuid.uuid5(self._account.uuid, f"imap:folder:{identity}")
        scope = {
            "project_id": filters.EQ(self._account.project_id),
            "owner_user_uuid": filters.EQ(self._account.owner_user_uuid),
            "external_account_uuid": filters.EQ(self._account.uuid),
        }
        resource = mail_models.MailFolder.objects.get_one_or_none(
            filters={**scope, "provider_identity": filters.EQ(identity)},
            session=self._session,
        )
        if resource is not None:
            return resource
        kind = folder.kind
        if kind != "custom":
            existing = mail_models.MailFolder.objects.get_one_or_none(
                filters={
                    **scope,
                    "kind": filters.EQ(kind),
                    "deleted": filters.EQ(False),
                },
                session=self._session,
                locked=True,
            )
            if existing is not None:
                if existing.provider_identity is None:
                    existing.properties["provider_identity"].set_value_force(identity)
                    existing.properties["source_name"].set_value_force(
                        mail_models.MailSource.PROVIDER.value
                    )
                    existing.update(session=self._session)
                    mail_events.create_mail_event(
                        existing, mail_events.MAIL_FOLDER_UPDATED_EVENT, self._session
                    )
                    return existing
                kind = "custom"
        resource = mail_models.MailFolder(
            **self._values(resource_uuid),
            external_account_uuid=self._account.uuid,
            name=folder.name[:256],
            kind=kind,
            provider_identity=identity,
            source_name=mail_models.MailSource.PROVIDER.value,
        )
        resource.insert(session=self._session)
        mail_events.create_mail_event(
            resource, mail_events.MAIL_FOLDER_CREATED_EVENT, self._session
        )
        return resource

    def mail_message(
        self,
        folder: mail_models.MailFolder,
        validity: int,
        incoming: groupware_mail.Message,
    ) -> mail_models.MailMessage:
        if folder.external_account_uuid != self._account.uuid:
            raise ValueError("Mailbox folder belongs to a different external account")
        identity = f"{folder.uuid}:{validity}:{incoming.uid}"
        resource_uuid = uuid.uuid5(self._account.uuid, f"imap:message:{identity}")
        resource = mail_models.MailMessage.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(self._account.project_id),
                "owner_user_uuid": filters.EQ(self._account.owner_user_uuid),
                "external_account_uuid": filters.EQ(self._account.uuid),
                "provider_identity": filters.EQ(identity),
            },
            session=self._session,
        )
        if resource is None:
            parsed = groupware_content.parse_mail(incoming.content)
            if folder.kind == "sent":
                matching = self._match_outgoing(folder, parsed, identity)
                if matching is not None:
                    return matching
            resource = mail_models.MailMessage(
                **self._values(resource_uuid),
                external_account_uuid=self._account.uuid,
                folder_uuid=folder.uuid,
                from_address=parsed.from_address,
                to_addresses=parsed.to_addresses,
                cc_addresses=parsed.cc_addresses,
                subject=parsed.subject,
                payload=parsed.payload,
                attachment_urns=self._attachments(resource_uuid, parsed.attachments),
                sent_at=parsed.sent_at,
                read=incoming.read,
                starred=incoming.starred,
                draft=False,
                delivery_status=(
                    mail_models.MailDeliveryStatus.SENT.value
                    if folder.kind == "sent"
                    else mail_models.MailDeliveryStatus.RECEIVED.value
                ),
                source_name=mail_models.MailSource.PROVIDER.value,
                provider_identity=identity,
            )
            resource.insert(session=self._session)
            mail_events.create_mail_event(
                resource, mail_events.MAIL_MESSAGE_CREATED_EVENT, self._session
            )
        elif not resource.deleted:
            resource.properties["read"].set_value_force(incoming.read)
            resource.properties["starred"].set_value_force(incoming.starred)
            if resource.is_dirty():
                resource.update(session=self._session)
                mail_events.create_mail_event(
                    resource, mail_events.MAIL_MESSAGE_UPDATED_EVENT, self._session
                )
        return resource

    def _match_outgoing(
        self,
        folder: mail_models.MailFolder,
        incoming: groupware_content.IncomingMail,
        identity: str,
    ) -> mail_models.MailMessage | None:
        match = _OUTBOUND_MESSAGE_ID.fullmatch(incoming.message_id)
        address = self._account.settings.get("email")
        if (
            match is None
            or not isinstance(address, str)
            or incoming.from_address != address
            or not address.endswith("@" + match[2])
        ):
            return None
        try:
            resource_uuid = uuid.UUID(match[1])
        except ValueError:
            return None
        resource = mail_models.MailMessage.objects.get_one_or_none(
            filters={
                **self._owned(resource_uuid),
                "external_account_uuid": filters.EQ(self._account.uuid),
                "delivery_status": filters.EQ("sent"),
                "provider_identity": filters.Is(None),
                "from_address": filters.EQ(address),
            },
            session=self._session,
            locked=True,
        )
        if resource is None:
            return None
        resource.properties["source_name"].set_value_force(
            mail_models.MailSource.PROVIDER.value
        )
        resource.properties["provider_identity"].set_value_force(identity)
        resource.folder_uuid = folder.uuid
        resource.update(session=self._session)
        return resource

    def _attachments(
        self, message_uuid: uuid.UUID, attachments: list[groupware_content.Attachment]
    ) -> list[str]:
        urns = []
        for index, attachment in enumerate(attachments):
            digest = hashlib.sha256(attachment.body).hexdigest()
            file_uuid = uuid.uuid5(message_uuid, f"attachment:{index}:{digest}")
            location = file_storage.save_workspace_file(file_uuid, attachment.body)
            name = attachment.name.replace("\\", "/").rsplit("/", 1)[-1]
            name = (
                "".join(char for char in name if char.isprintable())[:255]
                or "attachment"
            )
            metadata = file_storage.WorkspaceFileMetadata(
                uuid=file_uuid,
                project_id=self._account.project_id,
                stream_uuid=None,
                owner_uuid=self._account.owner_user_uuid,
                name=name,
                description="",
                content_type=attachment.content_type,
                size_bytes=len(attachment.body),
                sha256=digest,
                created_at=datetime.datetime.now(datetime.timezone.utc),
                acl_mode="owner",
            )
            try:
                file_storage.save_workspace_file_metadata(
                    metadata, storage_type=location.storage_type
                )
            except Exception:
                file_storage.delete_workspace_file(
                    file_uuid, location.storage_type, location.storage_object_id
                )
                raise
            helpers.create_workspace_file(
                self._account.project_id,
                self._account.owner_user_uuid,
                file_uuid,
                session=self._session,
                acl_mode="owner",
                name=name,
                description="",
                content_type=attachment.content_type,
                size_bytes=len(attachment.body),
                hash=digest,
                storage_type=location.storage_type,
                storage_id=location.storage_id,
                storage_object_id=location.storage_object_id,
            )
            urns.append(f"urn:file:{file_uuid}")
        return urns

    def calendar(
        self, incoming: groupware_calendar.Calendar
    ) -> calendar_models.Calendar:
        resource_uuid = uuid.uuid5(
            self._account.uuid, f"caldav:calendar:{incoming.path}"
        )
        resource = calendar_models.Calendar.objects.get_one_or_none(
            filters={
                **self._owned(resource_uuid),
                "external_account_uuid": filters.EQ(self._account.uuid),
            },
            session=self._session,
        )
        managed = self._account.provider == "caldav"
        read_only = not (managed and incoming.can_write)
        if resource is None:
            resource = calendar_models.Calendar(
                **self._values(resource_uuid),
                external_account_uuid=self._account.uuid,
                name=incoming.name[:256],
                read_only=read_only,
            )
            resource.insert(session=self._session)
            self._calendar_event(resource, "calendar.calendar.created")
        elif (resource.name, resource.read_only) != (incoming.name[:256], read_only):
            resource.name = incoming.name[:256]
            resource.properties["read_only"].set_value_force(read_only)
            resource.update(session=self._session)
            self._calendar_event(resource, "calendar.calendar.updated")
        if managed:
            self._session.execute(
                """
                INSERT INTO groupware_calendar_bindings (calendar_uuid, remote_path)
                VALUES (%s, %s) ON CONFLICT (calendar_uuid) DO NOTHING
                """,
                (resource.uuid, incoming.path),
            )
        return resource

    def calendar_event(
        self,
        calendar: calendar_models.Calendar,
        incoming: groupware_calendar.Event,
        timezone: datetime.tzinfo,
        observed_at: datetime.datetime | None = None,
    ) -> calendar_models.CalendarEvent | None:
        if calendar.external_account_uuid != self._account.uuid:
            raise ValueError("Calendar belongs to a different external account")
        payload = groupware_content.parse_event(incoming.data, timezone)
        if payload is None:
            return None
        times = calendar_models.event_times(payload)
        managed = self._account.provider == "caldav"
        remote_binding = None
        if managed:
            remote_binding = self._session.execute(
                """
                SELECT event_uuid FROM groupware_calendar_event_bindings
                WHERE calendar_uuid = %s AND remote_path = %s
                """,
                (calendar.uuid, incoming.path),
            ).fetchone()
        resource_uuid = (
            remote_binding["event_uuid"]
            if remote_binding is not None
            else uuid.uuid5(calendar.uuid, f"caldav:event:{incoming.path}")
        )
        if managed and remote_binding is None:
            created_uuid = groupware_caldav_write.created_event_uuid(incoming)
            if (
                created_uuid is not None
                and self._session.execute(
                    "SELECT 1 FROM m_workspace_calendar_events WHERE uuid = %s",
                    (created_uuid,),
                ).fetchone()
                is None
            ):
                resource_uuid = created_uuid
        resource = calendar_models.CalendarEvent.objects.get_one_or_none(
            filters={
                **self._owned(resource_uuid),
                "calendar_uuid": filters.EQ(calendar.uuid),
            },
            session=self._session,
            locked=managed,
        )
        if resource is None:
            resource = calendar_models.CalendarEvent(
                **self._values(resource_uuid),
                calendar_uuid=calendar.uuid,
                payload=payload,
                **times,
            )
            resource.insert(session=self._session)
            self._calendar_event(resource, "calendar.event.created")
        elif observed_at is not None and resource.updated_at > observed_at:
            return resource
        elif calendar_models.EVENT_PAYLOAD_TYPE.to_simple_type(
            resource.payload
        ) != calendar_models.EVENT_PAYLOAD_TYPE.to_simple_type(payload):
            resource.payload = payload
            for name, value in times.items():
                resource.properties[name].set_value_force(value)
            resource.update(session=self._session)
            self._calendar_event(resource, "calendar.event.updated")
        if managed:
            groupware_caldav_write.write_event_binding(
                self._session, resource, incoming
            )
        return resource

    def reconcile_calendar(
        self,
        calendar: calendar_models.Calendar,
        incoming: list[groupware_calendar.Event],
        start: datetime.datetime,
        end: datetime.datetime,
        timezone: datetime.tzinfo,
        observed_at: datetime.datetime,
    ) -> None:
        for event in incoming:
            self.calendar_event(calendar, event, timezone, observed_at)
        paths = {event.path for event in incoming}
        rows = self._session.execute(
            """
            SELECT events.uuid, bindings.remote_path FROM m_workspace_calendar_events events
            JOIN groupware_calendar_event_bindings bindings ON bindings.event_uuid = events.uuid
            WHERE events.calendar_uuid = %s AND events.project_id = %s AND events.owner_user_uuid = %s
              AND events.starts_at < %s AND events.ends_at > %s AND events.updated_at <= %s
            """,
            (
                calendar.uuid,
                self._account.project_id,
                self._account.owner_user_uuid,
                end,
                start,
                observed_at,
            ),
        ).fetchall()
        for row in rows:
            if row["remote_path"] in paths:
                continue
            resource = calendar_models.CalendarEvent.objects.get_one_or_none(
                filters={
                    **self._owned(row["uuid"]),
                    "updated_at": filters.LE(observed_at),
                },
                session=self._session,
                locked=True,
            )
            if resource is not None:
                resource.delete(session=self._session)
                self._calendar_event(resource, "calendar.event.deleted")

    def _calendar_event(
        self,
        resource: calendar_models.Calendar | calendar_models.CalendarEvent,
        kind: str,
    ) -> None:
        workspace_events.create_external_resource_event(
            self._account.project_id,
            self._account.owner_user_uuid,
            resource,
            kind,
            hidden_fields=("owner_user_uuid", "starts_at", "ends_at"),
            session=self._session,
        )
