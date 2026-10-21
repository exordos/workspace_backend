# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from unittest import mock

from oslo_config import cfg
import pytest

from workspace.cmd import history_import


@pytest.mark.parametrize(
    "queue,workers", [("background", 4), ("background", 8), ("realtime", 1)]
)
@pytest.mark.parametrize("configured", [2, 16])
def test_database_pool_has_one_connection_per_worker_and_one_for_dispatch(
    monkeypatch, queue, workers, configured
):
    conf = cfg.ConfigOpts()
    conf.register_opt(cfg.IntOpt("connection_pool_max_size", default=configured), "db")
    monkeypatch.setattr(history_import, "CONF", conf)
    with mock.patch.object(
        history_import.database, "configure_postgresql"
    ) as configure:
        history_import._configure_database(queue, workers)

    assert conf.db.connection_pool_max_size == workers + 1
    configure.assert_called_once_with(f"workspace-zulip-{queue}-tasks", conf=conf)
