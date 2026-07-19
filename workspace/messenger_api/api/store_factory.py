# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Construct the PostgreSQL-canonical Messenger store."""

from workspace.messenger_api.api import sql_canonical_store


def build_store_factory() -> sql_canonical_store.SQLCanonicalMessengerStoreFactory:
    return sql_canonical_store.SQLCanonicalMessengerStoreFactory()
