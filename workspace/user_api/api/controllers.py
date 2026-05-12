#    Copyright 2025 Genesis Corporation.
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

import collections
import datetime
import uuid as sys_uuid

from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import utils as oa_utils

from workspace.user_api import exceptions as user_api_exceptions
from workspace.user_api.api import schemas
from workspace.user_api.api import versions
from workspace.user_api.dm import models


class ApiEndpointController(ra_controllers.RoutesListController):
    """Controller for /v1/ endpoint"""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


_TRUE_VALUES = frozenset({"1", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "false", "no"})


def _parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in _TRUE_VALUES:
            return True
        if value.lower() in _FALSE_VALUES:
            return False
    return default


class UserScopedMixin:
    def _get_user_id(self):
        ctx = self.get_context()
        user_id = getattr(ctx, "user_id", None) if ctx is not None else None
        if user_id is None:
            raise ra_exc.ValidationErrorException()
        return user_id


class FolderController(UserScopedMixin, ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.Folder,
        hidden_fields=["user_id"],
        convert_underscore=False,
    )

    def _check_system_type(self, change_uuid, user_id):
        for folder in self.model.objects.get_all(
            filters={
                "user_id": dm_filters.EQ(user_id),
                "system_type": dm_filters.EQ(models.SystemFolderType.ALL),
                "uuid": dm_filters.NE(change_uuid),
            }
        ):
            raise user_api_exceptions.OnlyOneAllFolderPerUserError()

    def create(self, uuid=None, system_type=None, **kwargs):
        user_id = self._get_user_id()
        kwargs["user_id"] = user_id
        if system_type == models.SystemFolderType.ALL:
            uuid = uuid or sys_uuid.uuid4()
            self._check_system_type(uuid, user_id)
        return super().create(uuid=uuid, system_type=system_type, **kwargs)

    def get(self, uuid):
        user_id = self._get_user_id()
        return self.model.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "user_id": dm_filters.EQ(user_id),
            },
        )

    @oa_utils.extend_schema(
        summary="List folders with nested items",
        parameters=schemas.FOLDER_FILTER_PARAMETERS,
        responses=schemas.FOLDER_FILTER_RESPONSES,
    )
    def filter(self, filters, **kwargs):
        user_id = self._get_user_id()
        filters = (filters or {}).copy()
        filters["user_id"] = dm_filters.EQ(user_id)

        folders = models.Folder.objects.get_all(filters=filters)
        items = models.FolderItem.objects.get_all(
            filters={"user_id": dm_filters.EQ(user_id)},
        )
        items_by_folder = collections.defaultdict(list)
        for item in items:
            items_by_folder[item.folder.uuid].append(item.dump_to_simple_view())
        result = []
        for folder in folders:
            folder_view = folder.dump_to_simple_view()
            folder_view["items"] = items_by_folder.get(folder.uuid, [])
            result.append(folder_view)
        return result

    def delete(self, uuid):
        dm = self.get(uuid=uuid)
        dm.delete()

    def update(self, uuid, **kwargs):
        dm = self.get(uuid=uuid)
        system_type = kwargs.get("system_type", dm.system_type)
        # Check for system type conflict before updating
        if system_type == models.SystemFolderType.ALL:
            self._check_system_type(uuid, self._get_user_id())
        dm.update_dm(values=kwargs)
        dm.update()
        return dm


class FolderItemController(
    UserScopedMixin,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByModelWithCustomProps(
        model_class=models.FolderItem,
        hidden_fields=["folder", "user_id"],
        convert_underscore=False,
    )
    __pr_name__ = "folder"

    def create(self, parent_resource, **kwargs):
        user_id = self._get_user_id()
        kwargs["user_id"] = user_id
        return super().create(parent_resource=parent_resource, **kwargs)

    def get(self, parent_resource, uuid):
        user_id = self._get_user_id()
        return self.model.objects.get_one(
            filters={
                self.__pr_name__: dm_filters.EQ(parent_resource),
                "uuid": dm_filters.EQ(uuid),
                "user_id": dm_filters.EQ(user_id),
            },
        )

    def filter(self, parent_resource, filters, **kwargs):
        user_id = self._get_user_id()
        filters = (filters or {}).copy()
        filters["user_id"] = dm_filters.EQ(user_id)
        return super().filter(
            parent_resource=parent_resource,
            filters=filters,
            **kwargs,
        )

    def delete(self, parent_resource, uuid):
        dm = self.get(parent_resource=parent_resource, uuid=uuid)
        dm.delete()

    def update(self, parent_resource, uuid, **kwargs):
        dm = self.get(parent_resource=parent_resource, uuid=uuid)
        dm.update_dm(values=kwargs)
        dm.update()
        return dm

    @ra_actions.post
    def pin(self, resource, *args, **kwargs):
        resource.pinned_at = datetime.datetime.now(datetime.timezone.utc)
        resource.save()
        return resource

    @ra_actions.post
    def unpin(self, resource, *args, **kwargs):
        resource.pinned_at = None
        resource.save()
        return resource


class FolderItemsController(
    UserScopedMixin,
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByModelWithCustomProps(
        model_class=models.FolderItemRAFix,
        hidden_fields=["folder", "user_id"],
        convert_underscore=False,
    )

    def filter(self, filters, **kwargs):
        user_id = self._get_user_id()
        filters = (filters or {}).copy()
        filters["user_id"] = dm_filters.EQ(user_id)
        return super().filter(filters=filters, **kwargs)


class ServiceController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.Service,
        hidden_fields=[],
        convert_underscore=False,
    )
