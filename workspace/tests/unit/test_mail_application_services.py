# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import types
import uuid as sys_uuid
from unittest import mock

from workspace.mail_api import application_services


def test_send_reloads_and_locks_draft_before_creating_intent(monkeypatch):
    project_id = sys_uuid.uuid4()
    owner_user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    resource = types.SimpleNamespace(
        project_id=project_id,
        owner_user_uuid=owner_user_uuid,
        uuid=message_uuid,
    )
    locked_message = types.SimpleNamespace(
        project_id=project_id,
        owner_user_uuid=owner_user_uuid,
        uuid=message_uuid,
        draft=True,
        delivery_status="draft",
        to_addresses=["recipient@example.test"],
        cc_addresses=[],
        bcc_addresses=[],
        body_text="body",
        body_html=None,
        external_account_uuid=account_uuid,
        attachment_urns=[],
        from_address="sender@example.test",
        reply_to=None,
        subject="subject",
        in_reply_to=None,
        references=[],
        update=mock.Mock(),
    )
    account = types.SimpleNamespace(uuid=account_uuid, status="active")
    session = types.SimpleNamespace(execute=mock.Mock())
    reload_message = mock.Mock(return_value=locked_message)
    monkeypatch.setattr(application_services, "_message", reload_message)
    monkeypatch.setattr(
        application_services, "_account", mock.Mock(return_value=account)
    )
    monkeypatch.setattr(application_services, "_validate_attachment_urns", mock.Mock())
    monkeypatch.setattr(application_services, "_force_values", mock.Mock())
    monkeypatch.setattr(
        application_services.events,
        "create_mail_event",
        mock.Mock(),
    )

    result = application_services.send_message(session, resource)

    assert result is locked_message
    reload_message.assert_called_once_with(
        session,
        project_id,
        owner_user_uuid,
        message_uuid,
        locked=True,
    )
    session.execute.assert_called_once()
