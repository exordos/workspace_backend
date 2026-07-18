#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from restalchemy.common import exceptions as ra_exc
import typing


class OnlyOneAllFolderPerUserError(ra_exc.ValidationErrorException):
    message = "Only one 'all' folder is allowed per user"
    code = 400001001


class PrivateIndexIsTechnicalFieldError(ra_exc.ValidationErrorException):
    message = "'private_index' is a technical field and cannot be provided"
    code = 400001002


class DirectStreamSelfChatError(ra_exc.ValidationErrorException):
    message = "'direct_user_uuid' must point to another user"
    code = 400001003


class InvalidStreamBindingRoleError(ra_exc.ValidationErrorException):
    message = "Invalid stream binding role '%(role)s'"
    code = 400001004


class StreamBindingUsersPayloadError(ra_exc.ValidationErrorException):
    message = "Stream binding action expects role values to be user UUID lists"
    code = 400001005


class InvalidTopicNotificationModeError(ra_exc.ValidationErrorException):
    message = (
        "Topic notification mode '%(mode)s' is not allowed for current stream "
        "notification mode"
    )
    code = 400001006


class StreamDefaultTopicNotConfiguredError(ra_exc.ValidationErrorException):
    message = "Stream default topic is not configured"
    code = 400001007


class EventsCursorExpiredError(ra_exc.RestAlchemyException):
    """The saved events cursor can no longer produce a complete delta."""

    message = "The saved events cursor is outside the retained event journal"
    code = 410

    def __init__(
        self,
        *,
        reason: str,
        epoch_generation: str,
        current_epoch_version: int,
        minimum_epoch_version: int,
    ) -> None:
        super().__init__()
        self.reason = reason
        self.epoch_generation = epoch_generation
        self.current_epoch_version = current_epoch_version
        self.minimum_epoch_version = minimum_epoch_version

    def as_dict(self) -> dict[str, typing.Any]:
        return {
            "type": self.__class__.__name__,
            "code": self.code,
            "error": "epoch_pruned",
            "message": self.msg,
            "reason": self.reason,
            "epoch_generation": self.epoch_generation,
            "current_epoch_version": self.current_epoch_version,
            "minimum_epoch_version": self.minimum_epoch_version,
        }


class DraftConflictError(ra_exc.RestAlchemyException):
    message = "Draft UUID already exists with different canonical fields"
    code = 409


class DraftPreconditionRequiredError(ra_exc.RestAlchemyException):
    message = "Draft mutation requires If-Match"
    code = 428


class DraftPreconditionFailedError(ra_exc.RestAlchemyException):
    message = "Draft revision does not match If-Match"
    code = 412

    def __init__(self, current: dict[str, typing.Any]) -> None:
        super().__init__()
        self.current = current


class ExternalResourceForbiddenError(ra_exc.RestAlchemyException):
    message = "External resource access is forbidden"
    code = 403


class ExternalAccountConflictError(ra_exc.RestAlchemyException):
    message = "An external account for this provider already exists"
    code = 409


class ExternalPreconditionRequiredError(ra_exc.RestAlchemyException):
    message = "External resource mutation requires If-Match"
    code = 428


class ExternalPreconditionFailedError(ra_exc.RestAlchemyException):
    message = "External resource revision does not match If-Match"
    code = 412
