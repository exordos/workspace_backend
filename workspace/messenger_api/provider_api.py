# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Private IAM-authenticated Provider CRUD HTTP surface."""

import datetime
import json
import re
import typing
import uuid as sys_uuid

import webob
import webob.static
from restalchemy.api import middlewares
from restalchemy.common import contexts

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import provider_store
from workspace.messenger_api.api import v3_store


_ROOT = "/v1/provider/entities"
_BOOTSTRAP_PATH = "/v1/provider/bootstrap"
_REGISTRATION_PATH = "/v1/provider/registration"
_BATCH_PATH = f"{_ROOT}/actions/apply/invoke"
_RESOURCE_NAMES = "|".join(provider_store.RESOURCE_TYPES)
_COLLECTION_PATH = re.compile(rf"^{_ROOT}/(?P<resource>{_RESOURCE_NAMES})/?$")
_ENTITY_PATH = re.compile(
    rf"^{_ROOT}/(?P<resource>{_RESOURCE_NAMES})/"
    r"(?P<entity_uuid>[0-9a-fA-F-]{36})/?$"
)
_MAX_BODY_BYTES = 8 * 1024 * 1024


def _response(body: typing.Any, status: int = 200) -> webob.Response:
    return webob.Response(
        body=json.dumps(provider_store.jsonable(body), separators=(",", ":")).encode(
            "utf-8"
        ),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _body(req: typing.Any) -> dict[str, typing.Any]:
    if req.content_length is not None and req.content_length > _MAX_BODY_BYTES:
        raise messenger_exceptions.ProviderApiError(
            status=413,
            error="payload_too_large",
            message="Provider request body exceeds 8 MiB",
        )
    try:
        value = json.loads(req.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise messenger_exceptions.ProviderApiError(
            status=400,
            error="invalid_json",
            message="Provider request body must be a JSON object",
        ) from error
    if not isinstance(value, dict):
        raise messenger_exceptions.ProviderApiError(
            status=400,
            error="invalid_request",
            message="Provider request body must be a JSON object",
        )
    return value


def _uuid(value: typing.Any, field: str) -> sys_uuid.UUID:
    try:
        return sys_uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise messenger_exceptions.ProviderApiError(
            status=400,
            error="invalid_uuid",
            message=f"{field} must be a UUID",
        ) from error


def _rebind_identity(value: typing.Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise messenger_exceptions.ProviderApiError(
            status=422,
            error="invalid_identity_rebind",
            message="rebind_identity must be a boolean",
        )
    return value


class ProviderApiMiddleware(middlewares.Middleware):
    """Serve Provider CRUD before the public RestAlchemy route dispatcher."""

    def process_request(self, req: typing.Any) -> webob.Response | None:
        if (
            req.path != _BOOTSTRAP_PATH
            and req.path != _REGISTRATION_PATH
            and req.path != _ROOT
            and not req.path.startswith(f"{_ROOT}/")
        ):
            return None
        project_uuid = sys_uuid.UUID(str(req.context.project_id))
        iam_user_uuid = sys_uuid.UUID(str(req.context.user_uuid))
        session = contexts.Context().get_session()
        if req.path == _REGISTRATION_PATH:
            return self._registration(
                req,
                session,
                project_uuid,
                iam_user_uuid,
            )
        provider = v3_store.resolve_provider_consumer(project_uuid, iam_user_uuid)
        if provider is None:
            raise messenger_exceptions.ProviderApiError(
                status=403,
                error="provider_consumer_required",
                message="The IAM identity is not an enabled provider consumer",
            )
        store = provider_store.ProviderEntityStore(
            session,
            project_uuid,
            iam_user_uuid,
            provider,
        )
        if req.path == _BOOTSTRAP_PATH:
            if req.method != "GET":
                return self._method_not_allowed(("GET",))
            # Capture the event cursor first. Mutations observed while the eager
            # snapshot is built have a newer epoch and are replayed idempotently.
            cursor = store.events._cursor("provider", store.provider_uuid)
            if req.GET.get("mode") == "paged":
                return _response(
                    {
                        "record": "manifest",
                        "schema_version": 2,
                        "snapshot_uuid": sys_uuid.uuid4(),
                        "project_id": store.project_uuid,
                        "provider_uuid": store.provider_uuid,
                        "epoch_generation": cursor["epoch_generation"],
                        "snapshot_epoch_version": int(cursor["current_epoch_version"]),
                        "created_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                )
            return webob.Response(
                app_iter=webob.static.FileIter(store.bootstrap_snapshot(cursor)),
                status=200,
                content_type="application/x-ndjson",
                charset="utf-8",
                headers={"Cache-Control": "no-store"},
            )
        if req.path.rstrip("/") == _BATCH_PATH:
            if req.method != "POST":
                return self._method_not_allowed(("POST",))
            return _response(self._apply_batch(store, _body(req)))
        entity_match = _ENTITY_PATH.fullmatch(req.path)
        if entity_match is not None:
            return self._entity(req, store, entity_match)
        collection_match = _COLLECTION_PATH.fullmatch(req.path)
        if collection_match is not None:
            if req.method != "GET":
                return self._method_not_allowed(("GET",))
            return _response(self._list(req, store, collection_match["resource"]))
        raise messenger_exceptions.ProviderApiError(
            status=404,
            error="provider_route_not_found",
            message="Provider API route does not exist",
        )

    def _registration(
        self,
        req: typing.Any,
        session: typing.Any,
        project_uuid: sys_uuid.UUID,
        iam_user_uuid: sys_uuid.UUID,
    ) -> webob.Response:
        permissions = req.context.iam_context.get_introspection_info().permissions
        if "workspace.provider.sync" not in permissions:
            raise messenger_exceptions.ProviderApiError(
                status=403,
                error="provider_registration_forbidden",
                message="Provider synchronization permission is required",
            )
        if req.method != "PUT":
            return self._method_not_allowed(("PUT",))
        body = _body(req)
        provider_uuid = _uuid(body.get("provider_uuid"), "provider_uuid")
        name = body.get("name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,31}", name
        ):
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="invalid_provider_name",
                message="name must be a valid provider source name",
            )
        if name in {"iam", "native"}:
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="invalid_provider_name",
                message="name must identify an external provider",
            )
        existing = session.execute(
            """
            SELECT uuid, name, enabled
            FROM workspace_v3.provider_consumers
            WHERE project_id = %s AND iam_user_uuid = %s
            FOR UPDATE
            """,
            (project_uuid, iam_user_uuid),
        ).fetchone()
        if existing is not None and (
            existing["uuid"] != provider_uuid or existing["name"] != name
        ):
            raise messenger_exceptions.ProviderApiError(
                status=409,
                error="provider_registration_conflict",
                message="IAM identity is registered to another provider",
            )
        try:
            row = session.execute(
                """
                INSERT INTO workspace_v3.provider_consumers (
                    uuid, project_id, name, iam_user_uuid, enabled
                ) VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (project_id, iam_user_uuid) DO UPDATE
                SET enabled = TRUE, updated_at = clock_timestamp()
                RETURNING uuid, name, enabled
                """,
                (provider_uuid, project_uuid, name, iam_user_uuid),
            ).fetchone()
        except Exception as error:
            provider_store.translate_database_error(error)
        if row["uuid"] != provider_uuid or row["name"] != name:
            raise messenger_exceptions.ProviderApiError(
                status=409,
                error="provider_registration_conflict",
                message="IAM identity is registered to another provider",
            )
        return _response(
            {
                "provider_uuid": row["uuid"],
                "name": row["name"],
                "project_id": project_uuid,
                "iam_user_uuid": iam_user_uuid,
                "enabled": row["enabled"],
            }
        )

    def _entity(
        self,
        req: typing.Any,
        store: provider_store.ProviderEntityStore,
        match: typing.Any,
    ) -> webob.Response:
        resource = match["resource"]
        entity_uuid = _uuid(match["entity_uuid"], "uuid")
        if req.method == "GET":
            return _response(store.get(resource, entity_uuid))
        if req.method == "PUT":
            body = _body(req)
            content_hash = provider_store.parse_content_hash(body.get("content_hash"))
            source_updated_at = self._source_updated_at(body)
            data = body.get("data")
            if not isinstance(data, dict):
                raise messenger_exceptions.ProviderApiError(
                    status=400,
                    error="invalid_request",
                    message="data must be a JSON object",
                )
            store.lock_entities(((resource, entity_uuid),))
            try:
                result = store.upsert(
                    resource,
                    entity_uuid,
                    content_hash,
                    data,
                    source_updated_at,
                    rebind_identity=_rebind_identity(body.get("rebind_identity")),
                )
            except messenger_exceptions.ProviderApiError:
                raise
            except Exception as error:
                provider_store.translate_database_error(error)
            return _response(result)
        if req.method == "DELETE":
            store.lock_entities(((resource, entity_uuid),))
            try:
                result = store.delete(resource, entity_uuid)
            except messenger_exceptions.ProviderApiError:
                raise
            except Exception as error:
                provider_store.translate_database_error(error)
            return _response(result)
        return self._method_not_allowed(("GET", "PUT", "DELETE"))

    def _apply_batch(
        self,
        store: provider_store.ProviderEntityStore,
        body: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        delivery_class = body.get("delivery_class", "live")
        if delivery_class not in {"live", "backfill"}:
            raise messenger_exceptions.ProviderApiError(
                status=400,
                error="invalid_delivery_class",
                message="delivery_class must be live or backfill",
            )
        operations = body.get("operations")
        if not isinstance(operations, list) or not (
            1 <= len(operations) <= provider_store.MAX_BATCH_SIZE
        ):
            raise messenger_exceptions.ProviderApiError(
                status=400,
                error="invalid_batch",
                message="operations must contain between 1 and 500 items",
            )
        prepared = []
        seen = set()
        for index, operation in enumerate(operations):
            try:
                prepared_item = self._prepare_operation(operation)
                key = (prepared_item["resource"], prepared_item["entity_uuid"])
                if key in seen:
                    raise messenger_exceptions.ProviderApiError(
                        status=422,
                        error="duplicate_batch_entity",
                        message="A batch can mutate an entity only once",
                    )
                seen.add(key)
                prepared.append(prepared_item)
            except messenger_exceptions.ProviderApiError as error:
                raise messenger_exceptions.ProviderApiError(
                    status=error.status,
                    error=error.error,
                    message=error.safe_message,
                    item_index=index,
                ) from error
        store.lock_entities(
            (item["resource"], item["entity_uuid"]) for item in prepared
        )
        results = []
        messages_to_expand = []
        for index, operation in enumerate(prepared):
            try:
                if operation["action"] == "upsert":
                    result = store.upsert(
                        operation["resource"],
                        operation["entity_uuid"],
                        operation["content_hash"],
                        operation["data"],
                        operation["source_updated_at"],
                        rebind_identity=operation["rebind_identity"],
                        emit_event=(
                            delivery_class == "live"
                            or operation["resource"]
                            not in provider_store.HISTORY_RESOURCES
                        ),
                        expand_message_flags=(delivery_class == "live"),
                    )
                    if (
                        delivery_class == "backfill"
                        and operation["resource"] == "messages"
                        and result["status"] != "unchanged"
                    ):
                        messages_to_expand.append(operation["entity_uuid"])
                else:
                    result = store.delete(
                        operation["resource"],
                        operation["entity_uuid"],
                        emit_event=(
                            delivery_class == "live"
                            or operation["resource"]
                            not in provider_store.HISTORY_RESOURCES
                        ),
                    )
                results.append(result)
            except messenger_exceptions.ProviderApiError as error:
                raise messenger_exceptions.ProviderApiError(
                    status=error.status,
                    error=error.error,
                    message=error.safe_message,
                    item_index=index,
                ) from error
            except Exception as error:
                try:
                    provider_store.translate_database_error(error)
                except messenger_exceptions.ProviderApiError as translated:
                    raise messenger_exceptions.ProviderApiError(
                        status=translated.status,
                        error=translated.error,
                        message=translated.safe_message,
                        item_index=index,
                    ) from error
        store.expand_message_flags(messages_to_expand)
        if delivery_class == "backfill":
            store.defer_backfill_counter_projections()
        return {"results": results}

    def _prepare_operation(self, operation: typing.Any) -> dict[str, typing.Any]:
        if not isinstance(operation, dict):
            raise messenger_exceptions.ProviderApiError(
                status=400,
                error="invalid_operation",
                message="Each operation must be a JSON object",
            )
        action = operation.get("action")
        if action not in {"upsert", "delete"}:
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="invalid_action",
                message="action must be upsert or delete",
            )
        resource = operation.get("type")
        if resource not in provider_store.RESOURCE_TYPES:
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="invalid_entity_type",
                message="type is not a supported Provider entity",
            )
        result: dict[str, typing.Any] = {
            "action": action,
            "resource": resource,
            "entity_uuid": _uuid(operation.get("uuid"), "uuid"),
        }
        if action == "upsert":
            result["content_hash"] = provider_store.parse_content_hash(
                operation.get("content_hash")
            )
            result["source_updated_at"] = self._source_updated_at(operation)
            data = operation.get("data")
            if not isinstance(data, dict):
                raise messenger_exceptions.ProviderApiError(
                    status=422,
                    error="invalid_entity",
                    message="upsert data must be a JSON object",
                )
            result["data"] = data
            result["rebind_identity"] = _rebind_identity(
                operation.get("rebind_identity")
            )
        return result

    @staticmethod
    def _source_updated_at(body: dict[str, typing.Any]) -> datetime.datetime:
        value = body.get("source_updated_at")
        if value is None:
            return datetime.datetime.now(datetime.timezone.utc)
        return provider_store.parse_timestamp(value, "source_updated_at")

    def _list(
        self,
        req: typing.Any,
        store: provider_store.ProviderEntityStore,
        resource: str,
    ) -> dict[str, typing.Any]:
        raw_snapshot_after = req.GET.get("snapshot_after_uuid")
        if raw_snapshot_after is not None:
            after_uuid = _uuid(raw_snapshot_after, "snapshot_after_uuid")
            try:
                limit = int(req.GET.get("limit", 100))
            except (TypeError, ValueError) as error:
                raise messenger_exceptions.ProviderApiError(
                    status=400,
                    error="invalid_limit",
                    message="limit must be an integer",
                ) from error
            if not 1 <= limit <= provider_store.MAX_PAGE_SIZE:
                raise messenger_exceptions.ProviderApiError(
                    status=400,
                    error="invalid_limit",
                    message="limit must be between 1 and 500",
                )
            return store.bootstrap_page(resource, after_uuid, limit)
        raw_updated_after = req.GET.get("updated_after")
        updated_after = (
            datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
            if raw_updated_after is None
            else provider_store.parse_timestamp(raw_updated_after, "updated_after")
        )
        after_uuid = _uuid(
            req.GET.get("after_uuid", provider_store.ZERO_UUID), "after_uuid"
        )
        try:
            limit = int(req.GET.get("limit", 100))
        except (TypeError, ValueError) as error:
            raise messenger_exceptions.ProviderApiError(
                status=400,
                error="invalid_limit",
                message="limit must be an integer",
            ) from error
        if not 1 <= limit <= provider_store.MAX_PAGE_SIZE:
            raise messenger_exceptions.ProviderApiError(
                status=400,
                error="invalid_limit",
                message="limit must be between 1 and 500",
            )
        return store.list(resource, updated_after, after_uuid, limit)

    def _method_not_allowed(self, allowed: tuple[str, ...]) -> webob.Response:
        response = _response(
            {
                "type": "ProviderApiError",
                "status": 405,
                "error": "method_not_allowed",
                "message": "HTTP method is not allowed for this Provider route",
            },
            status=405,
        )
        response.headers["Allow"] = ", ".join(allowed)
        return response
