# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Process-specific PostgreSQL connection configuration."""

import urllib.parse

from oslo_config import cfg
from restalchemy.storage.sql import engines


def connection_url_with_application_name(connection_url: str, name: str) -> str:
    parts = urllib.parse.urlsplit(connection_url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(
            parts.query,
            keep_blank_values=True,
        )
        if key != "application_name"
    ]
    query.append(("application_name", name))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def configure_postgresql(
    application_name: str,
    conf: cfg.ConfigOpts = cfg.CONF,
) -> None:
    connection_url = connection_url_with_application_name(
        conf["db"].connection_url,
        application_name,
    )
    conf.set_override("connection_url", connection_url, group="db")
    engines.engine_factory.configure_postgresql_factory(conf=conf)
