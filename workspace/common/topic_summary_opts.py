# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "topic_summary"

topic_summary_opts = [
    cfg.StrOpt(
        "secret-encryption-key",
        secret=True,
        help="Server-side key material for encrypted LLM endpoint credentials",
    ),
    cfg.IntOpt(
        "request-timeout-seconds",
        default=30,
        min=1,
        max=300,
        help="OpenAI-compatible chat-completions request timeout",
    ),
    cfg.IntOpt(
        "topic-claim-seconds",
        default=120,
        min=30,
        max=3600,
        help="Lease duration for one topic summary job",
    ),
    cfg.IntOpt(
        "endpoint-claim-seconds",
        default=120,
        min=30,
        max=3600,
        help="Lease duration for one global LLM endpoint",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(topic_summary_opts, DOMAIN)
