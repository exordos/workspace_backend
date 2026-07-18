# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "external_bridge_control"
DEFAULT_STORE_PATH = "/var/lib/workspace/external-bridge-control"
DEFAULT_ENROLLMENT_CONFIG_PATH = "/etc/workspace/external-bridge-enrollment.json"

external_bridge_control_opts = [
    cfg.StrOpt("bind-host", default="0.0.0.0"),
    cfg.IntOpt("bootstrap-port", default=21085, min=1, max=65535),
    cfg.IntOpt("https-port", default=21443, min=1, max=65535),
    cfg.StrOpt("realm-uuid", required=True),
    cfg.StrOpt("hostname", required=True),
    cfg.StrOpt("persistent-store-path", default=DEFAULT_STORE_PATH),
    cfg.BoolOpt(
        "require-dedicated-filesystem",
        default=True,
        help="Fail unless the persistent store is a separate mounted filesystem",
    ),
    cfg.StrOpt(
        "enrollment-config-path",
        default=DEFAULT_ENROLLMENT_CONFIG_PATH,
        help="Core-managed one-time bridge enrollment configuration",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(external_bridge_control_opts, DOMAIN)
