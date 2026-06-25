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

import unittest
import uuid as sys_uuid
from unittest import mock

from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes


class MessengerFolderControllerTestCase(unittest.TestCase):
    def test_folder_route_allows_update(self):
        self.assertIn(ra_routes.UPDATE, routes.FolderRoute.__allow_methods__)

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


if __name__ == "__main__":
    unittest.main()
