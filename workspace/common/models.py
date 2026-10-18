# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types


DESCRIPTION_MAX_LENGTH = 10000


class ModelWithNameDesc(models.ModelWithNameDesc):
    description = properties.property(
        types.String(max_length=DESCRIPTION_MAX_LENGTH), default=""
    )


class ModelWithRequiredNameDesc(ModelWithNameDesc):
    name = properties.property(types.String(max_length=255), required=True)
