# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib
import subprocess
import sys
import types
import uuid as sys_uuid
from unittest import mock

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


def test_all_messenger_http_entrypoints_register_external_bridge_options():
    for module in (
        "workspace.cmd.messenger_api",
        "workspace.cmd.workspace_api",
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from oslo_config import cfg; "
                    f"import {module}; "
                    "assert cfg.CONF['external_bridge'].realm_uuid is None"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr


def test_all_message_snapshot_writers_register_reaction_options():
    for module in (
        "workspace.cmd.external_bridge_api",
        "workspace.cmd.messenger_api",
        "workspace.cmd.workspace_api",
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from oslo_config import cfg; "
                    f"import {module}; "
                    "options = cfg.CONF['messenger_reactions']; "
                    "assert options.user_list_limit == 4"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr


def test_messenger_worker_registers_file_storage_options():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from oslo_config import cfg; "
                "import workspace.cmd.messenger_worker; "
                "assert cfg.CONF['messenger_files'].default_type == 'file'; "
                "assert cfg.CONF['messenger_files_s3'].endpoint_url is None"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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


def test_postgresql_factory_syncs_active_request_iam_identity(monkeypatch):
    user_uuid = sys_uuid.uuid4()
    iam_user = types.SimpleNamespace(
        name="cassi",
        first_name="Cassandra",
        last_name="Volkova",
        email="cassi@exordos.com",
    )
    iam_context = mock.Mock()
    iam_context.get_introspection_info.return_value.user_info = iam_user

    class RequestContext:
        @property
        def iam_context(self):
            return iam_context

    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "get_context",
        lambda: RequestContext(),
    )
    store = mock.Mock()

    sql_canonical_store.SQLCanonicalMessengerStoreFactory._sync_request_iam_identity(
        store,
        user_uuid,
    )

    store.sync_iam_identity.assert_called_once_with(
        {
            "user_uuid": user_uuid,
            "username": "cassi",
            "first_name": "Cassandra",
            "last_name": "Volkova",
            "email": "cassi@exordos.com",
        }
    )
