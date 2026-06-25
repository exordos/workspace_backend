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

    def test_create_workspace_user_folder_item_updates_folder_event(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        returned_item = types.SimpleNamespace(uuid=item_uuid)
        returned_folder = types.SimpleNamespace(uuid=folder_uuid)
        created_item = {}

        class FakeFolderItem:
            def __init__(self, **kwargs):
                created_item.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]
                self.folder_uuid = kwargs["folder_uuid"]

            def insert(self, session=None):
                created_item["insert_session"] = session

        with mock.patch.object(
            dm_helpers.models, "FolderItem", FakeFolderItem
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_event, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=returned_folder
        ) as get_user_folder, mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder_item",
            return_value=returned_item,
        ) as get_user_folder_item:
            result = dm_helpers.create_workspace_user_folder_item(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=item_uuid,
                folder_uuid=folder_uuid,
                stream_uuid=stream_uuid,
                chat_type="stream",
                session=session,
            )

        self.assertIs(returned_item, result)
        self.assertEqual(item_uuid, created_item["uuid"])
        self.assertEqual(project_id, created_item["project_id"])
        self.assertEqual(user_uuid, created_item["user_uuid"])
        self.assertEqual(folder_uuid, created_item["folder_uuid"])
        self.assertEqual(stream_uuid, created_item["stream_uuid"])
        self.assertEqual("stream", created_item["chat_type"])
        self.assertIs(session, created_item["insert_session"])
        get_user_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )
        get_user_folder_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )

    def test_delete_workspace_user_folder_item_deletes_event_with_item_id(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        session = object()
        deleted_item = {}

        class ExistingItem:
            def delete(self, session=None):
                deleted_item["delete_session"] = session

        existing_item = ExistingItem()

        class FakeFolderItem:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_item)
            )

        with mock.patch.object(
            dm_helpers.models, "FolderItem", FakeFolderItem
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_item_deleted_event"
        ) as create_event:
            result = dm_helpers.delete_workspace_user_folder_item(
                project_id=project_id,
                user_uuid=user_uuid,
                item_uuid=item_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertIs(session, deleted_item["delete_session"])
        FakeFolderItem.objects.get_one.assert_called_once()
        create_event.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )

    def test_pin_workspace_user_folder_item_updates_folder_event(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        session = object()
        returned_item = types.SimpleNamespace(uuid=item_uuid)
        returned_folder = types.SimpleNamespace(uuid=folder_uuid)
        saved_item = {}

        class ExistingItem:
            def __init__(self):
                self.folder_uuid = folder_uuid
                self.pinned_at = None

            def save(self, session=None):
                saved_item["save_session"] = session
                saved_item["pinned_at"] = self.pinned_at

        existing_item = ExistingItem()

        class FakeFolderItem:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_item)
            )

        with mock.patch.object(
            dm_helpers.models, "FolderItem", FakeFolderItem
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_event, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=returned_folder
        ) as get_user_folder, mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder_item",
            return_value=returned_item,
        ) as get_user_folder_item:
            result = dm_helpers.pin_workspace_user_folder_item(
                project_id=project_id,
                user_uuid=user_uuid,
                item_uuid=item_uuid,
                session=session,
            )

        self.assertIs(returned_item, result)
        self.assertIs(session, saved_item["save_session"])
        self.assertIsNotNone(saved_item["pinned_at"])
        get_user_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )
        get_user_folder_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )

    def test_unpin_workspace_user_folder_item_updates_folder_event(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        session = object()
        returned_item = types.SimpleNamespace(uuid=item_uuid)
        returned_folder = types.SimpleNamespace(uuid=folder_uuid)
        saved_item = {}

        class ExistingItem:
            def __init__(self):
                self.folder_uuid = folder_uuid
                self.pinned_at = object()

            def save(self, session=None):
                saved_item["save_session"] = session
                saved_item["pinned_at"] = self.pinned_at

        existing_item = ExistingItem()

        class FakeFolderItem:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_item)
            )

        with mock.patch.object(
            dm_helpers.models, "FolderItem", FakeFolderItem
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_event, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=returned_folder
        ) as get_user_folder, mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder_item",
            return_value=returned_item,
        ) as get_user_folder_item:
            result = dm_helpers.unpin_workspace_user_folder_item(
                project_id=project_id,
                user_uuid=user_uuid,
                item_uuid=item_uuid,
                session=session,
            )

        self.assertIs(returned_item, result)
        self.assertIs(session, saved_item["save_session"])
        self.assertIsNone(saved_item["pinned_at"])
        get_user_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )
        get_user_folder_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
            session=session,
        )

    def test_update_workspace_user_folder_updates_event_and_returns_view(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        session = object()
        returned_folder = types.SimpleNamespace(uuid=folder_uuid)
        updated_folder = {}

        class ExistingFolder:
            title = "Inbox"

            def update_dm(self, values):
                updated_folder["values"] = values
                self.title = values["title"]

            def update(self, session=None):
                updated_folder["update_session"] = session

        existing_folder = ExistingFolder()

        class FakeFolder:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_folder)
            )

        with mock.patch.object(
            dm_helpers.models, "Folder", FakeFolder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_event, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=returned_folder
        ) as get_user_folder:
            result = dm_helpers.update_workspace_user_folder(
                project_id=project_id,
                user_uuid=user_uuid,
                folder_uuid=folder_uuid,
                title="Archive",
                session=session,
            )

        self.assertIs(returned_folder, result)
        self.assertEqual({"title": "Archive"}, updated_folder["values"])
        self.assertIs(session, updated_folder["update_session"])
        get_user_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )

    def test_update_workspace_user_folder_creates_event_for_same_title(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        session = object()
        returned_folder = types.SimpleNamespace(uuid=folder_uuid)
        updated_folder = {}

        class ExistingFolder:
            title = "Inbox"

            def update_dm(self, values):
                updated_folder["values"] = values
                self.title = values["title"]

            def update(self, session=None):
                updated_folder["update_session"] = session

        class FakeFolder:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingFolder())
            )

        with mock.patch.object(
            dm_helpers.models, "Folder", FakeFolder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_event, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=returned_folder
        ):
            result = dm_helpers.update_workspace_user_folder(
                project_id=project_id,
                user_uuid=user_uuid,
                folder_uuid=folder_uuid,
                title="Inbox",
                session=session,
            )

        self.assertIs(returned_folder, result)
        self.assertEqual({"title": "Inbox"}, updated_folder["values"])
        self.assertIs(session, updated_folder["update_session"])
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )

    def test_delete_workspace_user_folder_deletes_event_with_folder_id(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        session = object()
        deleted_folder = {}

        class ExistingFolder:
            def delete(self, session=None):
                deleted_folder["delete_session"] = session

        existing_folder = ExistingFolder()

        class FakeFolder:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_folder)
            )

        with mock.patch.object(
            dm_helpers.models, "Folder", FakeFolder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_deleted_event"
        ) as create_event:
            result = dm_helpers.delete_workspace_user_folder(
                project_id=project_id,
                user_uuid=user_uuid,
                folder_uuid=folder_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertIs(session, deleted_folder["delete_session"])
        FakeFolder.objects.get_one.assert_called_once()
        create_event.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
            session=session,
        )


if __name__ == "__main__":
    unittest.main()
