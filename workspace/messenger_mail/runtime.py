# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import typing
import uuid as sys_uuid

from oslo_config import cfg

from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository
from workspace.messenger_mail import service


DOMAIN = "messenger_mail"

mail_opts = [
    cfg.StrOpt("smtp-host", default="127.0.0.1"),
    cfg.IntOpt("smtp-port", default=25, min=1, max=65535),
    cfg.StrOpt(
        "smtp-security",
        default="plain",
        choices=tuple(sorted(protocol.SECURITY_MODES)),
    ),
    cfg.StrOpt("smtp-username", default=None),
    cfg.StrOpt("smtp-password", default=None, secret=True),
    cfg.StrOpt("smtp-ca-file", default=None),
    cfg.FloatOpt("smtp-timeout", default=10.0, min=0.1),
    cfg.StrOpt("imap-host", default="127.0.0.1"),
    cfg.IntOpt("imap-port", default=143, min=1, max=65535),
    cfg.StrOpt(
        "imap-security",
        default="plain",
        choices=tuple(sorted(protocol.SECURITY_MODES)),
    ),
    cfg.StrOpt("imap-master-username", default="workspace-master"),
    cfg.StrOpt("imap-master-password", default=None, secret=True),
    cfg.StrOpt("imap-ca-file", default=None),
    cfg.FloatOpt("imap-timeout", default=10.0, min=0.1),
    cfg.StrOpt(
        "technical-domain",
        default=service.DEFAULT_TECHNICAL_DOMAIN,
    ),
    cfg.StrOpt("state-mailbox", default=repository.STATE_MAILBOX),
    cfg.StrOpt(
        "event-mailbox-prefix",
        default=repository.EVENT_MAILBOX_PREFIX,
    ),
    cfg.StrOpt("message-mailbox", default=service.MESSAGE_MAILBOX),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(mail_opts, DOMAIN)


def project_service_address(
    project_uuid: sys_uuid.UUID,
    domain: str,
) -> str:
    return f"p-{project_uuid.hex}@{domain}"


class RuntimeFactory:
    def __init__(self, conf: cfg.ConfigOpts = cfg.CONF) -> None:
        self.conf = conf

    def _master_credentials(self, target: str) -> protocol.Credentials:
        group = self.conf[DOMAIN]
        password = group.imap_master_password
        if password is None:
            raise ValueError("IMAP master password is required")
        return protocol.Credentials(
            f"{target}*{group.imap_master_username}",
            password,
        )

    def _imap_settings(self, target: str) -> protocol.ImapSettings:
        group = self.conf[DOMAIN]
        return protocol.ImapSettings(
            host=group.imap_host,
            port=group.imap_port,
            credentials=self._master_credentials(target),
            security=group.imap_security,
            ca_file=group.imap_ca_file,
            timeout=group.imap_timeout,
        )

    def _smtp_settings(self) -> protocol.SmtpSettings:
        group = self.conf[DOMAIN]
        credentials = None
        if group.smtp_username is not None or group.smtp_password is not None:
            if group.smtp_username is None or group.smtp_password is None:
                raise ValueError(
                    "SMTP username and password must be configured together"
                )
            credentials = protocol.Credentials(
                group.smtp_username,
                group.smtp_password,
            )
        return protocol.SmtpSettings(
            host=group.smtp_host,
            port=group.smtp_port,
            security=group.smtp_security,
            credentials=credentials,
            ca_file=group.smtp_ca_file,
            timeout=group.smtp_timeout,
        )

    @contextlib.contextmanager
    def user_imap_client(
        self,
        user_uuid: sys_uuid.UUID,
    ) -> typing.Iterator[protocol.ImapClient]:
        group = self.conf[DOMAIN]
        target = service.technical_address(user_uuid, group.technical_domain)
        with protocol.ImapClient(self._imap_settings(target)) as client:
            yield client

    @contextlib.contextmanager
    def project_repository(
        self,
        project_uuid: sys_uuid.UUID,
    ) -> typing.Iterator[repository.MessengerMailRepository]:
        group = self.conf[DOMAIN]
        target = project_service_address(project_uuid, group.technical_domain)
        with protocol.ImapClient(self._imap_settings(target)) as client:
            yield repository.MessengerMailRepository(
                client,
                project_uuid,
                state_mailbox=group.state_mailbox,
                event_mailbox_prefix=group.event_mailbox_prefix,
            )

    @contextlib.contextmanager
    def smtp_client(self) -> typing.Iterator[protocol.SmtpClient]:
        with protocol.SmtpClient(self._smtp_settings()) as client:
            yield client

    @contextlib.contextmanager
    def messenger_service(
        self,
        project_uuid: sys_uuid.UUID,
    ) -> typing.Iterator[service.MessengerMailService]:
        group = self.conf[DOMAIN]
        with self.project_repository(project_uuid) as mail_repository:
            yield service.MessengerMailService(
                mail_repository,
                self.smtp_client,
                self.user_imap_client,
                technical_domain=group.technical_domain,
                message_mailbox=group.message_mailbox,
            )
