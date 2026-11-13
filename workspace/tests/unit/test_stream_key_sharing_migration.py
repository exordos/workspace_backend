# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import pathlib
from unittest import mock

import pytest


def test_delegated_grants_stop_rollback_before_recreating_the_view():
    path = pathlib.Path(__file__).resolve().parents[3] / (
        "migrations/0305-Add-stream-key-sharing-policy-58fcbc.py"
    )
    spec = importlib.util.spec_from_file_location("key_sharing_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    session = mock.Mock()
    with (
        mock.patch.object(migration, "_view"),
        mock.patch.object(migration, "_grants", return_value=[{"owner_grant": False}]),
    ):
        with pytest.raises(ValueError, match="delegated view grants"):
            migration.migration_step.downgrade(session)
    session.execute.assert_not_called()
