# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Construct the Messenger v2 PostgreSQL-canonical store."""

from workspace.messenger_api.api import v2_store


def build_store_factory() -> v2_store.MessengerV2StoreFactory:
    return v2_store.MessengerV2StoreFactory()
