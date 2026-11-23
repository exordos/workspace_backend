# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import urllib.parse

import psycopg.conninfo
import pytest

from workspace.common import database


@pytest.mark.parametrize(
    "connection_url",
    (
        "postgresql:///example",
        "postgresql:///example?host=/var/run/postgresql",
        "postgres:///example?host=/var/run/postgresql",
        "postgresql://example@/example?host=/var/run/postgresql",
        "postgresql://example@localhost:5432/example",
        "postgresql://example@[::1]:5432/example",
    ),
)
def test_connection_url_preserves_libpq_connection_parameters(connection_url):
    expected = psycopg.conninfo.conninfo_to_dict(connection_url)
    expected.update(application_name="workspace-tests", options="-c timezone=UTC")

    result = database.connection_url_for_process(connection_url, "workspace-tests")

    assert psycopg.conninfo.conninfo_to_dict(result) == expected


def test_connection_url_preserves_existing_query_options():
    query = {
        "host": "/var/run/postgresql",
        "connect_timeout": "5",
        "sslmode": "disable",
        "options": "-c work_mem=32MB -c timezone=Europe/Moscow",
        "application_name": "old",
    }
    connection_url = "postgresql:///example?" + urllib.parse.urlencode(
        query, quote_via=urllib.parse.quote
    )
    expected = psycopg.conninfo.conninfo_to_dict(connection_url)
    expected["application_name"] = "workspace-tests"
    expected["options"] += " -c timezone=UTC"

    result = database.connection_url_for_process(connection_url, "workspace-tests")

    assert psycopg.conninfo.conninfo_to_dict(result) == expected
