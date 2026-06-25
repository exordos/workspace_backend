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

from workspace.messenger_api.dm import helpers as dm_helpers


class MessengerDMHelpersTestCase(unittest.TestCase):
    def test_create_workspace_user_folder_creates_event_and_returns_view(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        session = object()
        returned_folder = types.SimpleNamespace(uuid=folder_uuid)
        created_folder = {}

        class FakeFolder:
            def __init__(self, **kwargs):
                created_folder.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]

            def insert(self, session=None):
                created_folder["insert_session"] = session

        with mock.patch.object(
            dm_helpers.models, "Folder", FakeFolder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_event"
        ) as create_event, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=returned_folder
        ) as get_user_folder:
            result = dm_helpers.create_workspace_user_folder(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=folder_uuid,
                title="Inbox",
                session=session,
            )

        self.assertIs(returned_folder, result)
        self.assertEqual(folder_uuid, created_folder["uuid"])
        self.assertEqual(project_id, created_folder["project_id"])
        self.assertEqual(user_uuid, created_folder["user_uuid"])
        self.assertEqual("Inbox", created_folder["title"])
        self.assertIs(session, created_folder["insert_session"])
        create_event.assert_called_once()
        self.assertIs(returned_folder, create_event.call_args.kwargs["folder"])
        self.assertIs(session, create_event.call_args.kwargs["session"])
        get_user_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )


if __name__ == "__main__":
    unittest.main()
