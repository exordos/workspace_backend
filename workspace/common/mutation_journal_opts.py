# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "messenger_mutations"
OPTIONS = [
    cfg.BoolOpt(
        "enabled", default=False, help="Enable the experimental mutation journal"
    ),
    cfg.IntOpt("threads", default=4, min=1, max=32),
    cfg.IntOpt("batch_size", default=32, min=1, max=256),
    cfg.IntOpt("max_pending_per_user", default=4096, min=1, max=100000),
    cfg.IntOpt("receipt_retention_seconds", default=86400, min=1),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(OPTIONS, DOMAIN)


def enabled() -> bool:
    return DOMAIN in cfg.CONF and cfg.CONF[DOMAIN].enabled
