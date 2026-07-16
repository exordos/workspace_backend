# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Storage boundary used by the public Messenger controllers.

The HTTP layer deliberately knows nothing about IMAP UIDs, SMTP recipients, or
Maildir paths.  A mail-backed implementation must satisfy this interface and
return public-contract dictionaries.  Tests install an in-memory factory with
``configure_store_factory``.
"""

import contextlib
import typing
import uuid as sys_uuid


class MessengerStore(typing.Protocol):
    def sync_iam_identity(
        self,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        """Materialize the current IAM identity in the SQL projection."""

    def filter_resources(
        self,
        resource: str,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
    ) -> list[dict[str, typing.Any]]: ...

    def filter_message_page(
        self,
        filters: dict[str, typing.Any],
        marker_uuid: sys_uuid.UUID | None,
        sort_direction: str,
        limit: int | None,
    ) -> list[dict[str, typing.Any]]:
        """Read one stable ``(created_at, uuid)`` keyset page."""

    def get_resource(
        self,
        resource: str,
        resource_uuid: sys_uuid.UUID,
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
        """Deliver one codec.MessengerEnvelope through SMTP, then journal it."""

    def update_message(
        self,
        message_uuid: sys_uuid.UUID,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any]: ...

    def delete_message(
        self,
        message_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        """Expunge every participant copy, then append a bodyless tombstone."""

    def events_after(
        self,
        filters: dict[str, typing.Any],
        order_by: dict[str, str] | None = None,
        epoch_generation: str | None = None,
    ) -> list[dict[str, typing.Any]]: ...

    def current_epoch(self) -> int: ...

    def event_cursor(self) -> dict[str, typing.Any]: ...


StoreContext = contextlib.AbstractContextManager[MessengerStore]
StoreFactory = typing.Callable[[sys_uuid.UUID, sys_uuid.UUID], StoreContext]


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


def configure_store_factory(factory: StoreFactory) -> None:
    global _store_factory
    _store_factory = factory


def reset_store_factory() -> None:
    global _store_factory
    _store_factory = _missing_store_factory


def open_store(project_uuid: sys_uuid.UUID, user_uuid: sys_uuid.UUID) -> StoreContext:
    return _store_factory(project_uuid, user_uuid)
