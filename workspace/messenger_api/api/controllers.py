# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
import logging
import re
import typing
import urllib.parse
import uuid as sys_uuid

import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import constants as ra_constants
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import field_permissions as ra_field_permissions
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils
from webob import multidict

from workspace.common import models as common_models
from workspace.messenger_api import file_storage
from workspace.messenger_api import application_services
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api import topic_summarization
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import push_devices
from workspace.messenger_api.dm import v2_models


LOG = logging.getLogger(__name__)
MAX_AVATAR_SIZE_BYTES = 25 * 1024 * 1024
AVATAR_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _normalize_avatar_content_type(value: str) -> str:
    value = value.lower()
    return "image/jpeg" if value == "image/jpg" else value


def _valid_avatar_bytes(content_type: str, data: bytes) -> bool:
    signatures = {
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
    }
    if content_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return any(data.startswith(prefix) for prefix in signatures[content_type])


def _update_internal_fields(
    resource: typing.Any, values: typing.Any, session: typing.Any = None
) -> None:
    for name, value in values.items():
        resource.properties[name].set_value_force(value)
    resource.update(session=session)


def _build_openapi_multipart_request_body(
    *, description: typing.Any, properties: typing.Any, required: typing.Any
) -> typing.Any:
    request_body = oa_c.build_openapi_req_body_multipart(
        description=description,
        properties=properties,
    )
    schema = request_body["content"]["multipart/form-data"]["schema"]
    schema["type"] = "object"
    schema["required"] = list(required)
    return request_body


class ContractJSONPacker(ra_packers.JSONPacker):
    """Apply resource visibility to dictionaries returned by the mail store."""

    def pack_resource(self, obj: typing.Any) -> typing.Any:
        if not isinstance(obj, dict) or self._rt is None:
            return super().pack_resource(obj)
        result = {}
        for name, field in self._rt.get_fields_by_request(self._req):
            if (
                field.is_public()
                and not self._rt._fields_permissions.is_hidden(name, self._req)
                and name in obj
            ):
                result[field.api_name] = obj[name]
        return result


class ContractMultipartPacker(ra_packers.MultipartPacker):
    pack_resource = ContractJSONPacker.pack_resource


class TopicSummaryManagementJSONPacker(ContractJSONPacker):
    def unpack(self, value: typing.Any) -> typing.Any:
        if self._req.method not in {"POST", "PUT"}:
            return super().unpack(value)
        try:
            result = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ra_exc.ValidationErrorException() from exc
        if not isinstance(result, dict):
            raise ra_exc.ValidationErrorException()
        return result


class StoreResourceController(ra_controllers.BaseResourceControllerPaginated):
    __generate_location_for__ = ()
    resource_name = ""
    _filter_operator_suffixes = (
        ("=>", dm_filters.GE),
        ("=<", dm_filters.LE),
        (">", dm_filters.GT),
        ("<", dm_filters.LT),
    )

    def _get_user_uuid(self) -> typing.Any:
        return self.get_context().user_uuid

    def _get_project_id(self) -> typing.Any:
        context = self.get_context()
        project_id = getattr(context, "project_id", None)
        if project_id is None:
            raise ra_exc.ValidationErrorException()
        return project_id

    def get_packer(
        self, content_type: typing.Any, resource_type: typing.Any = None
    ) -> typing.Any:
        packer = (
            ContractMultipartPacker
            if "multipart/form-data" in content_type
            else ContractJSONPacker
        )
        return packer(resource_type or self.get_resource(), request=self.request)

    def _values(self, values: typing.Any) -> typing.Any:
        result = values.copy()
        result["project_id"] = self._get_project_id()
        result["user_uuid"] = self._get_user_uuid()
        result.setdefault("uuid", sys_uuid.uuid4())
        result.setdefault("source_name", "native")
        return result

    @classmethod
    def _split_filter_operator(cls, name: typing.Any) -> typing.Any:
        for suffix, operator in cls._filter_operator_suffixes:
            if name.endswith(suffix):
                return name[: -len(suffix)], operator
        return name, None

    def _prepare_filters(self, params: typing.Any) -> typing.Any:
        regular = []
        conditional: dict[str, typing.Any] = {}
        for name, value in params.items():
            field_name, operator = self._split_filter_operator(name)
            if operator is None:
                regular.append((name, value))
                continue
            field_name, field_value = self._prepare_filter(field_name, value)
            clause = operator(field_value)
            if field_name in conditional:
                conditional[field_name] = dm_filters.AND(
                    conditional[field_name],
                    clause,
                )
            else:
                conditional[field_name] = clause
        result = super()._prepare_filters(multidict.MultiDict(regular))
        result.update(conditional)
        return result

    def _store_query(self, filters: typing.Any, order_by: typing.Any) -> typing.Any:
        filters = filters.copy()
        marker = getattr(self, "_pagination_marker", None)
        if marker is not None:
            filters[self.model.get_id_property_name()] = dm_filters.GT(marker)
        if not order_by:
            order_by = {self.model.get_id_property_name(): "asc"}
        return filters, order_by

    def _paginate_result(self, result: typing.Any) -> typing.Any:
        if not self._pagination_limit:
            self._pagination_has_more = False
            return result
        probe = result[: self._pagination_limit + 1]
        self._pagination_has_more = len(probe) > self._pagination_limit
        return probe[: self._pagination_limit]

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        filters, order_by = self._store_query(filters, order_by)
        limit = self._pagination_limit or None
        fetch_limit = limit + 1 if limit is not None else None
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            result = db.filter_resources(
                self.resource_name,
                filters,
                order_by,
                limit=fetch_limit,
            )
        return self._paginate_result(result)

    def _create_response(
        self, body: typing.Any, status: typing.Any, headers: typing.Any
    ) -> typing.Any:
        if self._pagination_limit:
            headers[self._header_page_limit] = str(self._pagination_limit)
            if getattr(self, "_pagination_has_more", False):
                id_name = self.model.get_id_property_name()
                marker = (
                    body[-1][id_name]
                    if isinstance(body[-1], dict)
                    else getattr(body[-1], id_name)
                )
                headers[self._header_page_marker] = str(marker)
        return ra_controllers.Controller._create_response(
            self,
            body,
            status,
            headers,
        )

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.get_resource(
                self.resource_name,
                typing.cast(sys_uuid.UUID, uuid),
            )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.create_resource(self.resource_name, self._values(kwargs))

    def update(self, uuid: typing.Any, **kwargs: typing.Any) -> typing.Any:
        values = kwargs.copy()
        values.pop("project_id", None)
        values.pop("user_uuid", None)
        values.pop("uuid", None)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.update_resource(self.resource_name, uuid, values)

    def delete(self, uuid: object) -> typing.Any:
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.delete_resource(
                self.resource_name,
                typing.cast(sys_uuid.UUID, uuid),
            )

    def _action(
        self,
        resource: typing.Any,
        action: typing.Any,
        values: typing.Any = None,
    ) -> typing.Any:
        resource_uuid = (
            resource["uuid"] if isinstance(resource, dict) else resource.uuid
        )
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.perform_action(
                self.resource_name,
                resource_uuid,
                action,
                values or {},
            )


PUSH_DEVICE_RESOURCE = ra_resources.ResourceByRAModel(
    model_class=push_devices.PushDevice,
    convert_underscore=False,
    fields_permissions=ra_field_permissions.FieldsPermissions(
        fields={
            field: {
                ra_constants.ALL: ra_field_permissions.Permissions.RO,
            }
            for field in (
                "uuid",
                "project_id",
                "user_uuid",
                "created_at",
                "updated_at",
            )
        },
    ),
)
PUSH_DEVICE_RESPONSE_SCHEMA = PUSH_DEVICE_RESOURCE.generate_schema_object(
    ra_constants.GET,
    oa_c.OPENAPI_SPECIFICATION_3_0_3,
)
PUSH_DEVICE_UPDATE_RESPONSES = {
    200: {
        "description": "Existing push device registration replaced.",
        "content": {
            "application/json": {
                "schema": PUSH_DEVICE_RESPONSE_SCHEMA,
            },
        },
    },
    201: {
        "description": "Push device registration created.",
        "content": {
            "application/json": {
                "schema": PUSH_DEVICE_RESPONSE_SCHEMA,
            },
        },
    },
    "default": oa_c.DEFAULT_RESPONSE,
}


class PushDeviceController(ra_controllers.BaseResourceController):
    __resource__ = PUSH_DEVICE_RESOURCE
    __generate_location_for__ = ()

    def _owner_scope(self) -> tuple[typing.Any, typing.Any]:
        context = self.get_context()
        user_uuid = getattr(context, "user_uuid", None)
        project_id = getattr(context, "project_id", None)
        if user_uuid is None or project_id is None:
            raise ra_exc.ValidationErrorException()
        return project_id, user_uuid

    def _owner_filters(self, uuid: object) -> dict[str, typing.Any]:
        project_id, user_uuid = self._owner_scope()
        return {
            "uuid": dm_filters.EQ(uuid),
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
        }

    def _response(self, device: typing.Any) -> dict[str, typing.Any]:
        return ra_packers.BaseResourcePacker(
            PUSH_DEVICE_RESOURCE,
            self.request,
        ).pack_resource(device)

    @oa_utils.extend_schema(
        summary="Register or rotate a push device",
        responses=PUSH_DEVICE_UPDATE_RESPONSES,
    )
    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        project_id, user_uuid = self._owner_scope()
        session = contexts.Context().get_session()
        models.WorkspaceUser.objects.get_one(
            filters={"uuid": dm_filters.EQ(user_uuid)},
            session=session,
            locked=True,
        )
        candidate = self.model(
            uuid=uuid,
            project_id=project_id,
            user_uuid=user_uuid,
            **kwargs,
        )
        device = self.model.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(uuid)},
            session=session,
        )
        if device is None:
            candidate.insert(session=session)
            device = self.model.objects.get_one(
                filters={"uuid": dm_filters.EQ(uuid)},
                session=session,
            )
            return self._response(device), 201, {}
        if device.project_id != project_id or device.user_uuid != user_uuid:
            raise ra_exc.ResourceNotFoundError(
                resource="PushDevice",
                path=str(uuid),
            )
        device.update_dm(
            values={
                "transport": candidate.transport,
                "platform": candidate.platform,
                "registration_token": candidate.registration_token,
                "encryption": candidate.encryption,
            },
        )
        device.update(session=session)
        return self._response(device), 200, {}

    @oa_utils.extend_schema(
        summary="Delete a push device registration",
    )
    def delete(self, uuid: object) -> None:
        session = contexts.Context().get_session()
        device = self.model.objects.get_one_or_none(
            filters=self._owner_filters(uuid),
            session=session,
        )
        if device is not None:
            device.delete(session=session)


class FolderController(StoreResourceController):
    resource_name = "folders"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.UserFolder,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )


class FolderItemController(StoreResourceController):
    resource_name = "folder_items"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.UserFolderItem,
        convert_underscore=False,
        process_filters=True,
    )

    @ra_actions.post
    def pin(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "pin")

    @ra_actions.post
    def unpin(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "unpin")


class WorkspaceFileController(StoreResourceController):
    resource_name = "files"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceFile,
        hidden_fields=[
            "project_id",
            "acl_mode",
            "storage_type",
            "storage_id",
            "storage_object_id",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _multipart_value(part: typing.Any) -> typing.Any:
        return getattr(part, "value", part)

    @classmethod
    def _optional_part(
        cls, parts: typing.Any, name: typing.Any, default: typing.Any = None
    ) -> typing.Any:
        part = parts.get(name)
        return default if part is None else cls._multipart_value(part)

    @staticmethod
    def _content_disposition(file: typing.Any) -> typing.Any:
        name = file["name"]
        fallback_name = name
        try:
            fallback_name.encode("ascii")
        except UnicodeEncodeError:
            _stem, separator, extension = name.rpartition(".")
            suffix = (
                f".{extension}"
                if separator and re.fullmatch(r"[A-Za-z0-9]{1,16}", extension)
                else ""
            )
            fallback_name = f"download{suffix}"
        quoted_name = fallback_name.replace("\\", "\\\\").replace('"', '\\"')
        encoded_name = urllib.parse.quote(name, safe="")
        return (
            f"attachment; filename=\"{quoted_name}\"; filename*=UTF-8''{encoded_name}"
        )

    @classmethod
    def _multipart_scope(
        cls,
        parts: typing.Mapping[str, typing.Any],
    ) -> tuple[sys_uuid.UUID | None, str]:
        stream_value = cls._optional_part(parts, "stream_uuid")
        acl_value = cls._optional_part(parts, "acl")
        if acl_value is None:
            if stream_value is None:
                raise ra_exc.ValidationErrorException()
            try:
                return sys_uuid.UUID(stream_value), "stream_members"
            except (TypeError, ValueError) as exc:
                raise ra_exc.ValidationErrorException() from exc
        if stream_value is not None:
            raise ra_exc.ValidationErrorException()
        try:
            acl = acl_value if isinstance(acl_value, dict) else json.loads(acl_value)
        except (TypeError, ValueError) as exc:
            raise ra_exc.ValidationErrorException() from exc
        if acl != {"mode": "public"}:
            raise ra_exc.ValidationErrorException()
        return None, "public"

    def process_result(
        self, result: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        if isinstance(result, webob.Response):
            return result
        return super().process_result(result, *args, **kwargs)

    def _create_from_multipart(self, parts: typing.Any) -> typing.Any:
        file_part = parts["file"]
        file_uuid = sys_uuid.uuid4()
        stream_uuid, acl_mode = self._multipart_scope(parts)
        name = self._optional_part(parts, "name", file_part.filename)
        description = self._optional_part(parts, "description", "")
        file_part.file.seek(0)
        data = file_part.file.read()
        file_part.file.seek(0)
        sha256 = hashlib.sha256(data).hexdigest()
        storage_info = file_storage.save_workspace_file(
            file_uuid=file_uuid,
            data=data,
        )
        metadata = file_storage.WorkspaceFileMetadata(
            uuid=file_uuid,
            project_id=sys_uuid.UUID(str(self._get_project_id())),
            stream_uuid=stream_uuid,
            owner_uuid=sys_uuid.UUID(str(self._get_user_uuid())),
            name=name,
            description=description,
            content_type=file_part.type,
            size_bytes=len(data),
            sha256=sha256,
            created_at=datetime.datetime.now(datetime.timezone.utc),
            acl_mode=acl_mode,
        )
        try:
            file_storage.save_workspace_file_metadata(
                metadata,
                storage_type=storage_info.storage_type,
            )
        except Exception:
            file_storage.delete_workspace_file(
                file_uuid=file_uuid,
                storage_type=storage_info.storage_type,
                storage_object_id=storage_info.storage_object_id,
            )
            raise
        values = self._values(
            {
                "uuid": file_uuid,
                "stream_uuid": stream_uuid,
                "acl_mode": "public" if acl_mode == "public" else "stream",
                "name": name,
                "description": description,
                "content_type": file_part.type,
                "size_bytes": len(data),
                "hash": sha256,
                "storage_type": storage_info.storage_type,
                "storage_id": storage_info.storage_id,
                "storage_object_id": storage_info.storage_object_id,
            }
        )
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            # Once the sidecar exists, a failing database write must not erase
            # the already stored file bytes.
            return db.create_resource(self.resource_name, values)

    def create(self, **kwargs: typing.Any) -> typing.Any:
        if kwargs.pop("multipart", False):
            return self._create_from_multipart(kwargs["parts"])
        if kwargs.get("stream_uuid") is None:
            raise ra_exc.ValidationErrorException()
        file_uuid = kwargs.get("uuid") or sys_uuid.uuid4()
        kwargs.pop("storage_type", None)
        storage_info = file_storage.get_workspace_file_storage_info(
            file_uuid=file_uuid,
        )
        kwargs.update(
            {
                "uuid": file_uuid,
                "acl_mode": "stream",
                "storage_type": storage_info.storage_type,
                "storage_id": storage_info.storage_id,
                "storage_object_id": storage_info.storage_object_id,
            }
        )
        return super().create(**kwargs)

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        kwargs.pop("storage_type", None)
        return super().update(uuid, **kwargs)

    def delete(self, uuid: object) -> typing.Any:
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            resource_uuid = typing.cast(sys_uuid.UUID, uuid)
            file = db.get_resource(self.resource_name, resource_uuid)
            result = db.delete_resource(self.resource_name, resource_uuid)
        references = (
            contexts.Context()
            .get_session()
            .execute(
                """SELECT 1 FROM m_workspace_files WHERE storage_type=%s AND storage_id=%s AND storage_object_id=%s
            UNION ALL SELECT 1 FROM zulip_outgoing_intents WHERE entity_type='file' AND operation='insert' AND status<>'done'
              AND data->>'storage_type'=%s AND data->>'storage_id'=%s AND data->>'storage_object_id'=%s LIMIT 1""",
                (
                    file["storage_type"],
                    file["storage_id"],
                    file["storage_object_id"],
                    file["storage_type"],
                    file["storage_id"],
                    file["storage_object_id"],
                ),
            )
            .fetchone()
        )
        if not references:
            file_storage.delete_workspace_file(
                file_uuid=resource_uuid,
                storage_type=file["storage_type"],
                storage_object_id=file["storage_object_id"],
            )
        file_storage.delete_workspace_file_metadata(
            file_uuid=resource_uuid,
            storage_type=file["storage_type"],
        )
        return result

    @ra_actions.get
    def download(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        data = file_storage.read_workspace_file(
            file_uuid=resource["uuid"],
            storage_type=resource["storage_type"],
            storage_object_id=resource["storage_object_id"],
        )
        return webob.Response(
            body=data,
            status=200,
            headers={
                "Content-Type": resource["content_type"],
                "Content-Disposition": self._content_disposition(resource),
                "ETag": f'"{resource["hash"]}"',
                "Cache-Control": "private, no-cache",
            },
        )


setattr(
    WorkspaceFileController.create,
    "openapi_schema",
    oa_utils.Schema(
        summary="Upload file",
        parameters=(),
        responses=oa_c.build_openapi_create_response("WorkspaceFile_Create"),
        request_body={
            "description": "Create file metadata or upload workspace file bytes",
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": [
                            "stream_uuid",
                            "name",
                            "content_type",
                            "size_bytes",
                            "hash",
                        ],
                        "properties": {
                            "stream_uuid": {"format": "uuid", "type": "string"},
                            "name": {"type": "string"},
                            "description": {
                                "type": "string",
                                "maxLength": common_models.DESCRIPTION_MAX_LENGTH,
                            },
                            "content_type": {"type": "string"},
                            "size_bytes": {"minimum": 0, "type": "integer"},
                            "hash": {"type": "string"},
                        },
                    },
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"format": "binary", "type": "string"},
                            "stream_uuid": {"format": "uuid", "type": "string"},
                            "acl": {
                                "description": (
                                    "JSON ACL object. The only public form is "
                                    '{"mode":"public"}.'
                                ),
                                "pattern": (
                                    '^\\s*\\{\\s*"mode"\\s*:\\s*"public"\\s*\\}\\s*$'
                                ),
                                "type": "string",
                            },
                            "name": {"type": "string"},
                            "description": {
                                "type": "string",
                                "maxLength": common_models.DESCRIPTION_MAX_LENGTH,
                            },
                        },
                        "oneOf": [
                            {
                                "required": ["stream_uuid"],
                                "not": {"required": ["acl"]},
                            },
                            {
                                "required": ["acl"],
                                "not": {"required": ["stream_uuid"]},
                            },
                        ],
                    },
                },
            },
        },
    ),
)
WorkspaceFileController.download.openapi_schema = oa_utils.Schema(
    summary="Download file",
    parameters=(),
    responses=oa_c.build_openapi_response_octet_stream("Download file"),
)


class WorkspaceStreamController(StoreResourceController):
    resource_name = "streams"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserStream,
        hidden_fields=["private_index"],
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        peer_uuid = kwargs.get("direct_user_uuid")
        if peer_uuid is not None:
            if "private" in kwargs and kwargs["private"] is not True:
                raise messenger_exc.StreamIdentityImmutableError(fields="private")
            kwargs["uuid"] = helpers.deterministic_direct_stream_uuid(
                self._get_project_id(),
                self._get_user_uuid(),
                peer_uuid,
            )
            kwargs["private"] = True
        return super().create(**kwargs)

    @ra_actions.post
    def archive(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "archive")

    @ra_actions.post
    def unarchive(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "unarchive")

    @ra_actions.post
    def notifications(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        return self._action(resource, "notifications", kwargs)

    @ra_actions.post
    def read(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "read")


class WorkspaceStreamBindingController(StoreResourceController):
    resource_name = "stream_bindings"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceStreamBinding,
        convert_underscore=False,
        process_filters=True,
    )

    @ra_actions.post
    def add_users(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        return self._action(resource, "add_users", kwargs)


class WorkspaceMessageController(StoreResourceController):
    resource_name = "messages"
    __default_sort__ = {"created_at": "asc"}
    __sortable_fields__ = ("created_at",)
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserMessage,
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        order_by = order_by or self.__default_sort__
        if tuple(order_by) != ("created_at",):
            raise ra_exc.ValidationErrorException()
        sort_direction = order_by["created_at"].lower()
        if sort_direction not in {"asc", "desc"}:
            raise ra_exc.ValidationErrorException()
        limit = self._pagination_limit or None
        fetch_limit = None if limit is None else limit + 1
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            result = db.filter_message_page(
                filters=filters,
                marker_uuid=getattr(self, "_pagination_marker", None),
                sort_direction=sort_direction,
                limit=fetch_limit,
            )
        self._message_page_has_more = limit is not None and len(result) > limit
        return result if limit is None else result[:limit]

    def _create_response(
        self, body: typing.Any, status: typing.Any, headers: typing.Any
    ) -> typing.Any:
        if self._pagination_limit:
            headers[self._header_page_limit] = str(self._pagination_limit)
            if self._message_page_has_more:
                marker = (
                    body[-1]["uuid"] if isinstance(body[-1], dict) else body[-1].uuid
                )
                headers[self._header_page_marker] = str(marker)
        return ra_controllers.Controller._create_response(
            self,
            body,
            status,
            headers,
        )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        values = self._values(kwargs)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.create_message(values)

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        values = kwargs.copy()
        for name in ("project_id", "user_uuid", "uuid"):
            values.pop(name, None)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.update_message(typing.cast(sys_uuid.UUID, uuid), values)

    def delete(self, uuid: object) -> typing.Any:
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.delete_message(typing.cast(sys_uuid.UUID, uuid))

    @ra_actions.post
    def read(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "read")

    @ra_actions.post
    def read_up_to(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "read_up_to")

    @ra_actions.post
    def star(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "star")

    @ra_actions.post
    def unstar(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "unstar")


class WorkspaceDraftController(StoreResourceController):
    resource_name = "drafts"
    __default_sort__ = {"updated_at": "asc"}
    __sortable_fields__ = ("updated_at",)
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceDraft,
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _etag(revision: int) -> str:
        return f'"{revision}"'

    def _required_if_match(self) -> typing.Any:
        value = self.request.headers.get("If-Match")
        if value is None:
            raise messenger_exc.DraftPreconditionRequiredError()
        if re.fullmatch(r'"[1-9][0-9]*"', value) is None:
            return 0
        return int(value[1:-1])

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        order_by = order_by or self.__default_sort__
        if tuple(order_by) != ("updated_at",):
            raise ra_exc.ValidationErrorException()
        sort_direction = order_by["updated_at"].lower()
        if sort_direction not in {"asc", "desc"}:
            raise ra_exc.ValidationErrorException()
        limit = self._pagination_limit or None
        fetch_limit = None if limit is None else limit + 1
        with api_store.open_draft_store(
            self._get_project_id(),
            self._get_user_uuid(),
        ) as db:
            result = db.filter_draft_page(
                filters=filters,
                marker_uuid=getattr(self, "_pagination_marker", None),
                sort_direction=sort_direction,
                limit=fetch_limit,
            )
        self._draft_page_has_more = limit is not None and len(result) > limit
        return result if limit is None else result[:limit]

    def _create_response(
        self, body: typing.Any, status: typing.Any, headers: typing.Any
    ) -> typing.Any:
        if self._pagination_limit:
            headers[self._header_page_limit] = str(self._pagination_limit)
            if self._draft_page_has_more:
                marker = (
                    body[-1]["uuid"] if isinstance(body[-1], dict) else body[-1].uuid
                )
                headers[self._header_page_marker] = str(marker)
        elif isinstance(body, dict) and "revision" in body:
            headers["ETag"] = self._etag(body["revision"])
        if status == 201 and getattr(self, "_draft_create_existing", False):
            status = 200
        return ra_controllers.Controller._create_response(
            self,
            body,
            status,
            headers,
        )

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        with api_store.open_draft_store(
            self._get_project_id(),
            self._get_user_uuid(),
        ) as db:
            return db.get_draft(typing.cast(sys_uuid.UUID, uuid))

    def create(self, **kwargs: typing.Any) -> typing.Any:
        required = {"uuid", "stream_uuid", "topic_uuid", "payload"}
        if set(kwargs) != required:
            raise ra_exc.ValidationErrorException()
        values = kwargs.copy()
        values.update(
            {
                "project_id": self._get_project_id(),
                "user_uuid": self._get_user_uuid(),
            }
        )
        with api_store.open_draft_store(
            self._get_project_id(),
            self._get_user_uuid(),
        ) as db:
            result, created = db.create_draft(values)
        self._draft_create_existing = not created
        return result

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        if set(kwargs) != {"payload"}:
            raise ra_exc.ValidationErrorException()
        expected_revision = self._required_if_match()
        with api_store.open_draft_store(
            self._get_project_id(),
            self._get_user_uuid(),
        ) as db:
            return db.update_draft(
                typing.cast(sys_uuid.UUID, uuid),
                kwargs["payload"],
                expected_revision,
            )

    def delete(self, uuid: object) -> typing.Any:
        expected_revision = self._required_if_match()
        with api_store.open_draft_store(
            self._get_project_id(),
            self._get_user_uuid(),
        ) as db:
            return db.delete_draft(
                typing.cast(sys_uuid.UUID, uuid),
                expected_revision,
            )


class WorkspaceMutationStatusController(StoreResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=v2_models.WorkspaceMutationRequest, convert_underscore=False
    )

    def get(
        self, uuid: object, **kwargs: typing.Any
    ) -> v2_models.WorkspaceMutationRequest:
        return v2_models.WorkspaceMutationRequest.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "user_uuid": dm_filters.EQ(self._get_user_uuid()),
            }
        )


class WorkspaceMessageReactionController(StoreResourceController):
    resource_name = "message_reactions"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceMessageReactions,
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        return super().create(**kwargs)

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        return super().update(uuid, **kwargs)


class WorkspaceEventController(StoreResourceController):
    resource_name = "events"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceEvent,
        convert_underscore=False,
        process_filters=True,
    )
    __default_sort__ = {"epoch_version": "asc"}

    def _prepare_filters(self, params: typing.Any) -> typing.Any:
        generations = params.getall("epoch_generation")
        if len(generations) > 1 or (generations and not generations[0]):
            raise ra_exc.ParseError(value=generations)
        self._epoch_generation = generations[0] if generations else None
        event_params = multidict.MultiDict(
            (name, value)
            for name, value in params.items()
            if name != "epoch_generation"
        )
        return super()._prepare_filters(event_params)

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        filters, order_by = self._store_query(filters, order_by)
        limit = self._pagination_limit or None
        fetch_limit = limit + 1 if limit is not None else None
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            epoch_generation = getattr(self, "_epoch_generation", None)
            if epoch_generation is None:
                result = db.events_after(filters, order_by, limit=fetch_limit)
            else:
                result = db.events_after(
                    filters,
                    order_by,
                    epoch_generation=epoch_generation,
                    limit=fetch_limit,
                )
        return self._paginate_result(result)


class WorkspaceEpochController(StoreResourceController):
    resource_name = "epoch"

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        del filters, order_by
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            cursor = db.event_cursor()
        return {
            "epoch_version": cursor["current_epoch_version"],
            **cursor,
        }


class ExternalResourceController(ra_controllers.BaseResourceControllerPaginated):
    __generate_location_for__ = ()

    def _get_user_uuid(self) -> typing.Any:
        return self.get_context().user_uuid

    def _get_project_id(self) -> typing.Any:
        return self.get_context().project_id

    def get_packer(
        self, content_type: typing.Any, resource_type: typing.Any = None
    ) -> typing.Any:
        return ContractJSONPacker(
            resource_type or self.get_resource(),
            request=self.request,
        )

    def _create_response(
        self, body: typing.Any, status: typing.Any, headers: typing.Any
    ) -> typing.Any:
        return ra_controllers.Controller._create_response(self, body, status, headers)

    def _owner_filters(self, filters: typing.Any) -> typing.Any:
        result = (filters or {}).copy()
        result["owner_user_uuid"] = dm_filters.EQ(self._get_user_uuid())
        return result

    def _emit_event(
        self,
        resource: typing.Any,
        kind: typing.Any,
        hidden_fields: typing.Any = (),
        session: typing.Any = None,
    ) -> typing.Any:
        project_id = self.get_context().project_id
        if project_id is None:
            raise ra_exc.ValidationErrorException()
        return messenger_events.create_external_resource_event(
            project_id,
            self._get_user_uuid(),
            resource,
            kind,
            hidden_fields=hidden_fields,
            session=session,
        )

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        return super().filter(self._owner_filters(filters), order_by=order_by)

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        return self.model.objects.get_one(
            filters=self._owner_filters({"uuid": dm_filters.EQ(uuid)}),
        )


class ExternalAccountController(ExternalResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalAccount,
        fields_permissions=ra_field_permissions.FieldsPermissions(
            fields={
                "external_account_id": {
                    ra_constants.ALL: ra_field_permissions.Permissions.RO
                }
            }
        ),
        hidden_fields=["owner_user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )

    def _owner_filters(self, filters: typing.Any) -> typing.Any:
        result = super()._owner_filters(filters)
        result["project_id"] = dm_filters.EQ(self._get_project_id())
        return result

    def _require_permission(self, action: str) -> None:
        permissions = (
            self.get_context().iam_context.get_introspection_info().permissions
        )
        if action not in permissions:
            raise messenger_exc.ExternalResourceForbiddenError()

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        self._require_permission("workspace.external_account.read")
        return super().filter(filters, order_by=order_by)

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._require_permission("workspace.external_account.read")
        return super().get(uuid, **kwargs)

    def create(self, **kwargs: typing.Any) -> typing.Any:
        self._require_permission("workspace.external_account.create")
        return application_services.ExternalAccountApplicationService.create(
            contexts.Context().get_session(),
            application_services.ExternalAccountActor(
                user_uuid=self._get_user_uuid(),
                project_id=self._get_project_id(),
            ),
            kwargs,
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._require_permission("workspace.external_account.update")
        session = contexts.Context().get_session()
        account = self.model.objects.get_one(
            filters=self._owner_filters({"uuid": dm_filters.EQ(uuid)}),
            session=session,
        )
        return application_services.ExternalAccountApplicationService.update(
            session,
            application_services.ExternalAccountActor(
                user_uuid=self._get_user_uuid(),
                project_id=self._get_project_id(),
            ),
            account,
            kwargs,
        )

    def delete(self, uuid: object) -> None:
        self._require_permission("workspace.external_account.delete")
        account = super().get(uuid)
        application_services.ExternalAccountApplicationService.delete(
            contexts.Context().get_session(),
            application_services.ExternalAccountActor(
                user_uuid=self._get_user_uuid(),
                project_id=self._get_project_id(),
            ),
            account,
        )


class TopicSummaryEndpointController(
    ra_controllers.BaseResourceController,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceLLMEndpoint,
        hidden_fields=["claim_token"],
        convert_underscore=False,
        process_filters=True,
    )

    def get_packer(
        self, content_type: typing.Any, resource_type: typing.Any = None
    ) -> typing.Any:
        return TopicSummaryManagementJSONPacker(
            resource_type or self.get_resource(),
            request=self.request,
        )

    def _require_permission(self) -> None:
        permissions = (
            self.get_context().iam_context.get_introspection_info().permissions
        )
        if topic_summarization.ENDPOINT_MANAGE_PERMISSION not in permissions:
            raise messenger_exc.ExternalResourceForbiddenError()

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        self._require_permission()
        if filters or order_by:
            raise ra_exc.ValidationErrorException()
        return topic_summarization.list_endpoints(
            contexts.Context().get_session(),
        )

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        self._require_permission()
        return models.WorkspaceLLMEndpoint.objects.get_one(
            filters={"uuid": dm_filters.EQ(uuid)},
        )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        self._require_permission()
        return topic_summarization.create_endpoint(
            contexts.Context().get_session(),
            kwargs,
            topic_summarization.configured_secret_key(),
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._require_permission()
        endpoint = models.WorkspaceLLMEndpoint.objects.get_one(
            filters={"uuid": dm_filters.EQ(uuid)},
        )
        return topic_summarization.update_endpoint(
            contexts.Context().get_session(),
            endpoint,
            kwargs,
            topic_summarization.configured_secret_key(),
        )

    def delete(self, uuid: object) -> None:
        self._require_permission()
        endpoint = models.WorkspaceLLMEndpoint.objects.get_one(
            filters={"uuid": dm_filters.EQ(uuid)},
        )
        endpoint.delete(session=contexts.Context().get_session())


class TopicSummarySettingsController(ra_controllers.BaseResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceTopicSummarySettings,
        convert_underscore=False,
        process_filters=True,
    )

    def get_packer(
        self, content_type: typing.Any, resource_type: typing.Any = None
    ) -> typing.Any:
        return TopicSummaryManagementJSONPacker(
            resource_type or self.get_resource(),
            request=self.request,
        )

    def _project_id(self, value: object) -> sys_uuid.UUID:
        project_id = self.get_context().project_id
        if project_id is None:
            raise ra_exc.ValidationErrorException()
        try:
            resource_project_id = sys_uuid.UUID(str(value))
            context_project_id = sys_uuid.UUID(str(project_id))
        except (TypeError, ValueError) as exc:
            raise ra_exc.ValidationErrorException() from exc
        if resource_project_id != context_project_id:
            raise messenger_exc.ExternalResourceForbiddenError()
        return context_project_id

    def _require_update_permission(self) -> None:
        permissions = (
            self.get_context().iam_context.get_introspection_info().permissions
        )
        if topic_summarization.SETTINGS_MANAGE_PERMISSION not in permissions:
            raise messenger_exc.ExternalResourceForbiddenError()

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        return topic_summarization.get_topic_summary_settings(
            contexts.Context().get_session(),
            self._project_id(uuid),
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._require_update_permission()
        return topic_summarization.update_topic_summary_settings(
            contexts.Context().get_session(),
            self._project_id(uuid),
            kwargs,
        )


class WorkspaceStreamTopicController(StoreResourceController):
    resource_name = "stream_topics"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserTopic,
        convert_underscore=False,
        process_filters=True,
    )

    @ra_actions.post
    def toggle_done(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "toggle_done")

    @ra_actions.post
    def notifications(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        return self._action(resource, "notifications", kwargs)

    @ra_actions.post
    def set_default(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "set_default")

    @ra_actions.post
    def set_summary_prompt(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        if not set(kwargs) or not set(kwargs) <= {
            "summary_system_prompt",
            "summary_reasoning_effort",
            "summary_enabled",
        }:
            raise ra_exc.ValidationErrorException()
        return self._action(resource, "set_summary_prompt", kwargs)

    @ra_actions.post
    def read(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "read")


class WorkspaceUserController(StoreResourceController):
    resource_name = "users"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUser,
        hidden_fields=[],
        convert_underscore=False,
        process_filters=True,
    )

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            if uuid == self._get_user_uuid():
                iam_user = (
                    self.get_context().iam_context.get_introspection_info().user_info
                )
                db.sync_iam_identity(
                    {
                        "user_uuid": uuid,
                        "username": iam_user.name,
                        "first_name": iam_user.first_name,
                        "last_name": iam_user.last_name,
                        "email": iam_user.email,
                    }
                )
            return db.get_resource(
                self.resource_name,
                typing.cast(sys_uuid.UUID, uuid),
            )

    @staticmethod
    def _resource_value(resource: typing.Any, name: typing.Any) -> typing.Any:
        return resource[name] if isinstance(resource, dict) else getattr(resource, name)

    def _replaced_avatar_file(self, db: typing.Any, resource: typing.Any) -> typing.Any:
        avatar = self._resource_value(resource, "avatar")
        if not avatar.startswith(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX):
            return None
        file_uuid = sys_uuid.UUID(
            avatar[len(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX) :]
        )
        return db.get_resource("files", file_uuid)

    @staticmethod
    def _delete_avatar_storage(file: typing.Any) -> None:
        if file is None:
            return
        try:
            file_storage.delete_workspace_file(
                file_uuid=file["uuid"],
                storage_type=file["storage_type"],
                storage_object_id=file["storage_object_id"],
            )
            file_storage.delete_workspace_file_metadata(
                file_uuid=file["uuid"],
                storage_type=file["storage_type"],
            )
        except Exception:
            LOG.exception("Failed to remove replaced avatar storage object")

    @staticmethod
    def _validate_avatar(file_part: typing.Any, data: typing.Any) -> typing.Any:
        content_type = _normalize_avatar_content_type(file_part.type)
        if (
            not data
            or len(data) > MAX_AVATAR_SIZE_BYTES
            or content_type not in AVATAR_CONTENT_TYPES
            or not _valid_avatar_bytes(content_type, data)
        ):
            raise ra_exc.ValidationErrorException()
        return content_type

    @ra_actions.post
    def avatar_upload(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        resource_uuid = sys_uuid.UUID(str(self._resource_value(resource, "uuid")))
        if resource_uuid != sys_uuid.UUID(str(self._get_user_uuid())) or not kwargs.pop(
            "multipart", False
        ):
            raise ra_exc.ValidationErrorException()
        file_part = kwargs["parts"]["file"]
        file_part.file.seek(0)
        data = file_part.file.read()
        file_part.file.seek(0)
        content_type = self._validate_avatar(file_part, data)
        file_uuid = sys_uuid.uuid4()
        sha256 = hashlib.sha256(data).hexdigest()
        storage_info = file_storage.save_workspace_file(
            file_uuid=file_uuid,
            data=data,
        )
        metadata = file_storage.WorkspaceFileMetadata(
            uuid=file_uuid,
            project_id=sys_uuid.UUID(str(self._get_project_id())),
            stream_uuid=None,
            owner_uuid=sys_uuid.UUID(str(self._get_user_uuid())),
            name=file_part.filename,
            description="Workspace user avatar",
            content_type=content_type,
            size_bytes=len(data),
            sha256=sha256,
            created_at=datetime.datetime.now(datetime.timezone.utc),
            acl_mode="public",
        )
        try:
            file_storage.save_workspace_file_metadata(
                metadata,
                storage_type=storage_info.storage_type,
            )
            with api_store.open_store(
                self._get_project_id(), self._get_user_uuid()
            ) as db:
                replaced_file = self._replaced_avatar_file(db, resource)
                result = db.perform_action(
                    self.resource_name,
                    resource_uuid,
                    "avatar_upload",
                    {
                        "uuid": file_uuid,
                        "name": file_part.filename,
                        "description": "Workspace user avatar",
                        "content_type": content_type,
                        "size_bytes": len(data),
                        "hash": sha256,
                        "storage_type": storage_info.storage_type,
                        "storage_id": storage_info.storage_id,
                        "storage_object_id": storage_info.storage_object_id,
                    },
                )
        except Exception:
            file_storage.delete_workspace_file(
                file_uuid=file_uuid,
                storage_type=storage_info.storage_type,
                storage_object_id=storage_info.storage_object_id,
            )
            file_storage.delete_workspace_file_metadata(
                file_uuid=file_uuid,
                storage_type=storage_info.storage_type,
            )
            raise
        self._delete_avatar_storage(replaced_file)
        return result

    @ra_actions.post
    def avatar_reset(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        resource_uuid = sys_uuid.UUID(str(self._resource_value(resource, "uuid")))
        if resource_uuid != sys_uuid.UUID(str(self._get_user_uuid())):
            raise ra_exc.ValidationErrorException()
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            replaced_file = self._replaced_avatar_file(db, resource)
            result = db.perform_action(
                self.resource_name,
                resource_uuid,
                "avatar_reset",
                {},
            )
        self._delete_avatar_storage(replaced_file)
        return result

    @ra_actions.post
    def presence(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        return self._action(resource, "presence", kwargs)


WorkspaceUserController.avatar_upload.openapi_schema = oa_utils.Schema(
    summary="Upload own avatar",
    parameters=(),
    responses=oa_c.build_openapi_create_response("WorkspaceUser_AvatarUpload"),
    request_body=_build_openapi_multipart_request_body(
        description="Upload own Workspace avatar",
        properties={
            "file": {"format": "binary", "type": "string"},
        },
        required=("file",),
    ),
)


class MeController(WorkspaceUserController):
    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        del filters, order_by
        return self.get(self._get_user_uuid())


setattr(
    MeController.filter,
    "openapi_schema",
    oa_utils.Schema(
        summary="Get current Workspace user",
        parameters=(),
        responses=oa_c.build_openapi_get_update_response("WorkspaceUser_Get"),
    ),
)
