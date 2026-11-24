# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
from oslo_config import cfg

DOMAIN = "mobile_push"
OPTS = [
    cfg.StrOpt("organization-uuid", help="Stable UUID of this Workspace installation"),
    cfg.StrOpt(
        "notification-endpoint",
        help="Local Notification base URL; unset disables pushes",
    ),
    cfg.StrOpt(
        "notification-token",
        default="",
        secret=True,
        help="Platform-managed bearer token used by the push sender",
    ),
    cfg.BoolOpt(
        "apns-enabled",
        default=False,
        help="Enable native Workspace push to iOS devices",
    ),
    cfg.StrOpt(
        "apns-notification-token",
        default="",
        secret=True,
        help="Platform-managed bearer token scoped to Workspace APNs events",
    ),
]
cfg.CONF.register_opts(OPTS, DOMAIN)


def enabled() -> bool:
    options = cfg.CONF[DOMAIN]
    return bool(options.organization_uuid and options.notification_endpoint)


def apns_enabled() -> bool:
    return enabled() and cfg.CONF[DOMAIN].apns_enabled
