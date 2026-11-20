# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Process-specific PostgreSQL connection configuration."""

import urllib.parse

from oslo_config import cfg
from restalchemy.storage.sql import engines


def connection_url_for_process(connection_url: str, name: str) -> str:
    """Use UTC for timestamp storage and identify the service connection."""
    base_url, _, query_string = connection_url.partition("?")
    query = dict(urllib.parse.parse_qsl(query_string, keep_blank_values=True))
    query["application_name"] = name
    query["options"] = f"{query.get('options', '')} -c timezone=UTC".strip()
    return base_url + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def configure_postgresql(
    application_name: str,
    conf: cfg.ConfigOpts = cfg.CONF,
) -> None:
    connection_url = connection_url_for_process(
        conf["db"].connection_url,
        application_name,
    )
    conf.set_override("connection_url", connection_url, group="db")
    engines.engine_factory.configure_postgresql_factory(conf=conf)
