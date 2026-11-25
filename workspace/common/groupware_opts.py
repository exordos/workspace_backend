# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from oslo_config import cfg


DOMAIN = "groupware"
OPTS = [
    cfg.StrOpt("secret-encryption-key", secret=True),
    cfg.IntOpt("sync-interval-seconds", default=300, min=10),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(OPTS, DOMAIN)
