# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from unittest import mock

import pytest

from workspace.bridges.zulip.dm import models


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("whitespace", [False, True])
def test_unknown_send_defers_candidates_without_fencing_the_senders_history(
    direct, whitespace
):
    account = mock.Mock(project_id="project")
    account.get_host.return_value = "zulip.example.com"
    pending = {
        "external_account_id": "7",
        "recipient": {"to": [8, 7] if direct else 42},
        "topic_name": None if direct else "General",
        "zulip_content": "\nSubmitted **text** \n"
        if whitespace
        else "Submitted **text**",
    }
    candidate = {
        "id": 1,
        "sender_id": 7,
        "content": "Submitted **text**",
        "type": "private" if direct else "stream",
        "stream_id": 42,
        "subject": "General",
        "display_recipient": [{"id": 8}, {"id": 7}],
    }
    messages = [
        candidate,
        {**candidate, "id": 2, "content": "Different historical message"},
        {**candidate, "id": 3, "sender_id": 9},
        {
            **candidate,
            "id": 4,
            "stream_id": 43,
            "display_recipient": [{"id": 7}, {"id": 9}],
        },
        {**candidate, "id": 5, "subject": "Other topic"},
        {**candidate, "id": 6},
        {
            **candidate,
            "id": 7,
            "content": "Edited after sending",
            "last_edit_timestamp": 123,
        },
        {**candidate, "id": 8, "content": "Submitted\n[message truncated]"},
        {**candidate, "id": 9, "subject": "Gen..."},
        {**candidate, "id": 10, "content": "Different\n[message truncated]"},
    ]
    session = mock.Mock()
    session.execute.return_value.fetchall.return_value = [pending]
    with mock.patch.object(models.contexts, "Context") as context:
        context.return_value.get_session.return_value = session
        blocked = models.MessageDelivery.pending_import_ids(account, messages)
    # Identical messages remain ambiguous; matching text never creates a binding.
    assert blocked == ({1, 5, 6, 7, 8, 9} if direct else {1, 6, 7, 8, 9})
    session.execute.assert_called_once_with(
        mock.ANY, (account.project_id, "zulip.example.com")
    )


def test_unknown_send_without_snapshot_retains_conservative_identity_fence():
    account = mock.Mock()
    session = mock.Mock()
    session.execute.return_value.fetchall.return_value = [
        {
            "external_account_id": "7",
            "recipient": {"to": 42},
            "topic_name": "General",
            "zulip_content": None,
        }
    ]
    with mock.patch.object(models.contexts, "Context") as context:
        context.return_value.get_session.return_value = session
        assert models.MessageDelivery.pending_import_ids(
            account,
            [
                {"id": 1, "sender_id": 7},
                {"id": 2, "sender_id": 8},
            ],
        ) == {1}
