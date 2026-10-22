# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from unittest import mock
import uuid

from workspace.tests.integration import conftest


def test_api_client_seeds_each_identity_once_without_rewriting_reads(monkeypatch):
    request = mock.Mock()
    monkeypatch.setattr(conftest.requests, "request", request)
    seed = mock.Mock()
    owner, peer = uuid.uuid4(), uuid.uuid4()
    client = conftest.ApiClient("http://localhost", owner, uuid.uuid4(), seed)

    client.get("/v1/users/")
    client.get("/v1/messenger/users/")
    client.get("/v1/users/", user=peer)
    client.get("/v1/messenger/users/", user=peer)

    assert seed.call_args_list == [mock.call(str(owner)), mock.call(str(peer))]
    assert request.call_count == 4
