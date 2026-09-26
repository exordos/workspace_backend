# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

import contextlib
import contextvars
import typing
import uuid as sys_uuid


_ORIGIN: contextvars.ContextVar[tuple[str, sys_uuid.UUID] | None] = (
    contextvars.ContextVar("workspace_event_origin", default=None)
)


def current() -> tuple[str, sys_uuid.UUID] | None:
    return _ORIGIN.get()


@contextlib.contextmanager
def use(
    consumer_type: str,
    consumer_uuid: sys_uuid.UUID,
) -> typing.Iterator[None]:
    token = _ORIGIN.set((consumer_type, consumer_uuid))
    try:
        yield
    finally:
        _ORIGIN.reset(token)
