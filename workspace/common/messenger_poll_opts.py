# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from oslo_config import cfg


DOMAIN = "messenger_polls"
OPTIONS = [
    cfg.IntOpt("poll_max_questions", default=20, min=1),
    cfg.IntOpt("poll_max_options_per_question", default=20, min=2),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(OPTIONS, DOMAIN)


def max_options_per_question() -> int:
    return cfg.CONF[DOMAIN].poll_max_options_per_question
