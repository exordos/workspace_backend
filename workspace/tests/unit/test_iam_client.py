#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from workspace.common.clients import iam

from unittest import mock
from bazooka import exceptions as bz_exc
import pytest
import requests


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeBazookaClient:
    calls = []

    def __init__(self, default_timeout):
        self.default_timeout = default_timeout

    def get(self, url, headers):
        type(self).calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout": self.default_timeout,
            },
        )
        return FakeResponse(
            [
                {
                    "uuid": "00000000-0000-0000-0000-000000000000",
                    "username": "admin",
                },
            ],
        )


def test_iam_client_gets_users_from_iam_root():
    FakeBazookaClient.calls = []
    client = iam.IamClient(
        endpoint="https://exordos.com/api/core/v1/iam/clients/default",
        client_cls=FakeBazookaClient,
    )

    users = client.get_users(token="111")

    assert users == [
        {
            "uuid": "00000000-0000-0000-0000-000000000000",
            "username": "admin",
        },
    ]
    assert FakeBazookaClient.calls == [
        {
            "url": "https://exordos.com/api/core/v1/iam/users/",
            "headers": {"Authorization": "Bearer 111"},
            "timeout": 5,
        },
    ]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://iam.example.invalid/v1/iam/clients/default",
        "https://iam.example.invalid/v1/iam/",
    ],
)
def test_profile_client_uses_authenticated_user_resource(endpoint):
    http = mock.Mock()
    http.get.return_value.json.return_value = {"username": "cassi"}
    http.put.return_value.json.return_value = {"username": "updated"}
    client = iam.IamClient(endpoint, client_cls=lambda **kwargs: http)
    assert client.get_user("user-id", "test-token") == {"username": "cassi"}
    assert client.update_user("user-id", "test-token", {"first_name": None}) == {
        "username": "updated"
    }
    http.get.assert_called_once_with(
        "https://iam.example.invalid/v1/iam/users/user-id",
        headers={"Authorization": "Bearer test-token"},
    )
    http.put.assert_called_once_with(
        "https://iam.example.invalid/v1/iam/users/user-id",
        headers={"Authorization": "Bearer test-token"},
        json={"first_name": None},
    )


@pytest.mark.parametrize(
    "status, expected",
    [
        (400, 400),
        (401, 401),
        (403, 403),
        (404, 404),
        (409, 409),
        (422, 422),
        (429, 429),
        (500, 502),
    ],
)
def test_profile_client_sanitizes_iam_errors(status, expected):
    response = requests.Response()
    response.status_code = status
    error = bz_exc.BaseHTTPException(
        requests.exceptions.HTTPError("upstream-private-data", response=response)
    )
    http = mock.Mock()
    http.put.side_effect = error
    client = iam.IamClient(
        "https://iam.example.invalid/v1/iam/", client_cls=lambda **kwargs: http
    )
    with pytest.raises(iam.IamProfileRequestError) as raised:
        client.update_user("user-id", "test-token", {"username": "cassi"})
    assert raised.value.code == expected
    assert "upstream-private-data" not in str(raised.value)


@pytest.mark.parametrize(
    "error", [requests.exceptions.Timeout(), ValueError("Invalid JSON")]
)
def test_profile_client_maps_unavailable_or_invalid_iam_response(error):
    http = mock.Mock()
    http.get.side_effect = error
    client = iam.IamClient(
        "https://iam.example.invalid/v1/iam/", client_cls=lambda **kwargs: http
    )
    with pytest.raises(iam.IamProfileRequestError) as raised:
        client.get_user("user-id", "test-token")
    assert raised.value.code == 502
