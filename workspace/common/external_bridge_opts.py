# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "external_bridge"

external_bridge_opts = [
    cfg.StrOpt(
        "realm-uuid",
        help="Stable realm UUID bound to external bridge records",
    ),
    cfg.StrOpt(
        "bridge-instance-uuid",
        help="Active realm-local bridge instance UUID",
    ),
    cfg.IntOpt(
        "identity-generation",
        default=1,
        min=1,
        help="Active bridge enrollment identity generation",
    ),
    cfg.StrOpt(
        "enrollment-secret",
        secret=True,
        help="Opaque Exordos-managed enrollment token used for mail key derivation",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(external_bridge_opts, DOMAIN)
