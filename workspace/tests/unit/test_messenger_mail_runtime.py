# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib
import re
import uuid as sys_uuid

from oslo_config import cfg
import pytest

from workspace.messenger_mail import protocol
from workspace.messenger_mail import runtime
from workspace.messenger_mail import service


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")


def _conf():
    conf = cfg.ConfigOpts()
    runtime.register_opts(conf)
    conf.set_override(
        "imap_master_username",
        "master",
        group=runtime.DOMAIN,
    )
    conf.set_override(
        "imap_master_password",
        "master-secret",
        group=runtime.DOMAIN,
    )
    conf.set_override(
        "technical_domain",
        "internal.example",
        group=runtime.DOMAIN,
    )
    return conf


class FakeImapClient:
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.exited = False
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True


class FakeSmtpClient:
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.exited = False
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True


def test_mail_options_mark_credentials_secret_and_have_internal_defaults():
    options = {option.name: option for option in runtime.mail_opts}
    conf = cfg.ConfigOpts()
    runtime.register_opts(conf)

    assert options["imap-master-password"].secret is True
    assert options["smtp-password"].secret is True
    assert conf[runtime.DOMAIN].smtp_host == "127.0.0.1"
    assert conf[runtime.DOMAIN].imap_host == "127.0.0.1"
    assert conf[runtime.DOMAIN].smtp_ca_file is None
    assert conf[runtime.DOMAIN].imap_ca_file is None
    assert conf[runtime.DOMAIN].technical_domain == (service.DEFAULT_TECHNICAL_DOMAIN)


def test_user_and_project_master_logins_are_deterministic_and_context_managed(
    monkeypatch,
):
    FakeImapClient.instances = []
    monkeypatch.setattr(runtime.protocol, "ImapClient", FakeImapClient)
    factory = runtime.RuntimeFactory(_conf())

    with factory.user_imap_client(USER_UUID) as user_client:
        assert user_client is FakeImapClient.instances[0]
        assert user_client.exited is False
    user_settings = FakeImapClient.instances[0].settings
    assert user_settings.credentials.username == (
        "u-20000000000000000000000000000002@internal.example*master"
    )
    assert user_settings.credentials.password == "master-secret"
    assert FakeImapClient.instances[0].exited is True

    with factory.project_repository(PROJECT_UUID) as mail_repository:
        assert mail_repository.project_uuid == PROJECT_UUID
        assert mail_repository.imap_client is FakeImapClient.instances[1]
    project_settings = FakeImapClient.instances[1].settings
    assert project_settings.credentials.username == (
        "p-10000000000000000000000000000001@internal.example*master"
    )
    assert project_settings.credentials.password == "master-secret"
    assert FakeImapClient.instances[1].exited is True
    assert "master-secret" not in repr(user_settings)
    assert "master-secret" not in repr(project_settings.credentials)
    assert "master-secret" not in service.technical_address(USER_UUID)


def test_runtime_factory_passes_internal_ca_to_verified_mail_clients(monkeypatch):
    FakeImapClient.instances = []
    FakeSmtpClient.instances = []
    monkeypatch.setattr(runtime.protocol, "ImapClient", FakeImapClient)
    monkeypatch.setattr(runtime.protocol, "SmtpClient", FakeSmtpClient)
    conf = _conf()
    conf.set_override("imap_security", "starttls", group=runtime.DOMAIN)
    conf.set_override("imap_ca_file", "/etc/workspace/mail-ca.crt", group=runtime.DOMAIN)
    conf.set_override("smtp_security", "starttls", group=runtime.DOMAIN)
    conf.set_override("smtp_ca_file", "/etc/workspace/mail-ca.crt", group=runtime.DOMAIN)
    factory = runtime.RuntimeFactory(conf)

    with factory.user_imap_client(USER_UUID):
        pass
    with factory.smtp_client():
        pass

    assert FakeImapClient.instances[0].settings.security == "starttls"
    assert FakeImapClient.instances[0].settings.ca_file == (
        "/etc/workspace/mail-ca.crt"
    )
    assert FakeSmtpClient.instances[0].settings.security == "starttls"
    assert FakeSmtpClient.instances[0].settings.ca_file == (
        "/etc/workspace/mail-ca.crt"
    )


def test_smtp_factory_supports_internal_optional_auth_and_hides_password(
    monkeypatch,
):
    FakeSmtpClient.instances = []
    monkeypatch.setattr(runtime.protocol, "SmtpClient", FakeSmtpClient)
    conf = _conf()
    factory = runtime.RuntimeFactory(conf)

    with factory.smtp_client() as client:
        assert client.settings.credentials is None

    conf.set_override("smtp_username", "submitter", group=runtime.DOMAIN)
    conf.set_override("smtp_password", "smtp-secret", group=runtime.DOMAIN)
    with factory.smtp_client() as client:
        settings = client.settings

    assert settings.credentials == protocol.Credentials("submitter", "smtp-secret")
    assert "smtp-secret" not in repr(settings)
    assert FakeSmtpClient.instances[-1].exited is True

    conf.set_override("smtp_password", None, group=runtime.DOMAIN)
    with pytest.raises(ValueError, match="must be configured together"):
        with factory.smtp_client():
            pass


def test_runtime_factory_builds_internal_service_with_configured_mailboxes(
    monkeypatch,
):
    FakeImapClient.instances = []
    monkeypatch.setattr(runtime.protocol, "ImapClient", FakeImapClient)
    conf = _conf()
    conf.set_override("state_mailbox", "State.Journal", group=runtime.DOMAIN)
    conf.set_override("event_mailbox_prefix", "User.Events", group=runtime.DOMAIN)
    conf.set_override("message_mailbox", "Messages", group=runtime.DOMAIN)
    factory = runtime.RuntimeFactory(conf)

    with factory.messenger_service(PROJECT_UUID) as mail_service:
        assert mail_service.repository.state_mailbox == "State.Journal"
        assert mail_service.repository.event_mailbox_prefix == "User.Events"
        assert mail_service.message_mailbox == "Messages"
        assert mail_service.technical_domain == "internal.example"


def test_service_entrypoints_register_mail_options_without_runtime_wiring():
    root = pathlib.Path(__file__).parents[2]
    for relative_path in (
        "cmd/messenger_api.py",
        "cmd/messenger_events.py",
        "cmd/messenger_worker.py",
        "cmd/workspace_api.py",
    ):
        source = (root / relative_path).read_text()
        assert "from workspace.messenger_mail import runtime" in source
        alias_match = re.search(
            r"from workspace\.messenger_mail import runtime as (?P<alias>\w+)",
            source,
        )
        assert alias_match is not None
        assert f"{alias_match.group('alias')}.register_opts(CONF)" in source
