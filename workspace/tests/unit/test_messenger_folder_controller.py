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

import types
import unittest
import uuid as sys_uuid
from unittest import mock

from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes


class MessengerFolderControllerTestCase(unittest.TestCase):
    def test_folder_route_allows_update(self):
        self.assertIn(ra_routes.UPDATE, routes.FolderRoute.__allow_methods__)

    def test_folder_route_allows_delete(self):
        self.assertIn(ra_routes.DELETE, routes.FolderRoute.__allow_methods__)

    def test_update_updates_folder_through_dm_helper(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        returned_folder = object()
        controller = controllers.FolderController.__new__(
            controllers.FolderController,
        )
        controller._get_project_id = mock.MagicMock(return_value=project_id)
        controller._get_user_uuid = mock.MagicMock(return_value=user_uuid)

        with mock.patch.object(
            controllers.messenger_dm_helpers,
            "update_workspace_user_folder",
            return_value=returned_folder,
        ) as update_folder:
            result = controller.update(folder_uuid, title="Archive")

        self.assertIs(returned_folder, result)
        update_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            title="Archive",
        )

    def test_delete_deletes_folder_through_dm_helper(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        controller = controllers.FolderController.__new__(
            controllers.FolderController,
        )
        controller._get_project_id = mock.MagicMock(return_value=project_id)
        controller._get_user_uuid = mock.MagicMock(return_value=user_uuid)

        with mock.patch.object(
            controllers.messenger_dm_helpers,
            "delete_workspace_user_folder",
            return_value=None,
        ) as delete_folder:
            result = controller.delete(folder_uuid)

        self.assertIsNone(result)
        delete_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
        )

    def test_create_folder_item_uses_dm_helper(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        returned_item = object()
        controller = controllers.FolderItemController.__new__(
            controllers.FolderItemController,
        )
        controller._get_project_id = mock.MagicMock(return_value=project_id)
        controller._get_user_uuid = mock.MagicMock(return_value=user_uuid)

        with mock.patch.object(
            controllers.messenger_dm_helpers,
            "create_workspace_user_folder_item",
            return_value=returned_item,
        ) as create_item:
            result = controller.create(
                uuid=item_uuid,
                folder_uuid=folder_uuid,
                stream_uuid=stream_uuid,
                chat_type="stream",
            )

        self.assertIs(returned_item, result)
        create_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            uuid=item_uuid,
            folder_uuid=folder_uuid,
            stream_uuid=stream_uuid,
            chat_type="stream",
        )

    def test_folder_item_route_allows_delete(self):
        self.assertIn(ra_routes.DELETE, routes.FolderItemRoute.__allow_methods__)

    def test_delete_folder_item_uses_dm_helper(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        controller = controllers.FolderItemController.__new__(
            controllers.FolderItemController,
        )
        controller._get_project_id = mock.MagicMock(return_value=project_id)
        controller._get_user_uuid = mock.MagicMock(return_value=user_uuid)

        with mock.patch.object(
            controllers.messenger_dm_helpers,
            "delete_workspace_user_folder_item",
            return_value=None,
        ) as delete_item:
            result = controller.delete(item_uuid)

        self.assertIsNone(result)
        delete_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
        )

    def test_pin_folder_item_uses_dm_helper_action(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        returned_item = object()
        controller = controllers.FolderItemController.__new__(
            controllers.FolderItemController,
        )
        controller._get_project_id = mock.MagicMock(return_value=project_id)
        controller._get_user_uuid = mock.MagicMock(return_value=user_uuid)
        resource = types.SimpleNamespace(uuid=item_uuid)

        with mock.patch.object(
            controllers.messenger_dm_helpers,
            "pin_workspace_user_folder_item",
            return_value=returned_item,
        ) as pin_item:
            result = controllers.FolderItemController.pin._post(
                controller,
                resource,
            )

        self.assertIs(returned_item, result)
        pin_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
        )

    def test_unpin_folder_item_uses_dm_helper_action(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        returned_item = object()
        controller = controllers.FolderItemController.__new__(
            controllers.FolderItemController,
        )
        controller._get_project_id = mock.MagicMock(return_value=project_id)
        controller._get_user_uuid = mock.MagicMock(return_value=user_uuid)
        resource = types.SimpleNamespace(uuid=item_uuid)

        with mock.patch.object(
            controllers.messenger_dm_helpers,
            "unpin_workspace_user_folder_item",
            return_value=returned_item,
        ) as unpin_item:
            result = controllers.FolderItemController.unpin._post(
                controller,
                resource,
            )

        self.assertIs(returned_item, result)
        unpin_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
        )


if __name__ == "__main__":
    unittest.main()
