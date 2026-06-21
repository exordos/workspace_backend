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

import collections
import datetime
import uuid as sys_uuid

from gcl_iam.api import controllers as iam_controllers
from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import utils as oa_utils

from workspace.common.api import controllers as common_controllers
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import schemas
from workspace.messenger_api.api import versions
from workspace.messenger_api.dm import models


class ApiEndpointController(ra_controllers.RoutesListController):
    """Controller for /v1/ endpoint."""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


class IamScopedMixin:
    def _get_user_uuid(self):
        ctx = self.get_context()
        user_uuid = getattr(ctx, "user_uuid", None) if ctx is not None else None
        if user_uuid is None:
            raise ra_exc.ValidationErrorException()
        return user_uuid

    def _get_project_id(self):
        ctx = self.get_context()
        project_id = getattr(ctx, "project_id", None) if ctx is not None else None
        if project_id is None:
            raise ra_exc.ValidationErrorException()
        return project_id

    def _get_complex_pk_scope(self):
        """Auto-mixed scoped part of a composite primary key (user_uuid)."""
        return {"user_uuid": self._get_user_uuid()}


class FolderController(IamScopedMixin, ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.Folder,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
    )

    def _check_system_type(self, change_uuid, project_id, user_uuid):
        for folder in self.model.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(user_uuid),
                "system_type": dm_filters.EQ(models.SystemFolderType.ALL),
                "uuid": dm_filters.NE(change_uuid),
            }
        ):
            raise messenger_exceptions.OnlyOneAllFolderPerUserError()

    def create(self, uuid=None, system_type=None, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        kwargs["project_id"] = project_id
        kwargs["user_uuid"] = user_uuid
        if system_type == models.SystemFolderType.ALL:
            uuid = uuid or sys_uuid.uuid4()
            self._check_system_type(uuid, project_id, user_uuid)
        return super().create(uuid=uuid, system_type=system_type, **kwargs)

    def get(self, uuid):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        return self.model.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(user_uuid),
            },
        )

    @oa_utils.extend_schema(
        summary="List folders with nested items",
        parameters=schemas.FOLDER_FILTER_PARAMETERS,
        responses=schemas.FOLDER_FILTER_RESPONSES,
    )
    def filter(self, filters, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        filters = (filters or {}).copy()
        filters["project_id"] = dm_filters.EQ(project_id)
        filters["user_uuid"] = dm_filters.EQ(user_uuid)

        folders = models.Folder.objects.get_all(filters=filters)
        items = models.FolderItem.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(user_uuid),
            },
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
        if system_type == models.SystemFolderType.ALL:
            self._check_system_type(
                uuid,
                self._get_project_id(),
                self._get_user_uuid(),
            )
        dm.update_dm(values=kwargs)
        dm.update()
        return dm


class FolderItemController(
    IamScopedMixin,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByModelWithCustomProps(
        model_class=models.FolderItem,
        hidden_fields=["folder", "project_id", "user_uuid"],
        convert_underscore=False,
    )
    __pr_name__ = "folder"

    def create(self, parent_resource, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        kwargs["project_id"] = project_id
        kwargs["user_uuid"] = user_uuid
        return super().create(parent_resource=parent_resource, **kwargs)

    def get(self, parent_resource, uuid):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        return self.model.objects.get_one(
            filters={
                self.__pr_name__: dm_filters.EQ(parent_resource),
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
                "user_uuid": dm_filters.EQ(user_uuid),
            },
        )

    def filter(self, parent_resource, filters, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        filters = (filters or {}).copy()
        filters["project_id"] = dm_filters.EQ(project_id)
        filters["user_uuid"] = dm_filters.EQ(user_uuid)
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
    IamScopedMixin,
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByModelWithCustomProps(
        model_class=models.FolderItemRAFix,
        hidden_fields=["folder", "project_id", "user_uuid"],
        convert_underscore=False,
    )

    def filter(self, filters, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        filters = (filters or {}).copy()
        filters["project_id"] = dm_filters.EQ(project_id)
        filters["user_uuid"] = dm_filters.EQ(user_uuid)
        return super().filter(filters=filters, **kwargs)


class WorkspaceStreamController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    common_controllers.BaseResourceControllerComplexPaginated,
):
    __complex_primary_key__ = ["uuid", "user_uuid"]

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserStream,
        hidden_fields=[],
        convert_underscore=False,
    )

    def create(self, **kwargs):
        # user_uuid is mixed in automatically by the complex-PK base.
        return super().create(init_stream=True, **kwargs)


class WorkspaceStreamBindingController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceStreamBinding,
        convert_underscore=False,
    )

    def create(self, **kwargs):
        kwargs["who_uuid"] = self._get_user_uuid()
        return super().create(**kwargs)


class WorkspaceMessageController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    common_controllers.BaseResourceControllerComplexPaginated,
):
    __complex_primary_key__ = ["uuid", "user_uuid"]

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserMessage,
        convert_underscore=False,
    )

    def create(self, **kwargs):
        # user_uuid is mixed in automatically by the complex-PK base.
        return super().create(init_message=True, **kwargs)


class WorkspaceStreamTopicController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceStreamTopic,
        convert_underscore=False,
    )


class MeController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/me/"
