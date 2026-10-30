# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Start the independent SQL/asyncio Zulip history reconciler."""

import argparse
import asyncio
import configparser
import logging

from oslo_config import cfg

from workspace.common import file_storage_opts
from workspace.services import zulip_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", action="append", required=True)
    args = parser.parse_args()
    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config_file)
    logging.basicConfig(level=logging.INFO)
    file_storage_opts.register_opts()
    for section in (file_storage_opts.DOMAIN, file_storage_opts.S3_DOMAIN):
        if config.has_section(section):
            for name, value in config.items(section):
                cfg.CONF.set_override(name.replace("-", "_"), value, group=section)
    options = (
        dict(config.items("zulip_history"))
        if config.has_section("zulip_history")
        else {}
    )
    service = zulip_history.ZulipHistoryService(
        config["db"]["connection_url"],
        connections=int(options.get("connections", 4)),
        concurrency=int(options.get("concurrency", 4)),
        reaction_user_limit=config.getint(
            "messenger_reactions", "user_list_limit", fallback=5
        ),
        batch_size=int(options.get("batch_size", 1000)),
        publication_limit=int(options.get("publication_limit", 256)),
        realtime_delay=float(options.get("realtime_delay", 0.5)),
        poll_interval=float(options.get("poll_interval", 1)),
    )
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
