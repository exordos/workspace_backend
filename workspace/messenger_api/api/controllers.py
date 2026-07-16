# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
import logging
import typing
import urllib.parse
import uuid as sys_uuid

import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils
from webob import multidict

from workspace.messenger_api import file_storage
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import versions
from workspace.messenger_api.dm import models
from workspace.messenger_mail import repository as mail_repository


LOG = logging.getLogger(__name__)
MAX_AVATAR_SIZE_BYTES = 25 * 1024 * 1024
AVATAR_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _normalize_avatar_content_type(value):
    value = value.lower()
    return "image/jpeg" if value == "image/jpg" else value


def _valid_avatar_bytes(content_type, data):
    signatures = {
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
    }
    if content_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return any(data.startswith(prefix) for prefix in signatures[content_type])


def _build_openapi_multipart_request_body(*, description, properties, required):
    request_body = oa_c.build_openapi_req_body_multipart(
        description=description,
        properties=properties,
    )
    schema = request_body["content"]["multipart/form-data"]["schema"]
    schema["type"] = "object"
    schema["required"] = list(required)
    return request_body


class ApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


class ContractJSONPacker(ra_packers.JSONPacker):
    """Apply resource visibility to dictionaries returned by the mail store."""

    def pack_resource(self, obj):
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
        for extension_name in ("provider", "delivery"):
            if extension_name in obj:
                result[extension_name] = obj[extension_name]
        return result


class ContractMultipartPacker(ra_packers.MultipartPacker):
    pack_resource = ContractJSONPacker.pack_resource


class StoreResourceController(ra_controllers.BaseResourceControllerPaginated):
    __generate_location_for__ = ()
    resource_name = ""
    _filter_operator_suffixes = (
        ("=>", dm_filters.GE),
        ("=<", dm_filters.LE),
        (">", dm_filters.GT),
        ("<", dm_filters.LT),
    )

    def _get_user_uuid(self):
        return self.get_context().user_uuid

    def _get_project_id(self):
        context = self.get_context()
        project_id = getattr(context, "project_id", None)
        if project_id is None:
            raise ra_exc.ValidationErrorException()
        return project_id

    def get_packer(self, content_type, resource_type=None):
        packer = (
            ContractMultipartPacker
            if "multipart/form-data" in content_type
            else ContractJSONPacker
        )
        return packer(resource_type or self.get_resource(), request=self.request)

    def _values(self, values):
        result = values.copy()
        result["project_id"] = self._get_project_id()
        result["user_uuid"] = self._get_user_uuid()
        result.setdefault("uuid", sys_uuid.uuid4())
        result.setdefault("source_name", "native")
        result.setdefault("source", {"kind": "native"})
        result.setdefault("provider", None)
        result.setdefault("delivery", None)
        return result

    @classmethod
    def _split_filter_operator(cls, name):
        for suffix, operator in cls._filter_operator_suffixes:
            if name.endswith(suffix):
                return name[: -len(suffix)], operator
        return name, None

    def _prepare_filters(self, params):
        regular = []
        conditional = {}
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

    def _store_query(self, filters, order_by):
        filters = filters.copy()
        marker = getattr(self, "_pagination_marker", None)
        if marker is not None:
            filters[self.model.get_id_property_name()] = dm_filters.GT(marker)
        if order_by is None:
            order_by = {self.model.get_id_property_name(): "asc"}
        return filters, order_by

    def filter(self, filters, order_by=None):
        filters, order_by = self._store_query(filters, order_by)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            result = db.filter_resources(self.resource_name, filters, order_by)
        if self._pagination_limit:
            return result[: self._pagination_limit]
        return result

    def _create_response(self, body, status, headers):
        if self._pagination_limit:
            headers[self._header_page_limit] = str(self._pagination_limit)
            if len(body) == self._pagination_limit:
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

    def get(self, uuid, **kwargs):
        del kwargs
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.get_resource(self.resource_name, uuid)

    def create(self, **kwargs):
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.create_resource(self.resource_name, self._values(kwargs))

    def update(self, uuid, **kwargs):
        values = kwargs.copy()
        values.pop("project_id", None)
        values.pop("user_uuid", None)
        values.pop("uuid", None)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.update_resource(self.resource_name, uuid, values)

    def delete(self, uuid):
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.delete_resource(self.resource_name, uuid)

    def _action(self, resource, action, values=None):
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
    def pin(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "pin")

    @ra_actions.post
    def unpin(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "unpin")


class WorkspaceFileController(StoreResourceController):
    resource_name = "files"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceFile,
        hidden_fields=["project_id", "storage_id", "storage_object_id"],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _multipart_value(part):
        return getattr(part, "value", part)

    @classmethod
    def _optional_part(cls, parts, name, default=None):
        part = parts.get(name)
        return default if part is None else cls._multipart_value(part)

    @staticmethod
    def _content_disposition(file):
        name = file["name"]
        quoted_name = name.replace("\\", "\\\\").replace('"', '\\"')
        encoded_name = urllib.parse.quote(name)
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

    def process_result(self, result, *args, **kwargs):
        if isinstance(result, webob.Response):
            return result
        return super().process_result(result, *args, **kwargs)

    def _create_from_multipart(self, parts):
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
            storage_type=self._optional_part(parts, "storage_type"),
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
            # Once the sidecar exists, a failing SQL projection must not erase
            # canonical storage: IMAP replay can repair the disposable row.
            return db.create_resource(self.resource_name, values)

    def create(self, **kwargs):
        if kwargs.pop("multipart", False):
            return self._create_from_multipart(kwargs["parts"])
        if kwargs.get("stream_uuid") is None:
            raise ra_exc.ValidationErrorException()
        file_uuid = kwargs.get("uuid") or sys_uuid.uuid4()
        storage_info = file_storage.get_workspace_file_storage_info(
            file_uuid=file_uuid,
            storage_type=kwargs.pop("storage_type", None),
        )
        kwargs.update(
            {
                "uuid": file_uuid,
                "storage_type": storage_info.storage_type,
                "storage_id": storage_info.storage_id,
                "storage_object_id": storage_info.storage_object_id,
            }
        )
        return super().create(**kwargs)

    def delete(self, uuid):
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            file = db.get_resource(self.resource_name, uuid)
            result = db.delete_resource(self.resource_name, uuid)
        file_storage.delete_workspace_file(
            file_uuid=uuid,
            storage_type=file["storage_type"],
            storage_object_id=file["storage_object_id"],
        )
        file_storage.delete_workspace_file_metadata(
            file_uuid=uuid,
            storage_type=file["storage_type"],
        )
        return result

    @ra_actions.get
    def download(self, resource, *args, **kwargs):
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


WorkspaceFileController.create.openapi_schema = oa_utils.Schema(
    summary="Upload file",
    parameters=(),
    responses=oa_c.build_openapi_create_response("WorkspaceFile_Create"),
    request_body=_build_openapi_multipart_request_body(
        description="Upload workspace file",
        properties={
            "file": {"format": "binary", "type": "string"},
            "stream_uuid": {"format": "uuid", "type": "string"},
            "acl": {
                "description": (
                    'JSON ACL object. Use {"mode":"public"} for authenticated '
                    "Workspace-wide access."
                ),
                "type": "string",
            },
            "name": {"type": "string"},
            "description": {"type": "string"},
            "storage_type": {"enum": ["file", "s3"], "type": "string"},
        },
        required=("file",),
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

    def create(self, **kwargs):
        peer_uuid = kwargs.get("direct_user_uuid")
        if peer_uuid is not None:
            if peer_uuid == self._get_user_uuid():
                raise ra_exc.ValidationErrorException()
            kwargs["uuid"] = mail_repository.deterministic_dm_uuid(
                self._get_project_id(),
                self._get_user_uuid(),
                peer_uuid,
            )
            kwargs["private"] = True
        return super().create(**kwargs)

    @ra_actions.post
    def archive(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "archive")

    @ra_actions.post
    def unarchive(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "unarchive")

    @ra_actions.post
    def notifications(self, resource, *args, **kwargs):
        del args
        return self._action(resource, "notifications", kwargs)

    @ra_actions.post
    def read(self, resource, *args, **kwargs):
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
    def add_users(self, resource, *args, **kwargs):
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

    def filter(self, filters, order_by=None):
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

    def _create_response(self, body, status, headers):
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

    def create(self, **kwargs):
        values = self._values(kwargs)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.create_message(values)

    def update(self, uuid, **kwargs):
        values = kwargs.copy()
        for name in ("project_id", "user_uuid", "uuid"):
            values.pop(name, None)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.update_message(uuid, values)

    def delete(self, uuid):
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            return db.delete_message(uuid)

    @ra_actions.post
    def read(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "read")

    @ra_actions.post
    def read_up_to(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "read_up_to")


class WorkspaceMessageReactionController(StoreResourceController):
    resource_name = "message_reactions"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceMessageReactions,
        convert_underscore=False,
        process_filters=True,
    )


class WorkspaceEventController(StoreResourceController):
    resource_name = "events"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceEvent,
        convert_underscore=False,
        process_filters=True,
    )
    __default_sort__ = {"epoch_version": "asc"}

    def _prepare_filters(self, params):
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

    def filter(self, filters, order_by=None):
        filters, order_by = self._store_query(filters, order_by)
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            epoch_generation = getattr(self, "_epoch_generation", None)
            if epoch_generation is None:
                result = db.events_after(filters, order_by)
            else:
                result = db.events_after(
                    filters,
                    order_by,
                    epoch_generation=epoch_generation,
                )
        if self._pagination_limit:
            return result[: self._pagination_limit]
        return result


class WorkspaceEpochController(StoreResourceController):
    resource_name = "epoch"

    def filter(self, filters, order_by=None):
        del filters, order_by
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            cursor = db.event_cursor()
        return {
            "epoch_version": cursor["current_epoch_version"],
            **cursor,
        }


class WorkspaceStreamTopicController(StoreResourceController):
    resource_name = "stream_topics"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserTopic,
        convert_underscore=False,
        process_filters=True,
    )

    @ra_actions.post
    def toggle_done(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "toggle_done")

    @ra_actions.post
    def notifications(self, resource, *args, **kwargs):
        del args
        return self._action(resource, "notifications", kwargs)

    @ra_actions.post
    def set_default(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "set_default")

    @ra_actions.post
    def read(self, resource, *args, **kwargs):
        del args, kwargs
        return self._action(resource, "read")


class WorkspaceUserController(StoreResourceController):
    resource_name = "users"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUser,
        convert_underscore=False,
        process_filters=True,
    )

    def get(self, uuid, **kwargs):
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
            return db.get_resource(self.resource_name, uuid)

    @staticmethod
    def _resource_value(resource, name):
        return resource[name] if isinstance(resource, dict) else getattr(resource, name)

    def _replaced_avatar_file(self, db, resource):
        avatar = self._resource_value(resource, "avatar")
        if not avatar.startswith(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX):
            return None
        file_uuid = sys_uuid.UUID(
            avatar[len(models.WORKSPACE_USER_IMAGE_AVATAR_PREFIX) :]
        )
        return db.get_resource("files", file_uuid)

    @staticmethod
    def _delete_avatar_storage(file):
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
    def _validate_avatar(file_part, data):
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
    def avatar_upload(self, resource, *args, **kwargs):
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
    def avatar_reset(self, resource, *args, **kwargs):
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
    def presence(self, resource, *args, **kwargs):
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
    def filter(self, filters, order_by=None):
        del filters, order_by
        return self.get(self._get_user_uuid())


MeController.filter.openapi_schema = oa_utils.Schema(
    summary="Get current Workspace user",
    parameters=(),
    responses=oa_c.build_openapi_get_update_response("WorkspaceUser_Get"),
)
