# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "messenger_reactions"
DEFAULT_USER_LIST_LIMIT = 4

messenger_reaction_opts = [
    cfg.IntOpt(
        "user-list-limit",
        default=DEFAULT_USER_LIST_LIMIT,
        min=0,
        help=(
            "Persist complete reaction user UUID lists only for emoji groups "
            "whose count does not exceed this value; zero disables the lists"
        ),
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(messenger_reaction_opts, DOMAIN)
