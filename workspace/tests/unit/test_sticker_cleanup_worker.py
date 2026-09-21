# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

from workspace.services.messenger_workers import sticker_cleanup


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Session:
    def __init__(self, task=None):
        self.task = task
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        if "WITH candidate AS" in statement:
            return _Result(self.task)
        return _Result()


class _Storage:
    def __init__(self, error=None):
        self.error = error
        self.deleted = []

    def delete(self, storage_object_id):
        self.deleted.append(storage_object_id)
        if self.error is not None:
            raise self.error


def _task(attempts=1):
    return {
        "sticker_uuid": sys_uuid.uuid4(),
        "storage_object_id": "stickers/deleted/media.gif",
        "attempts": attempts,
    }


def test_empty_cleanup_queue_does_not_build_storage(monkeypatch):
    session = _Session()
    calls = []
    monkeypatch.setattr(
        sticker_cleanup.sticker_storage,
        "get_sticker_storage",
        lambda: calls.append("storage"),
    )

    assert not sticker_cleanup.process_one_sticker_cleanup_task(session, "worker")
    assert calls == []
    assert "status IN ('pending', 'running', 'failed')" in session.calls[0][0]


def test_cleanup_deletes_after_claim_and_marks_completed(monkeypatch):
    task = _task()
    session = _Session(task)
    storage = _Storage()
    monkeypatch.setattr(
        sticker_cleanup.sticker_storage,
        "get_sticker_storage",
        lambda: storage,
    )

    assert sticker_cleanup.process_one_sticker_cleanup_task(session, "worker")

    assert storage.deleted == [task["storage_object_id"]]
    assert "status = 'running'" in session.calls[0][0]
    assert "status = 'completed'" in session.calls[1][0]
    assert session.calls[1][1] == (task["sticker_uuid"], "worker")


def test_cleanup_failure_is_persisted_for_idempotent_retry(monkeypatch):
    task = _task(attempts=2)
    session = _Session(task)
    storage = _Storage(RuntimeError("private backend details"))
    monkeypatch.setattr(
        sticker_cleanup.sticker_storage,
        "get_sticker_storage",
        lambda: storage,
    )

    assert sticker_cleanup.process_one_sticker_cleanup_task(session, "worker")

    assert storage.deleted == [task["storage_object_id"]]
    assert "status = 'failed'" in session.calls[1][0]
    assert session.calls[1][1] == (
        "RuntimeError",
        10,
        task["sticker_uuid"],
        "worker",
    )

    storage.error = None
    retry_session = _Session({**task, "attempts": 3})
    assert sticker_cleanup.process_one_sticker_cleanup_task(retry_session, "worker")
    assert storage.deleted == [task["storage_object_id"]] * 2
    assert "status = 'completed'" in retry_session.calls[1][0]
