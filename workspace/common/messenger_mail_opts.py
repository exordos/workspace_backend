# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Mail transport configuration without importing the mail runtime."""

from oslo_config import cfg


DOMAIN = "messenger_mail"
SECURITY_MODES = ("plain", "starttls", "tls")

mail_opts = [
    cfg.StrOpt("smtp-host", default="127.0.0.1"),
    cfg.IntOpt("smtp-port", default=25, min=1, max=65535),
    cfg.StrOpt("smtp-security", default="plain", choices=SECURITY_MODES),
    cfg.StrOpt("smtp-username", default=None),
    cfg.StrOpt("smtp-password", default=None, secret=True),
    cfg.StrOpt("smtp-ca-file", default=None),
    cfg.FloatOpt("smtp-timeout", default=10.0, min=0.1),
    cfg.StrOpt("imap-host", default="127.0.0.1"),
    cfg.IntOpt("imap-port", default=143, min=1, max=65535),
    cfg.StrOpt("imap-security", default="plain", choices=SECURITY_MODES),
    cfg.StrOpt("imap-master-username", default="workspace-master"),
    cfg.StrOpt("imap-master-password", default=None, secret=True),
    cfg.StrOpt("imap-ca-file", default=None),
    cfg.FloatOpt("imap-timeout", default=10.0, min=0.1),
    cfg.StrOpt("technical-domain", default="messenger.workspace.internal"),
    cfg.StrOpt("state-mailbox", default="Workspace/State"),
    cfg.StrOpt("event-mailbox-prefix", default="Workspace/Events"),
    cfg.StrOpt("message-mailbox", default="INBOX"),
    cfg.StrOpt(
        "external-bridge-outbox-target",
        default="zulip-bridge-producer@bridge.workspace.invalid",
    ),
    cfg.StrOpt(
        "external-bridge-outbox-prefix",
        default="Workspace/Bridge/Zulip/V1/Accounts",
    ),
    cfg.StrOpt(
        "external-bridge-ingress-target",
        default="zulip-bridge-ingress@messenger.workspace.invalid",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(mail_opts, DOMAIN)
