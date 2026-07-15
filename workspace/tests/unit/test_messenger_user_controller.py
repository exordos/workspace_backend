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

from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import orm

from workspace.messenger_api.api import controllers


class WorkspaceUserControllerTestCase(unittest.TestCase):
    def test_get_does_not_add_project_id_filter(self):
        controller = controllers.WorkspaceUserController.__new__(
            controllers.WorkspaceUserController,
        )

        user_uuid = sys_uuid.uuid4()
        expected_user = object()

        with mock.patch.object(
            orm.ObjectCollection,
            "get_one",
            return_value=expected_user,
        ) as get_one:
            result = controller.get(uuid=user_uuid)

        self.assertIs(result, expected_user)

        filters = get_one.call_args.kwargs["filters"]
        self.assertEqual(["uuid"], sorted(filters))
        self.assertIsInstance(filters["uuid"], dm_filters.EQ)
        self.assertEqual(user_uuid, filters["uuid"].value)


if __name__ == "__main__":
    unittest.main()
