# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Run against an isolated Realm's real Core API and Workspace gateway."""

import os
from urllib import parse

import pytest
import requests

CORE = os.environ.get("WORKSPACE_CORE_API_URL", "").rstrip("/")
GATEWAY = os.environ.get("WORKSPACE_GATEWAY_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not CORE or not GATEWAY,
    reason="Set WORKSPACE_CORE_API_URL and WORKSPACE_GATEWAY_URL for a test Realm",
)


@pytest.fixture
def http():
    with requests.Session() as session:
        session.trust_env = False
        session.verify = os.environ.get("REQUESTS_CA_BUNDLE", True)
        session.headers["X-Forwarded-Proto"] = "https"
        yield session


def test_core_api_reaches_the_gateway_without_a_redirect(http):
    assert parse.urlsplit(CORE).scheme == "https"
    for base in (CORE, GATEWAY):
        response = http.get(
            base + "/api/core/v1/", timeout=(3, 10), allow_redirects=False
        )
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("application/json")
        response.json()


def test_gateway_preserves_the_iam_refresh_response(http):
    path = "/api/core/v1/iam/clients/default/actions/get_token/invoke"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": "cassi-diagnostic-invalid-canary",
    }
    direct = http.post(CORE + path, data=data, timeout=(3, 10), allow_redirects=False)
    proxied = http.post(
        GATEWAY + path, data=data, timeout=(3, 10), allow_redirects=False
    )
    assert direct.status_code in (400, 401)
    assert proxied.status_code == direct.status_code


def test_gateway_proxies_iam_key_discovery(http):
    path = "/api/core/v1/iam/clients/default/actions/jwks"
    for base in (CORE, GATEWAY):
        response = http.get(base + path, timeout=(3, 10), allow_redirects=False)
        assert response.status_code == 200
        assert response.json()["keys"]
