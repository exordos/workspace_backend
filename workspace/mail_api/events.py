# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import typing

from workspace.messenger_api import events as workspace_events


MAIL_FOLDER_CREATED_EVENT = workspace_events.MAIL_FOLDER_CREATED_EVENT
MAIL_FOLDER_UPDATED_EVENT = workspace_events.MAIL_FOLDER_UPDATED_EVENT
MAIL_FOLDER_DELETED_EVENT = workspace_events.MAIL_FOLDER_DELETED_EVENT
MAIL_MESSAGE_CREATED_EVENT = workspace_events.MAIL_MESSAGE_CREATED_EVENT
MAIL_MESSAGE_UPDATED_EVENT = workspace_events.MAIL_MESSAGE_UPDATED_EVENT
MAIL_MESSAGE_DELETED_EVENT = workspace_events.MAIL_MESSAGE_DELETED_EVENT
MAIL_DRAFT_CREATED_EVENT = workspace_events.MAIL_DRAFT_CREATED_EVENT
MAIL_DRAFT_UPDATED_EVENT = workspace_events.MAIL_DRAFT_UPDATED_EVENT
MAIL_DRAFT_DELETED_EVENT = workspace_events.MAIL_DRAFT_DELETED_EVENT


def create_mail_event(
    resource: typing.Any,
    kind: str,
    session: typing.Any,
) -> typing.Any:
    hidden_fields = (
        "owner_user_uuid",
        "provider_identity",
        "deleted",
        "source_name",
        "delivery_error",
        "trash_origin_uuid",
    )
    if kind.endswith(".deleted"):
        hidden_fields = tuple(
            name for name in resource.properties if name not in {"uuid", "folder_uuid"}
        )
    return workspace_events.create_external_resource_event(
        resource.project_id,
        resource.owner_user_uuid,
        resource,
        kind,
        hidden_fields=hidden_fields,
        session=session,
    )
