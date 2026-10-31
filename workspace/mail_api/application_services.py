# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import re
import typing
import uuid as sys_uuid

from psycopg.types import json as pg_json
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters

from workspace.mail_api import events
from workspace.mail_api.dm import models
from workspace.messenger_api.dm import external_models


FILE_URN_RE = re.compile(
    r"^urn:file:([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})(?:\?.*)?$",
    re.IGNORECASE,
)
MESSAGE_CONTENT_FIELDS = frozenset(
    {
        "from_address",
        "to_addresses",
        "cc_addresses",
        "bcc_addresses",
        "reply_to",
        "subject",
        "snippet",
        "body_html",
        "body_text",
        "attachment_urns",
        "in_reply_to",
        "references",
    }
)


def _force_values(resource: typing.Any, values: dict[str, typing.Any]) -> None:
    for name, value in values.items():
        resource.properties[name].set_value_force(value)


def _account(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    account_uuid: sys_uuid.UUID | None,
) -> external_models.ExternalAccount | None:
    if account_uuid is None:
        return None
    return external_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(account_uuid),
            "project_id": dm_filters.EQ(project_id),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
            "provider": dm_filters.EQ("mail"),
            "disabled": dm_filters.EQ(False),
        },
        session=session,
    )


def _folder(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    folder_uuid: sys_uuid.UUID,
) -> models.MailFolder:
    return models.MailFolder.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(folder_uuid),
            "project_id": dm_filters.EQ(project_id),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
            "deleted": dm_filters.EQ(False),
        },
        session=session,
    )


def _message(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    message_uuid: sys_uuid.UUID,
    *,
    locked: bool = False,
) -> models.MailMessage:
    return models.MailMessage.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(message_uuid),
            "project_id": dm_filters.EQ(project_id),
            "owner_user_uuid": dm_filters.EQ(owner_user_uuid),
            "deleted": dm_filters.EQ(False),
        },
        session=session,
        locked=locked,
    )


def _validate_attachment_urns(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    urns: typing.Iterable[str],
) -> None:
    file_uuids = []
    for urn in urns:
        match = FILE_URN_RE.fullmatch(urn)
        if match is None:
            raise ra_exc.ValidationErrorException()
        file_uuids.append(sys_uuid.UUID(match.group(1)))
    if len(set(file_uuids)) != len(file_uuids):
        raise ra_exc.ValidationErrorException()
    for file_uuid in file_uuids:
        visible = session.execute(
            """
            SELECT 1
            FROM m_workspace_visible_files_v1
            WHERE project_id = %s AND uuid = %s
              AND (viewer_user_uuid = %s OR viewer_user_uuid IS NULL)
            LIMIT 1
            """,
            (project_id, file_uuid, owner_user_uuid),
        ).fetchone()
        if visible is None:
            raise ra_exc.ValidationErrorException()


def create_folder(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    values: dict[str, typing.Any],
) -> models.MailFolder:
    values = values.copy()
    account = _account(
        session,
        project_id,
        owner_user_uuid,
        values.get("external_account_uuid"),
    )
    values.update(
        {
            "uuid": values.get("uuid") or sys_uuid.uuid4(),
            "project_id": project_id,
            "owner_user_uuid": owner_user_uuid,
            "source_name": models.MailSource.NATIVE.value,
            "provider_identity": None,
            "deleted": False,
        }
    )
    if account is None:
        values["external_account_uuid"] = None
    folder = models.MailFolder(**values)
    folder.insert(session=session)
    events.create_mail_event(folder, events.MAIL_FOLDER_CREATED_EVENT, session)
    return folder


def update_folder(
    session: typing.Any,
    folder: models.MailFolder,
    values: dict[str, typing.Any],
) -> models.MailFolder:
    allowed = {"external_account_uuid", "path", "name", "delimiter", "special_use"}
    if not values or set(values) - allowed:
        raise ra_exc.ValidationErrorException()
    if "external_account_uuid" in values:
        _account(
            session,
            folder.project_id,
            folder.owner_user_uuid,
            values["external_account_uuid"],
        )
        if values["external_account_uuid"] != folder.external_account_uuid:
            message = session.execute(
                """
                SELECT 1 FROM m_mail_messages
                WHERE project_id = %s AND owner_user_uuid = %s
                  AND folder_uuid = %s AND NOT deleted
                LIMIT 1
                """,
                (folder.project_id, folder.owner_user_uuid, folder.uuid),
            ).fetchone()
            if message is not None:
                raise ra_exc.ValidationErrorException()
    folder.update_dm(values=values)
    folder.update(session=session)
    events.create_mail_event(folder, events.MAIL_FOLDER_UPDATED_EVENT, session)
    return folder


def delete_folder(session: typing.Any, folder: models.MailFolder) -> None:
    message = session.execute(
        """
        SELECT 1 FROM m_mail_messages
        WHERE project_id = %s AND owner_user_uuid = %s
          AND folder_uuid = %s AND NOT deleted
        LIMIT 1
        """,
        (folder.project_id, folder.owner_user_uuid, folder.uuid),
    ).fetchone()
    if message is not None:
        raise ra_exc.ValidationErrorException()
    _force_values(folder, {"deleted": True})
    folder.update(session=session)
    events.create_mail_event(folder, events.MAIL_FOLDER_DELETED_EVENT, session)


def create_message(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    values: dict[str, typing.Any],
) -> models.MailMessage:
    values = values.copy()
    folder = _folder(
        session,
        project_id,
        owner_user_uuid,
        values["folder_uuid"],
    )
    _validate_attachment_urns(
        session,
        project_id,
        owner_user_uuid,
        values.get("attachment_urns", ()),
    )
    account = _account(
        session,
        project_id,
        owner_user_uuid,
        folder.external_account_uuid,
    )
    sender = ""
    if account is not None:
        sender = account.settings.get("email") or ""
    values.update(
        {
            "uuid": values.get("uuid") or sys_uuid.uuid4(),
            "project_id": project_id,
            "owner_user_uuid": owner_user_uuid,
            "external_account_uuid": folder.external_account_uuid,
            "from_address": sender or values.get("from_address", ""),
            "draft": True,
            "delivery_status": models.MailDeliveryStatus.DRAFT.value,
            "delivery_error": None,
            "source_name": models.MailSource.NATIVE.value,
            "provider_identity": None,
            "sent_at": None,
            "received_at": None,
            "message_id": None,
            "deleted": False,
        }
    )
    message = models.MailMessage(**values)
    message.insert(session=session)
    events.create_mail_event(message, events.MAIL_MESSAGE_CREATED_EVENT, session)
    return message


def update_message(
    session: typing.Any,
    message: models.MailMessage,
    values: dict[str, typing.Any],
) -> models.MailMessage:
    allowed = MESSAGE_CONTENT_FIELDS | {"seen", "flagged"}
    if not values or set(values) - allowed:
        raise ra_exc.ValidationErrorException()
    if not message.draft and set(values).intersection(MESSAGE_CONTENT_FIELDS):
        raise ra_exc.ValidationErrorException()
    if "attachment_urns" in values:
        _validate_attachment_urns(
            session,
            message.project_id,
            message.owner_user_uuid,
            values["attachment_urns"],
        )
    message.update_dm(values=values)
    message.update(session=session)
    events.create_mail_event(message, events.MAIL_MESSAGE_UPDATED_EVENT, session)
    return message


def move_message(
    session: typing.Any,
    message: models.MailMessage,
    folder_uuid: sys_uuid.UUID,
) -> models.MailMessage:
    folder = _folder(
        session,
        message.project_id,
        message.owner_user_uuid,
        folder_uuid,
    )
    if folder.external_account_uuid != message.external_account_uuid:
        raise ra_exc.ValidationErrorException()
    message.update_dm(values={"folder_uuid": folder.uuid})
    message.update(session=session)
    events.create_mail_event(message, events.MAIL_MESSAGE_UPDATED_EVENT, session)
    return message


def send_message(
    session: typing.Any,
    message: models.MailMessage,
) -> models.MailMessage:
    message = _message(
        session,
        message.project_id,
        message.owner_user_uuid,
        message.uuid,
        locked=True,
    )
    if not message.draft or message.delivery_status not in {"draft", "failed"}:
        raise ra_exc.ValidationErrorException()
    if (
        not message.to_addresses
        and not message.cc_addresses
        and not message.bcc_addresses
    ):
        raise ra_exc.ValidationErrorException()
    if not message.from_address:
        raise ra_exc.ValidationErrorException()
    if message.body_text is None and message.body_html is None:
        raise ra_exc.ValidationErrorException()
    account = _account(
        session,
        message.project_id,
        message.owner_user_uuid,
        message.external_account_uuid,
    )
    if (
        account is None
        or account.status != external_models.ExternalAccountStatus.ACTIVE.value
    ):
        raise ra_exc.ValidationErrorException()
    _validate_attachment_urns(
        session,
        message.project_id,
        message.owner_user_uuid,
        message.attachment_urns,
    )
    intent = {
        "message_uuid": str(message.uuid),
        "from_address": message.from_address,
        "to_addresses": message.to_addresses,
        "cc_addresses": message.cc_addresses,
        "bcc_addresses": message.bcc_addresses,
        "reply_to": message.reply_to,
        "subject": message.subject,
        "body_text": message.body_text,
        "body_html": message.body_html,
        "attachment_urns": message.attachment_urns,
        "in_reply_to": message.in_reply_to,
        "references": message.references,
    }
    session.execute(
        """
        INSERT INTO mail_outgoing_intents (
            project_id, message_uuid, external_account_uuid, payload
        ) VALUES (%s, %s, %s, %s)
        """,
        (
            message.project_id,
            message.uuid,
            account.uuid,
            pg_json.Jsonb(intent),
        ),
    )
    _force_values(
        message,
        {
            "draft": False,
            "delivery_status": models.MailDeliveryStatus.QUEUED.value,
            "delivery_error": None,
        },
    )
    message.update(session=session)
    events.create_mail_event(message, events.MAIL_MESSAGE_UPDATED_EVENT, session)
    return message


def delete_message(session: typing.Any, message: models.MailMessage) -> None:
    if message.delivery_status == models.MailDeliveryStatus.QUEUED.value:
        raise ra_exc.ValidationErrorException()
    _force_values(message, {"deleted": True})
    message.update(session=session)
    events.create_mail_event(message, events.MAIL_MESSAGE_DELETED_EVENT, session)


get_folder = _folder
get_message = _message
