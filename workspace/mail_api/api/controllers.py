# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import typing
import uuid as sys_uuid

from restalchemy.api import actions as ra_actions
from restalchemy.api import resources as ra_resources
from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils

from workspace.mail_api import application_services
from workspace.mail_api.dm import models
from workspace.messenger_api.api import controllers as workspace_controllers


class MailResourceController(workspace_controllers.ExternalResourceController):
    def _owner_filters(self, filters: typing.Any) -> typing.Any:
        result = super()._owner_filters(filters)
        result["project_id"] = dm_filters.EQ(self._get_project_id())
        result["deleted"] = dm_filters.EQ(False)
        return result


class MailFolderController(MailResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.MailFolder,
        hidden_fields=[
            "owner_user_uuid",
            "provider_identity",
            "deleted",
            "source_name",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        return application_services.get_folder(
            contexts.Context().get_session(),
            self._get_project_id(),
            self._get_user_uuid(),
            typing.cast(sys_uuid.UUID, uuid),
            locked=self.request.method in ("PUT", "DELETE"),
        )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        allowed = {
            "uuid",
            "external_account_uuid",
            "name",
            "kind",
        }
        if set(kwargs) - allowed:
            raise ra_exc.ValidationErrorException()
        return application_services.create_folder(
            contexts.Context().get_session(),
            self._get_project_id(),
            self._get_user_uuid(),
            kwargs,
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        return application_services.update_folder(
            contexts.Context().get_session(),
            self.get(uuid),
            kwargs,
        )

    def delete(self, uuid: object) -> None:
        application_services.delete_folder(
            contexts.Context().get_session(),
            self.get(uuid),
        )


class MailMessageController(MailResourceController):
    __default_sort__ = {"created_at": "desc"}
    draft = False
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.MailMessage,
        hidden_fields=[
            "owner_user_uuid",
            "provider_identity",
            "deleted",
            "trash_origin_uuid",
            "delivery_error",
            "source_name",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def _owner_filters(self, filters: typing.Any) -> typing.Any:
        result = super()._owner_filters(filters)
        result["draft"] = dm_filters.EQ(self.draft)
        return result

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        return application_services.get_message(
            contexts.Context().get_session(),
            self._get_project_id(),
            self._get_user_uuid(),
            typing.cast(sys_uuid.UUID, uuid),
            draft=self.draft,
            locked=(
                self.request.method in ("PUT", "DELETE")
                or (self.request.method == "POST" and not self.draft)
            ),
        )

    def delete(self, uuid: object) -> None:
        application_services.delete_message(
            contexts.Context().get_session(),
            self.get(uuid),
        )

    @ra_actions.post
    def move(self, resource: typing.Any, **kwargs: typing.Any) -> typing.Any:
        if set(kwargs) != {"folder_uuid"}:
            raise ra_exc.ValidationErrorException()
        try:
            folder_uuid = sys_uuid.UUID(kwargs["folder_uuid"])
        except (AttributeError, TypeError, ValueError) as error:
            raise ra_exc.ValidationErrorException() from error
        return application_services.move_message(
            contexts.Context().get_session(), resource, folder_uuid
        )

    @ra_actions.post
    def read(self, resource: typing.Any) -> typing.Any:
        return self._set_flag(resource, "read", True)

    @ra_actions.post
    def unread(self, resource: typing.Any) -> typing.Any:
        return self._set_flag(resource, "read", False)

    @ra_actions.post
    def star(self, resource: typing.Any) -> typing.Any:
        return self._set_flag(resource, "starred", True)

    @ra_actions.post
    def unstar(self, resource: typing.Any) -> typing.Any:
        return self._set_flag(resource, "starred", False)

    @ra_actions.post
    def trash(self, resource: typing.Any) -> typing.Any:
        return application_services.trash_message(
            contexts.Context().get_session(), resource
        )

    @ra_actions.post
    def restore(self, resource: typing.Any) -> typing.Any:
        return application_services.restore_message(
            contexts.Context().get_session(), resource
        )

    def _set_flag(self, resource: typing.Any, name: str, value: bool) -> typing.Any:
        return application_services.set_message_flag(
            contexts.Context().get_session(),
            resource,
            name,
            value,
        )


class MailDraftController(MailMessageController):
    draft = True
    __default_sort__ = {"updated_at": "desc"}
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.MailDraft,
        hidden_fields=[
            "owner_user_uuid",
            "provider_identity",
            "deleted",
            "trash_origin_uuid",
            "delivery_error",
            "source_name",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        allowed = application_services.MESSAGE_CONTENT_FIELDS | {
            "uuid",
            "folder_uuid",
        }
        if set(kwargs) - allowed or "folder_uuid" not in kwargs:
            raise ra_exc.ValidationErrorException()
        return application_services.create_message(
            contexts.Context().get_session(),
            self._get_project_id(),
            self._get_user_uuid(),
            kwargs,
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        return application_services.update_message(
            contexts.Context().get_session(),
            self.get(uuid),
            kwargs,
        )

    @ra_actions.post
    def send(
        self,
        resource: typing.Any,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        del args
        if kwargs:
            raise ra_exc.ValidationErrorException()
        return application_services.send_message(
            contexts.Context().get_session(),
            resource,
        )


MailDraftController.send.openapi_schema = oa_utils.Schema(
    summary="Queue mail message for delivery",
    parameters=(),
    responses=oa_c.build_openapi_get_update_response("MailMessage_Get"),
)

for name in ("read", "unread", "star", "unstar", "trash", "restore"):
    action = getattr(MailMessageController, name)
    action.openapi_schema = oa_utils.Schema(
        summary=name.capitalize() + " mail message",
        parameters=(),
        responses=oa_c.build_openapi_get_update_response("MailMessage_Get"),
    )
MailMessageController.move.openapi_schema = oa_utils.Schema(
    summary="Move mail message",
    parameters=(),
    responses=oa_c.build_openapi_get_update_response("MailMessage_Get"),
    request_body=oa_c.build_openapi_req_body(
        "Owned destination folder",
        "application/json",
        {
            "type": "object",
            "required": ["folder_uuid"],
            "additionalProperties": False,
            "properties": {
                "folder_uuid": {"type": "string", "format": "uuid"},
            },
        },
    ),
)
