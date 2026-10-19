# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import threading
import urllib.parse
from unittest import mock

import psycopg.conninfo
import pytest
import webob

from workspace.cmd import workspace_api
from workspace.common import database


def test_database_application_name_replaces_only_process_identity():
    connection_url = (
        "postgresql://user:pass@db/workspace"
        "?options=-c%20work_mem%3D32MB%20-c%20jit%3Doff&application_name=old"
    )

    result = database.connection_url_with_application_name(
        connection_url,
        "workspace-api",
    )

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(result).query)
    assert query == {
        "options": ["-c work_mem=32MB -c jit=off"],
        "application_name": ["workspace-api"],
    }
    assert psycopg.conninfo.conninfo_to_dict(result)["options"] == (
        "-c work_mem=32MB -c jit=off"
    )


def test_free_workspace_worker_serves_settings_while_peer_is_delayed(monkeypatch):
    delayed_started = threading.Event()
    release_delayed = threading.Event()
    settings_finished = threading.Event()
    built_apps = []

    def build_application(iam_driver):
        worker_index = len(built_apps)

        def application(environ, start_response):
            path = environ["PATH_INFO"]
            if worker_index == 0 and path == "/delayed":
                delayed_started.set()
                assert release_delayed.wait(timeout=5)
            if path == "/v1/messenger/server_settings":
                settings_finished.set()
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b"{}"]

        built_apps.append(application)
        return application

    class FakeService:
        def __init__(self, **kwargs):
            self.wsgi_app = kwargs["wsgi_app"]
            self.setups = []

        def add_setup(self, setup):
            self.setups.append(setup)

    monkeypatch.setattr(
        workspace_api.app,
        "build_wsgi_application",
        build_application,
    )
    monkeypatch.setattr(
        workspace_api.bjoern_service,
        "BjoernService",
        FakeService,
    )
    workspace_api.CONF.set_override("workers", 2, group=workspace_api.DOMAIN)
    try:
        services = workspace_api.build_http_services(object())
    finally:
        workspace_api.CONF.clear_override("workers", group=workspace_api.DOMAIN)

    def request(application, path):
        statuses = []
        result = application(
            {"PATH_INFO": path},
            lambda status, headers: statuses.append(status),
        )
        return statuses, result

    assert len(services) == 2
    assert services[0].wsgi_app is not services[1].wsgi_app
    assert all(len(service.setups) == 1 for service in services)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        delayed = executor.submit(request, services[0].wsgi_app, "/delayed")
        assert delayed_started.wait(timeout=1)
        settings = executor.submit(
            request,
            services[1].wsgi_app,
            "/v1/messenger/server_settings",
        )
        assert settings_finished.wait(timeout=1)
        assert settings.result(timeout=1) == (["200 OK"], [b"{}"])
        release_delayed.set()
        assert delayed.result(timeout=1) == (["200 OK"], [b"{}"])


@pytest.mark.parametrize(
    "path",
    (
        "/v1/messenger/server_settings",
        "/v1/messenger/server_settings/",
        "/v1/server_settings",
        "/v1/server_settings/",
    ),
)
def test_unified_app_serves_public_settings_without_iam(path):
    iam_driver = mock.Mock()
    application = workspace_api.app.build_wsgi_application(iam_driver)

    response = webob.Request.blank(path).get_response(application)

    assert response.status_code == 200
    assert response.json["result"] == "success"
    assert not iam_driver.mock_calls
