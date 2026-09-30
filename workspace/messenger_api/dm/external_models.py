# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class ExternalAccount(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_external_accounts_v2"

    owner_user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    provider = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    settings = properties.property(types.Dict(), required=True)
    revision = properties.property(
        types.Integer(min_value=1),
        default=1,
        read_only=True,
    )
