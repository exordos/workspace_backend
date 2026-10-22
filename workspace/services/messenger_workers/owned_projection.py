# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Transaction-local invalidations returned to the sole dispatcher after commit."""

import contextlib
import contextvars
import dataclasses
import typing


@dataclasses.dataclass
class Changes:
    topics: dict = dataclasses.field(default_factory=dict)
    streams: dict = dataclasses.field(default_factory=dict)
    folders: dict = dataclasses.field(default_factory=dict)
    projects: set = dataclasses.field(default_factory=set)

    def topic(
        self,
        project_id: object,
        stream_uuid: object,
        topic_uuid: object,
        users: typing.Iterable[object],
    ) -> None:
        self.topics.setdefault((project_id, stream_uuid, topic_uuid), set()).update(
            users
        )

    def stream(
        self, project_id: object, stream_uuid: object, users: typing.Iterable[object]
    ) -> None:
        self.streams.setdefault((project_id, stream_uuid), set()).update(users)


CURRENT: contextvars.ContextVar[Changes | None] = contextvars.ContextVar(
    "messenger_owned_projection", default=None
)


@contextlib.contextmanager
def capture() -> typing.Iterator[Changes]:
    changes = Changes()
    token = CURRENT.set(changes)
    try:
        yield changes
    finally:
        CURRENT.reset(token)
