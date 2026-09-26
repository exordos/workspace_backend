# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Construct the configured PostgreSQL-canonical Messenger store."""

from workspace.messenger_api.api import v2_store
from workspace.messenger_api.api import v3_store


def build_store_factory(
    backend: str = "v2",
) -> v2_store.MessengerV2StoreFactory | v3_store.MessengerV3StoreFactory:
    if backend == "v2":
        return v2_store.MessengerV2StoreFactory()
    if backend == "v3":
        return v3_store.MessengerV3StoreFactory()
    raise ValueError(f"Unsupported Messenger store backend {backend}")


def build_v3_store_factory() -> v3_store.MessengerV3StoreFactory:
    """Build the clean v3 store without changing the deployed cutover."""
    return v3_store.MessengerV3StoreFactory()
