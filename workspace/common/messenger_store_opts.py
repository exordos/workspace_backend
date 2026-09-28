# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "messenger_store"

store_opts = [
    cfg.StrOpt(
        "backend",
        default="v2",
        choices=("v2", "v3"),
        help="Canonical Messenger PostgreSQL schema used by API and events",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(store_opts, DOMAIN)
