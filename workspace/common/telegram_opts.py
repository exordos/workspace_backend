# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Telegram client application and server-side credential configuration."""

from oslo_config import cfg


DOMAIN = "telegram"
OPTIONS = [
    cfg.IntOpt("api-id", default=0, min=0),
    cfg.StrOpt("api-hash", default="", secret=True),
    cfg.StrOpt("secret-encryption-key", default="", secret=True),
    cfg.FloatOpt("poll-interval", default=1.0, min=0.1),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(OPTIONS, DOMAIN)
