# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""A single dispatcher owns scheduling; pool threads own transactions.

All methods run in the dispatcher thread. A running key stays reserved until
its Future really finishes, including on failure or shutdown. A trigger during
execution requests one further pass, merging its payload with later triggers.
"""

import collections
import concurrent.futures
import dataclasses
import time
import typing


@dataclasses.dataclass
class Work:
    key: tuple
    values: set = dataclasses.field(default_factory=set)
    reservations: frozenset = frozenset()
    ready_at: float = 0
    priority: int = 0
    created_at: float = dataclasses.field(default_factory=time.monotonic)


class KeyedDispatcher:
    def __init__(self, execute: typing.Callable, workers: int = 4):
        self._execute = execute
        self._workers = workers
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="messenger-owned"
        )
        self._pending: collections.OrderedDict[tuple, Work] = collections.OrderedDict()
        self._running: dict[concurrent.futures.Future, Work] = {}
        self._reserved: set = set()
        self._closed = False

    def trigger(
        self,
        key: tuple,
        values: typing.Iterable = (),
        reservations: typing.Iterable[tuple] = (),
        priority: int = 0,
    ) -> None:
        if self._closed:
            raise RuntimeError("Dispatcher is closed")
        work = self._pending.setdefault(key, Work(key))
        work.values.update(values)
        work.priority = max(work.priority, priority)
        work.reservations |= frozenset((key, *reservations))

    def poll(self) -> list[tuple[Work, object, BaseException | None]]:
        completed: list[tuple[Work, object, BaseException | None]] = []
        for future, work in list(self._running.items()):
            if not future.done():
                continue
            del self._running[future]
            self._reserved.difference_update(work.reservations)
            try:
                result = future.result()
            except Exception as error:
                # Requeue failed inputs as well as triggers received while
                # running. Never release a reservation on a timeout alone.
                self.trigger(work.key, work.values, work.reservations, work.priority)
                self._pending[work.key].ready_at = time.monotonic() + 0.1
                completed.append((work, None, error))
            else:
                completed.append((work, result, None))
            # A retrigger received during execution is a later batch. Let
            # already-waiting jobs sharing its reservations run first.
            if work.key in self._pending:
                self._pending.move_to_end(work.key)
        # Age waiting jobs so continuous message traffic cannot starve flags.
        # Equal effective priorities retain FIFO and all reservation fences.
        now = time.monotonic()
        for key, work in sorted(
            self._pending.items(),
            key=lambda item: -(item[1].priority + int((now - item[1].created_at) / 2)),
        ):
            if len(self._running) >= self._workers:
                break
            if work.ready_at > time.monotonic():
                continue
            if self._reserved.intersection(work.reservations):
                continue
            del self._pending[key]
            self._reserved.update(work.reservations)
            self._running[self._pool.submit(self._execute, work)] = work
        return completed

    @property
    def idle(self) -> bool:
        return not self._pending and not self._running

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._running)

    def close(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._running.clear()
        self._pending.clear()
        self._reserved.clear()
