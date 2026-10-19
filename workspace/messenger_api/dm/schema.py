# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Coordinate Messenger request transactions with schema cutovers."""

import typing


LOCK_KEY = "workspace-read-state-schema-v1"


def lock_shared(session: typing.Any) -> None:
    session.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        (LOCK_KEY,),
    )
