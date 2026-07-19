# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory


def test_factory_is_always_postgresql_canonical():
    factory = store_factory.build_store_factory()

    assert isinstance(factory, sql_canonical_store.SQLCanonicalMessengerStoreFactory)


def test_all_messenger_entrypoints_use_the_canonical_factory():
    for relative_path in (
        "workspace/cmd/messenger_api.py",
        "workspace/cmd/workspace_api.py",
        "workspace/cmd/messenger_events.py",
        "workspace/cmd/messenger_worker.py",
    ):
        source = pathlib.Path(relative_path).read_text()
        assert "store_factory.build_store_factory()" in source


def test_projection_move_is_delegated_to_configured_storage_factory():
    calls = []

    class Factory:
        def move_stream_projection(self, **kwargs):
            calls.append(kwargs)

    api_store.configure_store_factory(Factory())
    try:
        api_store.move_stream_projection(stream_uuid="stream", write_old=False)
    finally:
        api_store.reset_store_factory()

    assert calls == [{"stream_uuid": "stream", "write_old": False}]


def test_postgresql_projection_move_is_a_noop():
    factory = sql_canonical_store.SQLCanonicalMessengerStoreFactory()

    assert factory.move_stream_projection(stream_uuid="stream") is None
