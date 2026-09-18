# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "workspace_v3_projection_worker"

worker_opts = [
    cfg.IntOpt(
        "workers",
        default=1,
        min=1,
        max=8,
        help="Workspace v3 projection process count",
    ),
    cfg.IntOpt(
        "batch-size",
        default=1000,
        min=1,
        max=10000,
        help="Maximum tasks claimed by one Workspace v3 transaction",
    ),
    cfg.IntOpt(
        "lease-seconds",
        default=30,
        min=5,
        max=600,
        help="Workspace v3 task lease duration",
    ),
    cfg.IntOpt(
        "max-attempts",
        default=8,
        min=1,
        max=32,
        help="Attempts before a Workspace v3 task becomes dead letter",
    ),
    cfg.IntOpt(
        "reaction-user-limit",
        default=100,
        min=1,
        max=1000,
        help="Largest complete reaction user list stored in a message",
    ),
    cfg.FloatOpt(
        "idle-sleep-seconds",
        default=0.1,
        min=0.01,
        max=3.0,
        help="Delay after an empty Workspace v3 projection poll",
    ),
    cfg.IntOpt(
        "metrics-log-interval-seconds",
        default=30,
        min=5,
        max=300,
        help="Workspace v3 projection metrics log interval",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(worker_opts, DOMAIN)
