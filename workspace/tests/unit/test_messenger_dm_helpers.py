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

    def test_get_or_create_workspace_user_stream_creates_topic_event_and_returns_view(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        other_user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
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
            dm_helpers, "create_workspace_stream_topic_with_flags"
        ) as create_topic, mock.patch.object(
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
                session=session,
            )

        self.assertIs(returned_stream, result)
        self.assertEqual(stream_uuid, created_stream["uuid"])
        self.assertEqual(project_id, created_stream["project_id"])
        self.assertEqual(user_uuid, created_stream["user_uuid"])
        self.assertEqual("Engineering", created_stream["name"])
        self.assertEqual("native", created_stream["source_name"])
        self.assertIs(session, created_stream["insert_session"])
        self.assertEqual(project_id, created_binding["project_id"])
        self.assertEqual(stream_uuid, created_binding["stream_uuid"])
        self.assertEqual(user_uuid, created_binding["user_uuid"])
        self.assertEqual(user_uuid, created_binding["who_uuid"])
        self.assertEqual("owner", created_binding["role"])
        self.assertIs(session, created_binding["insert_session"])
        create_topic.assert_called_once_with(
            project_id=project_id,
            stream_uuid=stream_uuid,
            name="General Topic",
            default_for_stream_uuid=stream_uuid,
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
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                    session=session,
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
            dm_helpers, "create_workspace_stream_topic_with_flags"
        ) as create_topic, mock.patch.object(
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
            stream_uuid=stream_uuid,
            name="General Topic",
            default_for_stream_uuid=stream_uuid,
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
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=direct_user_uuid,
                    folder_uuid=dm_helpers.PERSONAL_FOLDER_UUID,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.ALL_CHATS_FOLDER_UUID,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.PERSONAL_FOLDER_UUID,
                    session=session,
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
            session=session,
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
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=dm_helpers.CHANNELS_FOLDER_UUID,
                    session=session,
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
        ) as create_events:
            result = dm_helpers.get_or_create_workspace_stream_binding(
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
                who_uuid=who_uuid,
                role=dm_helpers.models.WorkspaceStreamRole.MEMBER.value,
            )

        self.assertIs(existing, result)
        create_events.assert_not_called()
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
        create_batch_events.assert_called_once_with(
            bindings=created_bindings,
            session=None,
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
            session=session,
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
            session=session,
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
        )
        other_stream = types.SimpleNamespace(
            uuid=stream_uuid,
            user_uuid=other_user_uuid,
            private=True,
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
            session=session,
        )
        create_stream_deleted.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    stream_uuid=stream_uuid,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    stream_uuid=stream_uuid,
                    session=session,
                ),
            ]
        )
        self.assertEqual(5, get_user_folder.call_count)
        self.assertEqual(5, create_folder_updated.call_count)

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

        class FakeStreamBinding:
            objects = types.SimpleNamespace(
                get_one=mock.Mock(return_value=object())
            )

        with mock.patch.object(
            dm_helpers.models, "WorkspaceStreamBinding", FakeStreamBinding
        ), mock.patch.object(
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
        FakeStreamBinding.objects.get_one.assert_called_once()
        create_topic.assert_called_once_with(
            project_id=project_id,
            session=session,
            name="Planning",
            stream_uuid=stream_uuid,
        )
        get_topics.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
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
                values={"name": "Retros"},
                session=session,
            )

        self.assertIs(returned_topic, result)
        self.assertEqual({"name": "Retros"}, updated_topic["values"])
        self.assertIs(session, updated_topic["update_session"])
        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )
        get_topics.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
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
                self.stream_uuid = stream_uuid

            def delete(self, session=None):
                deleted_topic["delete_session"] = session

        with mock.patch.object(
            dm_helpers,
            "_get_workspace_stream_topic_for_user",
            return_value=ExistingTopic(),
        ) as get_topic, mock.patch.object(
            dm_helpers,
            "_get_workspace_user_stream_topics",
            return_value=[returned_topic, other_topic],
        ) as get_topics, mock.patch.object(
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
        get_topic.assert_called_once_with(
            project_id=project_id,
            user_uuid=user_uuid,
            topic_uuid=topic_uuid,
            session=session,
        )
        get_topics.assert_called_once_with(
            project_id=project_id,
            topic_uuid=topic_uuid,
            session=session,
        )
        create_event.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                    stream_uuid=stream_uuid,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    user_uuid=other_user_uuid,
                    topic_uuid=topic_uuid,
                    stream_uuid=stream_uuid,
                    session=session,
                ),
            ]
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
            session=session,
        )
        get_topics.assert_has_calls(
            [
                mock.call(
                    project_id=project_id,
                    topic_uuid=topic_uuid,
                    session=session,
                ),
                mock.call(
                    project_id=project_id,
                    topic_uuid=topic_uuid,
                    session=session,
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
