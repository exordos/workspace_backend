# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Track provider entity synchronization inside the caller's transaction."""

import collections.abc
import contextlib
import dataclasses
import functools
import typing
import uuid

from restalchemy.common import contexts
from restalchemy.storage.sql import utils

from workspace.messenger_api.dm import helpers


@dataclasses.dataclass(frozen=True)
class EntityScope:
    table: str
    project_id: uuid.UUID
    identity: dict[str, typing.Any]

    def set_status(self, session: typing.Any, status: str) -> None:
        condition = " AND ".join(f"{name}=%s" for name in self.identity)
        session.execute(
            f"UPDATE {self.table} SET status=%s WHERE project_id=%s AND {condition} "
            "AND source_name<>'native' AND status IS DISTINCT FROM %s",
            (status, self.project_id, *self.identity.values(), status),
        )


@contextlib.contextmanager
def synchronize(
    model: typing.Any, project_id: uuid.UUID, **identity: typing.Any
) -> collections.abc.Iterator[None]:
    scope = EntityScope(model.__tablename__, project_id, identity)
    try:
        with utils.savepoint() as session:
            scope.set_status(session, "syncing")
            yield
            scope.set_status(session, "active")
    except Exception as error:
        # The worker records the failure after rolling back its outer savepoint.
        setattr(
            error,
            "workspace_sync_scopes",
            (*getattr(error, "workspace_sync_scopes", ()), scope),
        )
        raise


def track_entity(model: typing.Any, **identity: str) -> typing.Callable:
    def decorate(method: typing.Callable) -> typing.Callable:
        @functools.wraps(method)
        def execute(state: typing.Any) -> typing.Any:
            helpers._lock_workspace_project_event_writes(
                state.project_id, contexts.Context().get_session()
            )
            values = {name: getattr(state, field) for name, field in identity.items()}
            with synchronize(model, state.project_id, **values):
                return method(state)

        return execute

    return decorate


def record_error(error: Exception, session: typing.Any = None) -> None:
    session = session or contexts.Context().get_session()
    for scope in getattr(error, "workspace_sync_scopes", ()):
        scope.set_status(session, "error")
