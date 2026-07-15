# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.common import urns
from workspace.groupware.dm import models as groupware_models
from workspace.workspace_api import events as workspace_events
from workspace.messenger_api.dm import models as messenger_models
from workspace.provider_api.dm import models


def create_mail_command(resource, operation, session=None):
    return _create_groupware_command(
        resource,
        operation,
        models.ProviderDomain.MAIL.value,
        urns.MAIL_FOLDER,
        urns.MAIL_MESSAGE,
        session=session,
    )


def create_calendar_command(resource, operation, session=None):
    return _create_groupware_command(
        resource,
        operation,
        models.ProviderDomain.CALENDAR.value,
        urns.CALENDAR,
        urns.CALENDAR_EVENT,
        session=session,
    )


def _create_groupware_command(
    resource,
    operation,
    domain,
    container_urn_type,
    item_urn_type,
    session=None,
):
    if resource.external_user_uuid is None:
        return None
    account = messenger_models.ExternalAccount.objects.get_one(
        filters={"uuid": dm_filters.EQ(resource.external_user_uuid)},
        session=session,
    )
    if account.provider_uuid is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    resource.update_dm(
        values={
            "provider_uuid": account.provider_uuid,
            "delivery_status": models.ProviderCommandStatus.PENDING.value,
            "delivery_error": None,
            "delivery_updated_at": now,
        },
    )
    resource.update(session=session)
    entity_type = item_urn_type
    if isinstance(
        resource,
        (groupware_models.MailFolder, groupware_models.Calendar),
    ):
        entity_type = container_urn_type
    entity_urn = urns.build(entity_type, resource.uuid)
    payload = workspace_events.groupware_resource_payload(
        resource,
        session=session,
    )
    if isinstance(resource, groupware_models.MailMessage):
        payload["folder_urn"] = urns.build(urns.MAIL_FOLDER, resource.folder_uuid)
        payload.pop("folder_uuid", None)
    elif isinstance(resource, groupware_models.CalendarEvent):
        payload["calendar_urn"] = urns.build(
            urns.CALENDAR,
            resource.calendar_uuid,
        )
        payload.pop("calendar_uuid", None)
    command = models.ProviderCommand(
        uuid=sys_uuid.uuid4(),
        project_id=resource.project_id,
        user_uuid=resource.user_uuid,
        provider_uuid=account.provider_uuid,
        external_account_uuid=account.uuid,
        domain=domain,
        operation=operation,
        entity_uuid=resource.uuid,
        entity_urn=entity_urn,
        payload=payload,
        status=models.ProviderCommandStatus.PENDING.value,
    )
    command.insert(session=session)
    return command


def messenger_command_payload(resource, entity_type, session=None):
    payload = workspace_events._model_to_event_payload_value(resource)
    for name in (
        "provider_uuid",
        "external_account_uuid",
        "provider_external_id",
        "delivery_status",
        "delivery_error",
        "delivery_updated_at",
        "source_name",
        "source",
    ):
        payload.pop(name, None)
    payload["urn"] = urns.build(entity_type, resource.uuid)
    if isinstance(resource, groupware_models.Calendar):
        return payload
    if isinstance(resource, messenger_models.WorkspaceStream):
        payload["owner_urn"] = urns.build(
            urns.MESSENGER_USER,
            resource.user_uuid,
        )
        payload.pop("user_uuid", None)
    elif isinstance(resource, messenger_models.WorkspaceStreamTopic):
        payload["stream_urn"] = urns.build(
            urns.MESSENGER_STREAM,
            resource.stream_uuid,
        )
        payload.pop("stream_uuid", None)
    elif isinstance(resource, messenger_models.WorkspaceMessage):
        payload["stream_urn"] = urns.build(
            urns.MESSENGER_STREAM,
            resource.stream_uuid,
        )
        payload["topic_urn"] = urns.build(
            urns.MESSENGER_TOPIC,
            resource.topic_uuid,
        )
        payload["author_urn"] = urns.build(
            urns.MESSENGER_USER,
            resource.user_uuid,
        )
        payload.pop("stream_uuid", None)
        payload.pop("topic_uuid", None)
        payload.pop("user_uuid", None)
    elif isinstance(resource, messenger_models.WorkspaceMessageReactions):
        payload["message_urn"] = urns.build(
            urns.MESSENGER_MESSAGE,
            resource.message_uuid,
        )
        payload["author_urn"] = urns.build(
            urns.MESSENGER_USER,
            resource.user_uuid,
        )
        payload.pop("message_uuid", None)
        payload.pop("user_uuid", None)
    return workspace_events.add_provider_delivery_payload(
        payload,
        resource,
        session=session,
    )


def mark_messenger_delivery_pending(resource, session=None):
    if resource is None or getattr(resource, "provider_uuid", None) is None:
        return False
    resource.update_dm(
        values={
            "delivery_status": models.ProviderCommandStatus.PENDING.value,
            "delivery_error": None,
            "delivery_updated_at": datetime.datetime.now(datetime.timezone.utc),
        },
    )
    resource.update(session=session)
    return True


def create_messenger_command(
    resource,
    operation,
    entity_type,
    session=None,
    update_projection=True,
):
    if resource is None or getattr(resource, "provider_uuid", None) is None:
        return None
    if update_projection:
        mark_messenger_delivery_pending(resource, session=session)
    account = messenger_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(resource.external_account_uuid),
            "provider_uuid": dm_filters.EQ(resource.provider_uuid),
        },
        session=session,
    )
    command = models.ProviderCommand(
        uuid=sys_uuid.uuid4(),
        project_id=resource.project_id,
        user_uuid=account.user_uuid,
        provider_uuid=resource.provider_uuid,
        external_account_uuid=resource.external_account_uuid,
        domain=models.ProviderDomain.MESSENGER.value,
        operation=operation,
        entity_uuid=resource.uuid,
        entity_urn=urns.build(entity_type, resource.uuid),
        payload=messenger_command_payload(resource, entity_type, session=session),
        status=models.ProviderCommandStatus.PENDING.value,
    )
    command.insert(session=session)
    return command
