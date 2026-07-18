# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Explicit canonical Messenger storage cutover settings."""

from oslo_config import cfg


DOMAIN = "messenger_storage"
MAIL_PROJECTION = "mail_projection"
POSTGRESQL_CANONICAL = "postgresql_canonical"

messenger_storage_opts = [
    cfg.StrOpt(
        "mode",
        default=MAIL_PROJECTION,
        choices=(MAIL_PROJECTION, POSTGRESQL_CANONICAL),
        help=(
            "Canonical Messenger persistence mode. Keep mail_projection until "
            "the Maildir migration and parity checks have completed."
        ),
    ),
    cfg.BoolOpt(
        "canonical-cutover-confirmed",
        default=False,
        help=(
            "Operator confirmation that the Maildir-to-PostgreSQL migration, "
            "parity checks, backup, and rollback boundary are complete."
        ),
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(messenger_storage_opts, DOMAIN)
