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

import datetime
import uuid as sys_uuid

from gcl_iam.api import controllers as iam_controllers
from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters

from workspace.common.api import controllers as common_controllers
from workspace.messenger_api.api import versions
from workspace.messenger_api.dm import models


def _create_topic_with_flags(project_id, **kwargs):
    topic_uuid = kwargs.pop("uuid", None) or sys_uuid.uuid4()
    topic = models.WorkspaceStreamTopic(
        uuid=topic_uuid,
        project_id=project_id,
        **kwargs,
    )
    topic.insert()

    bindings = models.WorkspaceStreamBinding.objects.get_all(
        filters={
            "stream_uuid": dm_filters.EQ(topic.stream_uuid),
            "project_id": dm_filters.EQ(project_id),
        }
    )
    for binding in bindings:
        flags = models.WorkspaceUserTopicFlags(
            uuid=topic.uuid,
            user_uuid=binding.user_uuid,
            project_id=project_id,
            is_done=False,
        )
        flags.insert()

    return topic


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


class FolderController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    common_controllers.BaseResourceControllerComplexPaginated,
):
    __complex_primary_key__ = ["uuid", "user_uuid"]

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.UserFolder,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
    )

    def create(self, **kwargs):
        user_uuid = self._get_user_uuid()
        folder = models.Folder(
            user_uuid=user_uuid,
            project_id=self._get_project_id(),
            **kwargs,
        )
        folder.insert()
        return self.get(uuid=folder.uuid)


class FolderItemController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    common_controllers.BaseResourceControllerComplexPaginated,
):
    __complex_primary_key__ = ["uuid", "user_uuid"]

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.UserFolderItem,
        convert_underscore=False,
    )

    def create(self, **kwargs):
        user_uuid = self._get_user_uuid()
        item = models.FolderItem(
            user_uuid=user_uuid,
            project_id=self._get_project_id(),
            **kwargs,
        )
        item.insert()
        return self.get(uuid=item.uuid, user_uuid=user_uuid)

    @ra_actions.post
    def pin(self, resource, *args, **kwargs):
        dm = models.FolderItem.objects.get_one(
            filters={"uuid": dm_filters.EQ(resource.uuid), **self._scoped_pk_filters()},
        )
        dm.pinned_at = datetime.datetime.now(datetime.timezone.utc)
        dm.save()
        return self.get(uuid=resource.uuid, user_uuid=self._get_user_uuid())

    @ra_actions.post
    def unpin(self, resource, *args, **kwargs):
        dm = models.FolderItem.objects.get_one(
            filters={"uuid": dm_filters.EQ(resource.uuid), **self._scoped_pk_filters()},
        )
        dm.pinned_at = None
        dm.save()
        return self.get(uuid=resource.uuid, user_uuid=self._get_user_uuid())



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
        stream_uuid = kwargs.pop("uuid", None) or sys_uuid.uuid4()
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()

        stream = models.WorkspaceStream(
            uuid=stream_uuid,
            project_id=project_id,
            user_uuid=user_uuid,
            **kwargs,
        )
        stream.insert()

        binding = models.WorkspaceStreamBinding(
            project_id=project_id,
            stream_uuid=stream.uuid,
            user_uuid=user_uuid,
            who_uuid=user_uuid,
            role=models.WorkspaceStreamRole.OWNER.value,
        )
        binding.insert()

        default_topic = _create_topic_with_flags(
            project_id=project_id,
            stream_uuid=stream.uuid,
            name="General Topic",
            default_for_stream_uuid=stream.uuid,
        )

        return self.get(uuid=stream.uuid, user_uuid=user_uuid)


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
        message_uuid = kwargs.pop("uuid", None) or sys_uuid.uuid4()
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()

        message = models.WorkspaceMessage(
            uuid=message_uuid,
            project_id=project_id,
            user_uuid=user_uuid,
            **kwargs,
        )
        message.insert()

        return self.get(uuid=message.uuid, user_uuid=user_uuid)


class WorkspaceStreamTopicController(
    iam_controllers.PolicyBasedController,
    IamScopedMixin,
    common_controllers.BaseResourceControllerComplexPaginated,
):
    __complex_primary_key__ = ["uuid", "user_uuid"]

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserTopic,
        convert_underscore=False,
    )

    def create(self, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()

        topic = _create_topic_with_flags(project_id=project_id, **kwargs)

        return self.get(uuid=topic.uuid, user_uuid=user_uuid)

    def update(self, uuid, **kwargs):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()

        topic = models.WorkspaceStreamTopic.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            }
        )

        models.WorkspaceStreamBinding.objects.get_one(
            filters={
                "stream_uuid": dm_filters.EQ(topic.stream_uuid),
                "user_uuid": dm_filters.EQ(user_uuid),
                "project_id": dm_filters.EQ(project_id),
            }
        )

        if "name" not in kwargs:
            raise ra_exc.ValidationErrorException()

        topic.update_dm(values={"name": kwargs["name"]})
        topic.update()

        return self.get(uuid=uuid, user_uuid=user_uuid)

    @ra_actions.post
    def toggle_done(self, resource, *args, **kwargs):
        flags = resource.get_flags()
        flags.is_done = not flags.is_done
        flags.update()
        return self.get(uuid=resource.uuid, user_uuid=self._get_user_uuid())


class WorkspaceUserController(
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUser,
        convert_underscore=False,
    )


class MeController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/me/"
