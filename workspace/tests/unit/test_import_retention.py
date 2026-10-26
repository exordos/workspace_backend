# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import os
import types
import uuid
from unittest import mock

import pytest

from workspace.messenger_api import file_storage
from workspace.services.messenger_workers import import_retention


def _key():
    return f"history-imports/v1/{uuid.uuid4()}/{uuid.uuid4()}/batch.json"


def test_local_retired_batches_expire_but_recent_batches_and_user_files_survive(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    storage = file_storage.LocalWorkspaceFileStorage()
    monkeypatch.setattr(file_storage, "get_workspace_file_storage", lambda: storage)
    now = datetime.datetime.now(datetime.timezone.utc)
    old, recent, user_file = _key(), _key(), f"aa/{uuid.uuid4()}"
    for key in [old, recent, user_file]:
        storage.save(uuid.uuid4(), b"preserve or collect", storage_object_id=key)
    os.utime(tmp_path / old, (now.timestamp() - 7200,) * 2)
    os.utime(tmp_path / user_file, (now.timestamp() - 7200,) * 2)
    collector = import_retention.Collector()
    assert collector.prune(now, 3600, 1000) == (1, False)
    assert not (tmp_path / old).exists()
    assert (tmp_path / recent).exists()
    assert (tmp_path / user_file).exists()


def test_storage_pagination_is_bounded_and_partial_errors_are_retried(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    old = {"Key": _key(), "LastModified": now - datetime.timedelta(hours=2)}
    fresh = {"Key": _key(), "LastModified": now}
    unrelated = {
        "Key": "history-imports/v1/unrelated.json",
        "LastModified": old["LastModified"],
    }
    client = mock.Mock()
    client.get_paginator.return_value.paginate.side_effect = lambda **kwargs: iter(
        [
            {"Contents": [old]},
            {"Contents": [fresh, unrelated]},
        ]
    )
    storage = types.SimpleNamespace(
        storage_type="s3", bucket_name="test-bucket", client=client
    )
    monkeypatch.setattr(file_storage, "get_workspace_file_storage", lambda: storage)
    client.delete_objects.return_value = {
        "Errors": [{"Key": old["Key"], "Code": "Unavailable"}]
    }
    collector = import_retention.Collector()
    with pytest.raises(RuntimeError, match="deletion failed"):
        collector.prune(now, 3600, 1)
    client.delete_objects.return_value = {}
    assert collector.prune(now, 3600, 1) == (1, True)
    assert collector.prune(now, 3600, 1) == (0, True)
    assert collector.prune(now, 3600, 1) == (0, True)
    assert collector.prune(now, 3600, 1) == (0, False)
    assert client.get_paginator.return_value.paginate.call_count == 2
    assert client.delete_objects.call_count == 2
