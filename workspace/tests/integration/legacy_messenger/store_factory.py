# Frozen from 724bb466b6d7fab96470c0b4c0131c302e90392e for historical migration tests.
# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Construct the Messenger v2 PostgreSQL-canonical store."""

from workspace.tests.integration.legacy_messenger import v2_store


def build_store_factory() -> v2_store.MessengerV2StoreFactory:
    return v2_store.MessengerV2StoreFactory()
