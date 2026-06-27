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
