# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Minimal realtime Provider API layered on the v3 entity store."""

import typing
import uuid as sys_uuid

import webob
from restalchemy.common import contexts

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import provider_api
from workspace.messenger_api import provider_store
from workspace.messenger_api.api import v3_store


ROOT = "/v1/provider/v4/realtime"
REALTIME_RESOURCE_TYPES = frozenset(
    {
        "users",
        "streams",
        "stream_bindings",
        "topics",
        "topic_bindings",
        "messages",
    }
)
REALTIME_OPERATION_FIELDS = frozenset(
    {"action", "type", "uuid", "content_hash", "source_updated_at", "data"}
)


def _context_uuid(value: typing.Any, index: int, field: str) -> sys_uuid.UUID:
    try:
        return sys_uuid.UUID(str(value))
    except (TypeError, ValueError) as error:
        raise messenger_exceptions.ProviderApiError(
            status=422,
            error="realtime_context_mismatch",
            message=f"Realtime context has an invalid {field}",
            item_index=index,
        ) from error


def _operation_uuid(
    operation: dict[str, typing.Any],
    index: int,
) -> sys_uuid.UUID:
    return _context_uuid(operation.get("uuid"), index, "uuid")


def _operation_data(
    operation: dict[str, typing.Any],
    index: int,
) -> dict[str, typing.Any]:
    data = operation.get("data")
    if not isinstance(data, dict):
        raise messenger_exceptions.ProviderApiError(
            status=422,
            error="realtime_context_mismatch",
            message="Realtime upserts require object data",
            item_index=index,
        )
    return data


def _validate_realtime_context(
    operations: list[typing.Any],
) -> None:
    parsed: list[tuple[int, dict[str, typing.Any]]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        if set(operation) - REALTIME_OPERATION_FIELDS:
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="realtime_operation_fields",
                message="The v4 API accepts only realtime operation fields",
                item_index=index,
            )
        parsed.append((index, operation))

    message_operations = [item for item in parsed if item[1].get("type") == "messages"]
    if not message_operations:
        raise messenger_exceptions.ProviderApiError(
            status=422,
            error="realtime_message_required",
            message="Every v4 batch must contain a message event",
        )
    if all(operation.get("action") == "delete" for _, operation in parsed):
        if len(message_operations) != len(parsed):
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="realtime_context_mismatch",
                message="Message delete batches cannot include context",
            )
        return

    by_type: dict[str, list[tuple[int, dict[str, typing.Any]]]] = {
        resource: [] for resource in REALTIME_RESOURCE_TYPES
    }
    for item in parsed:
        operation = item[1]
        resource = operation.get("type")
        if resource in by_type:
            by_type[resource].append(item)
        if operation.get("action") != "upsert":
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="realtime_context_mismatch",
                message="Message upserts cannot mix delete operations",
                item_index=item[0],
            )
    expected_counts = {
        "streams": 1,
        "stream_bindings": 1,
        "topics": 1,
        "topic_bindings": 1,
        "messages": 1,
    }
    if any(
        len(by_type[resource]) != count for resource, count in expected_counts.items()
    ):
        raise messenger_exceptions.ProviderApiError(
            status=422,
            error="realtime_context_mismatch",
            message="A message upsert requires exactly its current route context",
        )

    message_index, message = by_type["messages"][0]
    message_data = _operation_data(message, message_index)
    stream_uuid = _context_uuid(
        message_data.get("stream_uuid"), message_index, "stream_uuid"
    )
    topic_uuid = _context_uuid(
        message_data.get("topic_uuid"), message_index, "topic_uuid"
    )
    author_uuid = _context_uuid(
        message_data.get("author_uuid"), message_index, "author_uuid"
    )

    stream_index, stream = by_type["streams"][0]
    stream_data = _operation_data(stream, stream_index)
    topic_index, topic = by_type["topics"][0]
    topic_data = _operation_data(topic, topic_index)
    stream_binding_index, stream_binding = by_type["stream_bindings"][0]
    stream_binding_data = _operation_data(stream_binding, stream_binding_index)
    topic_binding_index, topic_binding = by_type["topic_bindings"][0]
    topic_binding_data = _operation_data(topic_binding, topic_binding_index)

    referenced_users = {
        author_uuid,
        _context_uuid(stream_data.get("owner_uuid"), stream_index, "owner_uuid"),
        _context_uuid(
            stream_binding_data.get("user_uuid"),
            stream_binding_index,
            "user_uuid",
        ),
        _context_uuid(
            topic_binding_data.get("user_uuid"),
            topic_binding_index,
            "user_uuid",
        ),
    }
    if stream_data.get("direct_user_uuid") is not None:
        referenced_users.add(
            _context_uuid(
                stream_data["direct_user_uuid"],
                stream_index,
                "direct_user_uuid",
            )
        )
    user_uuids = {
        _operation_uuid(operation, index) for index, operation in by_type["users"]
    }
    route_matches = (
        _operation_uuid(stream, stream_index) == stream_uuid
        and _operation_uuid(topic, topic_index) == topic_uuid
        and _context_uuid(topic_data.get("stream_uuid"), topic_index, "stream_uuid")
        == stream_uuid
        and _context_uuid(
            stream_binding_data.get("stream_uuid"),
            stream_binding_index,
            "stream_uuid",
        )
        == stream_uuid
        and _context_uuid(
            topic_binding_data.get("stream_uuid"),
            topic_binding_index,
            "stream_uuid",
        )
        == stream_uuid
        and _context_uuid(
            topic_binding_data.get("topic_uuid"),
            topic_binding_index,
            "topic_uuid",
        )
        == topic_uuid
        and user_uuids
        and len(user_uuids) == len(by_type["users"])
        and user_uuids <= referenced_users
    )
    if not route_matches:
        raise messenger_exceptions.ProviderApiError(
            status=422,
            error="realtime_context_mismatch",
            message="Realtime context must belong to the batch message",
        )


class ProviderRealtimeApiMiddleware(provider_api.ProviderApiMiddleware):
    """Accept current events only and reuse v3 canonical mutations."""

    def process_request(self, req: typing.Any) -> webob.Response | None:
        if req.path.rstrip("/") != ROOT:
            return None
        if req.method != "POST":
            return self._method_not_allowed(("POST",))

        permissions = req.context.iam_context.get_introspection_info().permissions
        if "workspace.provider.sync" not in permissions:
            raise messenger_exceptions.ProviderApiError(
                status=403,
                error="provider_sync_forbidden",
                message="Provider synchronization permission is required",
            )

        project_uuid = sys_uuid.UUID(str(req.context.project_id))
        iam_user_uuid = sys_uuid.UUID(str(req.context.user_uuid))
        provider = v3_store.resolve_provider_consumer(project_uuid, iam_user_uuid)
        if provider is None:
            raise messenger_exceptions.ProviderApiError(
                status=403,
                error="provider_consumer_required",
                message="The IAM identity is not an enabled provider consumer",
            )

        body = provider_api._body(req)
        if set(body) != {"operations"}:
            raise messenger_exceptions.ProviderApiError(
                status=422,
                error="realtime_operations_only",
                message="The v4 API accepts only realtime operations",
            )
        operations = body["operations"]
        if isinstance(operations, list):
            for index, operation in enumerate(operations):
                if not isinstance(operation, dict):
                    continue
                resource_type = operation.get("type")
                if resource_type not in REALTIME_RESOURCE_TYPES:
                    raise messenger_exceptions.ProviderApiError(
                        status=422,
                        error="realtime_entity_type_required",
                        message="The v4 API accepts only realtime message context",
                        item_index=index,
                    )
                if operation.get("action") == "delete" and resource_type != "messages":
                    raise messenger_exceptions.ProviderApiError(
                        status=422,
                        error="realtime_message_delete_required",
                        message="The v4 API deletes messages only",
                        item_index=index,
                    )
            _validate_realtime_context(operations)

        store = provider_store.ProviderEntityStore(
            contexts.Context().get_session(),
            project_uuid,
            iam_user_uuid,
            provider,
        )
        return provider_api._response(
            self._apply_batch(
                store,
                {"delivery_class": "live", "operations": operations},
            )
        )
