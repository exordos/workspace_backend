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

from workspace.common.clients import zulip


class FakeZulipSdkClient:
    init_values = None

    def __init__(self, email, api_key, site):
        self.init_values = {
            "email": email,
            "api_key": api_key,
            "site": site,
        }
        type(self).init_values = self.init_values

    def get_profile(self):
        return {"result": "success", "user_id": 42}

    def get_members(self):
        return {
            "result": "success",
            "members": [
                {
                    "user_id": 42,
                    "email": "user@example.com",
                    "delivery_email": "user@example.com",
                    "full_name": "User Example",
                },
            ],
        }


def test_zulip_client_uses_official_sdk_for_profile():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    data = client.get_current_user_with_api_key(
        login="user@example.com",
        token="zulip-token",
    )

    assert data == {"result": "success", "user_id": 42}
    assert FakeZulipSdkClient.init_values == {
        "email": "user@example.com",
        "api_key": "zulip-token",
        "site": "https://zulip.example.com",
    }


def test_zulip_client_gets_members_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    members = client.get_users_with_api_key(
        login="user@example.com",
        token="zulip-token",
    )

    assert members == [
        {
            "user_id": 42,
            "email": "user@example.com",
            "delivery_email": "user@example.com",
            "full_name": "User Example",
        },
    ]
