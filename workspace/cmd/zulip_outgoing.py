# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Start the independent SQL/asyncio Zulip sender."""

import argparse
import asyncio
import configparser
import logging

from oslo_config import cfg

from workspace.common import file_storage_opts
from workspace.services import zulip_outgoing


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
        dict(config.items("zulip_outgoing"))
        if config.has_section("zulip_outgoing")
        else {}
    )
    service = zulip_outgoing.ZulipOutgoingService(
        config["db"]["connection_url"],
        concurrency=int(options.get("concurrency", 128)),
        connections=int(options.get("connections", 128)),
        dependency_delay=float(options.get("dependency_delay", 0.01)),
        poll_interval=float(options.get("poll_interval", 1)),
    )
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
