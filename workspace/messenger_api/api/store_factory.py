# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Select a Messenger store only through the explicit migration cutover flag."""

import typing

from workspace.common import messenger_storage_opts
from workspace.messenger_api.api import sql_canonical_store


class CanonicalCutoverNotConfirmed(RuntimeError):
    pass


class _StorageConfig(typing.Protocol):
    mode: str
    canonical_cutover_confirmed: bool


def select_store_factory(
    storage_config: _StorageConfig,
    mail_projection_factory: typing.Any,
) -> typing.Any:
    """Return the configured store while preserving mail as the safe default."""
    if storage_config.mode == messenger_storage_opts.MAIL_PROJECTION:
        return mail_projection_factory
    if storage_config.mode != messenger_storage_opts.POSTGRESQL_CANONICAL:
        raise ValueError(f"Unsupported Messenger storage mode {storage_config.mode}")
    if not storage_config.canonical_cutover_confirmed:
        raise CanonicalCutoverNotConfirmed(
            "PostgreSQL canonical mode requires an explicit, verified cutover"
        )
    return sql_canonical_store.SQLCanonicalMessengerStoreFactory()


def build_configured_store_factory(
    conf: typing.Mapping[str, _StorageConfig],
    bridge_config: typing.Any = None,
) -> typing.Any:
    """Construct mail dependencies only while the safe pre-cutover mode is active."""
    storage_config = conf[messenger_storage_opts.DOMAIN]
    if storage_config.mode == messenger_storage_opts.POSTGRESQL_CANONICAL:
        return select_store_factory(storage_config, None)
    from workspace.messenger_api.api import sql_store
    from workspace.messenger_mail import runtime as mail_runtime

    return select_store_factory(
        storage_config,
        sql_store.SQLProjectedMessengerStoreFactory(
            mail_runtime.RuntimeFactory(conf),
            bridge_config=bridge_config,
        ),
    )
