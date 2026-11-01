# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Start the independent Telegram synchronization service."""

import argparse
import asyncio
import configparser
import logging

from oslo_config import cfg

from workspace.common import file_storage_opts
from workspace.common import telegram_opts
from workspace.services import telegram


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", action="append", required=True)
    args = parser.parse_args()
    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config_file)
    logging.basicConfig(level=logging.INFO)
    # MTProto may log peer identifiers at INFO. Keep transport logs quiet.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    file_storage_opts.register_opts()
    telegram_opts.register_opts()
    for section in (
        file_storage_opts.DOMAIN,
        file_storage_opts.S3_DOMAIN,
        telegram_opts.DOMAIN,
    ):
        if config.has_section(section):
            for name, value in config.items(section):
                cfg.CONF.set_override(name.replace("-", "_"), value, group=section)
    options = cfg.CONF[telegram_opts.DOMAIN]
    service = telegram.TelegramService(
        config["db"]["connection_url"],
        options.api_id,
        options.api_hash,
        options.secret_encryption_key,
        batch_size=options.history_batch_size,
        poll_interval=options.poll_interval,
    )
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
