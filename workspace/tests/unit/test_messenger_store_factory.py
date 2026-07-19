# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import contextlib
import pathlib
import subprocess
import sys
import types
import uuid as sys_uuid

import pytest

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import sql_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.messenger_mail import runtime as mail_runtime
from workspace.services.messenger_workers import agents


def _config(mode="mail_projection", confirmed=False):
    return types.SimpleNamespace(
        mode=mode,
        canonical_cutover_confirmed=confirmed,
    )


def test_mail_projection_remains_the_default_factory():
    mail_factory = object()

    selected = store_factory.select_store_factory(_config(), mail_factory)

    assert selected is mail_factory


def test_postgresql_cutover_requires_explicit_operator_confirmation():
    with pytest.raises(store_factory.CanonicalCutoverNotConfirmed):
        store_factory.select_store_factory(
            _config(mode="postgresql_canonical"),
            object(),
        )


def test_confirmed_postgresql_cutover_uses_no_mail_runtime():
    selected = store_factory.select_store_factory(
        _config(mode="postgresql_canonical", confirmed=True),
        object(),
    )

    assert isinstance(
        selected,
        sql_canonical_store.SQLCanonicalMessengerStoreFactory,
    )


def test_configured_postgresql_factory_does_not_construct_mail_runtime(monkeypatch):
    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("Canonical mode must not construct a mail runtime")

    monkeypatch.setattr(mail_runtime, "RuntimeFactory", fail_if_constructed)
    conf = {
        "messenger_storage": _config(
            mode="postgresql_canonical",
            confirmed=True,
        )
    }

    selected = store_factory.build_configured_store_factory(conf)

    assert isinstance(
        selected,
        sql_canonical_store.SQLCanonicalMessengerStoreFactory,
    )


def test_canonical_worker_does_not_construct_mail_runtime(monkeypatch):
    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("Canonical worker must not construct a mail runtime")

    monkeypatch.setattr(agents.mail_runtime, "RuntimeFactory", fail_if_constructed)

    worker = agents.MessengerWorkerAgent(
        runtime_factory=None,
        storage_mode="postgresql_canonical",
    )

    assert worker._runtime_factory is None


def test_all_messenger_entrypoints_use_the_configured_store_factory():
    for relative_path in (
        "workspace/cmd/messenger_api.py",
        "workspace/cmd/workspace_api.py",
        "workspace/cmd/messenger_events.py",
        "workspace/cmd/messenger_worker.py",
    ):
        source = pathlib.Path(relative_path).read_text()
        assert "store_factory.build_configured_store_factory" in source
        assert "messenger_storage_opts.register_opts" in source


@pytest.mark.parametrize(
    "entrypoint",
    (
        "workspace.cmd.messenger_api",
        "workspace.cmd.workspace_api",
        "workspace.cmd.messenger_events",
        "workspace.cmd.messenger_worker",
    ),
)
def test_canonical_entrypoint_import_and_factory_are_mail_free(entrypoint):
    script = """
import importlib
import importlib.abc
import sys

class BlockMessengerMail(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "workspace.messenger_mail" or fullname.startswith(
            "workspace.messenger_mail."
        ):
            raise RuntimeError(f"forbidden canonical import: {fullname}")
        return None

sys.meta_path.insert(0, BlockMessengerMail())
importlib.import_module(sys.argv[1])
factory_module = importlib.import_module(
    "workspace.messenger_api.api.store_factory"
)

class Storage:
    mode = "postgresql_canonical"
    canonical_cutover_confirmed = True

factory = factory_module.build_configured_store_factory(
    {"messenger_storage": Storage()}
)
assert factory.__class__.__name__ == "SQLCanonicalMessengerStoreFactory"
assert not any(
    name == "workspace.messenger_mail"
    or name.startswith("workspace.messenger_mail.")
    for name in sys.modules
)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, entrypoint],
        cwd=pathlib.Path(__file__).parents[3],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_postgresql_import_target_import_is_mail_free():
    script = """
import importlib
import importlib.abc
import sys

class BlockMessengerMail(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "workspace.messenger_mail" or fullname.startswith(
            "workspace.messenger_mail."
        ):
            raise RuntimeError(f"forbidden importer import: {fullname}")
        return None

sys.meta_path.insert(0, BlockMessengerMail())
importlib.import_module("workspace.messenger_migration.postgres_target")
assert not any(
    name == "workspace.messenger_mail"
    or name.startswith("workspace.messenger_mail.")
    for name in sys.modules
)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=pathlib.Path(__file__).parents[3],
        capture_output=True,
        text=True,
        check=False,
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


def test_postgresql_projection_move_does_not_touch_mail_runtime():
    factory = sql_canonical_store.SQLCanonicalMessengerStoreFactory()

    assert factory.move_stream_projection(stream_uuid="stream") is None


def test_transitional_factory_keeps_mail_projection_move_behind_adapter():
    chat_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    old_project_uuid = sys_uuid.uuid4()
    new_project_uuid = sys_uuid.uuid4()
    appended = {old_project_uuid: [], new_project_uuid: []}

    class Repository:
        def __init__(self, project_uuid, projection):
            self.project_uuid = project_uuid
            self.projection = projection

        def append_operation(self, operation):
            appended[self.project_uuid].append(operation)

    old_projection = types.SimpleNamespace(
        streams={
            stream_uuid: {
                "uuid": str(stream_uuid),
                "name": "External stream",
            }
        },
        bindings={},
        topics={},
        files={},
        messages={},
        reactions={},
        message_states={},
    )
    new_projection = types.SimpleNamespace(
        streams={},
        bindings={},
        topics={},
        files={},
        messages={},
        reactions={},
        message_states={},
    )
    stores = {
        old_project_uuid: types.SimpleNamespace(
            mail_service=types.SimpleNamespace(
                repository=Repository(old_project_uuid, old_projection)
            )
        ),
        new_project_uuid: types.SimpleNamespace(
            mail_service=types.SimpleNamespace(
                repository=Repository(new_project_uuid, new_projection)
            )
        ),
    }

    class Factory(sql_store.SQLProjectedMessengerStoreFactory):
        def __init__(self):
            pass

        @contextlib.contextmanager
        def __call__(self, project_uuid, user_uuid):
            assert user_uuid == owner_uuid
            yield stores[project_uuid]

    Factory().move_stream_projection(
        chat_uuid=chat_uuid,
        revision=2,
        owner_uuid=owner_uuid,
        stream_uuid=stream_uuid,
        old_project_uuid=old_project_uuid,
        new_project_uuid=new_project_uuid,
    )

    assert [item.operation for item in appended[new_project_uuid]] == ["stream.create"]
    assert [item.operation for item in appended[old_project_uuid]] == ["stream.delete"]
