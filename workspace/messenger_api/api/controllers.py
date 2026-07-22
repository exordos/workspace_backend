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

from cryptography import x509
from cryptography.hazmat.primitives import serialization
import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils
from webob import multidict

from workspace.messenger_api import file_storage
from workspace.messenger_api import application_services
from workspace.messenger_api import credential_crypto
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api import external_projection
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import versions
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import sql_state


LOG = logging.getLogger(__name__)
MAX_AVATAR_SIZE_BYTES = 25 * 1024 * 1024
AVATAR_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _journal_projection_move(
    chat_uuid: object,
    revision: typing.Any,
    owner_uuid: object,
    stream_uuid: object,
    old_project_uuid: object,
    new_project_uuid: object = None,
    *,
    write_new: typing.Any = True,
    write_old: typing.Any = True,
) -> typing.Any:
    """Move a projection through the configured canonical storage adapter."""
    return api_store.move_stream_projection(
        chat_uuid=chat_uuid,
        revision=revision,
        owner_uuid=owner_uuid,
        stream_uuid=stream_uuid,
        old_project_uuid=old_project_uuid,
        new_project_uuid=new_project_uuid,
        write_new=write_new,
        write_old=write_old,
    )


def _move_projection_rows(
    session: typing.Any,
    stream_uuid: object,
    old_project_uuid: object,
    new_project_uuid: object,
) -> None:
    identifiers = session.execute(
        """
        SELECT
          ARRAY(SELECT uuid FROM m_workspace_stream_topics WHERE stream_uuid = %s)
            AS topic_uuids,
          ARRAY(SELECT uuid FROM m_workspace_messages WHERE stream_uuid = %s)
            AS message_uuids,
          ARRAY(SELECT uuid FROM m_workspace_files WHERE stream_uuid = %s)
            AS file_uuids
        """,
        (stream_uuid, stream_uuid, stream_uuid),
    ).fetchone()
    topic_uuids = identifiers["topic_uuids"]
    message_uuids = identifiers["message_uuids"]
    file_uuids = identifiers["file_uuids"]
    session.execute(
        "DELETE FROM m_workspace_drafts WHERE stream_uuid = %s AND project_id = %s",
        (stream_uuid, old_project_uuid),
    )
    session.execute(
        "DELETE FROM m_folder_items WHERE stream_uuid = %s AND project_id = %s",
        (stream_uuid, old_project_uuid),
    )
    for table, predicate, values in (
        ("m_workspace_stream_bindings", "stream_uuid = %s", (stream_uuid,)),
        ("m_workspace_stream_topics", "stream_uuid = %s", (stream_uuid,)),
        ("m_workspace_messages", "stream_uuid = %s", (stream_uuid,)),
        ("m_workspace_files", "stream_uuid = %s", (stream_uuid,)),
        ("m_workspace_user_topic_flags", "uuid = ANY(%s)", (topic_uuids,)),
        ("m_workspace_user_message_flags", "uuid = ANY(%s)", (message_uuids,)),
        ("m_workspace_message_reactions", "message_uuid = ANY(%s)", (message_uuids,)),
        ("m_workspace_file_accesses", "file_uuid = ANY(%s)", (file_uuids,)),
    ):
        session.execute(
            f"UPDATE {table} SET project_id = %s WHERE project_id = %s AND {predicate}",
            (new_project_uuid, old_project_uuid, *values),
        )
    session.execute(
        "UPDATE m_workspace_streams SET project_id = %s WHERE uuid = %s AND project_id = %s",
        (new_project_uuid, stream_uuid, old_project_uuid),
    )


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


class ApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


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
        for extension_name in (
            "provider",
            "delivery",
            "identity_kind",
            "display_name",
        ):
            if extension_name in obj:
                result[extension_name] = obj[extension_name]
        return result


class ContractMultipartPacker(ra_packers.MultipartPacker):
    pack_resource = ContractJSONPacker.pack_resource


class ExternalContractJSONPacker(ContractJSONPacker):
    _nullable_fields = {
        external_models.ExternalAccount: ("safe_error", "last_progress_at"),
        external_models.ExternalChat: (
            "project_id",
            "projection_stream_uuid",
            "safe_error",
        ),
        external_models.ExternalOperation: (
            "target_uuid",
            "safe_error",
            "original_url",
            "reconciliation_reason",
        ),
        external_models.ExternalBridgeInstance: (
            "last_heartbeat_at",
            "certificate_not_after",
            "safe_error",
        ),
        external_models.ExternalProviderPolicy: ("custom_ca_bundle",),
    }

    def pack_resource(self, obj: typing.Any) -> typing.Any:
        if isinstance(obj, dict) and {
            "allowed",
            "losses",
            "requires_confirmation",
        } <= set(obj):
            return obj
        result = super().pack_resource(obj)
        for model_class, fields in self._nullable_fields.items():
            if isinstance(obj, model_class):
                for name in fields:
                    if getattr(obj, name) is None:
                        result.setdefault(name, None)
                break
        return result


class ExternalProviderPolicyJSONPacker(ExternalContractJSONPacker):
    def unpack(self, value: typing.Any) -> typing.Any:
        if self._req.method != "PUT":
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
        result.setdefault("source", {"kind": "native"})
        result.setdefault("provider", None)
        result.setdefault("delivery", None)
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
        if order_by is None:
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
            "provider_uuid",
            "external_account_uuid",
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
        kwargs.pop("provider_uuid", None)
        kwargs.pop("external_account_uuid", None)
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
        kwargs.pop("provider_uuid", None)
        kwargs.pop("external_account_uuid", None)
        kwargs.pop("storage_type", None)
        return super().update(uuid, **kwargs)

    def delete(self, uuid: object) -> typing.Any:
        with api_store.open_store(self._get_project_id(), self._get_user_uuid()) as db:
            resource_uuid = typing.cast(sys_uuid.UUID, uuid)
            file = db.get_resource(self.resource_name, resource_uuid)
            result = db.delete_resource(self.resource_name, resource_uuid)
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
                            "description": {"type": "string"},
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
                            "description": {"type": "string"},
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
            if peer_uuid == self._get_user_uuid():
                raise ra_exc.ValidationErrorException()
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


class WorkspaceMessageReactionController(StoreResourceController):
    resource_name = "message_reactions"
    _internal_fields = {
        "provider_uuid",
        "external_account_uuid",
        "provider_external_id",
        "delivery_status",
        "delivery_error",
        "delivery_updated_at",
        "provider",
        "delivery",
    }
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceMessageReactions,
        hidden_fields=sorted(_internal_fields),
        convert_underscore=False,
        process_filters=True,
    )

    @classmethod
    def _reject_internal_fields(cls, values: typing.Any) -> None:
        if cls._internal_fields.intersection(values):
            raise ra_exc.ValidationErrorException()

    def create(self, **kwargs: typing.Any) -> typing.Any:
        self._reject_internal_fields(kwargs)
        return super().create(**kwargs)

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._reject_internal_fields(kwargs)
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
        return ExternalContractJSONPacker(
            resource_type or self.get_resource(),
            request=self.request,
        )

    @staticmethod
    def _etag(revision: int) -> str:
        return f'"{revision}"'

    def _required_if_match(self, resource: typing.Any) -> None:
        value = self.request.headers.get("If-Match")
        if value is None:
            raise messenger_exc.ExternalPreconditionRequiredError()
        if value != self._etag(resource.revision):
            raise messenger_exc.ExternalPreconditionFailedError()

    def _create_response(
        self, body: typing.Any, status: typing.Any, headers: typing.Any
    ) -> typing.Any:
        revision = None
        if isinstance(body, dict):
            revision = body.get("revision")
        elif hasattr(body, "revision"):
            revision = body.revision
        if revision is not None:
            headers["ETag"] = self._etag(revision)
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

    def _require_provider_enabled(
        self,
        provider: typing.Any,
        *,
        creating: typing.Any = False,
        session: typing.Any = None,
    ) -> typing.Any:
        return application_services.require_external_provider_enabled(
            session,
            self._get_user_uuid(),
            provider,
            creating=creating,
        )


class ExternalAccountController(ExternalResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalAccount,
        hidden_fields=["owner_user_uuid", "provider"],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _settings(selector: typing.Any, value: typing.Any) -> typing.Any:
        return application_services.normalize_settings(selector, value)

    @staticmethod
    def _credential(account: typing.Any, session: typing.Any) -> typing.Any:
        return application_services.external_credential(account, session)

    @staticmethod
    def _desired_resource(
        account: typing.Any,
        credential: typing.Any,
        *,
        synchronization_enabled: typing.Any = None,
    ) -> typing.Any:
        return application_services.desired_external_account_resource(
            account,
            credential,
            synchronization_enabled=synchronization_enabled,
        )

    @staticmethod
    def _append_desired(
        account: typing.Any,
        credential: typing.Any,
        session: typing.Any,
        *,
        enabled: typing.Any = None,
    ) -> typing.Any:
        return application_services.append_desired_external_account(
            account,
            credential,
            session,
            enabled=enabled,
        )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        session = contexts.Context().get_session()
        return application_services.ExternalAccountApplicationService.create(
            session,
            application_services.ExternalAccountActor(
                user_uuid=self._get_user_uuid(),
                project_id=self._get_project_id(),
            ),
            kwargs,
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        if set(kwargs) != {"settings"}:
            raise ra_exc.ValidationErrorException()
        account = self.get(uuid)
        self._required_if_match(account)
        update_settings = self._settings(
            external_models.EXTERNAL_ACCOUNT_UPDATE_SETTINGS_TYPE,
            kwargs["settings"],
        )
        settings = account.settings.copy()
        settings.update(update_settings)
        session = contexts.Context().get_session()
        _update_internal_fields(
            account,
            {
                "settings": settings,
                "desired_generation": account.desired_generation + 1,
                "revision": account.revision + 1,
            },
            session=session,
        )
        credential = self._credential(account, session)
        self._append_desired(account, credential, session)
        self._emit_event(
            account,
            messenger_events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider"),
            session=session,
        )
        return account

    @ra_actions.post
    def reconnect(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        self._required_if_match(resource)
        self._require_provider_enabled(resource.provider)
        if set(kwargs) != {"settings"}:
            raise ra_exc.ValidationErrorException()
        values = self._settings(
            external_models.EXTERNAL_ACCOUNT_RECONNECT_SETTINGS_TYPE,
            kwargs["settings"],
        )
        api_key = values.pop("api_key")
        if values["kind"] != resource.provider:
            raise ra_exc.ValidationErrorException()
        settings = resource.settings.copy()
        settings.update(values)
        session = contexts.Context().get_session()
        recipient, envelope = credential_crypto.encrypt_for_active_bridge(
            session,
            resource.uuid,
            resource.owner_user_uuid,
            resource.provider,
            resource.desired_generation + 1,
            {
                "server_url": settings["server_url"],
                "email": settings["email"],
                "api_key": api_key,
            },
        )
        credential = self._credential(resource, session)
        credential.update_dm(
            values={
                "key_version": recipient["identity_generation"],
                "envelope": envelope,
            },
        )
        credential.update(session=session)
        _update_internal_fields(
            resource,
            {
                "settings": settings,
                "credential_present": True,
                "status": external_models.ExternalAccountStatus.CONNECTING.value,
                "live_ready": False,
                "safe_error": None,
                "desired_generation": resource.desired_generation + 1,
                "revision": resource.revision + 1,
            },
            session=session,
        )
        self._append_desired(resource, credential, session)
        sql_state.refresh_effective_capabilities(
            session,
            provider_kind=resource.provider,
        )
        self._emit_event(
            resource,
            messenger_events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider"),
            session=session,
        )
        return resource

    @ra_actions.post
    def disconnect(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        session = contexts.Context().get_session()
        _update_internal_fields(
            resource,
            {
                "status": external_models.ExternalAccountStatus.DISCONNECTED.value,
                "live_ready": False,
                "desired_generation": resource.desired_generation + 1,
                "revision": resource.revision + 1,
            },
            session=session,
        )
        credential = self._credential(resource, session)
        self._append_desired(resource, credential, session, enabled=False)
        sql_state.refresh_effective_capabilities(
            session,
            provider_kind=resource.provider,
        )
        self._emit_event(
            resource,
            messenger_events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider"),
            session=session,
        )
        return resource

    def delete(self, uuid: object) -> None:
        account = self.get(uuid)
        session = contexts.Context().get_session()
        credential = self._credential(account, session)
        bridge_instance_uuid = credential.envelope["associated_data"][
            "bridge_instance_uuid"
        ]
        chats = external_models.ExternalChat.objects.get_all(
            filters={"external_account_uuid": dm_filters.EQ(account.uuid)},
            session=session,
        )
        cleanup_files = []
        removed_streams = set()
        for chat in chats:
            sql_state.append_delete(
                session,
                bridge_instance_uuid,
                account.provider,
                "external_chat_assignment",
                chat.uuid,
                chat.revision + 1,
            )
            if (
                chat.projection_stream_uuid is not None
                and chat.project_id is not None
                and chat.projection_stream_uuid not in removed_streams
            ):
                _journal_projection_move(
                    chat.uuid,
                    chat.revision + 1,
                    chat.owner_user_uuid,
                    chat.projection_stream_uuid,
                    chat.project_id,
                    write_new=False,
                )
                cleanup_files.extend(
                    session.execute(
                        """
                        SELECT uuid, storage_type, storage_object_id
                        FROM m_workspace_files
                        WHERE stream_uuid = %s AND project_id = %s
                          AND external_account_uuid = %s
                        """,
                        (
                            chat.projection_stream_uuid,
                            chat.project_id,
                            account.uuid,
                        ),
                    ).fetchall()
                )
                session.execute(
                    "DELETE FROM m_workspace_streams "
                    "WHERE uuid = %s AND project_id = %s",
                    (chat.projection_stream_uuid, chat.project_id),
                )
                removed_streams.add(chat.projection_stream_uuid)
            if chat.project_id is not None:
                messenger_events.create_external_resource_event(
                    chat.project_id,
                    chat.owner_user_uuid,
                    chat,
                    messenger_events.EXTERNAL_CHAT_DELETED_EVENT,
                    hidden_fields=(
                        "owner_user_uuid",
                        "provider",
                        "provider_chat_id",
                    ),
                    session=session,
                )
        sql_state.append_delete(
            session,
            bridge_instance_uuid,
            account.provider,
            "external_account",
            account.uuid,
            account.desired_generation + 1,
        )
        self._emit_event(
            account,
            messenger_events.EXTERNAL_ACCOUNT_DELETED_EVENT,
            hidden_fields=("owner_user_uuid", "provider"),
            session=session,
        )
        account.delete(session=session)
        for item in cleanup_files:
            file_storage.delete_workspace_file(
                item["uuid"],
                storage_type=item["storage_type"],
                storage_object_id=item["storage_object_id"],
            )
            file_storage.delete_workspace_file_metadata(
                item["uuid"],
                storage_type=item["storage_type"],
            )


class ExternalChatController(ExternalResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalChat,
        hidden_fields=["owner_user_uuid", "provider", "provider_chat_id"],
        convert_underscore=False,
        process_filters=True,
    )

    def _project_id(self, value: str | sys_uuid.UUID) -> sys_uuid.UUID:
        project_id = (
            external_models.ExternalChat.properties.properties["project_id"]
            .get_property_type()
            .from_simple_type(value)
        )
        if project_id != self._get_project_id():
            raise messenger_exc.ExternalResourceForbiddenError()
        return project_id

    @staticmethod
    def _desired_resource(
        resource: typing.Any, session: typing.Any = None
    ) -> typing.Any:
        return sql_state.external_chat_assignment_desired(resource, session=session)

    @staticmethod
    def _transition_phase(
        session: typing.Any,
        transition_uuid: object,
        phase: typing.Any,
        safe_error: typing.Any = None,
    ) -> None:
        session.execute(
            """
            UPDATE m_external_projection_transitions_v1
            SET phase = %s, safe_error = %s, updated_at = NOW()
            WHERE uuid = %s
            """,
            (phase, safe_error, transition_uuid),
        )

    def _plan_transition(
        self,
        resource: typing.Any,
        action: typing.Any,
        new_project_uuid: object,
        session: typing.Any,
    ) -> typing.Any:
        transition_uuid = sys_uuid.uuid5(
            resource.uuid,
            f"projection:{resource.revision}:{action}",
        )
        files = session.execute(
            """
            SELECT uuid, storage_type, storage_object_id
            FROM m_workspace_files
            WHERE stream_uuid = %s AND project_id = %s
              AND external_account_uuid = %s
            """,
            (
                resource.projection_stream_uuid,
                resource.project_id,
                resource.external_account_uuid,
            ),
        ).fetchall()
        session.execute(
            """
            INSERT INTO m_external_projection_transitions_v1 (
                uuid, external_chat_uuid, owner_user_uuid, action, revision,
                stream_uuid, old_project_uuid, new_project_uuid, cleanup_files
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (external_chat_uuid, revision, action) DO NOTHING
            """,
            (
                transition_uuid,
                resource.uuid,
                resource.owner_user_uuid,
                action,
                resource.revision,
                resource.projection_stream_uuid,
                resource.project_id,
                new_project_uuid,
                json.dumps(
                    [
                        {
                            "uuid": str(row["uuid"]),
                            "storage_type": row["storage_type"],
                            "storage_object_id": row["storage_object_id"],
                        }
                        for row in files
                    ]
                ),
            ),
        )
        session.execute(
            "UPDATE m_external_chats_v2 SET transition_pending = TRUE WHERE uuid = %s",
            (resource.uuid,),
        )
        return transition_uuid

    @classmethod
    def _resume_transition(
        cls,
        transition_uuid: object,
        resource: typing.Any,
        session: typing.Any,
    ) -> typing.Any:
        transition = session.execute(
            "SELECT * FROM m_external_projection_transitions_v1 WHERE uuid = %s",
            (transition_uuid,),
        ).fetchone()
        if transition is None:
            raise ra_exc.ValidationErrorException()
        if transition["phase"] == "planned":
            if transition["action"] == "move":
                _journal_projection_move(
                    transition["external_chat_uuid"],
                    transition["revision"],
                    transition["owner_user_uuid"],
                    transition["stream_uuid"],
                    transition["old_project_uuid"],
                    transition["new_project_uuid"],
                    write_old=False,
                )
            cls._transition_phase(session, transition_uuid, "canonical_new")
            transition["phase"] = "canonical_new"
        if transition["phase"] == "canonical_new":
            _journal_projection_move(
                transition["external_chat_uuid"],
                transition["revision"],
                transition["owner_user_uuid"],
                transition["stream_uuid"],
                transition["old_project_uuid"],
                write_new=False,
            )
            cls._transition_phase(session, transition_uuid, "canonical_old")
            transition["phase"] = "canonical_old"
        if transition["phase"] == "canonical_old":
            chat = external_models.ExternalChat.objects.get_one(
                filters={"uuid": dm_filters.EQ(transition["external_chat_uuid"])},
                session=session,
            )
            if transition["action"] == "move":
                _move_projection_rows(
                    session,
                    transition["stream_uuid"],
                    transition["old_project_uuid"],
                    transition["new_project_uuid"],
                )
                values = {
                    "selected": True,
                    "project_id": transition["new_project_uuid"],
                    "status": external_models.ExternalChatStatus.SYNCING.value,
                    "revision": chat.revision + 1,
                }
            else:
                session.execute(
                    "DELETE FROM m_workspace_streams WHERE uuid = %s AND project_id = %s",
                    (transition["stream_uuid"], transition["old_project_uuid"]),
                )
                values = {
                    "selected": False,
                    "project_id": None,
                    "projection_stream_uuid": None,
                    "status": external_models.ExternalChatStatus.DESELECTED.value,
                    "revision": chat.revision + 1,
                }
            _update_internal_fields(chat, values, session=session)
            credential = ExternalAccountController._credential(
                external_models.ExternalAccount.objects.get_one(
                    filters={"uuid": dm_filters.EQ(chat.external_account_uuid)},
                    session=session,
                ),
                session,
            )
            bridge_instance_uuid = credential.envelope["associated_data"][
                "bridge_instance_uuid"
            ]
            if transition["action"] == "move":
                sql_state.append_upsert(
                    session,
                    bridge_instance_uuid,
                    chat.provider,
                    cls._desired_resource(chat, session=session),
                )
                messenger_events.create_external_resource_event(
                    transition["new_project_uuid"],
                    chat.owner_user_uuid,
                    chat,
                    messenger_events.EXTERNAL_CHAT_UPDATED_EVENT,
                    hidden_fields=("owner_user_uuid", "provider", "provider_chat_id"),
                    session=session,
                )
            else:
                sql_state.append_delete(
                    session,
                    bridge_instance_uuid,
                    chat.provider,
                    "external_chat_assignment",
                    chat.uuid,
                    chat.revision,
                )
            messenger_events.create_external_resource_event(
                transition["old_project_uuid"],
                chat.owner_user_uuid,
                chat,
                messenger_events.EXTERNAL_CHAT_DELETED_EVENT,
                hidden_fields=("owner_user_uuid", "provider", "provider_chat_id"),
                session=session,
            )
            cls._transition_phase(session, transition_uuid, "sql_applied")
            transition["phase"] = "sql_applied"
        if transition["phase"] == "sql_applied":
            if transition["action"] == "deselect":
                for item in transition["cleanup_files"]:
                    file_storage.delete_workspace_file(
                        item["uuid"],
                        storage_type=item["storage_type"],
                        storage_object_id=item["storage_object_id"],
                    )
                    file_storage.delete_workspace_file_metadata(
                        item["uuid"], storage_type=item["storage_type"]
                    )
            cls._transition_phase(session, transition_uuid, "files_purged")
            transition["phase"] = "files_purged"
        if transition["phase"] == "files_purged":
            session.execute(
                "UPDATE m_external_chats_v2 SET transition_pending = FALSE WHERE uuid = %s",
                (transition["external_chat_uuid"],),
            )
            cls._transition_phase(session, transition_uuid, "completed")
        return external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(transition["external_chat_uuid"])},
            session=session,
        )

    def _change_assignment(
        self,
        resource: typing.Any,
        selected: typing.Any,
        project_id: object,
        status: typing.Any,
    ) -> typing.Any:
        session = contexts.Context().get_session()
        unchanged = (
            not resource.transition_pending
            and resource.selected == selected
            and resource.project_id == project_id
        )
        if unchanged and not selected:
            return resource
        transition_action = None
        if (
            resource.projection_stream_uuid is not None
            and resource.project_id is not None
        ):
            if not selected:
                transition_action = "deselect"
            elif project_id != resource.project_id:
                transition_action = "move"
        if resource.transition_pending:
            pending = session.execute(
                """
                SELECT uuid FROM m_external_projection_transitions_v1
                WHERE external_chat_uuid = %s AND phase != 'completed'
                ORDER BY created_at LIMIT 1
                """,
                (resource.uuid,),
            ).fetchone()
            if pending is None:
                raise ra_exc.ValidationErrorException()
            return self._resume_transition(pending["uuid"], resource, session)
        if transition_action is not None:
            transition_uuid = self._plan_transition(
                resource,
                transition_action,
                project_id if transition_action == "move" else None,
                session,
            )
            return self._resume_transition(transition_uuid, resource, session)
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(resource.external_account_uuid),
                "owner_user_uuid": dm_filters.EQ(self._get_user_uuid()),
            },
            session=session,
        )
        policy = self._require_provider_enabled(
            account.provider,
            session=session,
        )
        if selected and not resource.selected:
            maximum = policy.limits.get("max_selected_chats_per_account")
            selected_chats = external_models.ExternalChat.objects.get_all(
                filters={
                    "external_account_uuid": dm_filters.EQ(account.uuid),
                    "selected": dm_filters.EQ(True),
                },
                session=session,
            )
            if not isinstance(maximum, int) or len(selected_chats) >= maximum:
                raise messenger_exc.ExternalResourceForbiddenError()
        credential = application_services.external_credential(account, session)
        bridge_instance_uuid = credential.envelope["associated_data"][
            "bridge_instance_uuid"
        ]
        if selected:
            external_projection.ensure_external_chat_stream(
                session,
                project_id=sys_uuid.UUID(str(project_id)),
                owner_user_uuid=resource.owner_user_uuid,
                projection_stream_uuid=resource.projection_stream_uuid,
                bridge_instance_uuid=sys_uuid.UUID(str(bridge_instance_uuid)),
                external_account_uuid=resource.external_account_uuid,
                provider_kind=resource.provider,
                provider_chat_id=resource.provider_chat_id,
                display_name=resource.display_name,
                source=resource.source,
                capabilities=resource.capabilities,
                account_settings=account.settings,
            )
        if unchanged:
            return resource
        _update_internal_fields(
            resource,
            {
                "selected": selected,
                "project_id": project_id,
                "status": status,
                "revision": resource.revision + 1,
            },
            session=session,
        )
        if selected:
            sql_state.append_upsert(
                session,
                bridge_instance_uuid,
                resource.provider,
                self._desired_resource(resource, session=session),
            )
        else:
            sql_state.append_delete(
                session,
                bridge_instance_uuid,
                resource.provider,
                "external_chat_assignment",
                resource.uuid,
                resource.revision,
            )
        self._emit_event(
            resource,
            messenger_events.EXTERNAL_CHAT_UPDATED_EVENT,
            hidden_fields=("owner_user_uuid", "provider", "provider_chat_id"),
            session=session,
        )
        return resource

    @ra_actions.post
    def select(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        if set(kwargs) != {"project_id"}:
            raise ra_exc.ValidationErrorException()
        return self._change_assignment(
            resource,
            True,
            self._project_id(kwargs["project_id"]),
            external_models.ExternalChatStatus.SYNCING.value,
        )

    @ra_actions.post
    def deselect(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._change_assignment(
            resource,
            False,
            None,
            external_models.ExternalChatStatus.DESELECTED.value,
        )

    @ra_actions.post
    def move(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        if set(kwargs) != {"project_id"}:
            raise ra_exc.ValidationErrorException()
        if not resource.transition_pending:
            self._required_if_match(resource)
        return self._change_assignment(
            resource,
            True,
            self._project_id(kwargs["project_id"]),
            external_models.ExternalChatStatus.SYNCING.value,
        )


class ExternalOperationController(ExternalResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalOperation,
        hidden_fields=["owner_user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )

    @ra_actions.post
    def retry(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args
        if set(kwargs) - {"confirm_duplicate_risk"}:
            raise ra_exc.ValidationErrorException()
        if not resource.can_retry:
            raise ra_exc.ValidationErrorException()
        if resource.retry_requires_confirmation and not kwargs.get(
            "confirm_duplicate_risk",
            False,
        ):
            raise ra_exc.ValidationErrorException()
        history = list(resource.attempt_history)
        history.append(
            {
                "attempt": resource.attempt,
                "status": resource.status,
                "safe_error": resource.safe_error,
                "duplicate_risk": resource.duplicate_risk,
                "original_url": resource.original_url,
                "reconciliation_state": resource.reconciliation_state,
                "reconciliation_reason": resource.reconciliation_reason,
            }
        )
        next_attempt = resource.attempt + 1
        session = contexts.Context().get_session()
        try:
            queued = provider_data.retry_provider_operation(
                session,
                external_operation_uuid=resource.uuid,
                next_attempt=next_attempt,
            )
        except ValueError as exc:
            raise ra_exc.ValidationErrorException() from exc
        details = resource.details.copy()
        details["record_uuid"] = str(queued["uuid"])
        _update_internal_fields(
            resource,
            {
                "details": details,
                "status": external_models.ExternalOperationStatus.QUEUED.value,
                "attempt": next_attempt,
                "attempt_history": history,
                "safe_error": None,
                "can_retry": False,
                "can_discard": True,
                "duplicate_risk": False,
                "retry_requires_confirmation": False,
                "reconciliation_state": (
                    external_models.ExternalReconciliationState.NOT_REQUIRED.value
                ),
                "reconciliation_reason": None,
                "reconciliation_evidence": {},
                "revision": resource.revision + 1,
            },
            session=session,
        )
        provider_data.publish_operation_event(
            session,
            resource,
            self._get_project_id(),
            messenger_events.EXTERNAL_OPERATION_UPDATED_EVENT,
        )
        return resource

    def delete(self, uuid: object) -> None:
        operation = self.get(uuid)
        if not operation.can_discard:
            raise ra_exc.ValidationErrorException()
        session = contexts.Context().get_session()
        try:
            discarded = provider_data.discard_provider_operation(
                session,
                external_operation_uuid=operation.uuid,
            )
        except ValueError as exc:
            raise ra_exc.ValidationErrorException() from exc
        _update_internal_fields(
            operation,
            {
                "status": external_models.ExternalOperationStatus.DISCARDED.value,
                "can_retry": False,
                "can_discard": False,
                "revision": operation.revision + 1,
            },
            session=session,
        )
        provider_data.sync_operation_target_delivery(
            session,
            operation,
            discarded["project_id"],
        )
        self._emit_event(
            operation,
            messenger_events.EXTERNAL_OPERATION_DELETED_EVENT,
            hidden_fields=("owner_user_uuid",),
            session=session,
        )
        operation.delete(session=session)

    @ra_actions.post
    def preflight(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del resource, args
        required = {"external_account_uuid", "action", "target"}
        if set(kwargs) != required:
            raise ra_exc.ValidationErrorException()
        session = contexts.Context().get_session()
        account = external_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(kwargs["external_account_uuid"]),
                "owner_user_uuid": dm_filters.EQ(self._get_user_uuid()),
            },
            session=session,
        )
        self._require_provider_enabled(account.provider, session=session)
        action = kwargs["action"]
        target = kwargs["target"]
        if (
            not isinstance(action, str)
            or not action
            or not isinstance(target, dict)
            or set(target) - {"type", "uuid"}
            or not isinstance(target.get("type"), str)
            or not target["type"]
        ):
            raise ra_exc.ValidationErrorException()
        if "uuid" not in target:
            raise ra_exc.ValidationErrorException()
        try:
            target["uuid"] = str(sys_uuid.UUID(target["uuid"]))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ra_exc.ValidationErrorException() from exc
        target_models: dict[str, typing.Any] = {
            "stream": models.WorkspaceStream,
            "topic": models.WorkspaceStreamTopic,
            "message": models.WorkspaceMessage,
        }
        target_model = target_models.get(target["type"])
        if target_model is None:
            raise ra_exc.ValidationErrorException()
        target_resource = target_model.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self._get_project_id()),
                "uuid": dm_filters.EQ(target["uuid"]),
            },
            session=session,
        )
        stream_uuid = (
            target_resource.uuid
            if target["type"] == "stream"
            else target_resource.stream_uuid
        )
        chats = external_models.ExternalChat.objects.get_all(
            filters={
                "external_account_uuid": dm_filters.EQ(account.uuid),
                "owner_user_uuid": dm_filters.EQ(self._get_user_uuid()),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "projection_stream_uuid": dm_filters.EQ(stream_uuid),
            },
            session=session,
            limit=2,
        )
        chat = chats[0] if len(chats) == 1 else None
        capability = (
            chat.capabilities.get(action)
            if chat is not None and isinstance(chat.capabilities, dict)
            else None
        )
        account_capability = (
            account.capabilities.get(action)
            if isinstance(account.capabilities, dict)
            else None
        )
        available = (
            chat is not None
            and chat.selected
            and chat.status
            in {
                external_models.ExternalChatStatus.SYNCING.value,
                external_models.ExternalChatStatus.LIVE.value,
            }
            and not chat.transition_pending
            and isinstance(account_capability, dict)
            and account_capability.get("available") is True
            and isinstance(capability, dict)
            and capability.get("available") is True
        )
        losses = (
            capability.get("losses", [])
            if available and isinstance(capability, dict)
            else []
        )
        if not isinstance(losses, list) or any(
            not isinstance(loss, dict) for loss in losses
        ):
            raise ra_exc.ValidationErrorException()
        return {
            "allowed": account.live_ready and available,
            "action": action,
            "target": target,
            "losses": losses,
            "requires_confirmation": bool(losses),
        }


class ExternalBridgeInstanceController(
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalBridgeInstance,
        convert_underscore=False,
        process_filters=True,
    )

    def get_packer(
        self, content_type: typing.Any, resource_type: typing.Any = None
    ) -> typing.Any:
        return ExternalContractJSONPacker(
            resource_type or self.get_resource(),
            request=self.request,
        )

    def _require_permission(self, action: typing.Any) -> None:
        permissions = (
            self.get_context().iam_context.get_introspection_info().permissions
        )
        if action not in permissions:
            raise messenger_exc.ExternalResourceForbiddenError()

    def filter(self, filters: typing.Any, order_by: typing.Any = None) -> typing.Any:
        self._require_permission("workspace.external_bridge_instance.read")
        return super().filter(filters, order_by=order_by)

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._require_permission("workspace.external_bridge_instance.read")
        return super().get(uuid, **kwargs)

    def _change_status(
        self,
        resource: typing.Any,
        permission: typing.Any,
        status: typing.Any,
    ) -> typing.Any:
        self._require_permission(permission)
        if (
            resource.status
            == external_models.ExternalBridgeInstanceStatus.REVOKED.value
        ):
            raise messenger_exc.ExternalResourceForbiddenError()
        session = contexts.Context().get_session()
        _update_internal_fields(
            resource,
            {"status": status, "revision": resource.revision + 1},
            session=session,
        )
        sql_state.refresh_effective_capabilities(
            session,
            provider_kind=resource.provider,
        )
        return resource

    @ra_actions.post
    def suspend(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._change_status(
            resource,
            "workspace.external_bridge_instance.suspend",
            external_models.ExternalBridgeInstanceStatus.SUSPENDED.value,
        )

    @ra_actions.post
    def resume(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._change_status(
            resource,
            "workspace.external_bridge_instance.resume",
            external_models.ExternalBridgeInstanceStatus.ACTIVE.value,
        )

    @ra_actions.post
    def revoke(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        self._require_permission("workspace.external_bridge_instance.revoke")
        if (
            resource.status
            == external_models.ExternalBridgeInstanceStatus.REVOKED.value
        ):
            return resource
        session = contexts.Context().get_session()
        _update_internal_fields(
            resource,
            {
                "status": external_models.ExternalBridgeInstanceStatus.REVOKED.value,
                "identity_generation": resource.identity_generation + 1,
                "revision": resource.revision + 1,
            },
            session=session,
        )
        sql_state.refresh_effective_capabilities(
            session,
            provider_kind=resource.provider,
        )
        return resource


class ExternalProviderPolicyController(ExternalResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalProviderPolicy,
        hidden_fields=["custom_ca_certificates"],
        convert_underscore=False,
        process_filters=True,
    )
    _namespace = sys_uuid.UUID("0d7130fa-ce83-52d3-9496-78753b297158")

    def get_packer(
        self, content_type: typing.Any, resource_type: typing.Any = None
    ) -> typing.Any:
        return ExternalProviderPolicyJSONPacker(
            resource_type or self.get_resource(),
            request=self.request,
        )

    def _require_permission(self, action: typing.Any) -> None:
        permissions = (
            self.get_context().iam_context.get_introspection_info().permissions
        )
        if action not in permissions:
            raise messenger_exc.ExternalResourceForbiddenError()

    @staticmethod
    def _provider(value: str) -> str:
        return external_models.ExternalProvider(value).value

    def _get_or_create(
        self, provider: typing.Any, session: typing.Any = None
    ) -> typing.Any:
        provider = self._provider(provider)
        policy = external_models.ExternalProviderPolicy.objects.get_one_or_none(
            filters={"provider": dm_filters.EQ(provider)},
            session=session,
        )
        if policy is None:
            policy = external_models.ExternalProviderPolicy(
                uuid=sys_uuid.uuid5(self._namespace, provider),
                provider=provider,
                limits={
                    "max_accounts": 0,
                    "max_selected_chats_per_account": 0,
                    "max_file_bytes": 0,
                },
            )
            policy.insert(session=session)
        return policy

    @staticmethod
    def _desired_resource(policy: typing.Any) -> typing.Any:
        return {
            "resource_type": "external_provider_policy",
            "uuid": str(policy.uuid),
            "generation": policy.revision,
            "provider_kind": policy.provider,
            "enabled": policy.enabled,
            "emergency_suspended": policy.emergency_suspended,
            "limits": policy.limits,
            "custom_ca_bundle_uuid": (
                policy.custom_ca_bundle["uuid"]
                if policy.custom_ca_bundle is not None
                else None
            ),
        }

    @classmethod
    def _append_desired(
        cls,
        policy: typing.Any,
        session: typing.Any,
        previous_ca_uuid: object = None,
    ) -> None:
        target = sql_state.active_encryption_target(policy.provider, session)
        sql_state.append_upsert(
            session,
            target["bridge_instance_uuid"],
            policy.provider,
            cls._desired_resource(policy),
        )
        current_ca_uuid = None
        if policy.custom_ca_bundle is not None:
            current_ca_uuid = policy.custom_ca_bundle["uuid"]
            sql_state.append_upsert(
                session,
                target["bridge_instance_uuid"],
                policy.provider,
                {
                    "resource_type": "custom_ca_bundle",
                    "uuid": current_ca_uuid,
                    "generation": policy.custom_ca_bundle["generation"],
                    "provider_kind": policy.provider,
                    "sha256": policy.custom_ca_bundle["sha256"],
                    "certificates_pem": policy.custom_ca_certificates[
                        "certificates_pem"
                    ],
                },
            )
        if previous_ca_uuid is not None and previous_ca_uuid != current_ca_uuid:
            sql_state.append_delete(
                session,
                target["bridge_instance_uuid"],
                policy.provider,
                "custom_ca_bundle",
                typing.cast(str | sys_uuid.UUID, previous_ca_uuid),
                policy.revision,
            )

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        if "/actions/" not in self.request.path:
            self._require_permission("workspace.external_provider_policy.read")
        return self._get_or_create(uuid)

    @staticmethod
    def _limits(values: typing.Any) -> typing.Any:
        if set(values) != {
            "max_accounts",
            "max_selected_chats_per_account",
            "max_file_bytes",
        }:
            raise ra_exc.ValidationErrorException()
        bounds = {
            "max_accounts": 100000,
            "max_selected_chats_per_account": 1000000,
            "max_file_bytes": 5368709120,
        }
        for name, maximum in bounds.items():
            if (
                isinstance(values[name], bool)
                or not isinstance(values[name], int)
                or not 0 <= values[name] <= maximum
            ):
                raise ra_exc.ValidationErrorException()
        return values.copy()

    @staticmethod
    def _custom_ca_bundle(
        provider: typing.Any, value: typing.Any, generation: typing.Any
    ) -> typing.Any:
        if value is None:
            return None, None
        if set(value) != {"certificates_pem"}:
            raise ra_exc.ValidationErrorException()
        certificates = value["certificates_pem"]
        if not isinstance(certificates, list) or not 1 <= len(certificates) <= 32:
            raise ra_exc.ValidationErrorException()
        normalized = []
        for certificate_pem in certificates:
            if not isinstance(certificate_pem, str) or "PRIVATE KEY" in certificate_pem:
                raise ra_exc.ValidationErrorException()
            try:
                certificate = x509.load_pem_x509_certificate(
                    certificate_pem.encode("ascii")
                )
                basic_constraints = certificate.extensions.get_extension_for_class(
                    x509.BasicConstraints
                ).value
            except (ValueError, UnicodeEncodeError, x509.ExtensionNotFound) as exc:
                raise ra_exc.ValidationErrorException() from exc
            if not basic_constraints.ca:
                raise ra_exc.ValidationErrorException()
            normalized.append(
                certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
            )
        content = "".join(normalized).encode("ascii")
        metadata = {
            "uuid": str(
                sys_uuid.uuid5(
                    ExternalProviderPolicyController._namespace,
                    f"{provider}:custom-ca",
                )
            ),
            "generation": generation,
            "sha256": hashlib.sha256(content).hexdigest(),
            "certificate_count": len(normalized),
        }
        return metadata, {"certificates_pem": normalized}

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        self._require_permission("workspace.external_provider_policy.update")
        if set(kwargs) != {"settings"} or set(kwargs["settings"]) != {
            "kind",
            "enabled",
            "limits",
            "custom_ca_bundle",
        }:
            raise ra_exc.ValidationErrorException()
        provider = self._provider(typing.cast(str, uuid))
        settings = kwargs["settings"]
        if settings["kind"] != provider or not isinstance(settings["enabled"], bool):
            raise ra_exc.ValidationErrorException()
        policy = self._get_or_create(provider)
        self._required_if_match(policy)
        generation = policy.revision + 1
        metadata, certificates = self._custom_ca_bundle(
            provider,
            settings["custom_ca_bundle"],
            generation,
        )
        session = contexts.Context().get_session()
        previous_ca_uuid = (
            policy.custom_ca_bundle["uuid"]
            if policy.custom_ca_bundle is not None
            else None
        )
        _update_internal_fields(
            policy,
            {
                "enabled": settings["enabled"],
                "limits": self._limits(settings["limits"]),
                "custom_ca_bundle": metadata,
                "custom_ca_certificates": certificates,
                "revision": generation,
            },
            session=session,
        )
        self._append_desired(policy, session, previous_ca_uuid)
        sql_state.refresh_effective_capabilities(
            session,
            provider_kind=policy.provider,
        )
        return policy

    def _change_status(
        self,
        resource: typing.Any,
        permission: typing.Any,
        suspended: typing.Any,
    ) -> typing.Any:
        self._require_permission(permission)
        session = contexts.Context().get_session()
        _update_internal_fields(
            resource,
            {
                "emergency_suspended": suspended,
                "revision": resource.revision + 1,
            },
            session=session,
        )
        self._append_desired(resource, session)
        sql_state.refresh_effective_capabilities(
            session,
            provider_kind=resource.provider,
        )
        return resource

    @ra_actions.post
    def suspend(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._change_status(
            resource,
            "workspace.external_provider_policy.suspend",
            True,
        )

    @ra_actions.post
    def resume(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._change_status(
            resource,
            "workspace.external_provider_policy.resume",
            False,
        )


class ExternalProviderHealthController(ra_controllers.BaseResourceController):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=external_models.ExternalProviderHealth,
        convert_underscore=False,
        process_filters=True,
    )

    def _require_permission(self) -> None:
        permissions = (
            self.get_context().iam_context.get_introspection_info().permissions
        )
        if "workspace.external_provider_health.read" not in permissions:
            raise messenger_exc.ExternalResourceForbiddenError()

    @staticmethod
    def _counts(resources: typing.Any, field: typing.Any) -> typing.Any:
        result: dict[typing.Any, int] = {}
        for resource in resources:
            value = getattr(resource, field)
            result[value] = result.get(value, 0) + 1
        return result

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        del kwargs
        self._require_permission()
        provider = external_models.ExternalProvider(uuid).value
        accounts = external_models.ExternalAccount.objects.get_all(
            filters={"provider": dm_filters.EQ(provider)}
        )
        instances = external_models.ExternalBridgeInstance.objects.get_all(
            filters={"provider": dm_filters.EQ(provider)}
        )
        operations = external_models.ExternalOperation.objects.get_all(filters={})
        account_uuids = {account.uuid for account in accounts}
        operations = [
            operation
            for operation in operations
            if operation.external_account_uuid in account_uuids
        ]
        healthy = any(
            instance.status == external_models.ExternalBridgeInstanceStatus.ACTIVE.value
            for instance in instances
        )
        return external_models.ExternalProviderHealth(
            provider=provider,
            status="healthy" if healthy else "unavailable",
            account_counts=self._counts(accounts, "status"),
            bridge_counts=self._counts(instances, "status"),
            operation_counts=self._counts(operations, "status"),
            metrics={"queue_depth": len(operations)},
            updated_at=datetime.datetime.now(datetime.timezone.utc),
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
    def read(
        self, resource: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        del args, kwargs
        return self._action(resource, "read")


class WorkspaceUserController(StoreResourceController):
    resource_name = "users"
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUser,
        hidden_fields=[
            "provider_uuid",
            "external_account_uuid",
            "provider_external_id",
        ],
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
