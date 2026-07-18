# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Storage boundary used by the public Messenger controllers.

The HTTP layer deliberately knows nothing about the canonical persistence
implementation.  Stores return public-contract dictionaries, so PostgreSQL can
replace the transitional mail projection without changing the UI API.  Tests
install an in-memory factory with ``configure_store_factory``.
"""

import contextlib
import typing
import uuid as sys_uuid

from restalchemy.common import contexts

from workspace.messenger_migration import writer_gate


class MessengerStore(typing.Protocol):
    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        """Materialize the current IAM identity in canonical storage."""

    def filter_resources(
        self,
        resource: str,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]: ...

    def filter_message_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        """Read one stable ``(created_at, uuid)`` keyset page."""

    def filter_draft_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        """Read one owner-scoped ``(updated_at, uuid)`` keyset page."""

    def get_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]: ...

    def get_draft(
        self,
        draft_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]: ...

    def create_resource(
        self,
        resource: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]: ...

    def update_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]: ...

    def delete_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None: ...

    def perform_action(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
        action: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any] | list[dict[str, typing.Any]]: ...

    def create_message(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        """Create one canonical message and its recipient events atomically."""

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]: ...

    def delete_message(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        """Delete one canonical message and emit recipient invalidations."""

    def create_draft(
        self,
        values: dict[str, typing.Any],
    ) -> tuple[dict[str, typing.Any], bool]: ...

    def update_draft(
        self,
        draft_uuid: sys_uuid.UUID,
        payload: dict[str, typing.Any],
        expected_revision: int,
    ) -> dict[str, typing.Any]: ...

    def delete_draft(
        self,
        draft_uuid: sys_uuid.UUID,
        expected_revision: int,
    ) -> None: ...

    def events_after(
        self,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        epoch_generation: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, typing.Any]]: ...

    def current_epoch(self) -> int: ...

    def event_cursor(self) -> dict[str, typing.Any]: ...


StoreContext = contextlib.AbstractContextManager[MessengerStore]
StoreFactory = typing.Callable[[sys_uuid.UUID, sys_uuid.UUID], StoreContext]

_MUTATING_METHODS = frozenset(
    {
        "sync_iam_identity",
        "create_resource",
        "update_resource",
        "delete_resource",
        "perform_action",
        "create_message",
        "update_message",
        "delete_message",
        "create_draft",
        "update_draft",
        "delete_draft",
    }
)


class WriterGateStoreProxy:
    """Guard the real API store without changing its public contract."""

    def __init__(
        self,
        project_uuid: sys_uuid.UUID,
        store: MessengerStore,
    ) -> None:
        self._project_uuid = project_uuid
        self._store = store

    def __getattr__(self, name: str) -> typing.Any:
        value = getattr(self._store, name)
        if name not in _MUTATING_METHODS:
            return value

        def guarded(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
            session = contexts.Context().get_session()
            writer_gate.assert_writable(session, self._project_uuid, "api")
            return value(*args, **kwargs)

        return guarded


def guard_api_store(
    project_uuid: sys_uuid.UUID,
    store: MessengerStore,
) -> WriterGateStoreProxy:
    return WriterGateStoreProxy(project_uuid, store)


class StoreNotConfigured(RuntimeError):
    pass


def _missing_store_factory(
    project_uuid: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
) -> StoreContext:
    del project_uuid, user_uuid
    raise StoreNotConfigured(
        "Messenger mail service is not configured. Install a MessengerStore factory."
    )


_store_factory: StoreFactory = _missing_store_factory
_draft_store_factory: StoreFactory = _missing_store_factory
_event_store_factory: StoreFactory = _missing_store_factory


def configure_store_factory(factory: StoreFactory) -> None:
    global _store_factory, _draft_store_factory, _event_store_factory
    _store_factory = factory
    _draft_store_factory = getattr(factory, "draft_store", factory)
    _event_store_factory = getattr(factory, "event_store", factory)


def reset_store_factory() -> None:
    global _store_factory, _draft_store_factory, _event_store_factory
    _store_factory = _missing_store_factory
    _draft_store_factory = _missing_store_factory
    _event_store_factory = _missing_store_factory


def open_store(project_uuid: sys_uuid.UUID, user_uuid: sys_uuid.UUID) -> StoreContext:
    return _store_factory(project_uuid, user_uuid)


def open_draft_store(
    project_uuid: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
) -> StoreContext:
    return _draft_store_factory(project_uuid, user_uuid)


def open_event_store(
    project_uuid: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
) -> StoreContext:
    """Open the lightweight per-user event journal without project replay."""

    return _event_store_factory(project_uuid, user_uuid)


def move_stream_projection(**kwargs: typing.Any) -> None:
    """Move one stream through the configured canonical storage adapter."""
    move = getattr(_store_factory, "move_stream_projection", None)
    if move is None:
        raise StoreNotConfigured(
            "Configured Messenger store cannot move stream projections"
        )
    move(**kwargs)
