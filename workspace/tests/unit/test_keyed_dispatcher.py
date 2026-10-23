# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import threading
import time

import pytest

from workspace.services.messenger_workers import keyed_dispatcher
from workspace.services.messenger_workers import mutation_runtime


def drain(dispatcher):
    deadline = time.monotonic() + 3
    completions = []
    while not dispatcher.idle:
        completions.extend(dispatcher.poll())
        assert time.monotonic() < deadline
        time.sleep(0.001)
    return completions


def test_message_work_overtakes_pending_flags_without_overlapping_its_topic():
    calls = []
    dispatcher = keyed_dispatcher.KeyedDispatcher(
        lambda work: calls.append(work.key), workers=1
    )
    topic = ("topic", 1)
    try:
        dispatcher.trigger(("flags", 1), reservations=[topic], priority=0)
        dispatcher.trigger(("message", 1), reservations=[topic], priority=2)
        drain(dispatcher)
        assert calls == [("message", 1), ("flags", 1)]
    finally:
        dispatcher.close()


def test_priority_still_allows_old_background_work_to_make_progress():
    calls = []
    dispatcher = keyed_dispatcher.KeyedDispatcher(
        lambda work: calls.append(work.key), workers=1
    )
    try:
        dispatcher.trigger(("flags", 1), priority=0)
        dispatcher._pending[("flags", 1)].created_at = time.monotonic() - 10
        dispatcher.trigger(("message", 1), priority=2)
        drain(dispatcher)
        assert calls == [("flags", 1), ("message", 1)]
    finally:
        dispatcher.close()


def test_running_scope_coalesces_one_rerun_without_blocking_other_topics():
    started, release, other = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def execute(work):
        calls.append((work.key, set(work.values)))
        if work.key == ("topic", 1) and work.values == {1}:
            started.set()
            assert release.wait(3)
        if work.key == ("topic", 2):
            other.set()

    dispatcher = keyed_dispatcher.KeyedDispatcher(execute, workers=2)
    try:
        dispatcher.trigger(("topic", 1), {1})
        dispatcher.poll()
        assert started.wait(3)
        dispatcher.trigger(("topic", 1), {2})
        dispatcher.trigger(("topic", 1), {3})
        dispatcher.trigger(("topic", 2), {4})
        dispatcher.poll()
        assert other.wait(3)
        assert not any(values == {2, 3} for _, values in calls)
        release.set()
        drain(dispatcher)
        assert calls.count((("topic", 1), {2, 3})) == 1
    finally:
        release.set()
        dispatcher.close()


def test_canonical_job_reserves_every_placement_topic_until_future_finishes():
    started, release = threading.Event(), threading.Event()
    calls = []

    def execute(work):
        calls.append(work.key)
        if work.key == ("message", 1):
            started.set()
            assert release.wait(3)

    dispatcher = keyed_dispatcher.KeyedDispatcher(execute, workers=3)
    try:
        dispatcher.trigger(("message", 1), reservations=[("topic", 1), ("topic", 2)])
        dispatcher.poll()
        assert started.wait(3)
        dispatcher.trigger(("topic", 1))
        dispatcher.trigger(("topic", 2))
        dispatcher.trigger(("topic", 3))
        dispatcher.poll()
        deadline = time.monotonic() + 3
        while ("topic", 3) not in calls:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert ("topic", 1) not in calls and ("topic", 2) not in calls
        release.set()
        drain(dispatcher)
        assert set(calls) == {("message", 1), ("topic", 1), ("topic", 2), ("topic", 3)}
    finally:
        release.set()
        dispatcher.close()


def test_failed_worker_retries_inputs_and_merges_concurrent_triggers():
    calls = []

    def execute(work):
        calls.append(set(work.values))
        if len(calls) == 1:
            raise RuntimeError("transaction rolled back")
        return "committed"

    dispatcher = keyed_dispatcher.KeyedDispatcher(execute, workers=1)
    try:
        dispatcher.trigger(("stream", 1), {1})
        dispatcher.poll()
        dispatcher.trigger(("stream", 1), {2})
        completed = drain(dispatcher)
        assert calls == [{1}, {1, 2}]
        assert isinstance(completed[0][2], RuntimeError)
        assert completed[-1][1] == "committed"
    finally:
        dispatcher.close()
    with pytest.raises(RuntimeError, match="closed"):
        dispatcher.trigger(("stream", 1))


def test_installation_refuses_a_second_dispatcher_until_workers_close(tmp_path):
    first = mutation_runtime.MutationRuntime(singleton_directory=tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            mutation_runtime.MutationRuntime(singleton_directory=tmp_path)
    finally:
        first.close()
    next_owner = mutation_runtime.MutationRuntime(singleton_directory=tmp_path)
    next_owner.close()


def test_repeated_running_key_yields_to_a_waiting_job_on_the_same_topic():
    started, release = threading.Event(), threading.Event()
    calls = []

    def execute(work):
        calls.append(work.key)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)

    dispatcher = keyed_dispatcher.KeyedDispatcher(execute, workers=2)
    provider, command, topic = ("provider", 1), ("command", 1), ("topic", 1)
    try:
        dispatcher.trigger(provider, reservations=[topic])
        dispatcher.poll()
        assert started.wait(3)
        # A continuous source retriggers the running key before a user's
        # command arrives. Its next batch must not always jump that command.
        dispatcher.trigger(provider, reservations=[topic])
        dispatcher.trigger(command, reservations=[topic])
        release.set()
        drain(dispatcher)
        assert calls == [provider, command, provider]
    finally:
        release.set()
        dispatcher.close()
