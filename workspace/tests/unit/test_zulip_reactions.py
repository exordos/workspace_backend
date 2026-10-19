# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from unittest import mock

import pytest

from workspace.bridges.zulip import client
from workspace.common import emoji


@pytest.mark.parametrize("present,method", [(True, "POST"), (False, "DELETE")])
def test_reaction_requests_preserve_the_exact_remote_emoji_identity(present, method):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "reader@example.com",
            "api_key": "reader-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {"result": "success"}
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            zulip.update_reaction(42, "old_name", "1f44d", "unicode_emoji", present)
        finally:
            zulip.close()
    assert session.auth == ("reader@example.com", "reader-key")
    session.request.assert_called_once_with(
        method,
        "https://zulip.example.com/api/v1/messages/42/reactions",
        timeout=(5, 30),
        data={
            "emoji_name": "old_name",
            "emoji_code": "1f44d",
            "reaction_type": "unicode_emoji",
        },
    )


def test_unicode_aliases_preserve_workspace_canonical_shortcodes():
    assert emoji.normalize_name("+1") == emoji.normalize_name("like") == "thumbs_up"
    assert emoji.name_for_code("1f44d", "+1") == "thumbs_up"
    assert emoji.CODE_BY_NAME["thumbs_up"] == "1f44d"
    assert emoji.normalize_name("working_on_it") == "working_on_it"
    assert emoji.normalize_name("custom_emoji") == "custom_emoji"
    assert emoji.CODE_BY_NAME["party"] == "1f389"
    assert emoji.ZULIP_NAME_BY_CODE["1f389"] == "tada"
