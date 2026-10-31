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


def create_mail_event(
    resource: typing.Any,
    kind: str,
    session: typing.Any,
) -> typing.Any:
    return workspace_events.create_external_resource_event(
        resource.project_id,
        resource.owner_user_uuid,
        resource,
        kind,
        hidden_fields=(
            "project_id",
            "owner_user_uuid",
            "provider_identity",
            "deleted",
        ),
        session=session,
    )
