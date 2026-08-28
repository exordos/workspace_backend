# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import threading
import urllib.parse

from workspace.cmd import messenger_api
from workspace.common import database


def test_database_application_name_replaces_only_process_identity():
    connection_url = (
        "postgresql://user:pass@db/workspace"
        "?options=-c%20work_mem%3D32MB&application_name=old"
    )

    result = database.connection_url_with_application_name(
        connection_url,
        "workspace-messenger-api",
    )

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(result).query)
    assert query == {
        "options": ["-c work_mem=32MB"],
        "application_name": ["workspace-messenger-api"],
    }


def test_free_messenger_worker_serves_settings_while_peer_is_delayed(monkeypatch):
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
            if path == "/api/workspace/v1/server_settings":
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
        messenger_api.app,
        "build_wsgi_application",
        build_application,
    )
    monkeypatch.setattr(
        messenger_api.bjoern_service,
        "BjoernService",
        FakeService,
    )
    messenger_api.CONF.set_override("workers", 2, group=messenger_api.DOMAIN)
    try:
        services = messenger_api.build_http_services(object())
    finally:
        messenger_api.CONF.clear_override("workers", group=messenger_api.DOMAIN)

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
            "/api/workspace/v1/server_settings",
        )
        assert settings_finished.wait(timeout=1)
        assert settings.result(timeout=1) == (["200 OK"], [b"{}"])
        release_delayed.set()
        assert delayed.result(timeout=1) == (["200 OK"], [b"{}"])
