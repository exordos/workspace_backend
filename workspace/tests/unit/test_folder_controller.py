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

import unittest
import uuid as sys_uuid
from unittest import mock

from workspace.user_api.api import controllers
from workspace.user_api.dm import models


def _make_folder(user_id, folder_uuid=None, title="test"):
    folder = mock.MagicMock(spec=models.Folder)
    folder.uuid = folder_uuid or sys_uuid.uuid4()
    folder.user_id = user_id
    folder.title = title
    folder.dump_to_simple_view.return_value = {
        "uuid": str(folder.uuid),
        "user_id": user_id,
        "title": title,
    }
    return folder


def _make_folder_item(folder, user_id, item_uuid=None, chat_id=1):
    item = mock.MagicMock(spec=models.FolderItem)
    item.uuid = item_uuid or sys_uuid.uuid4()
    item.folder = folder
    item.user_id = user_id
    item.chat_id = chat_id
    item.dump_to_simple_view.return_value = {
        "uuid": str(item.uuid),
        "chat_id": chat_id,
    }
    return item


MODELS_PATH = "workspace.user_api.dm.models"


class TestFolderControllerFilter(unittest.TestCase):
    def setUp(self):
        self.user_id = 42
        self.controller = controllers.FolderController.__new__(
            controllers.FolderController,
        )
        self.controller._get_user_id = mock.MagicMock(
            return_value=self.user_id,
        )

    @mock.patch(f"{MODELS_PATH}.FolderItem.objects")
    @mock.patch(f"{MODELS_PATH}.Folder.objects")
    def test_returns_folders_with_items(self, mock_folder_objects, mock_item_objects):
        folder = _make_folder(self.user_id, title="work")
        item1 = _make_folder_item(folder, self.user_id, chat_id=10)
        item2 = _make_folder_item(folder, self.user_id, chat_id=20)

        mock_folder_objects.get_all.return_value = [folder]
        mock_item_objects.get_all.return_value = [item1, item2]

        result = self.controller.filter(filters={})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "work")
        self.assertEqual(len(result[0]["items"]), 2)
        chat_ids = {i["chat_id"] for i in result[0]["items"]}
        self.assertEqual(chat_ids, {10, 20})

    @mock.patch(f"{MODELS_PATH}.FolderItem.objects")
    @mock.patch(f"{MODELS_PATH}.Folder.objects")
    def test_folder_without_items_has_empty_list(
        self, mock_folder_objects, mock_item_objects
    ):
        folder = _make_folder(self.user_id)
        mock_folder_objects.get_all.return_value = [folder]
        mock_item_objects.get_all.return_value = []

        result = self.controller.filter(filters={})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["items"], [])

    @mock.patch(f"{MODELS_PATH}.FolderItem.objects")
    @mock.patch(f"{MODELS_PATH}.Folder.objects")
    def test_multiple_folders_items_grouped_correctly(
        self, mock_folder_objects, mock_item_objects
    ):
        folder_a = _make_folder(self.user_id, title="a")
        folder_b = _make_folder(self.user_id, title="b")
        item_a = _make_folder_item(folder_a, self.user_id, chat_id=1)
        item_b = _make_folder_item(folder_b, self.user_id, chat_id=2)

        mock_folder_objects.get_all.return_value = [folder_a, folder_b]
        mock_item_objects.get_all.return_value = [item_a, item_b]

        result = self.controller.filter(filters={})

        self.assertEqual(len(result), 2)
        by_title = {r["title"]: r for r in result}
        self.assertEqual(len(by_title["a"]["items"]), 1)
        self.assertEqual(by_title["a"]["items"][0]["chat_id"], 1)
        self.assertEqual(len(by_title["b"]["items"]), 1)
        self.assertEqual(by_title["b"]["items"][0]["chat_id"], 2)

    @mock.patch(f"{MODELS_PATH}.FolderItem.objects")
    @mock.patch(f"{MODELS_PATH}.Folder.objects")
    def test_empty_folders_and_items(self, mock_folder_objects, mock_item_objects):
        mock_folder_objects.get_all.return_value = []
        mock_item_objects.get_all.return_value = []

        result = self.controller.filter(filters={})

        self.assertEqual(result, [])

    @mock.patch(f"{MODELS_PATH}.FolderItem.objects")
    @mock.patch(f"{MODELS_PATH}.Folder.objects")
    def test_items_for_unknown_folder_not_included(
        self, mock_folder_objects, mock_item_objects
    ):
        folder = _make_folder(self.user_id, title="known")
        orphan_folder = _make_folder(self.user_id, title="orphan")
        orphan_item = _make_folder_item(orphan_folder, self.user_id, chat_id=99)

        mock_folder_objects.get_all.return_value = [folder]
        mock_item_objects.get_all.return_value = [orphan_item]

        result = self.controller.filter(filters={})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["items"], [])

    @mock.patch(f"{MODELS_PATH}.FolderItem.objects")
    @mock.patch(f"{MODELS_PATH}.Folder.objects")
    def test_filters_contain_user_id(self, mock_folder_objects, mock_item_objects):
        mock_folder_objects.get_all.return_value = []
        mock_item_objects.get_all.return_value = []

        self.controller.filter(filters={"title": "x"})

        call_filters = mock_folder_objects.get_all.call_args[1]["filters"]
        self.assertIn("user_id", call_filters)
        self.assertIn("title", call_filters)
