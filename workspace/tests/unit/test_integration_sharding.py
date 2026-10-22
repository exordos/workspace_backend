# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import collections
import configparser
import pathlib
import shlex
import types
from unittest import mock

import pytest

from workspace.tests.integration import conftest as integration

pytest_plugins = ["pytester"]


@pytest.fixture
def suite(pytester):
    directory = pytester.mkdir("integration")
    (directory / "conftest.py").write_text(
        """
from workspace.tests.integration import conftest as integration

def pytest_addoption(parser):
    integration.pytest_addoption(parser)

def pytest_configure(config):
    integration.pytest_configure(config)

def pytest_collection_modifyitems(config, items):
    integration.pytest_collection_modifyitems(config, items)
"""
    )
    source = pytester.makepyfile(
        """
import pytest

@pytest.mark.parametrize('value', range(20))
def test_scenario(value):
    assert value >= 0

def test_new_scenario():
    pass
"""
    )
    source.rename(directory / "test_scenarios.py")
    return pytester


def test_shards_run_every_case_exactly_once():
    items = [
        types.SimpleNamespace(nodeid=f"test_api.py::test_case[{i}]") for i in range(20)
    ]
    items.append(types.SimpleNamespace(nodeid="new/test_api.py::test_new_case"))
    combined = collections.Counter()
    for index in range(4):
        config = mock.Mock()
        config.getoption.return_value = (index, 4)
        selected = list(items)
        integration.pytest_collection_modifyitems(config, selected)
        assert selected == [item for item in items if item in selected]
        deselected = config.hook.pytest_deselected.call_args.kwargs["items"]
        assert len(selected) + len(deselected) == len(items)
        assert not any(item in selected for item in deselected)
        reordered = list(reversed(items))
        integration.pytest_collection_modifyitems(config, reordered)
        assert reordered == list(reversed(selected))
        combined.update(item.nodeid for item in selected)
    assert combined == collections.Counter(item.nodeid for item in items)

    config.getoption.return_value = None
    complete = list(items)
    integration.pytest_collection_modifyitems(config, complete)
    assert complete == items


def test_tox_shard_command_runs_the_complete_nested_suite(suite):
    config = configparser.ConfigParser(interpolation=None)
    config.read(pathlib.Path(__file__).resolve().parents[3] / "tox.ini")
    command = next(
        line.removeprefix("nocoverage: ")
        for line in config["testenv"]["commands"].splitlines()
        if line.startswith("nocoverage:")
    )
    command = command.replace("{env:TEST_PATH}", "integration").replace(
        "{posargs}", "--integration-shard 0 1"
    )
    suite.runpytest_subprocess(*shlex.split(command)[3:]).assert_outcomes(passed=21)


@pytest.mark.parametrize("index,count", [(-1, 4), (4, 4), (0, 0), (0, -1)])
def test_invalid_shard_fails_before_running_tests(index, count):
    config = mock.Mock()
    config.getoption.return_value = (index, count)
    with pytest.raises(pytest.UsageError, match="0 <= INDEX < COUNT"):
        integration.pytest_configure(config)


def test_shards_keep_module_scoped_database_fixtures_together():
    nodes = [
        "test_migrations.py::test_upgrade",
        "test_migrations.py::test_downgrade",
        "test_migrations.py::test_repair[partial]",
        "test_migrations.py::test_repair[complete]",
    ]
    locations = {}
    for index in range(4):
        config = mock.Mock()
        config.getoption.return_value = (index, 4)
        items = [types.SimpleNamespace(nodeid=node) for node in nodes]
        integration.pytest_collection_modifyitems(config, items)
        locations.update((item.nodeid, index) for item in items)
    assert set(locations) == set(nodes)
    assert len(set(locations.values())) == 1
