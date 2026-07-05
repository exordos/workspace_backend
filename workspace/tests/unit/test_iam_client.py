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
