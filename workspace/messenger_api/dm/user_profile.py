# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.dm import properties
from restalchemy.dm import models as ra_models
from restalchemy.dm import types

from workspace.messenger_api.dm import models


EDITABLE_FIELDS = (
    "username",
    "first_name",
    "last_name",
    "surname",
    "phone",
    "description",
    "custom_props",
)
IAM_ONLY_FIELDS = ("surname", "phone", "description", "custom_props")


class UserProfileUpdate(ra_models.Model):
    """Validate partial profile input without defaulting omitted fields in IAM."""

    username = properties.property(
        types.String(min_length=1, max_length=128), default=None
    )
    first_name = properties.property(
        types.AllowNone(types.String(max_length=128)), default=None
    )
    last_name = properties.property(
        types.AllowNone(types.String(max_length=128)), default=None
    )
    surname = properties.property(types.String(max_length=128), default="")
    phone = properties.property(
        types.AllowNone(types.String(max_length=15)), default=None
    )
    description = properties.property(types.String(max_length=255), default="")
    custom_props = properties.property(types.AllowNone(types.Dict()), default=None)


class WorkspaceUserProfile(UserProfileUpdate, models.WorkspaceUser):
    """API view of an IAM profile and its Workspace projection; never persisted."""
