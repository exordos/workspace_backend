# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import typing
import uuid as sys_uuid

import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import constants as ra_constants
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import field_permissions as ra_field_permissions
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.storage import exceptions as storage_exceptions

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_import
from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_storage
from workspace.messenger_api.api import permission_guards
from workspace.messenger_api.dm import stickers


_QUERY_PARAMETERS = frozenset(
    ("q", "favorite", "uuid", "category", "format", "page_limit", "page_marker")
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_response(value: object, status: int = 200) -> webob.Response:
    return webob.Response(
        body=_json_bytes(value),
        status=status,
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )


def _http_response(result: sticker_catalog.StickerHttpResponse) -> webob.Response:
    return webob.Response(
        body=result.body,
        status=result.status,
        headerlist=list(result.headers.items()),
    )


class StickerJSONPackerPreEncoded(ra_packers.JSONPackerPreEncoded):
    """Keep pre-encoded responses while requiring JSON objects for writes."""

    def unpack(self, value: typing.Any) -> dict[str, typing.Any]:
        try:
            decoded = json.loads(value)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise ra_exceptions.ParseBodyError() from None
        if not isinstance(decoded, dict):
            raise ra_exceptions.ValidationErrorException()
        return typing.cast(dict[str, typing.Any], super().unpack(value))


class StickerController(ra_controllers.BaseResourceController):
    """HTTP boundary for the global sticker catalog."""

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=stickers.Sticker,
        convert_underscore=False,
        process_filters=False,
        fields_permissions=ra_field_permissions.FieldsPermissions(
            fields={
                field: {
                    ra_constants.ALL: ra_field_permissions.Permissions.RO,
                }
                for field in stickers.STICKER_READ_ONLY_FIELDS
            },
        ),
    )
    __packer__ = StickerJSONPackerPreEncoded
    __filter_param__ = None
    __generate_location_for__ = ()

    def _repository(self) -> sticker_repository.StickerRepository:
        return sticker_repository.StickerRepository()

    def _storage(self) -> sticker_storage.StickerStorage:
        return sticker_storage.get_sticker_storage()

    def _session(self) -> typing.Any:
        return contexts.Context().get_session()

    def _user_uuid(self) -> sys_uuid.UUID:
        return typing.cast(sys_uuid.UUID, self.get_context().user_uuid)

    def get_packer(
        self,
        content_type: typing.Any,
        resource_type: typing.Any = None,
    ) -> typing.Any:
        if self.request.api_context.get_active_method() == ra_constants.UPDATE:
            permission_guards.require_iam_permission(
                self.get_context(),
                sticker_catalog.STICKER_CATALOG_MANAGE_PERMISSION,
            )
        return super().get_packer(content_type, resource_type)

    def _parse_uuid(self, value: object) -> sys_uuid.UUID:
        return typing.cast(
            sys_uuid.UUID,
            self._parse_resource_uuid(
                "uuid",
                value,
                self.get_resource().get_id_type(),
            ),
        )

    def _query_value(self, name: str) -> str | None:
        values = self.request.GET.getall(name)
        if len(values) > 1:
            raise ra_exceptions.ValidationErrorException()
        return values[0] if values else None

    def filter(
        self, filters: typing.Any, order_by: typing.Any = None
    ) -> tuple[bytes | None, int, dict[str, str]]:
        # Read the original query to validate reserved parameters and repeats.
        del filters, order_by
        if set(self.request.GET).difference(_QUERY_PARAMETERS):
            raise ra_exceptions.ValidationErrorException()
        try:
            query = sticker_catalog.build_list_query(
                q=self._query_value("q"),
                favorite=self._query_value("favorite"),
                uuids=self.request.GET.getall("uuid"),
                category=self._query_value("category"),
                format=self._query_value("format"),
                page_limit=self._query_value("page_limit"),
                page_marker=self._query_value("page_marker"),
            )
        except (TypeError, ValueError):
            raise ra_exceptions.ValidationErrorException() from None
        result = sticker_catalog.list_public_stickers(
            self._session(),
            self._user_uuid(),
            self._repository(),
            query=query,
            if_none_match=self.request.headers.get("If-None-Match"),
        )
        return result.body, result.status, result.headers

    def get(self, uuid: object, **kwargs: typing.Any) -> bytes:
        del kwargs
        return _json_bytes(
            sticker_catalog.get_public_sticker(
                self._session(),
                self._user_uuid(),
                self._repository(),
                typing.cast(sys_uuid.UUID, uuid),
            )
        )

    def update(self, uuid: object, **kwargs: typing.Any) -> bytes:
        updated = sticker_catalog.update_sticker(
            self._session(),
            self._user_uuid(),
            self._repository(),
            typing.cast(sys_uuid.UUID, uuid),
            kwargs,
        )
        return _json_bytes(
            sticker_catalog.public_card_dict(
                sticker_catalog.build_public_card(
                    updated.sticker,
                    is_favorite=updated.is_favorite,
                )
            )
        )

    def get_resource_by_uuid(
        self,
        uuid: object,
        parent_resource: typing.Any = None,
    ) -> stickers.Sticker:
        del parent_resource
        sticker_uuid = self._parse_uuid(uuid)
        try:
            return stickers.Sticker.objects.get_one(filters={"uuid": sticker_uuid})
        except storage_exceptions.RecordNotFound:
            raise ra_exceptions.ResourceNotFoundError(
                resource="Sticker",
                path=str(sticker_uuid),
            ) from None

    def process_result(
        self,
        result: typing.Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        add_location: bool = False,
    ) -> webob.Response:
        if isinstance(result, webob.Response):
            return result
        return super().process_result(
            result,
            status_code=status_code,
            headers=headers,
            add_location=add_location,
        )

    @ra_actions.get
    def download(
        self,
        resource: stickers.Sticker,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> webob.Response:
        del args, kwargs
        result = sticker_catalog.download_sticker(
            self._session(),
            self._user_uuid(),
            self._repository(),
            self._storage(),
            resource.uuid,
        )
        return _http_response(result)

    @ra_actions.post
    def star(
        self,
        resource: stickers.Sticker,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> webob.Response:
        del args, kwargs
        sticker_catalog.star_sticker(
            self._session(), self._user_uuid(), self._repository(), resource.uuid
        )
        return webob.Response(status=200)

    @ra_actions.post
    def unstar(
        self,
        resource: stickers.Sticker,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> webob.Response:
        del args, kwargs
        sticker_catalog.unstar_sticker(
            self._session(), self._user_uuid(), self._repository(), resource.uuid
        )
        return webob.Response(status=200)

    @ra_actions.post
    def import_archive(
        self,
        resource: None,
        **kwargs: typing.Any,
    ) -> webob.Response:
        del resource
        if set(kwargs) != {"archive"}:
            raise ra_exceptions.ValidationErrorException()
        archive = kwargs["archive"]
        source = getattr(archive, "file", archive)
        try:
            result = sticker_import.import_archive(
                source,
                self._session(),
                self._repository(),
                self._storage(),
            )
        except sticker_import.StickerImportValidationError:
            raise ra_exceptions.ValidationErrorException() from None
        return _json_response(result.to_simple_type())
