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

from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api.dm import helpers as dm_helpers


class MessengerDMHelpersTestCase(unittest.TestCase):
    def _existing_stream(self, **kwargs):
        class ExistingStream:
            def __init__(self, **values):
                object.__setattr__(self, "_dirty", False)
                object.__setattr__(self, "update_session", None)
                for field_name, value in values.items():
                    object.__setattr__(self, field_name, value)

            def __setattr__(self, name, value):
                if getattr(self, name, None) != value:
                    object.__setattr__(self, "_dirty", True)
                object.__setattr__(self, name, value)

            def is_dirty(self):
                return self._dirty

            def update_dm(self, values):
                for field_name, value in values.items():
                    setattr(self, field_name, value)

            def update(self, session=None):
                self.update_session = session
                self._dirty = False

        return ExistingStream(**kwargs)

    def test_get_workspace_event_project_ids_uses_project_view(self):
        first_project_id = sys_uuid.uuid4()
        second_project_id = sys_uuid.uuid4()
        get_all = mock.Mock(
            return_value=[
                types.SimpleNamespace(project_id=first_project_id),
                types.SimpleNamespace(project_id=second_project_id),
            ],
        )

        class FakeWorkspaceProject:
            objects = types.SimpleNamespace(get_all=get_all)

        with mock.patch.object(
            dm_helpers.models, "WorkspaceProject", FakeWorkspaceProject,
        ):
            result = dm_helpers._get_workspace_event_project_ids()

        self.assertEqual([first_project_id, second_project_id], result)
        get_all.assert_called_once_with(order_by={"project_id": "asc"})

    def test_mark_stale_workspace_users_offline_skips_projects_without_users(self):
        with mock.patch.object(
            dm_helpers, "_get_stale_workspace_users", return_value=[],
        ) as get_users, mock.patch.object(
            dm_helpers, "_get_workspace_event_project_ids",
        ) as get_project_ids:
            result = dm_helpers.mark_stale_workspace_users_offline()

        self.assertEqual([], result)
        get_users.assert_called_once()
        get_project_ids.assert_not_called()

    def test_get_or_create_workspace_user_stream_creates_topic_event_and_returns_view(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        returned_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
        )
        other_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=other_user_uuid,
        )
        other_all_folder = types.SimpleNamespace(
            uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
            user_uuid=other_user_uuid,
        )
        other_channels_folder = types.SimpleNamespace(
            uuid=dm_helpers.CHANNELS_FOLDER_UUID,
            user_uuid=other_user_uuid,
        )
        returned_all_folder = types.SimpleNamespace(
            uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        returned_channels_folder = types.SimpleNamespace(
            uuid=dm_helpers.CHANNELS_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        created_stream = {}
        created_binding = {}

        class FakeWorkspaceStream:
            def __init__(self, **kwargs):
                created_stream.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]

            def insert(self, session=None):
                created_stream["insert_session"] = session

        class FakeWorkspaceStreamBinding:
            def __init__(self, **kwargs):
                created_binding.update(kwargs)

            def insert(self, session=None):
                created_binding["insert_session"] = session

        get_user_streams = mock.Mock(return_value=[other_stream, returned_stream])

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(get_all=get_user_streams)

        get_user_folder = mock.Mock(
            side_effect=[
                other_all_folder,
                other_channels_folder,
                returned_all_folder,
                returned_channels_folder,
            ]
        )

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers, "_random_color", return_value=12345
        ), mock.patch.object(
            dm_helpers,
            "create_workspace_stream_topic_with_flags",
            return_value=types.SimpleNamespace(uuid=topic_uuid),
        ) as create_topic, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_topic_events",
        ) as create_topic_events, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", get_user_folder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_event"
        ) as create_event, mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_folder_event:
            result = dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=stream_uuid,
                name="Engineering",
                description="Engineering workspace",
                source_name="native",
                source={"kind": "native"},
                default_topic_name="zulip",
                session=session,
            )

        self.assertIs(returned_stream, result)
        self.assertEqual(stream_uuid, created_stream["uuid"])
        self.assertEqual(project_id, created_stream["project_id"])
        self.assertEqual(user_uuid, created_stream["user_uuid"])
        self.assertEqual("Engineering", created_stream["name"])
        self.assertEqual("native", created_stream["source_name"])
        self.assertEqual(12345, created_stream["color"])
        self.assertNotIn("default_topic_name", created_stream)
        self.assertIs(session, created_stream["insert_session"])
        self.assertEqual(project_id, created_binding["project_id"])
        self.assertEqual(stream_uuid, created_binding["stream_uuid"])
        self.assertEqual(user_uuid, created_binding["user_uuid"])
        self.assertEqual(user_uuid, created_binding["who_uuid"])
        self.assertEqual("owner", created_binding["role"])
        self.assertIs(session, created_binding["insert_session"])
        create_topic.assert_called_once_with(
            project_id=project_id,
            uuid=mock.ANY,
            stream_uuid=stream_uuid,
            name="zulip",
            source_name="native",
            source=mock.ANY,
        )
        self.assertEqual(
            created_stream["default_topic_uuid"],
            create_topic.call_args.kwargs["uuid"],
        )
        self.assertEqual("native", create_topic.call_args.kwargs["source"].KIND)
        create_topic_events.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
        )
        get_user_streams.assert_called_once_with(
            filters={
                "uuid": mock.ANY,
                "project_id": mock.ANY,
            },
            session=session,
        )
        filters = get_user_streams.call_args.kwargs["filters"]
        self.assertEqual(stream_uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertNotIn("user_uuid", filters)
        create_event.assert_has_calls(
            [
                mock.call(stream=other_stream, session=session),
                mock.call(stream=returned_stream, session=session),
            ]
        )
        self.assertEqual(2, create_event.call_count)
        get_user_folder.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                ),
            ]
        )
        create_folder_event.assert_has_calls(
            [
                mock.call(folder=other_all_folder, session=session),
                mock.call(folder=other_channels_folder, session=session),
                mock.call(folder=returned_all_folder, session=session),
                mock.call(folder=returned_channels_folder, session=session),
            ]
        )
        self.assertEqual(4, create_folder_event.call_count)

    def test_get_or_create_workspace_user_stream_with_direct_user_creates_private_pair(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        direct_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        owner_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
        )
        direct_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=direct_user_uuid,
        )
        direct_all_folder = types.SimpleNamespace(
            uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
            user_uuid=direct_user_uuid,
        )
        direct_personal_folder = types.SimpleNamespace(
            uuid=dm_helpers.PERSONAL_FOLDER_UUID,
            user_uuid=direct_user_uuid,
        )
        owner_all_folder = types.SimpleNamespace(
            uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        owner_personal_folder = types.SimpleNamespace(
            uuid=dm_helpers.PERSONAL_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        created_stream = {}
        created_bindings = []

        class FakeWorkspaceStream:
            def __init__(self, **kwargs):
                created_stream.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]

            def insert(self, session=None):
                created_stream["insert_session"] = session

        class FakeWorkspaceStreamBinding:
            def __init__(self, **kwargs):
                self._values = kwargs
                created_bindings.append(self._values)

            def insert(self, session=None):
                self._values["insert_session"] = session

        expected_index = ":".join(
            sorted([str(user_uuid), str(direct_user_uuid)])
        )
        get_all_streams = mock.Mock(return_value=[])

        FakeWorkspaceStream.objects = types.SimpleNamespace(
            get_all=get_all_streams,
        )
        get_user_streams = mock.Mock(return_value=[direct_stream, owner_stream])

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(get_all=get_user_streams)

        get_user_folder = mock.Mock(
            side_effect=[
                direct_all_folder,
                direct_personal_folder,
                owner_all_folder,
                owner_personal_folder,
            ]
        )

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers, "_random_color", return_value=23456
        ), mock.patch.object(
            dm_helpers,
            "create_workspace_stream_topic_with_flags",
            return_value=types.SimpleNamespace(uuid=topic_uuid),
        ) as create_topic, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_topic_events",
        ) as create_topic_events, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", get_user_folder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_event"
        ) as create_event, mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_folder_event:
            result = dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=stream_uuid,
                name="Direct",
                description="Private chat",
                source_name="native",
                source={"kind": "native"},
                direct_user_uuid=direct_user_uuid,
                session=session,
            )

        self.assertIs(owner_stream, result)
        self.assertEqual(True, created_stream["private"])
        self.assertEqual(direct_user_uuid, created_stream["direct_user_uuid"])
        self.assertEqual(expected_index, created_stream["private_index"])
        self.assertEqual(23456, created_stream["color"])
        self.assertIs(session, created_stream["insert_session"])
        get_all_streams.assert_called_once_with(
            filters={
                "project_id": mock.ANY,
                "private_index": mock.ANY,
            },
            limit=1,
            session=session,
        )
        filters = get_all_streams.call_args.kwargs["filters"]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(expected_index, filters["private_index"].value)
        get_user_streams.assert_called_once_with(
            filters={
                "project_id": mock.ANY,
                "private_index": mock.ANY,
            },
            session=session,
        )
        filters = get_user_streams.call_args.kwargs["filters"]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(expected_index, filters["private_index"].value)
        self.assertNotIn("user_uuid", filters)
        self.assertEqual(2, len(created_bindings))
        self.assertEqual(
            [user_uuid, direct_user_uuid],
            [binding["user_uuid"] for binding in created_bindings],
        )
        self.assertEqual(
            ["owner", "owner"],
            [binding["role"] for binding in created_bindings],
        )
        self.assertEqual(
            [user_uuid, user_uuid],
            [binding["who_uuid"] for binding in created_bindings],
        )
        create_topic.assert_called_once_with(
            project_id=project_id,
            uuid=mock.ANY,
            stream_uuid=stream_uuid,
            name="General Topic",
            source_name="native",
            source=mock.ANY,
        )
        self.assertEqual(
            created_stream["default_topic_uuid"],
            create_topic.call_args.kwargs["uuid"],
        )
        self.assertEqual("native", create_topic.call_args.kwargs["source"].KIND)
        create_topic_events.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
        )
        create_event.assert_has_calls(
            [
                mock.call(stream=direct_stream, session=session),
                mock.call(stream=owner_stream, session=session),
            ]
        )
        self.assertEqual(2, create_event.call_count)
        get_user_folder.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=direct_user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=direct_user_uuid,
                    folder_uuid=dm_helpers.PERSONAL_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.PERSONAL_FOLDER_UUID,
                ),
            ]
        )
        create_folder_event.assert_has_calls(
            [
                mock.call(folder=direct_all_folder, session=session),
                mock.call(folder=direct_personal_folder, session=session),
                mock.call(folder=owner_all_folder, session=session),
                mock.call(folder=owner_personal_folder, session=session),
            ]
        )
        self.assertEqual(4, create_folder_event.call_count)

    def test_create_workspace_private_group_stream(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        owner_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
        )
        all_folder = types.SimpleNamespace(
            uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        personal_folder = types.SimpleNamespace(
            uuid=dm_helpers.PERSONAL_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        created_stream = {}
        created_bindings = []

        class FakeWorkspaceStream:
            def __init__(self, **kwargs):
                created_stream.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]

            def insert(self, session=None):
                created_stream["insert_session"] = session

        class FakeWorkspaceStreamBinding:
            def __init__(self, **kwargs):
                self._values = kwargs
                created_bindings.append(self._values)

            def insert(self, session=None):
                self._values["insert_session"] = session

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(return_value=[owner_stream]),
            )

        get_user_folder = mock.Mock(
            side_effect=[
                all_folder,
                personal_folder,
            ]
        )

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers, "_random_color", return_value=23456
        ), mock.patch.object(
            dm_helpers,
            "create_workspace_stream_topic_with_flags",
            return_value=types.SimpleNamespace(uuid=topic_uuid),
        ) as create_topic, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_topic_events",
        ) as create_topic_events, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", get_user_folder
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_event"
        ) as create_event, mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_folder_event:
            result = dm_helpers.create_workspace_private_group_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=stream_uuid,
                name="Zulip group DM",
                description="Private group chat",
                source_name="zulip",
                source={"kind": "zulip", "stream_id": 20},
                default_topic_name="zulip",
                session=session,
            )

        self.assertIs(owner_stream, result)
        self.assertEqual(True, created_stream["private"])
        self.assertNotIn("direct_user_uuid", created_stream)
        self.assertNotIn("private_index", created_stream)
        self.assertEqual(23456, created_stream["color"])
        self.assertIs(session, created_stream["insert_session"])
        self.assertEqual(1, len(created_bindings))
        self.assertEqual(user_uuid, created_bindings[0]["user_uuid"])
        self.assertEqual("owner", created_bindings[0]["role"])
        self.assertEqual(user_uuid, created_bindings[0]["who_uuid"])
        create_topic.assert_called_once_with(
            project_id=project_id,
            uuid=mock.ANY,
            stream_uuid=stream_uuid,
            name="zulip",
            source_name="zulip",
            source=mock.ANY,
        )
        self.assertEqual(
            created_stream["default_topic_uuid"],
            create_topic.call_args.kwargs["uuid"],
        )
        self.assertEqual("zulip", create_topic.call_args.kwargs["source"].KIND)
        self.assertEqual(20, create_topic.call_args.kwargs["source"].stream_id)
        create_topic_events.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
        )
        create_event.assert_called_once_with(
            stream=owner_stream,
            session=session,
        )
        get_user_folder.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.PERSONAL_FOLDER_UUID,
                ),
            ]
        )
        create_folder_event.assert_has_calls(
            [
                mock.call(folder=all_folder, session=session),
                mock.call(folder=personal_folder, session=session),
            ]
        )
        self.assertEqual(2, create_folder_event.call_count)

    def test_get_or_create_workspace_user_stream_with_direct_user_returns_existing(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        direct_user_uuid = sys_uuid.uuid4()
        existing_stream = self._existing_stream(
            uuid=sys_uuid.uuid4(),
            name="Old direct",
            description="Old private chat",
            source_name="native",
            source={"kind": "native"},
        )
        owner_stream = types.SimpleNamespace(
            uuid=existing_stream.uuid,
            user_uuid=user_uuid,
        )
        direct_stream = types.SimpleNamespace(
            uuid=existing_stream.uuid,
            user_uuid=direct_user_uuid,
        )
        session = object()
        get_all_streams = mock.Mock(return_value=[existing_stream])

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(get_all=get_all_streams)

            def __init__(self, **kwargs):
                raise AssertionError("existing stream should be returned")

        get_user_streams = mock.Mock(return_value=[direct_stream, owner_stream])

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(get_all=get_user_streams)

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_updated_event"
        ) as create_event:
            result = dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                name="Direct",
                description="Private chat",
                source_name="native",
                source={"kind": "native"},
                direct_user_uuid=direct_user_uuid,
                session=session,
            )

        self.assertIs(owner_stream, result)
        self.assertEqual("Direct", existing_stream.name)
        self.assertEqual("Private chat", existing_stream.description)
        self.assertIs(session, existing_stream.update_session)
        get_user_streams.assert_called_once_with(
            filters={
                "uuid": mock.ANY,
                "project_id": mock.ANY,
            },
            session=session,
        )
        filters = get_user_streams.call_args.kwargs["filters"]
        self.assertEqual(existing_stream.uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertNotIn("user_uuid", filters)
        create_event.assert_has_calls(
            [
                mock.call(stream=direct_stream, session=session),
                mock.call(stream=owner_stream, session=session),
            ]
        )
        self.assertEqual(2, create_event.call_count)

    def test_get_or_create_workspace_user_stream_skips_existing_update_when_clean(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        direct_user_uuid = sys_uuid.uuid4()
        source = {"kind": "native"}
        existing_stream = self._existing_stream(
            uuid=sys_uuid.uuid4(),
            name="Direct",
            description="Private chat",
            source_name="native",
            source=source,
        )
        owner_stream = types.SimpleNamespace(
            uuid=existing_stream.uuid,
            user_uuid=user_uuid,
        )
        direct_stream = types.SimpleNamespace(
            uuid=existing_stream.uuid,
            user_uuid=direct_user_uuid,
        )
        session = object()
        get_all_streams = mock.Mock(return_value=[existing_stream])

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(get_all=get_all_streams)

            def __init__(self, **kwargs):
                raise AssertionError("existing stream should be returned")

        get_user_streams = mock.Mock(return_value=[direct_stream, owner_stream])

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(get_all=get_user_streams)

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_event"
        ) as create_event:
            result = dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                name="Direct",
                description="Private chat",
                source_name="native",
                source=source,
                direct_user_uuid=direct_user_uuid,
                session=session,
            )

        self.assertIs(owner_stream, result)
        self.assertIs(session, existing_stream.update_session)
        get_user_streams.assert_called_once()
        create_event.assert_not_called()

    def test_get_or_create_workspace_user_stream_rejects_client_private_index(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        direct_user_uuid = sys_uuid.uuid4()
        private_index = ":".join(sorted([str(user_uuid), str(direct_user_uuid)]))

        with self.assertRaises(
            messenger_exc.PrivateIndexIsTechnicalFieldError
        ) as error_context:
            dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                name="Direct",
                description="Private chat",
                source_name="native",
                source={"kind": "native"},
                direct_user_uuid=direct_user_uuid,
                private_index=private_index,
            )
        self.assertIn("private_index", error_context.exception.msg)

    def test_get_or_create_workspace_user_stream_rejects_self_direct_user(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()

        with self.assertRaises(
            messenger_exc.DirectStreamSelfChatError
        ) as error_context:
            dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                name="Direct",
                description="Private chat",
                source_name="native",
                source={"kind": "native"},
                direct_user_uuid=user_uuid,
            )
        self.assertIn("direct_user_uuid", error_context.exception.msg)

    def test_create_workspace_stream_binding_events_for_public_stream(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        binding = types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        user_stream = types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
            uuid=stream_uuid,
            private=False,
        )
        all_folder = types.SimpleNamespace(
            uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
            user_uuid=user_uuid,
        )
        channels_folder = types.SimpleNamespace(
            uuid=dm_helpers.CHANNELS_FOLDER_UUID,
            user_uuid=user_uuid,
        )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            return_value=user_stream,
        ) as get_user_stream, mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder",
            side_effect=[all_folder, channels_folder],
        ) as get_user_folder, mock.patch.object(
            dm_helpers.messenger_events,
            "create_stream_event",
        ) as create_stream_event, mock.patch.object(
            dm_helpers.messenger_events,
            "create_folder_updated_event",
        ) as create_folder_event:
            dm_helpers.create_workspace_stream_binding_events(
                binding=binding,
                session=session,
            )

        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        create_stream_event.assert_called_once_with(
            stream=user_stream,
            session=session,
        )
        get_user_folder.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                ),
            ]
        )
        create_folder_event.assert_has_calls(
            [
                mock.call(folder=all_folder, session=session),
                mock.call(folder=channels_folder, session=session),
            ]
        )

    def test_create_workspace_stream_binding_events_for_private_stream(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        binding = types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        user_stream = types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
            uuid=stream_uuid,
            private=True,
        )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            return_value=user_stream,
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder",
            side_effect=[
                types.SimpleNamespace(uuid=dm_helpers.ALL_CHATS_FOLDER_UUID),
                types.SimpleNamespace(uuid=dm_helpers.PERSONAL_FOLDER_UUID),
            ],
        ) as get_user_folder, mock.patch.object(
            dm_helpers.messenger_events,
            "create_stream_event",
        ), mock.patch.object(
            dm_helpers.messenger_events,
            "create_folder_updated_event",
        ):
            dm_helpers.create_workspace_stream_binding_events(binding=binding)

        folder_uuids = [
            call.kwargs["folder_uuid"]
            for call in get_user_folder.call_args_list
        ]
        self.assertEqual(
            [
                dm_helpers.ALL_CHATS_FOLDER_UUID,
                dm_helpers.PERSONAL_FOLDER_UUID,
            ],
            folder_uuids,
        )

    def test_create_workspace_stream_binding_events_skips_hidden_zulip_stream(
        self,
    ):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        binding = types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        stream = types.SimpleNamespace(
            source_name=dm_helpers.models.SourceName.ZULIP.value,
        )

        class FakeWorkspaceStream:
            objects = mock.Mock()

        FakeWorkspaceStream.objects.get_one.return_value = stream

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            side_effect=dm_helpers.storage_exc.RecordNotFound(
                model=dm_helpers.models.WorkspaceUserStream,
                filters={},
            ),
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ), mock.patch.object(
            dm_helpers.messenger_events,
            "create_stream_event",
        ) as create_stream_event, mock.patch.object(
            dm_helpers.messenger_events,
            "create_folder_updated_event",
        ) as create_folder_event:
            dm_helpers.create_workspace_stream_binding_events(
                binding=binding,
                session=session,
            )

        get_stream = FakeWorkspaceStream.objects.get_one
        filters = get_stream.call_args.kwargs["filters"]
        self.assertEqual(stream_uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(session, get_stream.call_args.kwargs["session"])
        create_stream_event.assert_not_called()
        create_folder_event.assert_not_called()

    def test_create_workspace_stream_binding_events_raises_for_hidden_native(
        self,
    ):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        binding = types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        stream = types.SimpleNamespace(
            source_name=dm_helpers.models.SourceName.NATIVE.value,
        )

        class FakeWorkspaceStream:
            objects = mock.Mock()

        FakeWorkspaceStream.objects.get_one.return_value = stream

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            side_effect=dm_helpers.storage_exc.RecordNotFound(
                model=dm_helpers.models.WorkspaceUserStream,
                filters={},
            ),
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with self.assertRaises(dm_helpers.storage_exc.RecordNotFound):
                dm_helpers.create_workspace_stream_binding_events(
                    binding=binding,
                )

    def test_create_workspace_stream_bindings_created_events_batches_bindings(self):
        project_id = sys_uuid.uuid4()
        owner_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        added_user_uuid = sys_uuid.uuid4()
        second_added_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        added_binding = types.SimpleNamespace(
            project_id=project_id,
            stream_uuid=stream_uuid,
            user_uuid=added_user_uuid,
        )
        second_added_binding = types.SimpleNamespace(
            project_id=project_id,
            stream_uuid=stream_uuid,
            user_uuid=second_added_user_uuid,
        )
        owner_binding = types.SimpleNamespace(user_uuid=owner_uuid)
        other_binding = types.SimpleNamespace(user_uuid=other_user_uuid)
        get_bindings = mock.Mock(
            return_value=[
                owner_binding,
                added_binding,
                other_binding,
                second_added_binding,
            ]
        )

        class FakeWorkspaceStreamBinding:
            objects = types.SimpleNamespace(get_all=get_bindings)

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers.messenger_events,
            "create_stream_bindings_created_event",
        ) as create_binding_event:
            dm_helpers.create_workspace_stream_bindings_created_events(
                bindings=[added_binding, second_added_binding],
                session=session,
            )

        get_bindings.assert_called_once_with(
            filters={
                "project_id": mock.ANY,
                "stream_uuid": mock.ANY,
            },
            session=session,
        )
        filters = get_bindings.call_args.kwargs["filters"]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(stream_uuid, filters["stream_uuid"].value)
        create_binding_event.assert_has_calls(
            [
                mock.call(
                    bindings=[added_binding, second_added_binding],
                    user_uuid=owner_uuid,
                    session=session,
                ),
                mock.call(
                    bindings=[added_binding, second_added_binding],
                    user_uuid=other_user_uuid,
                    session=session,
                ),
            ],
            any_order=True,
        )
        self.assertEqual(2, create_binding_event.call_count)

    def test_delete_workspace_stream_binding_sends_removed_user_events(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        binding_uuid = sys_uuid.uuid4()
        custom_folder_uuid = sys_uuid.uuid4()
        session = object()
        order = []
        user_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            private=False,
            source_name="native",
            source=dm_helpers.models.NativeSource(),
        )
        all_folder = types.SimpleNamespace(uuid=dm_helpers.ALL_CHATS_FOLDER_UUID)
        channels_folder = types.SimpleNamespace(uuid=dm_helpers.CHANNELS_FOLDER_UUID)
        custom_folder = types.SimpleNamespace(uuid=custom_folder_uuid)

        class ExistingBinding:
            def __init__(self):
                self.uuid = binding_uuid
                self.project_id = project_id
                self.user_uuid = user_uuid
                self.stream_uuid = stream_uuid

            def delete(self, session=None):
                order.append(("delete", session))

        class FakeWorkspaceStreamBinding:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingBinding())
            )

        def create_stream_deleted_event(**kwargs):
            order.append(("stream.deleted", kwargs))

        def create_folder_updated_event(**kwargs):
            order.append(("folder.updated", kwargs))

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            return_value=user_stream,
        ) as get_user_stream, mock.patch.object(
            dm_helpers,
            "_get_user_stream_folder_event_targets",
            return_value=[
                (user_uuid, dm_helpers.ALL_CHATS_FOLDER_UUID),
                (user_uuid, dm_helpers.CHANNELS_FOLDER_UUID),
                (user_uuid, custom_folder_uuid),
            ],
        ) as get_folder_targets, mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder",
            side_effect=[all_folder, channels_folder, custom_folder],
        ) as get_user_folder, mock.patch.object(
            dm_helpers.messenger_events,
            "create_stream_deleted_event",
            side_effect=create_stream_deleted_event,
        ) as create_stream_deleted, mock.patch.object(
            dm_helpers,
            "_delete_workspace_stream_binding_file_accesses",
            side_effect=lambda **kwargs: order.append(
                ("file_accesses.deleted", kwargs)
            ),
        ) as delete_file_accesses, mock.patch.object(
            dm_helpers.messenger_events,
            "create_folder_updated_event",
            side_effect=create_folder_updated_event,
        ) as create_folder_updated:
            result = dm_helpers.delete_workspace_stream_binding(
                project_id=project_id,
                binding_uuid=binding_uuid,
                session=session,
            )

        self.assertIsNone(result)
        FakeWorkspaceStreamBinding.objects.get_one.assert_called_once()
        filters = FakeWorkspaceStreamBinding.objects.get_one.call_args.kwargs[
            "filters"
        ]
        self.assertEqual(binding_uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        get_folder_targets.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            private=False,
        )
        delete_file_accesses.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
            user_uuid=user_uuid,
            session=session,
        )
        create_stream_deleted.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            source_name="native",
            source=user_stream.source,
            session=session,
        )
        self.assertEqual(3, get_user_folder.call_count)
        create_folder_updated.assert_has_calls(
            [
                mock.call(folder=all_folder, session=session),
                mock.call(folder=channels_folder, session=session),
                mock.call(folder=custom_folder, session=session),
            ]
        )
        self.assertEqual(
            [
                "stream.deleted",
                "file_accesses.deleted",
                "delete",
                "folder.updated",
                "folder.updated",
                "folder.updated",
            ],
            [item[0] for item in order],
        )

    def test_get_or_create_workspace_stream_binding_returns_existing(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        who_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        existing = types.SimpleNamespace(uuid=sys_uuid.uuid4())
        get_all = mock.Mock(return_value=[existing])

        class FakeWorkspaceStreamBinding:
            objects = types.SimpleNamespace(get_all=get_all)

            def __init__(self, **kwargs):
                raise AssertionError("existing binding should be returned")

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers,
            "create_workspace_stream_binding_events",
        ) as create_events, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_binding_message_flags",
        ) as create_flags, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_binding_file_accesses",
        ) as create_file_accesses:
            result = dm_helpers.get_or_create_workspace_stream_binding(
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
                who_uuid=who_uuid,
                role=dm_helpers.models.WorkspaceStreamRole.MEMBER.value,
            )

        self.assertIs(existing, result)
        create_events.assert_not_called()
        create_flags.assert_not_called()
        create_file_accesses.assert_not_called()
        get_all.assert_called_once_with(
            filters={
                "project_id": mock.ANY,
                "stream_uuid": mock.ANY,
                "user_uuid": mock.ANY,
            },
            limit=1,
            session=None,
        )

    def test_get_or_create_workspace_stream_bindings_creates_grouped_roles(self):
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        who_uuid = sys_uuid.uuid4()
        member_uuid = sys_uuid.uuid4()
        owner_uuid = sys_uuid.uuid4()
        created_bindings = []
        get_all = mock.Mock(return_value=[])

        class FakeWorkspaceStreamBinding:
            objects = types.SimpleNamespace(get_all=get_all)

            def __init__(self, **kwargs):
                self._values = kwargs
                self.project_id = kwargs["project_id"]
                self.stream_uuid = kwargs["stream_uuid"]
                self.user_uuid = kwargs["user_uuid"]
                self.who_uuid = kwargs["who_uuid"]
                self.role = kwargs["role"]
                created_bindings.append(self)

            def insert(self, session=None):
                self.insert_session = session

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers,
            "create_workspace_stream_binding_events",
        ) as create_user_events, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_binding_message_flags",
        ) as create_flags, mock.patch.object(
            dm_helpers,
            "_create_workspace_stream_binding_file_accesses",
        ) as create_file_accesses, mock.patch.object(
            dm_helpers,
            "create_workspace_stream_bindings_created_events",
        ) as create_batch_events:
            result = dm_helpers.get_or_create_workspace_stream_bindings(
                project_id=project_id,
                stream_uuid=stream_uuid,
                who_uuid=who_uuid,
                role_user_uuids={
                    dm_helpers.models.WorkspaceStreamRole.MEMBER.value: [
                        member_uuid
                    ],
                    dm_helpers.models.WorkspaceStreamRole.OWNER.value: [
                        owner_uuid
                    ],
                },
            )

        self.assertEqual(created_bindings, result)
        self.assertEqual(
            [
                (member_uuid, "member"),
                (owner_uuid, "owner"),
            ],
            [
                (binding.user_uuid, binding.role)
                for binding in created_bindings
            ],
        )
        self.assertEqual(2, create_user_events.call_count)
        create_flags.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    stream_uuid=stream_uuid,
                    user_uuid=member_uuid,
                    session=None,
                ),
                mock.call(
                    project_id=project_id,
                    stream_uuid=stream_uuid,
                    user_uuid=owner_uuid,
                    session=None,
                ),
            ]
        )
        create_file_accesses.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    stream_uuid=stream_uuid,
                    user_uuid=member_uuid,
                    session=None,
                ),
                mock.call(
                    project_id=project_id,
                    stream_uuid=stream_uuid,
                    user_uuid=owner_uuid,
                    session=None,
                ),
            ]
        )
        create_batch_events.assert_called_once_with(
            bindings=created_bindings,
            session=None,
        )

    def test_create_workspace_stream_binding_message_flags_inserts_missing(self):
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        session = types.SimpleNamespace(execute=mock.Mock())

        dm_helpers._create_workspace_stream_binding_message_flags(
            project_id=project_id,
            stream_uuid=stream_uuid,
            user_uuid=user_uuid,
            session=session,
        )

        session.execute.assert_called_once()
        statement, values = session.execute.call_args.args
        self.assertIn(
            'INSERT INTO "m_workspace_user_message_flags"',
            statement,
        )
        self.assertIn('FROM "m_workspace_messages" AS m', statement)
        self.assertIn(
            'ON CONFLICT ("uuid", "user_uuid") DO NOTHING',
            statement,
        )
        self.assertEqual(
            [
                str(user_uuid),
                str(user_uuid),
                str(project_id),
                str(stream_uuid),
            ],
            list(values),
        )

    def test_get_or_create_workspace_stream_bindings_rejects_invalid_role(self):
        with self.assertRaises(
            messenger_exc.InvalidStreamBindingRoleError
        ) as error_context:
            dm_helpers.get_or_create_workspace_stream_bindings(
                project_id=sys_uuid.uuid4(),
                stream_uuid=sys_uuid.uuid4(),
                who_uuid=sys_uuid.uuid4(),
                role_user_uuids={"not-a-role": [sys_uuid.uuid4()]},
            )

        self.assertIn("not-a-role", error_context.exception.msg)

    def test_create_workspace_stream_binding_message_flags_uses_engine_session(self):
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        session = types.SimpleNamespace(execute=mock.Mock())

        class SessionManager:
            def __enter__(self):
                return session

            def __exit__(self, exc_type, exc, traceback):
                return None

        engine = types.SimpleNamespace(
            session_manager=mock.Mock(return_value=SessionManager()),
        )

        with mock.patch.object(
            dm_helpers.models.WorkspaceUserMessageFlags,
            "_get_engine",
            return_value=engine,
        ):
            dm_helpers._create_workspace_stream_binding_message_flags(
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
            )

        engine.session_manager.assert_called_once_with()
        session.execute.assert_called_once()

    def test_get_or_create_workspace_stream_bindings_rejects_non_list_users(self):
        with self.assertRaises(
            messenger_exc.StreamBindingUsersPayloadError
        ) as error_context:
            dm_helpers.get_or_create_workspace_stream_bindings(
                project_id=sys_uuid.uuid4(),
                stream_uuid=sys_uuid.uuid4(),
                who_uuid=sys_uuid.uuid4(),
                role_user_uuids={
                    dm_helpers.models.WorkspaceStreamRole.MEMBER.value:
                        str(sys_uuid.uuid4()),
                },
            )

        self.assertIn("user UUID lists", error_context.exception.msg)

    def test_get_or_create_workspace_stream_bindings_rejects_tuple_users(self):
        with self.assertRaises(messenger_exc.StreamBindingUsersPayloadError):
            dm_helpers.get_or_create_workspace_stream_bindings(
                project_id=sys_uuid.uuid4(),
                stream_uuid=sys_uuid.uuid4(),
                who_uuid=sys_uuid.uuid4(),
                role_user_uuids={
                    dm_helpers.models.WorkspaceStreamRole.MEMBER.value: (
                        sys_uuid.uuid4(),
                    ),
                },
            )

    def test_update_workspace_user_stream_updates_event_and_returns_view(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        updated_stream = {}
        returned_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
        )
        other_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=other_user_uuid,
        )

        class ExistingStream:
            name = "Engineering"
            description = "Engineering chat"
            invite_only = False

            def update_dm(self, values):
                updated_stream["values"] = values
                self.name = values["name"]
                self.description = values["description"]
                self.invite_only = values["invite_only"]

            def update(self, session=None):
                updated_stream["update_session"] = session

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingStream())
            )

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(return_value=[returned_stream, other_stream])
            )

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers, "get_workspace_user_stream", return_value=returned_stream
        ) as get_user_stream, mock.patch.object(
            dm_helpers.messenger_events, "create_stream_updated_event"
        ) as create_event:
            result = dm_helpers.update_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                values={
                    "name": "Core Team",
                    "description": "Core team chat",
                    "invite_only": True,
                },
                session=session,
            )

        self.assertIs(returned_stream, result)
        self.assertEqual(
            {
                "name": "Core Team",
                "description": "Core team chat",
                "invite_only": True,
            },
            updated_stream["values"],
        )
        self.assertIs(session, updated_stream["update_session"])
        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        FakeWorkspaceStream.objects.get_one.assert_called_once()
        filters = FakeWorkspaceStream.objects.get_one.call_args.kwargs["filters"]
        self.assertEqual(stream_uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        create_event.assert_has_calls(
            [
                mock.call(stream=returned_stream, session=session),
                mock.call(stream=other_stream, session=session),
            ]
        )

    def test_update_workspace_user_stream_notifications_updates_binding(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        returned_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            notification_mode="mentions_only",
        )
        updated_binding = {}

        class ExistingBinding:
            notification_mode = "all_messages"

            def update_dm(self, values):
                updated_binding["values"] = values
                self.notification_mode = values["notification_mode"]

            def update(self, session=None):
                updated_binding["update_session"] = session

        class FakeWorkspaceStreamBinding:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingBinding())
            )

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers, "get_workspace_user_stream", return_value=returned_stream
        ) as get_user_stream, mock.patch.object(
            dm_helpers.messenger_events, "create_stream_updated_event"
        ) as create_event:
            result = dm_helpers.update_workspace_user_stream_notifications(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                notification_mode="mentions_only",
                session=session,
            )

        self.assertIs(returned_stream, result)
        self.assertEqual(
            {"notification_mode": "mentions_only"},
            updated_binding["values"],
        )
        self.assertIs(session, updated_binding["update_session"])
        FakeWorkspaceStreamBinding.objects.get_one.assert_called_once()
        filters = FakeWorkspaceStreamBinding.objects.get_one.call_args.kwargs[
            "filters"
        ]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(stream_uuid, filters["stream_uuid"].value)
        self.assertEqual(user_uuid, filters["user_uuid"].value)
        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        create_event.assert_called_once_with(
            stream=returned_stream,
            session=session,
        )

    def test_delete_workspace_user_stream_deletes_stream_and_sends_events(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        custom_folder_uuid = sys_uuid.uuid4()
        session = mock.Mock()
        actor_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            private=False,
            source_name="native",
            source=dm_helpers.models.NativeSource(),
        )
        other_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=other_user_uuid,
            private=True,
            source_name="native",
            source=dm_helpers.models.NativeSource(),
        )
        custom_item = types.SimpleNamespace(
            user_uuid=user_uuid,
            folder_uuid=custom_folder_uuid,
        )

        class ExistingStream:
            def delete(self, session=None):
                self.delete_session = session

        existing_stream = ExistingStream()

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_stream)
            )

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(return_value=[actor_stream, other_stream])
            )

        class FakeFolderItem:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(return_value=[custom_item])
            )

        with mock.patch.object(
            dm_helpers, "get_workspace_user_stream", return_value=actor_stream
        ) as get_user_stream, mock.patch.object(
            dm_helpers, "get_workspace_user_folder", return_value=object()
        ) as get_user_folder, mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers.models, "FolderItem", FakeFolderItem
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_deleted_event"
        ) as create_stream_deleted, mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_folder_updated:
            result = dm_helpers.delete_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertIs(session, existing_stream.delete_session)
        self.assertFalse(session.execute.called)
        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        create_stream_deleted.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    stream_uuid=stream_uuid,
                    source_name="native",
                    source=actor_stream.source,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    stream_uuid=stream_uuid,
                    source_name="native",
                    source=other_stream.source,
                    session=session,
                ),
            ]
        )
        self.assertEqual(5, get_user_folder.call_count)
        self.assertEqual(5, create_folder_updated.call_count)

    def test_delete_workspace_user_stream_skips_missing_system_folders(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        custom_folder_uuid = sys_uuid.uuid4()
        session = object()
        user_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            private=False,
            source_name="native",
            source=dm_helpers.models.NativeSource(),
        )
        custom_folder = types.SimpleNamespace(uuid=custom_folder_uuid)

        class ExistingStream:
            def delete(self, session=None):
                self.delete_session = session

        existing_stream = ExistingStream()

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_stream)
            )

        class FakeWorkspaceUserStream:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(return_value=[user_stream])
            )

        with mock.patch.object(
            dm_helpers, "get_workspace_user_stream", return_value=user_stream
        ), mock.patch.object(
            dm_helpers,
            "_get_stream_folder_event_targets",
            return_value=[
                (user_uuid, dm_helpers.ALL_CHATS_FOLDER_UUID),
                (user_uuid, dm_helpers.CHANNELS_FOLDER_UUID),
                (user_uuid, custom_folder_uuid),
            ],
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder",
            side_effect=[
                dm_helpers.storage_exc.RecordNotFound(
                    model=dm_helpers.models.UserFolder,
                    filters={},
                ),
                dm_helpers.storage_exc.RecordNotFound(
                    model=dm_helpers.models.UserFolder,
                    filters={},
                ),
                custom_folder,
            ],
        ) as get_user_folder, mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceUserStream", FakeWorkspaceUserStream
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_stream_deleted_event"
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_folder_updated:
            result = dm_helpers.delete_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertIs(session, existing_stream.delete_session)
        self.assertEqual(3, get_user_folder.call_count)
        create_folder_updated.assert_called_once_with(
            folder=custom_folder,
            session=session,
        )

    def test_create_workspace_stream_topic_with_flags_defaults_color(self):
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        created_topic = {}
        created_flags = []

        class FakeWorkspaceStreamTopic:
            def __init__(self, **kwargs):
                created_topic.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.stream_uuid = kwargs["stream_uuid"]

            def insert(self, session=None):
                created_topic["insert_session"] = session

        class FakeWorkspaceStreamBinding:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(
                    return_value=[types.SimpleNamespace(user_uuid=user_uuid)]
                )
            )

        class FakeWorkspaceUserTopicFlags:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def insert(self, session=None):
                self.kwargs["insert_session"] = session
                created_flags.append(self.kwargs)

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamTopic",
            FakeWorkspaceStreamTopic,
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceStreamBinding",
            FakeWorkspaceStreamBinding,
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserTopicFlags",
            FakeWorkspaceUserTopicFlags,
        ), mock.patch.object(
            dm_helpers, "_random_color", return_value=445566
        ):
            topic = dm_helpers.create_workspace_stream_topic_with_flags(
                project_id=project_id,
                uuid=topic_uuid,
                stream_uuid=stream_uuid,
                name="Planning",
            )

        self.assertEqual(topic_uuid, topic.uuid)
        self.assertEqual(445566, created_topic["color"])
        self.assertIsNone(created_topic["insert_session"])
        FakeWorkspaceStreamBinding.objects.get_all.assert_called_once()
        filters = FakeWorkspaceStreamBinding.objects.get_all.call_args.kwargs[
            "filters"
        ]
        self.assertEqual(stream_uuid, filters["stream_uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertNotIn(
            "session",
            FakeWorkspaceStreamBinding.objects.get_all.call_args.kwargs,
        )
        self.assertEqual(
            [
                {
                    "uuid": topic_uuid,
                    "user_uuid": user_uuid,
                    "project_id": project_id,
                    "is_done": False,
                    "insert_session": None,
                }
            ],
            created_flags,
        )

    def test_create_workspace_user_stream_topic_creates_events_for_users(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        created_topic = types.SimpleNamespace(uuid=topic_uuid)
        returned_topic = types.SimpleNamespace(user_uuid=user_uuid)
        other_topic = types.SimpleNamespace(user_uuid=other_user_uuid)

        with mock.patch.object(
            dm_helpers, "get_workspace_user_stream", return_value=object()
        ) as get_user_stream, mock.patch.object(
            dm_helpers,
            "create_workspace_stream_topic_with_flags",
            return_value=created_topic,
        ) as create_topic, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_stream_topics",
            return_value=[other_topic, returned_topic],
        ) as get_topics, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_event"
        ) as create_event:
            result = dm_helpers.create_workspace_user_stream_topic(
                project_id=project_id,
                user_uuid=user_uuid,
                values={
                    "name": "Planning",
                    "stream_uuid": stream_uuid,
                },
                session=session,
            )

        self.assertIs(returned_topic, result)
        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        create_topic.assert_called_once_with(
            project_id=project_id,
            name="Planning",
            stream_uuid=stream_uuid,
        )
        get_topics.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
        )
        create_event.assert_has_calls(
            [
                mock.call(topic=other_topic, session=session),
                mock.call(topic=returned_topic, session=session),
            ]
        )

    def test_update_workspace_user_stream_topic_creates_events_for_users(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        updated_topic = {}
        returned_topic = types.SimpleNamespace(user_uuid=user_uuid)
        other_topic = types.SimpleNamespace(user_uuid=other_user_uuid)

        class ExistingTopic:
            def update_dm(self, values):
                updated_topic["values"] = values

            def update(self, session=None):
                updated_topic["update_session"] = session

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_stream_topic_for_user",
            return_value=ExistingTopic(),
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_stream_topics",
            return_value=[returned_topic, other_topic],
        ) as get_topics, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_updated_event"
        ) as create_event:
            result = dm_helpers.update_workspace_user_stream_topic(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
                values={
                    "name": "Retros",
                    "color": 11259375,
                },
                session=session,
            )

        self.assertIs(returned_topic, result)
        self.assertEqual(
            {
                "name": "Retros",
                "color": 11259375,
            },
            updated_topic["values"],
        )
        self.assertIs(session, updated_topic["update_session"])
        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
        )
        get_topics.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
        )
        create_event.assert_has_calls(
            [
                mock.call(topic=returned_topic, session=session),
                mock.call(topic=other_topic, session=session),
            ]
        )

    def test_delete_workspace_user_stream_topic_creates_events_for_users(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        deleted_topic = {}
        returned_topic = types.SimpleNamespace(user_uuid=user_uuid)
        other_topic = types.SimpleNamespace(user_uuid=other_user_uuid)

        class ExistingTopic:
            def __init__(self):
                self.uuid = topic_uuid
                self.stream_uuid = stream_uuid
                self.source_name = "native"
                self.source = dm_helpers.models.NativeSource()

            def delete(self, session=None):
                deleted_topic["delete_session"] = session

        class ExistingStream:
            def __init__(self):
                self.default_topic_uuid = topic_uuid

            def update_dm(self, values):
                deleted_topic["stream_values"] = values

            def update(self):
                deleted_topic["stream_updated"] = True

        get_stream = mock.Mock(return_value=ExistingStream())

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(get_one=get_stream)

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_stream_topic_for_user",
            return_value=ExistingTopic(),
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_stream_topics",
            return_value=[returned_topic, other_topic],
        ) as get_topics, mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers, "_create_workspace_stream_updated_events"
        ) as create_stream_events, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_deleted_event"
        ) as create_event:
            result = dm_helpers.delete_workspace_user_stream_topic(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertIs(session, deleted_topic["delete_session"])
        self.assertEqual(
            {"default_topic_uuid": None},
            deleted_topic["stream_values"],
        )
        self.assertTrue(deleted_topic["stream_updated"])
        get_stream.assert_called_once_with(
            filters={
                "uuid": mock.ANY,
                "project_id": mock.ANY,
            },
        )
        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
        )
        get_topics.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
        )
        create_stream_events.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
        )
        create_event.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                    stream_uuid=stream_uuid,
                    source_name="native",
                    source=mock.ANY,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    topic_uuid=topic_uuid,
                    stream_uuid=stream_uuid,
                    source_name="native",
                    source=mock.ANY,
                    session=session,
                ),
            ]
        )

    def test_set_workspace_user_stream_topic_default_updates_stream_events(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        previous_topic_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        updated_stream = {}
        topic = types.SimpleNamespace(uuid=topic_uuid, stream_uuid=stream_uuid)
        returned_topic = object()

        class ExistingStream:
            default_topic_uuid = previous_topic_uuid

            def update_dm(self, values):
                updated_stream["values"] = values

            def update(self):
                updated_stream["updated"] = True

        get_stream = mock.Mock(return_value=ExistingStream())

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(get_one=get_stream)

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_stream_topic_for_user",
            return_value=topic,
        ) as get_topic, mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers, "_create_workspace_stream_updated_events"
        ) as create_stream_events, mock.patch.object(
            dm_helpers, "_create_workspace_stream_topic_updated_events"
        ) as create_topic_events, mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream_topic",
            return_value=returned_topic,
        ) as get_user_topic:
            result = dm_helpers.set_workspace_user_stream_topic_default(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
            )

        self.assertIs(returned_topic, result)
        self.assertEqual(
            {"default_topic_uuid": topic_uuid},
            updated_stream["values"],
        )
        self.assertTrue(updated_stream["updated"])
        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
        )
        get_stream.assert_called_once_with(
            filters={
                "uuid": mock.ANY,
                "project_id": mock.ANY,
            },
        )
        create_stream_events.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
        )
        create_topic_events.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    topic_uuid=previous_topic_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    topic_uuid=topic_uuid,
                ),
            ]
        )
        get_user_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
        )

    def test_toggle_workspace_user_stream_topic_done_creates_event(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        current_topic = types.SimpleNamespace(is_done=False)
        user_topic = types.SimpleNamespace(user_uuid=user_uuid)
        other_topic = types.SimpleNamespace(user_uuid=other_user_uuid)
        returned_topic = types.SimpleNamespace(
            uuid=topic_uuid,
            user_uuid=user_uuid,
            is_done=True,
        )
        returned_other_topic = types.SimpleNamespace(
            uuid=topic_uuid,
            user_uuid=other_user_uuid,
            is_done=True,
        )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream_topic",
            return_value=current_topic,
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_stream_topics",
            side_effect=[
                [user_topic, other_topic],
                [returned_topic, returned_other_topic],
            ],
        ) as get_topics, mock.patch.object(
            dm_helpers, "_set_workspace_user_topic_done"
        ) as set_done, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_updated_event"
        ) as create_event:
            result = dm_helpers.toggle_workspace_user_stream_topic_done(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
                session=session,
            )

        self.assertIs(returned_topic, result)
        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
        )
        get_topics.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    topic_uuid=topic_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    topic_uuid=topic_uuid,
                ),
            ]
        )
        set_done.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                    is_done=True,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    topic_uuid=topic_uuid,
                    is_done=True,
                    session=session,
                ),
            ]
        )
        create_event.assert_has_calls(
            [
                mock.call(topic=returned_topic, session=session),
                mock.call(topic=returned_other_topic, session=session),
            ]
        )

    def test_update_workspace_user_stream_topic_notifications_allows_unmute_on_muted_stream(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        current_topic = types.SimpleNamespace(stream_uuid=stream_uuid)
        current_stream = types.SimpleNamespace(notification_mode="muted")
        returned_topic = types.SimpleNamespace(
            uuid=topic_uuid,
            user_uuid=user_uuid,
            notification_mode="unmute",
        )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream_topic",
            side_effect=[current_topic, returned_topic],
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            return_value=current_stream,
        ) as get_stream, mock.patch.object(
            dm_helpers, "_set_workspace_user_topic_notification_mode"
        ) as set_notification, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_updated_event"
        ) as create_event:
            result = dm_helpers.update_workspace_user_stream_topic_notifications(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
                notification_mode="unmute",
                session=session,
            )

        self.assertIs(returned_topic, result)
        get_topic.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                ),
            ]
        )
        get_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        set_notification.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
            notification_mode="unmute",
            session=session,
        )
        create_event.assert_called_once_with(
            topic=returned_topic,
            session=session,
        )

    def test_update_workspace_user_stream_topic_notifications_rejects_unmute_on_default_stream(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        current_topic = types.SimpleNamespace(stream_uuid=stream_uuid)
        current_stream = types.SimpleNamespace(notification_mode="all_messages")

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream_topic",
            return_value=current_topic,
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            return_value=current_stream,
        ), mock.patch.object(
            dm_helpers, "_set_workspace_user_topic_notification_mode"
        ) as set_notification:
            with self.assertRaises(
                messenger_exc.InvalidTopicNotificationModeError
            ):
                dm_helpers.update_workspace_user_stream_topic_notifications(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                    notification_mode="unmute",
                    session=session,
                )

        set_notification.assert_not_called()

    def test_topic_notification_mode_matrix_matches_stream_mode(self):
        shared_modes = ("mute", "default", "follow")
        for stream_mode in ("all_messages", "mentions_only", "muted"):
            stream = types.SimpleNamespace(notification_mode=stream_mode)
            for topic_mode in shared_modes:
                dm_helpers._validate_topic_notification_mode(
                    stream=stream,
                    notification_mode=topic_mode,
                )

        muted_stream = types.SimpleNamespace(notification_mode="muted")
        dm_helpers._validate_topic_notification_mode(
            stream=muted_stream,
            notification_mode="unmute",
        )

        default_stream = types.SimpleNamespace(notification_mode="all_messages")
        with self.assertRaises(messenger_exc.InvalidTopicNotificationModeError):
            dm_helpers._validate_topic_notification_mode(
                stream=default_stream,
                notification_mode="unmute",
            )

    def test_create_workspace_user_message_flags_and_unread_events(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        returned_message = object()
        created_message = {}
        created_flags = []
        source_fields = {
            "source_name": dm_helpers.models.SourceName.NATIVE.value,
            "source": dm_helpers.models.NativeSource(),
        }

        class FakeWorkspaceMessage:
            def __init__(self, **kwargs):
                created_message.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]
                self.stream_uuid = kwargs["stream_uuid"]
                self.topic_uuid = kwargs["topic_uuid"]

            def insert(self, session=None):
                created_message["insert_session"] = session

            def get_recipients(self, session=None):
                created_message["recipients_session"] = session
                return [user_uuid, other_user_uuid]

        class FakeWorkspaceUserMessageFlags:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def insert(self, session=None):
                self.kwargs["insert_session"] = session
                created_flags.append(self.kwargs)

        with mock.patch.object(
            dm_helpers.models, "WorkspaceMessage", FakeWorkspaceMessage
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_events"
        ) as create_message_events, mock.patch.object(
            dm_helpers, "_create_messages_unread_updated_events"
        ) as create_unread_events, mock.patch.object(
            dm_helpers,
            "_get_message_topic_source_fields",
            return_value=source_fields,
        ) as get_topic_source_fields, mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=returned_message,
        ) as get_user_message:
            result = dm_helpers.create_workspace_user_message(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=message_uuid,
                stream_uuid=stream_uuid,
                topic_uuid=topic_uuid,
                payload={"kind": "markdown", "content": "hello"},
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual(message_uuid, created_message["uuid"])
        self.assertEqual(project_id, created_message["project_id"])
        self.assertEqual(user_uuid, created_message["user_uuid"])
        self.assertEqual("native", created_message["source_name"])
        self.assertIs(source_fields["source"], created_message["source"])
        self.assertIs(session, created_message["insert_session"])
        self.assertIs(session, created_message["recipients_session"])
        self.assertEqual(
            [
                {
                    "uuid": message_uuid,
                    "user_uuid": user_uuid,
                    "project_id": project_id,
                    "read": True,
                    "insert_session": session,
                },
                {
                    "uuid": message_uuid,
                    "user_uuid": other_user_uuid,
                    "project_id": project_id,
                    "read": False,
                    "insert_session": session,
                },
            ],
            created_flags,
        )
        create_message_events.assert_called_once()
        get_topic_source_fields.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuids=[other_user_uuid],
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )

    def test_create_workspace_user_message_uses_stream_default_topic(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        returned_message = object()
        created_message = {}
        created_flags = []
        topic_source = dm_helpers.models.NativeSource()
        get_default_topic = mock.Mock(
            return_value=types.SimpleNamespace(
                uuid=topic_uuid,
                source_name=dm_helpers.models.SourceName.NATIVE.value,
                source=topic_source,
            )
        )

        class FakeWorkspaceMessage:
            def __init__(self, **kwargs):
                created_message.update(kwargs)
                self.uuid = kwargs["uuid"]
                self.project_id = kwargs["project_id"]
                self.user_uuid = kwargs["user_uuid"]
                self.stream_uuid = kwargs["stream_uuid"]
                self.topic_uuid = kwargs["topic_uuid"]

            def insert(self, session=None):
                created_message["insert_session"] = session

            def get_recipients(self, session=None):
                created_message["recipients_session"] = session
                return [user_uuid]

        class FakeWorkspaceStreamTopic:
            objects = types.SimpleNamespace(get_one=get_default_topic)

        get_stream = mock.Mock(
            return_value=types.SimpleNamespace(
                default_topic_uuid=topic_uuid,
            )
        )

        class FakeWorkspaceStream:
            objects = types.SimpleNamespace(get_one=get_stream)

        class FakeWorkspaceUserMessageFlags:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def insert(self, session=None):
                self.kwargs["insert_session"] = session
                created_flags.append(self.kwargs)

        with mock.patch.object(
            dm_helpers.models, "WorkspaceMessage", FakeWorkspaceMessage
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceStreamTopic", FakeWorkspaceStreamTopic
        ), mock.patch.object(
            dm_helpers.models, "WorkspaceStream", FakeWorkspaceStream
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_events"
        ) as create_message_events, mock.patch.object(
            dm_helpers, "_create_messages_unread_updated_events"
        ) as create_unread_events, mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=returned_message,
        ) as get_user_message:
            result = dm_helpers.create_workspace_user_message(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=message_uuid,
                stream_uuid=stream_uuid,
                topic_uuid=None,
                payload={"kind": "markdown", "content": "hello"},
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual(topic_uuid, created_message["topic_uuid"])
        self.assertEqual("native", created_message["source_name"])
        self.assertIsInstance(
            created_message["source"],
            dm_helpers.models.NativeSource,
        )
        self.assertIs(session, created_message["insert_session"])
        self.assertEqual(2, get_default_topic.call_count)
        get_stream.assert_called_once_with(
            filters={
                "project_id": mock.ANY,
                "uuid": mock.ANY,
            },
        )
        filters = get_default_topic.call_args_list[0].kwargs["filters"]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(stream_uuid, filters["stream_uuid"].value)
        self.assertEqual(topic_uuid, filters["uuid"].value)
        source_filters = get_default_topic.call_args_list[1].kwargs["filters"]
        self.assertEqual(project_id, source_filters["project_id"].value)
        self.assertEqual(topic_uuid, source_filters["uuid"].value)
        self.assertEqual(
            [
                {
                    "uuid": message_uuid,
                    "user_uuid": user_uuid,
                    "project_id": project_id,
                    "read": True,
                    "insert_session": session,
                },
            ],
            created_flags,
        )
        create_message_events.assert_called_once()
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuids=[],
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )

    def test_update_workspace_user_message_updates_payload_and_sends_events(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        updated_message = {}
        returned_message = types.SimpleNamespace(user_uuid=user_uuid)
        other_message = types.SimpleNamespace(user_uuid=other_user_uuid)

        class ExistingMessage:
            payload = {"kind": "markdown", "content": "old"}

            def update_dm(self, values):
                updated_message["values"] = values

            def update(self, session=None):
                updated_message["update_session"] = session

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=returned_message,
        ) as get_user_message, mock.patch.object(
            dm_helpers,
            "_get_workspace_message_for_author",
            return_value=ExistingMessage(),
        ) as get_author_message, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_messages",
            return_value=[other_message, returned_message],
        ) as get_user_messages, mock.patch.object(
            dm_helpers.messenger_events, "create_message_updated_event"
        ) as create_event:
            result = dm_helpers.update_workspace_user_message(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                values={
                    "payload": {"kind": "markdown", "content": "edited"},
                },
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual(
            {"payload": {"kind": "markdown", "content": "edited"}},
            updated_message["values"],
        )
        self.assertIs(session, updated_message["update_session"])
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        get_author_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        get_user_messages.assert_called_once_with(
            project_id=project_id,
            message_uuid=message_uuid,
        )
        create_event.assert_has_calls(
            [
                mock.call(message=other_message, session=session),
                mock.call(message=returned_message, session=session),
            ]
        )

    def test_update_workspace_user_message_skips_matching_payload(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()

        class ExistingMessage:
            payload = {"kind": "markdown", "content": "edited"}

            def update_dm(self, values):
                raise AssertionError("message must not be changed")

            def update(self, session=None):
                raise AssertionError("message must not be saved")

        existing_message = ExistingMessage()

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            mock.Mock(),
        ) as get_user_message, mock.patch.object(
            dm_helpers,
            "_get_workspace_message_for_author",
            return_value=existing_message,
        ) as get_author_message, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_messages",
            mock.Mock(),
        ) as get_user_messages, mock.patch.object(
            dm_helpers.messenger_events, "create_message_updated_event"
        ) as create_event:
            result = dm_helpers.update_workspace_user_message(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                values={
                    "payload": {"kind": "markdown", "content": "edited"},
                },
                session=session,
                enforce_visibility=False,
            )

        self.assertIs(existing_message, result)
        get_user_message.assert_not_called()
        get_author_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        get_user_messages.assert_not_called()
        create_event.assert_not_called()

    def test_update_workspace_message_source_sends_events(self):
        project_id = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        old_source = dm_helpers.models.ZulipSource(
            stream_id=10,
            server_url="https://zulip.example.com",
            topic_name="general",
            message_id=None,
        )
        new_source = dm_helpers.models.ZulipSource(
            stream_id=10,
            server_url="https://zulip.example.com",
            topic_name="general",
            message_id=12345,
        )
        user_messages = [types.SimpleNamespace(), types.SimpleNamespace()]
        updated_message = {}

        class ExistingMessage:
            def update_dm(self, values):
                updated_message["values"] = values

            def update(self, session=None):
                updated_message["session"] = session

        message = ExistingMessage()
        message.uuid = message_uuid
        message.project_id = project_id
        message.source = old_source
        with mock.patch.object(
            dm_helpers,
            "_get_workspace_user_messages",
            return_value=user_messages,
        ) as get_user_messages, mock.patch.object(
            dm_helpers.messenger_events,
            "create_message_updated_event",
        ) as create_event:
            result = dm_helpers.update_workspace_message_source(
                message=message,
                source=new_source,
                session=session,
            )

        self.assertIs(message, result)
        self.assertEqual({"source": new_source}, updated_message["values"])
        self.assertIs(session, updated_message["session"])
        get_user_messages.assert_called_once_with(
            project_id=project_id,
            message_uuid=message_uuid,
        )
        create_event.assert_has_calls(
            [
                mock.call(message=user_messages[0], session=session),
                mock.call(message=user_messages[1], session=session),
            ]
        )

    def test_update_workspace_message_source_skips_matching_source(self):
        source = dm_helpers.models.ZulipSource(
            stream_id=10,
            server_url="https://zulip.example.com",
            topic_name="general",
            message_id=12345,
        )

        class ExistingMessage:
            def update_dm(self, values):
                raise AssertionError("message must not be changed")

            def update(self, session=None):
                raise AssertionError("message must not be saved")

        message = ExistingMessage()
        message.source = source
        with mock.patch.object(
            dm_helpers,
            "_create_workspace_message_updated_events",
        ) as create_events:
            result = dm_helpers.update_workspace_message_source(
                message=message,
                source=source,
            )

        self.assertIs(message, result)
        create_events.assert_not_called()

    def test_get_workspace_user_message_uuids_scopes_visible_messages(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        first_message_uuid = sys_uuid.uuid4()
        second_message_uuid = sys_uuid.uuid4()

        class FakeWorkspaceUserMessage:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(
                    return_value=[
                        types.SimpleNamespace(uuid=first_message_uuid),
                        types.SimpleNamespace(uuid=second_message_uuid),
                    ]
                )
            )

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessage",
            FakeWorkspaceUserMessage,
        ):
            result = dm_helpers.get_workspace_user_message_uuids(
                project_id=project_id,
                user_uuid=user_uuid,
            )

        self.assertEqual([first_message_uuid, second_message_uuid], result)
        filters = FakeWorkspaceUserMessage.objects.get_all.call_args.kwargs[
            "filters"
        ]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(user_uuid, filters["user_uuid"].value)
        self.assertNotIn(
            "session",
            FakeWorkspaceUserMessage.objects.get_all.call_args.kwargs,
        )

    def test_create_workspace_message_updated_events_sends_snapshots(self):
        project_id = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        first_message = object()
        second_message = object()

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_user_messages",
            return_value=[first_message, second_message],
        ) as get_user_messages, mock.patch.object(
            dm_helpers.messenger_events,
            "create_message_updated_event",
        ) as create_event:
            dm_helpers._create_workspace_message_updated_events(
                project_id=project_id,
                message_uuid=message_uuid,
                session=session,
            )

        get_user_messages.assert_called_once_with(
            project_id=project_id,
            message_uuid=message_uuid,
        )
        create_event.assert_has_calls(
            [
                mock.call(message=first_message, session=session),
                mock.call(message=second_message, session=session),
            ]
        )

    def test_create_workspace_message_reaction_checks_message_and_inserts(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        reaction_uuid = sys_uuid.uuid4()
        session = object()
        created_reaction = {}

        class FakeWorkspaceMessageReactions:
            def __init__(self, **kwargs):
                created_reaction.update(kwargs)

            def insert(self, session=None):
                created_reaction["insert_session"] = session

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=object(),
        ) as get_user_message, mock.patch.object(
            dm_helpers.models,
            "WorkspaceMessageReactions",
            FakeWorkspaceMessageReactions,
        ), mock.patch.object(
            dm_helpers,
            "_create_workspace_message_updated_events",
        ) as create_events:
            with mock.patch.object(
                dm_helpers.messenger_events,
                "create_message_reaction_created_event",
            ) as create_reaction_event:
                result = dm_helpers.create_workspace_message_reaction(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    uuid=reaction_uuid,
                    message_uuid=message_uuid,
                    emoji_name="thumbs_up",
                    session=session,
                )

        self.assertIsInstance(result, FakeWorkspaceMessageReactions)
        self.assertEqual(reaction_uuid, created_reaction["uuid"])
        self.assertEqual(project_id, created_reaction["project_id"])
        self.assertEqual(user_uuid, created_reaction["user_uuid"])
        self.assertEqual(message_uuid, created_reaction["message_uuid"])
        self.assertEqual("thumbs_up", created_reaction["emoji_name"])
        self.assertNotIn("status", created_reaction)
        self.assertIs(session, created_reaction["insert_session"])
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        create_reaction_event.assert_called_once_with(
            reaction=result,
            message=get_user_message.return_value,
            session=session,
        )
        create_events.assert_called_once_with(
            project_id=project_id,
            message_uuid=message_uuid,
            session=session,
        )

    def test_update_workspace_message_reaction_scopes_and_checks_new_message(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        reaction_uuid = sys_uuid.uuid4()
        old_message_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        updated_reaction = {}

        class ExistingReaction:
            def __init__(self):
                self.message_uuid = old_message_uuid
                self.emoji_name = "thumbs_up"

            def update_dm(self, values):
                updated_reaction["values"] = values

            def update(self, session=None):
                updated_reaction["update_session"] = session

        existing_reaction = ExistingReaction()
        old_message = object()
        new_message = object()

        class FakeWorkspaceMessageReactions:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_reaction)
            )

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceMessageReactions",
            FakeWorkspaceMessageReactions,
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            side_effect=[old_message, new_message],
        ) as get_user_message, mock.patch.object(
            dm_helpers,
            "_create_workspace_message_updated_events",
        ) as create_events, mock.patch.object(
            dm_helpers.messenger_events,
            "create_message_reaction_updated_event",
        ) as create_reaction_event:
            result = dm_helpers.update_workspace_message_reaction(
                project_id=project_id,
                user_uuid=user_uuid,
                reaction_uuid=reaction_uuid,
                values={
                    "message_uuid": message_uuid,
                    "emoji_name": "heart",
                    "project_id": sys_uuid.uuid4(),
                    "user_uuid": sys_uuid.uuid4(),
                    "uuid": sys_uuid.uuid4(),
                },
                session=session,
            )

        self.assertIs(existing_reaction, result)
        filters = FakeWorkspaceMessageReactions.objects.get_one.call_args.kwargs[
            "filters"
        ]
        self.assertEqual(reaction_uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(user_uuid, filters["user_uuid"].value)
        self.assertEqual(
            {
                "message_uuid": message_uuid,
                "emoji_name": "heart",
            },
            updated_reaction["values"],
        )
        self.assertIs(session, updated_reaction["update_session"])
        get_user_message.assert_has_calls([
            mock.call(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=old_message_uuid,
            ),
            mock.call(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
            ),
        ])
        create_reaction_event.assert_called_once_with(
            reaction=existing_reaction,
            message=new_message,
            old_message=old_message,
            old_emoji_name="thumbs_up",
            session=session,
        )
        create_events.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    message_uuid=old_message_uuid,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    message_uuid=message_uuid,
                    session=session,
                ),
            ]
        )

    def test_delete_workspace_message_reaction_scopes_and_deletes(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        reaction_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        deleted_reaction = {}

        class ExistingReaction:
            def __init__(self):
                self.uuid = reaction_uuid
                self.message_uuid = message_uuid
                self.user_uuid = user_uuid

            def delete(self, session=None):
                deleted_reaction["delete_session"] = session

        class FakeWorkspaceMessageReactions:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingReaction())
            )

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceMessageReactions",
            FakeWorkspaceMessageReactions,
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=object(),
        ) as get_user_message, mock.patch.object(
            dm_helpers,
            "_create_workspace_message_updated_events",
        ) as create_events, mock.patch.object(
            dm_helpers.messenger_events,
            "create_message_reaction_deleted_event",
        ) as create_reaction_event:
            result = dm_helpers.delete_workspace_message_reaction(
                project_id=project_id,
                user_uuid=user_uuid,
                reaction_uuid=reaction_uuid,
                session=session,
            )

        self.assertIsNone(result)
        filters = FakeWorkspaceMessageReactions.objects.get_one.call_args.kwargs[
            "filters"
        ]
        self.assertEqual(reaction_uuid, filters["uuid"].value)
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(user_uuid, filters["user_uuid"].value)
        self.assertIs(session, deleted_reaction["delete_session"])
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        create_reaction_event.assert_called_once_with(
            reaction=mock.ANY,
            message=get_user_message.return_value,
            session=session,
        )
        create_events.assert_called_once_with(
            project_id=project_id,
            message_uuid=message_uuid,
            session=session,
        )
    def test_read_workspace_user_messages_updates_flags_and_returns_ids(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid_1 = sys_uuid.uuid4()
        topic_uuid_2 = sys_uuid.uuid4()
        message_uuid_1 = sys_uuid.uuid4()
        message_uuid_2 = sys_uuid.uuid4()
        session = object()
        message_1 = types.SimpleNamespace(
            uuid=message_uuid_1,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid_1,
        )
        message_2 = types.SimpleNamespace(
            uuid=message_uuid_2,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid_2,
        )
        class ExistingFlags:
            def __init__(self):
                self.values = None
                self.update_session = None

            def update_dm(self, values):
                self.values = values

            def update(self, session=None):
                self.update_session = session

        flags = [ExistingFlags(), ExistingFlags()]

        class FakeWorkspaceUserMessageFlags:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(side_effect=flags)
            )

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_messages_read_event"
        ) as create_event:
            stream_uuid_result, topic_uuids, message_uuids = (
                dm_helpers._read_workspace_user_messages(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    messages=[message_1, message_2],
                    session=session,
                )
            )

        self.assertEqual(stream_uuid, stream_uuid_result)
        self.assertEqual([topic_uuid_1, topic_uuid_2], topic_uuids)
        self.assertEqual([message_uuid_1, message_uuid_2], message_uuids)
        self.assertEqual({"read": True}, flags[0].values)
        self.assertEqual({"read": True}, flags[1].values)
        self.assertIs(session, flags[0].update_session)
        self.assertIs(session, flags[1].update_session)
        self.assertEqual(2, FakeWorkspaceUserMessageFlags.objects.get_one.call_count)
        first_filters = (
            FakeWorkspaceUserMessageFlags.objects.get_one
            .call_args_list[0]
            .kwargs["filters"]
        )
        second_filters = (
            FakeWorkspaceUserMessageFlags.objects.get_one
            .call_args_list[1]
            .kwargs["filters"]
        )
        self.assertEqual(message_uuid_1, first_filters["uuid"].value)
        self.assertEqual(message_uuid_2, second_filters["uuid"].value)
        create_event.assert_not_called()

    def test_read_workspace_user_stream_messages_reads_all_unread_messages(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid_1 = sys_uuid.uuid4()
        topic_uuid_2 = sys_uuid.uuid4()
        session = object()
        initial_stream = types.SimpleNamespace(uuid=stream_uuid)
        returned_stream = types.SimpleNamespace(uuid=stream_uuid, unread_count=0)
        unread_messages = [object()]

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            side_effect=[initial_stream, returned_stream],
        ) as get_stream, mock.patch.object(
            dm_helpers,
            "_get_unread_workspace_user_messages",
            return_value=unread_messages,
        ) as get_unread, mock.patch.object(
            dm_helpers,
            "_read_workspace_user_messages",
            return_value=(
                stream_uuid,
                [topic_uuid_1, topic_uuid_2],
                [sys_uuid.uuid4()],
            ),
        ) as read_messages, mock.patch.object(
            dm_helpers.messenger_events, "create_stream_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers, "_create_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.read_workspace_user_stream_messages(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                session=session,
            )

        self.assertIs(returned_stream, result)
        get_stream.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    stream_uuid=stream_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    stream_uuid=stream_uuid,
                ),
            ]
        )
        get_unread.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        read_messages.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            messages=unread_messages,
            session=session,
        )
        create_read_event.assert_called_once_with(
            stream=returned_stream,
            session=session,
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuids=[topic_uuid_1, topic_uuid_2],
            session=session,
        )

    def test_read_workspace_user_stream_topic_messages_reads_topic_messages(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        initial_topic = types.SimpleNamespace(
            uuid=topic_uuid,
            stream_uuid=stream_uuid,
        )
        returned_topic = types.SimpleNamespace(uuid=topic_uuid, unread_count=0)
        unread_messages = [object()]

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream_topic",
            side_effect=[initial_topic, returned_topic],
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "_get_unread_workspace_user_messages",
            return_value=unread_messages,
        ) as get_unread, mock.patch.object(
            dm_helpers,
            "_read_workspace_user_messages",
            return_value=(stream_uuid, [topic_uuid], [sys_uuid.uuid4()]),
        ) as read_messages, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers, "_create_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.read_workspace_user_stream_topic_messages(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
                session=session,
            )

        self.assertIs(returned_topic, result)
        get_topic.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                ),
            ]
        )
        get_unread.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        )
        read_messages.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            messages=unread_messages,
            session=session,
        )
        create_read_event.assert_called_once_with(
            topic=returned_topic,
            session=session,
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuids=[topic_uuid],
            session=session,
        )

    def test_read_workspace_user_message_sets_flag_and_updates_counts(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        current_message = types.SimpleNamespace(
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        )
        returned_message = types.SimpleNamespace(read=True)
        updated_flag = {}

        class ExistingFlags:
            read = False

            def update_dm(self, values):
                updated_flag["values"] = values
                self.read = values["read"]

            def update(self, session=None):
                updated_flag["update_session"] = session

        class FakeWorkspaceUserMessageFlags:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingFlags())
            )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            side_effect=[current_message, returned_message],
        ) as get_user_message, mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers, "_create_message_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.read_workspace_user_message(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual({"read": True}, updated_flag["values"])
        self.assertIs(session, updated_flag["update_session"])
        get_user_message.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    message_uuid=message_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    message_uuid=message_uuid,
                ),
            ]
        )
        FakeWorkspaceUserMessageFlags.objects.get_one.assert_called_once()
        create_read_event.assert_called_once_with(
            message=returned_message,
            session=session,
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )

    def test_sync_workspace_user_message_flags_marks_message_unread(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        author_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        current_message = types.SimpleNamespace(
            author_uuid=author_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        )
        returned_message = types.SimpleNamespace(read=False)
        updated_flag = {}

        class ExistingFlags:
            read = True
            starred = False
            pinned = False

            def update_dm(self, values):
                updated_flag["values"] = values
                self.read = values["read"]

            def update(self, session=None):
                updated_flag["update_session"] = session

        class FakeWorkspaceUserMessageFlags:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingFlags())
            )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            side_effect=[current_message, returned_message],
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers.messenger_events, "create_message_updated_event"
        ) as create_updated_event, mock.patch.object(
            dm_helpers, "_create_message_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.sync_workspace_user_message_flags(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                values={"read": False},
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual({"read": False}, updated_flag["values"])
        self.assertIs(session, updated_flag["update_session"])
        create_read_event.assert_not_called()
        create_updated_event.assert_called_once_with(
            message=returned_message,
            session=session,
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )

    def test_sync_workspace_user_message_flags_keeps_own_message_read(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        current_message = types.SimpleNamespace(
            author_uuid=user_uuid,
            read=True,
        )
        existing_flags = types.SimpleNamespace(
            read=True,
            starred=False,
            pinned=False,
        )

        class FakeWorkspaceUserMessageFlags:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=existing_flags)
            )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=current_message,
        ) as get_user_message, mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers.messenger_events, "create_message_updated_event"
        ) as create_updated_event, mock.patch.object(
            dm_helpers, "_create_message_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.sync_workspace_user_message_flags(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                values={"read": False},
                session=session,
            )

        self.assertIs(current_message, result)
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        create_read_event.assert_not_called()
        create_updated_event.assert_not_called()
        create_unread_events.assert_not_called()

    def test_sync_workspace_user_message_flags_marks_message_read(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        current_message = types.SimpleNamespace(
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
        )
        returned_message = types.SimpleNamespace(read=True)
        updated_flag = {}

        class ExistingFlags:
            read = False
            starred = False
            pinned = False

            def update_dm(self, values):
                updated_flag["values"] = values
                self.read = values["read"]

            def update(self, session=None):
                updated_flag["update_session"] = session

        class FakeWorkspaceUserMessageFlags:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingFlags())
            )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            side_effect=[current_message, returned_message],
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers.messenger_events, "create_message_updated_event"
        ) as create_updated_event, mock.patch.object(
            dm_helpers, "_create_message_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.sync_workspace_user_message_flags(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                values={"read": True},
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual({"read": True}, updated_flag["values"])
        create_read_event.assert_called_once_with(
            message=returned_message,
            session=session,
        )
        create_updated_event.assert_not_called()
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )

    def test_sync_workspace_user_message_flags_updates_starred_flag(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        current_message = types.SimpleNamespace()
        returned_message = types.SimpleNamespace(starred=True)
        updated_flag = {}

        class ExistingFlags:
            read = False
            starred = False
            pinned = False

            def update_dm(self, values):
                updated_flag["values"] = values
                self.starred = values["starred"]

            def update(self, session=None):
                updated_flag["update_session"] = session

        class FakeWorkspaceUserMessageFlags:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=ExistingFlags())
            )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            side_effect=[current_message, returned_message],
        ), mock.patch.object(
            dm_helpers.models,
            "WorkspaceUserMessageFlags",
            FakeWorkspaceUserMessageFlags,
        ), mock.patch.object(
            dm_helpers.messenger_events, "create_message_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers.messenger_events, "create_message_updated_event"
        ) as create_updated_event, mock.patch.object(
            dm_helpers, "_create_message_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.sync_workspace_user_message_flags(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                values={"starred": True},
                session=session,
            )

        self.assertIs(returned_message, result)
        self.assertEqual({"starred": True}, updated_flag["values"])
        self.assertIs(session, updated_flag["update_session"])
        create_read_event.assert_not_called()
        create_updated_event.assert_called_once_with(
            message=returned_message,
            session=session,
        )
        create_unread_events.assert_not_called()

    def test_read_workspace_user_topic_messages_to_message_reads_to_current(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        created_at = object()
        session = object()
        current_message = types.SimpleNamespace(
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            created_at=created_at,
        )
        returned_message = types.SimpleNamespace(read=True)
        unread_messages = [object()]

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            side_effect=[current_message, returned_message],
        ) as get_user_message, mock.patch.object(
            dm_helpers,
            "_get_unread_workspace_user_messages",
            return_value=unread_messages,
        ) as get_unread, mock.patch.object(
            dm_helpers,
            "_read_workspace_user_messages",
            return_value=(stream_uuid, [topic_uuid], [message_uuid]),
        ) as read_messages, mock.patch.object(
            dm_helpers.messenger_events, "create_message_read_event"
        ) as create_read_event, mock.patch.object(
            dm_helpers, "_create_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.read_workspace_user_topic_messages_to_message(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                session=session,
            )

        self.assertIs(returned_message, result)
        get_user_message.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    message_uuid=message_uuid,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    message_uuid=message_uuid,
                ),
            ]
        )
        get_unread.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            created_at=created_at,
        )
        read_messages.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            messages=unread_messages,
            session=session,
        )
        create_read_event.assert_called_once_with(
            message=returned_message,
            session=session,
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
            topic_uuids=[topic_uuid],
            session=session,
        )

    def test_create_message_unread_updated_events_sends_aggregate_snapshots(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        custom_folder_uuid = sys_uuid.uuid4()
        session = object()
        user_topic = types.SimpleNamespace(uuid=topic_uuid, user_uuid=user_uuid)
        user_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            private=False,
        )
        all_folder = types.SimpleNamespace(uuid=dm_helpers.ALL_CHATS_FOLDER_UUID)
        channels_folder = types.SimpleNamespace(uuid=dm_helpers.CHANNELS_FOLDER_UUID)
        custom_folder = types.SimpleNamespace(uuid=custom_folder_uuid)
        custom_item = types.SimpleNamespace(
            user_uuid=user_uuid,
            folder_uuid=custom_folder_uuid,
        )

        class FakeFolderItem:
            objects = types.SimpleNamespace(
                get_all=mock.Mock(return_value=[custom_item])
            )

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream_topic",
            return_value=user_topic,
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "get_workspace_user_stream",
            return_value=user_stream,
        ) as get_stream, mock.patch.object(
            dm_helpers.models, "FolderItem", FakeFolderItem
        ), mock.patch.object(
            dm_helpers,
            "get_workspace_user_folder",
            side_effect=[all_folder, channels_folder, custom_folder],
        ) as get_folder, mock.patch.object(
            dm_helpers.messenger_events, "create_topic_updated_event"
        ) as create_topic_event, mock.patch.object(
            dm_helpers.messenger_events, "create_stream_updated_event"
        ) as create_stream_event, mock.patch.object(
            dm_helpers.messenger_events, "create_folder_updated_event"
        ) as create_folder_event:
            dm_helpers._create_message_unread_updated_events(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                topic_uuid=topic_uuid,
                session=session,
            )

        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
        )
        get_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        FakeFolderItem.objects.get_all.assert_called_once()
        filters = FakeFolderItem.objects.get_all.call_args.kwargs["filters"]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(user_uuid, filters["user_uuid"].value)
        self.assertEqual(stream_uuid, filters["stream_uuid"].value)
        self.assertEqual(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=custom_folder_uuid,
                ),
            ],
            get_folder.call_args_list,
        )
        create_topic_event.assert_called_once_with(
            topic=user_topic,
            session=session,
        )
        create_stream_event.assert_called_once_with(
            stream=user_stream,
            session=session,
        )
        create_folder_event.assert_has_calls(
            [
                mock.call(folder=all_folder, session=session),
                mock.call(folder=channels_folder, session=session),
                mock.call(folder=custom_folder, session=session),
            ]
        )

    def test_delete_workspace_user_message_deletes_root_and_sends_events(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        session = object()
        deleted_message = {}
        source = dm_helpers.models.NativeSource()
        current_message = types.SimpleNamespace(
            user_uuid=user_uuid,
            read=True,
        )
        other_message = types.SimpleNamespace(
            user_uuid=other_user_uuid,
            read=False,
        )

        class ExistingMessage:
            def __init__(self):
                self.user_uuid = user_uuid
                self.stream_uuid = stream_uuid
                self.topic_uuid = topic_uuid
                self.source_name = dm_helpers.models.SourceName.NATIVE.value
                self.source = source

            def delete(self, session=None):
                deleted_message["delete_session"] = session

        with mock.patch.object(
            dm_helpers,
            "get_workspace_user_message",
            return_value=current_message,
        ) as get_user_message, mock.patch.object(
            dm_helpers,
            "_get_workspace_message_for_author",
            return_value=ExistingMessage(),
        ) as get_author_message, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_messages",
            return_value=[current_message, other_message],
        ) as get_user_messages, mock.patch.object(
            dm_helpers.messenger_events, "create_message_deleted_event"
        ) as create_deleted_event, mock.patch.object(
            dm_helpers, "_create_messages_unread_updated_events"
        ) as create_unread_events:
            result = dm_helpers.delete_workspace_user_message(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertIs(session, deleted_message["delete_session"])
        get_user_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        get_author_message.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            message_uuid=message_uuid,
        )
        get_user_messages.assert_called_once_with(
            project_id=project_id,
            message_uuid=message_uuid,
        )
        create_deleted_event.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    message_uuid=message_uuid,
                    stream_uuid=stream_uuid,
                    topic_uuid=topic_uuid,
                    author_uuid=user_uuid,
                    source_name="native",
                    source=source,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    message_uuid=message_uuid,
                    stream_uuid=stream_uuid,
                    topic_uuid=topic_uuid,
                    author_uuid=user_uuid,
                    source_name="native",
                    source=source,
                    session=session,
                ),
            ]
        )
        create_unread_events.assert_called_once_with(
            project_id=project_id,
            user_uuids=[other_user_uuid],
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )

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
            dm_helpers, "get_workspace_user_stream", return_value=object()
        ) as get_user_stream, mock.patch.object(
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
        get_user_stream.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            stream_uuid=stream_uuid,
        )
        get_user_folder.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            folder_uuid=folder_uuid,
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )
        get_user_folder_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
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
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )
        get_user_folder_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
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
        )
        create_event.assert_called_once_with(
            folder=returned_folder,
            session=session,
        )
        get_user_folder_item.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            item_uuid=item_uuid,
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

    def test_create_workspace_file_creates_stream_accesses(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        file_uuid = sys_uuid.uuid4()
        session = object()
        created_file = {}

        class FakeWorkspaceFile:
            def __init__(self, **kwargs):
                created_file.update(kwargs)
                self.uuid = kwargs["uuid"]

            def insert(self, session=None):
                created_file["insert_session"] = session

        storage_info = dm_helpers.file_storage.WorkspaceFileStorageInfo(
            storage_type="file",
            storage_id="",
            storage_object_id="aa/file",
        )

        with mock.patch.object(
            dm_helpers.models, "WorkspaceFile", FakeWorkspaceFile
        ), mock.patch.object(
            dm_helpers.file_storage,
            "get_workspace_file_storage_info",
            return_value=storage_info,
        ) as get_storage_info, mock.patch.object(
            dm_helpers.models,
            "get_stream_recipients",
            return_value=[user_uuid, other_user_uuid],
        ) as get_recipients, mock.patch.object(
            dm_helpers, "get_or_create_workspace_file_access"
        ) as get_access:
            result = dm_helpers.create_workspace_file(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=file_uuid,
                stream_uuid=stream_uuid,
                name="example.txt",
                description="Example",
                content_type="text/plain",
                size_bytes=12,
                hash="abc",
                session=session,
            )

        self.assertEqual(file_uuid, result.uuid)
        self.assertEqual(project_id, created_file["project_id"])
        self.assertEqual(user_uuid, created_file["user_uuid"])
        self.assertEqual(stream_uuid, created_file["stream_uuid"])
        self.assertEqual("example.txt", created_file["name"])
        self.assertEqual("abc", created_file["hash"])
        self.assertEqual("file", created_file["storage_type"])
        self.assertEqual("", created_file["storage_id"])
        self.assertEqual("aa/file", created_file["storage_object_id"])
        self.assertIs(session, created_file["insert_session"])
        get_storage_info.assert_called_once_with(
            file_uuid=file_uuid,
            storage_type=None,
            storage_object_id=None,
        )
        get_recipients.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
            session=session,
        )
        get_access.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    file_uuid=file_uuid,
                    user_uuid=user_uuid,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    file_uuid=file_uuid,
                    user_uuid=other_user_uuid,
                    session=session,
                ),
            ]
        )
        self.assertEqual(2, get_access.call_count)

    def test_create_workspace_stream_binding_file_accesses_grants_stream_files(self):
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        first_file_uuid = sys_uuid.uuid4()
        second_file_uuid = sys_uuid.uuid4()
        session = object()
        files = [
            types.SimpleNamespace(uuid=first_file_uuid),
            types.SimpleNamespace(uuid=second_file_uuid),
        ]

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_stream_files",
            return_value=files,
        ) as get_files, mock.patch.object(
            dm_helpers, "get_or_create_workspace_file_access"
        ) as get_access:
            dm_helpers._create_workspace_stream_binding_file_accesses(
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
                session=session,
            )

        get_files.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
        )
        get_access.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    file_uuid=first_file_uuid,
                    user_uuid=user_uuid,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    file_uuid=second_file_uuid,
                    user_uuid=user_uuid,
                    session=session,
                ),
            ]
        )
        self.assertEqual(2, get_access.call_count)

    def test_delete_workspace_stream_binding_file_accesses_removes_stream_files(self):
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        first_file_uuid = sys_uuid.uuid4()
        second_file_uuid = sys_uuid.uuid4()
        session = object()
        files = [
            types.SimpleNamespace(uuid=first_file_uuid),
            types.SimpleNamespace(uuid=second_file_uuid),
        ]
        existing_access = mock.Mock()
        get_one_or_none = mock.Mock(side_effect=[existing_access, None])

        class FakeWorkspaceFileAccess:
            objects = types.SimpleNamespace(get_one_or_none=get_one_or_none)

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_stream_files",
            return_value=files,
        ) as get_files, mock.patch.object(
            dm_helpers.models,
            "WorkspaceFileAccess",
            FakeWorkspaceFileAccess,
        ):
            dm_helpers._delete_workspace_stream_binding_file_accesses(
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
                session=session,
            )

        get_files.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
        )
        self.assertEqual(2, get_one_or_none.call_count)
        first_filters = get_one_or_none.call_args_list[0].kwargs["filters"]
        second_filters = get_one_or_none.call_args_list[1].kwargs["filters"]
        self.assertEqual(project_id, first_filters["project_id"].value)
        self.assertEqual(first_file_uuid, first_filters["file_uuid"].value)
        self.assertEqual(user_uuid, first_filters["user_uuid"].value)
        self.assertEqual(project_id, second_filters["project_id"].value)
        self.assertEqual(second_file_uuid, second_filters["file_uuid"].value)
        self.assertEqual(user_uuid, second_filters["user_uuid"].value)
        existing_access.delete.assert_called_once_with(session=session)

    def test_get_or_create_workspace_file_access_returns_existing(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        file_uuid = sys_uuid.uuid4()
        session = object()
        existing_access = object()
        get_one_or_none = mock.Mock(return_value=existing_access)

        class FakeWorkspaceFileAccess:
            objects = types.SimpleNamespace(get_one_or_none=get_one_or_none)

            def __init__(self, **kwargs):
                raise AssertionError("access already exists")

        with mock.patch.object(
            dm_helpers.models, "WorkspaceFileAccess", FakeWorkspaceFileAccess
        ):
            result = dm_helpers.get_or_create_workspace_file_access(
                project_id=project_id,
                file_uuid=file_uuid,
                user_uuid=user_uuid,
                session=session,
            )

        self.assertIs(existing_access, result)
        get_one_or_none.assert_called_once()
        filters = get_one_or_none.call_args.kwargs["filters"]
        self.assertEqual(project_id, filters["project_id"].value)
        self.assertEqual(file_uuid, filters["file_uuid"].value)
        self.assertEqual(user_uuid, filters["user_uuid"].value)
        self.assertIs(session, get_one_or_none.call_args.kwargs["session"])

    def test_get_or_create_workspace_file_access_creates_missing(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        file_uuid = sys_uuid.uuid4()
        session = object()
        get_one_or_none = mock.Mock(return_value=None)
        created_access = {}

        class FakeWorkspaceFileAccess:
            objects = types.SimpleNamespace(get_one_or_none=get_one_or_none)

            def __init__(self, **kwargs):
                created_access.update(kwargs)

            def insert(self, session=None):
                created_access["insert_session"] = session

        with mock.patch.object(
            dm_helpers.models, "WorkspaceFileAccess", FakeWorkspaceFileAccess
        ):
            result = dm_helpers.get_or_create_workspace_file_access(
                project_id=project_id,
                file_uuid=file_uuid,
                user_uuid=user_uuid,
                session=session,
            )

        self.assertIsInstance(result, FakeWorkspaceFileAccess)
        self.assertEqual(project_id, created_access["project_id"])
        self.assertEqual(file_uuid, created_access["file_uuid"])
        self.assertEqual(user_uuid, created_access["user_uuid"])
        self.assertIs(session, created_access["insert_session"])

    def test_update_workspace_file_updates_owned_file(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        file_uuid = sys_uuid.uuid4()
        session = object()
        updated_file = {}

        class ExistingFile:
            def update_dm(self, values):
                updated_file["values"] = values

            def update(self, session=None):
                updated_file["update_session"] = session

        existing_file = ExistingFile()

        with mock.patch.object(
            dm_helpers,
            "get_workspace_owned_file",
            return_value=existing_file,
        ) as get_file:
            result = dm_helpers.update_workspace_file(
                project_id=project_id,
                user_uuid=user_uuid,
                file_uuid=file_uuid,
                values={"name": "renamed.txt"},
                session=session,
            )

        self.assertIs(existing_file, result)
        get_file.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            file_uuid=file_uuid,
        )
        self.assertEqual({"name": "renamed.txt"}, updated_file["values"])
        self.assertIs(session, updated_file["update_session"])

    def test_delete_workspace_file_deletes_owned_file(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        file_uuid = sys_uuid.uuid4()
        session = object()
        deleted_file = {}

        class ExistingFile:
            def delete(self, session=None):
                deleted_file["delete_session"] = session

        existing_file = ExistingFile()

        with mock.patch.object(
            dm_helpers,
            "get_workspace_owned_file",
            return_value=existing_file,
        ) as get_file:
            result = dm_helpers.delete_workspace_file(
                project_id=project_id,
                user_uuid=user_uuid,
                file_uuid=file_uuid,
                session=session,
            )

        self.assertIsNone(result)
        get_file.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            file_uuid=file_uuid,
        )
        self.assertIs(session, deleted_file["delete_session"])


if __name__ == "__main__":
    unittest.main()
