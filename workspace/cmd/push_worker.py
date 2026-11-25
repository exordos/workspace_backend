# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""Independent push preparation and SDK delivery entry points."""

import logging
import sys
import typing

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts

from workspace.common import config, database, mobile_push_opts
from workspace.common import log as infra_log
from workspace.messenger_api import push_events
from workspace.services import push_notifications

ra_config_opts.register_posgresql_db_opts(cfg.CONF)


def _run(service_class: typing.Any, application_name: str) -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    if not mobile_push_opts.enabled():
        logging.getLogger(__name__).info("Mobile push is not configured")
        return
    database.configure_postgresql(application_name)
    service_class(iter_min_period=0.1, iter_pause=0).start()


def handler_main() -> None:
    _run(push_notifications.TopicPushHandler, "workspace-push-handler")


def sender_main() -> None:
    _run(push_events.WorkspacePushSender, "workspace-push-sender")
