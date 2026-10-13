# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import typing

from workspace.messenger_api import exceptions as messenger_exceptions


def require_iam_permission(context: typing.Any, permission: str) -> None:
    """Require one exact permission before parsing a mutating action body."""
    permissions = context.iam_context.get_introspection_info().permissions
    if permission not in permissions:
        raise messenger_exceptions.ExternalResourceForbiddenError()
