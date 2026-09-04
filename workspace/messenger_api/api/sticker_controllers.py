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
from restalchemy.api import resources as ra_resources
from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exceptions

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_import
from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_storage
from workspace.messenger_api.api import permission_guards
from workspace.messenger_api.dm import stickers


_QUERY_PARAMETERS = frozenset(
    ("q", "favorite", "uuid", "category", "format", "page_limit", "page_marker")
)


def _json_response(value: object, status: int = 200) -> webob.Response:
    body = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return webob.Response(
        body=body,
        status=status,
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )


def _http_response(result: sticker_catalog.StickerHttpResponse) -> webob.Response:
    return webob.Response(
        body=result.body,
        status=result.status,
        headerlist=list(result.headers.items()),
    )


class StickerController(ra_controllers.BaseResourceController):
    """HTTP boundary for the global sticker catalog."""

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=stickers.Sticker,
        convert_underscore=False,
        process_filters=False,
    )
    __generate_location_for__ = ()

    def _repository(self) -> sticker_repository.StickerRepository:
        return sticker_repository.StickerRepository()

    def _storage(self) -> sticker_storage.StickerStorage:
        return sticker_storage.get_sticker_storage()

    def _session(self) -> typing.Any:
        return contexts.Context().get_session()

    def _user_uuid(self) -> sys_uuid.UUID:
        return typing.cast(sys_uuid.UUID, self.get_context().user_uuid)

    def _media_url_root(self) -> str:
        prefix, separator, _ = self.request.path.partition("/stickers")
        if not separator:
            raise ra_exceptions.NotFoundError(path=self.request.path)
        return f"{prefix}/stickers"

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

    def _list_response(self) -> webob.Response:
        if set(self.request.GET).difference(_QUERY_PARAMETERS):
            raise ra_exceptions.ValidationErrorException()
        favorite_value = self._query_value("favorite")
        if favorite_value is None:
            favorite = False
        elif favorite_value == "true":
            favorite = True
        elif favorite_value == "false":
            favorite = False
        else:
            raise ra_exceptions.ValidationErrorException()
        page_limit_value = self._query_value("page_limit")
        try:
            page_limit = int(page_limit_value) if page_limit_value is not None else None
            uuids = [sys_uuid.UUID(value) for value in self.request.GET.getall("uuid")]
        except (TypeError, ValueError):
            raise ra_exceptions.ValidationErrorException() from None
        result = sticker_catalog.list_public_stickers(
            self._session(),
            self._user_uuid(),
            self._repository(),
            q=self._query_value("q"),
            favorite=favorite,
            uuids=uuids,
            category=self._query_value("category"),
            format=self._query_value("format"),
            page_limit=page_limit,
            page_marker=self._query_value("page_marker"),
            if_none_match=self.request.headers.get("If-None-Match"),
            media_url_root=self._media_url_root(),
        )
        return _http_response(result)

    def do_collection(self, parent_resource: typing.Any = None) -> webob.Response:
        del parent_resource
        if self.request.method != "GET":
            raise ra_exceptions.UnsupportedHttpMethod(method=self.request.method)
        self.request.api_context.set_active_method(ra_constants.FILTER)
        return self._list_response()

    def do_resource(
        self,
        uuid: object,
        parent_resource: typing.Any = None,
    ) -> webob.Response:
        del parent_resource
        sticker_uuid = self._parse_uuid(uuid)
        if self.request.method == "GET":
            self.request.api_context.set_active_method(ra_constants.GET)
            return _json_response(
                sticker_catalog.get_public_sticker(
                    self._session(),
                    self._user_uuid(),
                    self._repository(),
                    sticker_uuid,
                    media_url_root=self._media_url_root(),
                )
            )
        if self.request.method == "PUT":
            self.request.api_context.set_active_method(ra_constants.UPDATE)
            permission_guards.require_iam_permission(
                self.get_context(),
                sticker_catalog.STICKER_CATALOG_MANAGE_PERMISSION,
            )
            try:
                values = json.loads(self.request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ra_exceptions.ValidationErrorException() from None
            updated = sticker_catalog.update_sticker(
                self._session(),
                self._repository(),
                sticker_uuid,
                values,
            )
            return _json_response(
                sticker_catalog.public_card_dict(
                    sticker_catalog.build_public_card(
                        updated,
                        is_favorite=False,
                        media_url_root=self._media_url_root(),
                    )
                )
            )
        raise ra_exceptions.UnsupportedHttpMethod(method=self.request.method)

    def get_resource_by_uuid(
        self,
        uuid: object,
        parent_resource: typing.Any = None,
    ) -> stickers.Sticker:
        del parent_resource
        sticker_uuid = self._parse_uuid(uuid)
        return stickers.Sticker.objects.get_one(filters={"uuid": sticker_uuid})

    def process_result(
        self,
        result: typing.Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        add_location: bool = False,
    ) -> webob.Response:
        del status_code, headers, add_location
        if isinstance(result, webob.Response):
            return result
        return _json_response(result)

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
                self._user_uuid(),
                self._repository(),
                self._storage(),
            )
        except sticker_import.StickerImportValidationError:
            raise ra_exceptions.ValidationErrorException() from None
        return _json_response(result.to_simple_type())
