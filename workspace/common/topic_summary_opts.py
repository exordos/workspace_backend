# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "topic_summary"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_TIMEOUT_SECONDS = 25 * 60
DEFAULT_ENDPOINT_CLAIM_SECONDS = 30 * 60
DEFAULT_TOPIC_CLAIM_SECONDS = 90 * 60
CLAIM_GRACE_SECONDS = 60

topic_summary_opts = [
    cfg.StrOpt(
        "secret-encryption-key",
        secret=True,
        help="Server-side key material for encrypted LLM endpoint credentials",
    ),
    cfg.IntOpt(
        "connect-timeout-seconds",
        default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        min=1,
        max=300,
        help="OpenAI-compatible endpoint connection timeout",
    ),
    cfg.IntOpt(
        "request-timeout-seconds",
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        min=1,
        max=3600,
        help="OpenAI-compatible chat-completions response timeout",
    ),
    cfg.IntOpt(
        "topic-claim-seconds",
        default=DEFAULT_TOPIC_CLAIM_SECONDS,
        min=30,
        max=21600,
        help="Lease duration for one topic summary job",
    ),
    cfg.IntOpt(
        "endpoint-claim-seconds",
        default=DEFAULT_ENDPOINT_CLAIM_SECONDS,
        min=30,
        max=7200,
        help="Lease duration for one global LLM endpoint",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(topic_summary_opts, DOMAIN)
