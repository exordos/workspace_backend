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

from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import exceptions as messenger_exc
from workspace.messenger_api.dm import helpers as dm_helpers
from workspace.messenger_api.dm import message_payloads


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

    def test_draft_payload_uses_workspace_markdown_limit(self):
        content = "x" * message_payloads.MARKDOWN_CONTENT_MAX_LENGTH

        payload = dm_helpers._normalize_workspace_draft_payload(
            {"kind": "markdown", "content": content}
        )

        self.assertEqual(content, payload.content)

    def test_draft_payload_rejects_content_over_workspace_markdown_limit(self):
        content = "x" * (message_payloads.MARKDOWN_CONTENT_MAX_LENGTH + 1)

        with self.assertRaises(ra_exc.ValidationErrorException):
            dm_helpers._normalize_workspace_draft_payload(
                {"kind": "markdown", "content": content}
            )

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
            dm_helpers.models,
            "WorkspaceProject",
            FakeWorkspaceProject,
        ):
            result = dm_helpers._get_workspace_event_project_ids()

        self.assertEqual([first_project_id, second_project_id], result)
        get_all.assert_called_once_with(order_by={"project_id": "asc"})

    def test_workspace_user_event_recipients_are_project_scoped(self):
        project_id = sys_uuid.uuid4()
        first_user_uuid = sys_uuid.uuid4()
        second_user_uuid = sys_uuid.uuid4()
        result = types.SimpleNamespace(
            fetchall=lambda: [
                {"user_uuid": first_user_uuid},
                {"user_uuid": second_user_uuid},
            ]
        )
        session = types.SimpleNamespace(execute=mock.Mock(return_value=result))

        recipients = dm_helpers._get_workspace_user_event_recipients(
            project_id,
            session=session,
        )

        self.assertEqual([first_user_uuid, second_user_uuid], recipients)
        statement, params = session.execute.call_args.args
        self.assertIn("messenger_project_users", statement)
        self.assertNotIn("m_folders", statement)
        self.assertEqual((project_id,), params)

    def test_provider_users_are_not_presence_timeout_candidates(self):
        get_all = mock.Mock(return_value=[])

        class FakeWorkspaceUser:
            objects = types.SimpleNamespace(get_all=get_all)

        with mock.patch.object(
            dm_helpers.models,
            "WorkspaceUser",
            FakeWorkspaceUser,
        ):
            dm_helpers._get_stale_workspace_users(object())

        filters = get_all.call_args.kwargs["filters"]
        self.assertEqual(
            dm_helpers.models.WorkspaceUserSource.IAM.value,
            filters.clauses[0]["source"].value,
        )

    def test_mark_stale_workspace_users_offline_skips_projects_without_users(self):
        with (
            mock.patch.object(
                dm_helpers,
                "_get_stale_workspace_users",
                return_value=[],
            ) as get_users,
            mock.patch.object(
                dm_helpers,
                "_get_workspace_event_project_ids",
            ) as get_project_ids,
        ):
            result = dm_helpers.mark_stale_workspace_users_offline()

        self.assertEqual([], result)
        get_users.assert_called_once()
        get_project_ids.assert_not_called()

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
                    dm_helpers.models.WorkspaceStreamRole.MEMBER.value: str(
                        sys_uuid.uuid4()
                    ),
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

    def test_topic_notification_mode_matrix_matches_stream_mode(self):
        shared_modes = ("mute", "default", "follow")
        for stream_mode in ("all_messages", "mentions_only", "muted"):
            for topic_mode in shared_modes:
                dm_helpers._validate_topic_notification_mode(
                    stream_notification_mode=stream_mode,
                    notification_mode=topic_mode,
                )

        dm_helpers._validate_topic_notification_mode(
            stream_notification_mode="muted",
            notification_mode="unmute",
        )

        with self.assertRaises(messenger_exc.InvalidTopicNotificationModeError):
            dm_helpers._validate_topic_notification_mode(
                stream_notification_mode="all_messages",
                notification_mode="unmute",
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
                for name, value in kwargs.items():
                    setattr(self, name, value)

            def insert(self, session=None):
                created_file["insert_session"] = session

        storage_info = dm_helpers.file_storage.WorkspaceFileStorageInfo(
            storage_type="file",
            storage_id="",
            storage_object_id="aa/file",
        )

        with (
            mock.patch.object(dm_helpers.models, "WorkspaceFile", FakeWorkspaceFile),
            mock.patch.object(
                dm_helpers.v2_models,
                "WorkspaceStream",
                mock.Mock(
                    objects=mock.Mock(
                        get_one=mock.Mock(
                            return_value=types.SimpleNamespace(
                                source_name="native", provider_uuid=None
                            )
                        )
                    )
                ),
            ),
            mock.patch.object(
                dm_helpers.file_storage,
                "get_workspace_file_storage_info",
                return_value=storage_info,
            ) as get_storage_info,
            mock.patch.object(
                dm_helpers.models,
                "get_stream_recipients",
                return_value=[user_uuid, other_user_uuid],
            ) as get_recipients,
            mock.patch.object(
                dm_helpers, "get_or_create_workspace_file_access"
            ) as get_access,
            mock.patch.object(
                dm_helpers.messenger_events,
                "create_file_created_events",
            ) as create_events,
        ):
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
        create_events.assert_called_once_with(
            result,
            [user_uuid, other_user_uuid],
            session=session,
        )

    def test_create_workspace_file_can_suppress_backfill_event(self):
        class FakeWorkspaceFile:
            def __init__(self, **kwargs):
                for name, value in kwargs.items():
                    setattr(self, name, value)

            def insert(self, session=None):
                pass

        with (
            mock.patch.object(dm_helpers.models, "WorkspaceFile", FakeWorkspaceFile),
            mock.patch.object(
                dm_helpers.v2_models,
                "WorkspaceStream",
                mock.Mock(
                    objects=mock.Mock(
                        get_one=mock.Mock(
                            return_value=types.SimpleNamespace(
                                source_name="native", provider_uuid=None
                            )
                        )
                    )
                ),
            ),
            mock.patch.object(
                dm_helpers.models,
                "get_stream_recipients",
                return_value=[],
            ),
            mock.patch.object(
                dm_helpers.messenger_events,
                "create_file_created_events",
            ) as create_events,
        ):
            dm_helpers.create_workspace_file(
                project_id=sys_uuid.uuid4(),
                user_uuid=sys_uuid.uuid4(),
                uuid=sys_uuid.uuid4(),
                stream_uuid=sys_uuid.uuid4(),
                name="history.txt",
                description="",
                content_type="text/plain",
                size_bytes=7,
                hash="history",
                storage_type="file",
                storage_id="",
                storage_object_id="external-content/sha256/hi/history",
                emit_events=False,
            )

        create_events.assert_not_called()

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

        with (
            mock.patch.object(
                dm_helpers,
                "_get_workspace_stream_files",
                return_value=files,
            ) as get_files,
            mock.patch.object(
                dm_helpers, "get_or_create_workspace_file_access"
            ) as get_access,
        ):
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
        recipients = [user_uuid]

        with (
            mock.patch.object(
                dm_helpers,
                "get_workspace_owned_file",
                return_value=existing_file,
            ) as get_file,
            mock.patch.object(
                dm_helpers,
                "_get_workspace_file_event_recipients",
                return_value=recipients,
            ),
            mock.patch.object(
                dm_helpers.messenger_events,
                "create_file_updated_events",
            ) as create_events,
        ):
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
        create_events.assert_called_once_with(
            existing_file,
            recipients,
            session=session,
        )

    def test_public_file_event_recipients_are_project_scoped(self):
        project_id = sys_uuid.uuid4()
        first = sys_uuid.uuid4()
        second = sys_uuid.uuid4()
        statements = []
        result = mock.Mock()
        result.fetchall.return_value = [
            {"user_uuid": first},
            {"user_uuid": second},
        ]
        session = mock.Mock()
        session.execute.side_effect = lambda statement, params: (
            statements.append((statement, params)) or result
        )
        file = mock.Mock(stream_uuid=None, user_uuid=first)

        recipients = dm_helpers._get_workspace_file_event_recipients(
            project_id, file, session=session
        )

        self.assertEqual(sorted([first, second], key=str), recipients)
        self.assertEqual((project_id,), statements[0][1])
        self.assertIn("messenger_project_users", statements[0][0])
        self.assertNotIn("m_folders", statements[0][0])
        self.assertNotIn("m_workspace_users", statements[0][0])

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
        existing_file.uuid = file_uuid
        existing_file.stream_uuid = sys_uuid.uuid4()
        recipients = [user_uuid]

        with (
            mock.patch.object(
                dm_helpers,
                "get_workspace_owned_file",
                return_value=existing_file,
            ) as get_file,
            mock.patch.object(
                dm_helpers,
                "_get_workspace_file_event_recipients",
                return_value=recipients,
            ),
            mock.patch.object(
                dm_helpers.messenger_events,
                "create_file_deleted_events",
            ) as create_events,
        ):
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
        create_events.assert_called_once_with(
            project_id,
            existing_file.stream_uuid,
            file_uuid,
            recipients,
            session=session,
        )

    def test_delete_workspace_avatar_file_emits_public_file_event(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        file_uuid = sys_uuid.uuid4()
        session = object()
        deleted_file = {}

        class ExistingFile:
            def delete(self, session=None):
                deleted_file["delete_session"] = session

        existing_file = ExistingFile()
        existing_file.uuid = file_uuid
        existing_file.project_id = project_id
        existing_file.stream_uuid = None
        recipients = [user_uuid, sys_uuid.uuid4()]

        class FakeWorkspaceFile:
            class objects:
                @staticmethod
                def get_one_or_none(**kwargs):
                    get_file(**kwargs)
                    return existing_file

        get_file = mock.Mock()
        with (
            mock.patch.object(
                dm_helpers.models,
                "WorkspaceFile",
                FakeWorkspaceFile,
            ),
            mock.patch.object(
                dm_helpers,
                "_get_workspace_file_event_recipients",
                return_value=recipients,
            ),
            mock.patch.object(
                dm_helpers.messenger_events,
                "create_file_deleted_events",
            ) as create_events,
        ):
            result = dm_helpers.delete_workspace_avatar_file(
                user_uuid=user_uuid,
                file_uuid=file_uuid,
                session=session,
            )

        self.assertIsNone(result)
        self.assertEqual(
            {
                "uuid": mock.ANY,
                "user_uuid": mock.ANY,
            },
            get_file.call_args.kwargs["filters"],
        )
        self.assertIs(session, get_file.call_args.kwargs["session"])
        self.assertIs(session, deleted_file["delete_session"])
        create_events.assert_called_once_with(
            project_id,
            None,
            file_uuid,
            recipients,
            session=session,
        )


if __name__ == "__main__":
    unittest.main()
