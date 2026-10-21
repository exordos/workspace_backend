# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime

from restalchemy.dm import types


class UTCDateTimeWithOffset(types.UTCDateTimeZ):
    """Keep UTC explicit when PostgreSQL stores a timestamp with time zone."""

    def to_simple_type(self, value: datetime.datetime) -> str:
        return value.isoformat()
