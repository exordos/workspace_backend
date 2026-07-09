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
import io
import queue
import types
import unittest.mock as mock
import uuid as sys_uuid

from PIL import Image as pil_image

from workspace.services.integration_bridge import agents


def test_zulip_outbound_event_state_restores_retry_at_with_timezone_offset():
    state = agents.models.ZulipOutboundEventState.restore_from_storage(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        epoch_version=55,
        external_account_uuid=sys_uuid.uuid4(),
        status="pending",
        attempts=1,
        next_retry_at="2026-07-07 17:41:45.043680+03:00",
        last_error=None,
    )

    assert state.next_retry_at == datetime.datetime(
        2026,
        7,
        7,
        14,
        41,
        45,
        43680,
        tzinfo=datetime.timezone.utc,
    )


class FakeZulipProcessedEntity:
    inserted = []
    objects = types.SimpleNamespace(get_one_or_none=mock.Mock(return_value=None))

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.update_dm = mock.Mock(side_effect=self._update_dm)
        self.update = mock.Mock()

    def _update_dm(self, values):
        self.__dict__.update(values)

    def insert(self):
        type(self).inserted.append(self)


class FakeZulipQueueState:
    def __init__(
        self,
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
        subscription_version=(
            agents.zulip_client.MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION
        ),
    ):
        self.queue_id = queue_id
        self.last_event_id = last_event_id
        self.last_message_id = last_message_id
        self.is_synced = is_synced
        self.subscription_version = subscription_version
        self.update_dm = mock.Mock(side_effect=self._update_dm)
        self.update = mock.Mock()

    def _update_dm(self, values):
        self.__dict__.update(values)


class FakeOutboundState:
    def __init__(
        self,
        epoch_version=55,
        project_id="project",
        external_account_uuid="account-uuid",
        status="pending",
        attempts=0,
        next_retry_at=None,
        last_error=None,
    ):
        self.epoch_version = epoch_version
        self.project_id = project_id
        self.external_account_uuid = external_account_uuid
        self.status = status
        self.attempts = attempts
        self.next_retry_at = next_retry_at
        self.last_error = last_error
        self.update_dm = mock.Mock(side_effect=self._update_dm)
        self.update = mock.Mock()

    def _update_dm(self, values):
        self.__dict__.update(values)


class FakeZulipHistorySyncTask:
    inserted = []
    deleted = []
    objects = types.SimpleNamespace(get_all=mock.Mock(return_value=[]))

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.update_dm = mock.Mock(side_effect=self._update_dm)
        self.update = mock.Mock()

    def _update_dm(self, values):
        self.__dict__.update(values)

    def insert(self):
        type(self).inserted.append(self)

    def delete(self):
        type(self).deleted.append(self)


class FakeZulipOutboundEventState:
    inserted = []
    objects = types.SimpleNamespace(get_one_or_none=mock.Mock(return_value=None))

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def insert(self):
        type(self).inserted.append(self)


def _patch_zulip_processed_entities(return_value=None):
    FakeZulipProcessedEntity.inserted = []
    FakeZulipProcessedEntity.objects = types.SimpleNamespace(
        get_one_or_none=mock.Mock(return_value=return_value),
    )
    return mock.patch.object(
        agents.models,
        "ZulipProcessedEntity",
        FakeZulipProcessedEntity,
    )


def test_bridge_cache_preprocesses_zulip_file_links():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    image_data = io.BytesIO()
    image = pil_image.new("RGB", (1280, 720))
    image.save(image_data, format="PNG")
    png_data = image_data.getvalue()
    external_account = types.SimpleNamespace(
        project_id=project_id,
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            credentials=types.SimpleNamespace(
                login="user@example.com",
                token="zulip-token",
            ),
        ),
    )
    stream = types.SimpleNamespace(uuid=stream_uuid)
    message_info = {
        "message_id": 42,
        "sender_id": 24,
        "content": (
            "see ![photo.png](/user_uploads/1/photo.png) "
            "and [site](https://example.com)"
        ),
    }
    storage_info = agents.file_storage.WorkspaceFileStorageInfo(
        storage_type="file",
        storage_id="",
        storage_object_id="aa/file",
    )
    created_file = types.SimpleNamespace(uuid=file_uuid)

    class FakeZulipClient:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def download_file_with_api_key(self, login, token, url):
            assert self.endpoint == "https://zulip.example.com"
            assert login == "user@example.com"
            assert token == "zulip-token"
            assert url == "/user_uploads/1/photo.png"
            return {
                "content": png_data,
                "content_type": "application/octet-stream",
            }

    cache = agents.WorkspaceIntegrationBridgeCache()
    cache._get_or_create_zulip_message_sender_uuid = mock.Mock(
        return_value=user_uuid,
    )

    with mock.patch.object(
        agents.zulip_client,
        "ZulipClient",
        FakeZulipClient,
    ), mock.patch.object(
        agents.file_storage,
        "save_workspace_file",
        return_value=storage_info,
    ) as save_file, mock.patch.object(
        agents.file_storage,
        "delete_workspace_file",
    ) as delete_file, mock.patch.object(
        agents.messenger_dm_helpers,
        "create_workspace_file",
        return_value=created_file,
    ) as create_file:
        result = cache.preprocess_zulip_message(
            external_account=external_account,
            stream=stream,
            message_info=message_info,
        )

    assert result["content"] == (
        f"see ![photo.png](urn:image:{file_uuid}?"
        f"name=photo.png&content_type=image%2Fpng&"
        f"w=1280&h=720&size={len(png_data)}) "
        "and [site](https://example.com)"
    )
    assert message_info["content"] == (
        "see ![photo.png](/user_uploads/1/photo.png) "
        "and [site](https://example.com)"
    )
    cache._get_or_create_zulip_message_sender_uuid.assert_called_once_with(
        external_account=external_account,
        message_info=message_info,
    )
    save_file.assert_called_once_with(file_uuid=mock.ANY, data=png_data)
    create_file.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        uuid=mock.ANY,
        stream_uuid=stream_uuid,
        name="photo.png",
        description="",
        content_type="image/png",
        size_bytes=len(png_data),
        hash=agents.hashlib.sha256(png_data).hexdigest(),
        storage_type="file",
        storage_id="",
        storage_object_id="aa/file",
    )
    assert create_file.call_args.kwargs["uuid"] == (
        save_file.call_args.kwargs["file_uuid"]
    )
    delete_file.assert_not_called()


def test_bridge_cache_uses_placeholder_when_zulip_file_import_fails():
    external_account = types.SimpleNamespace(
        project_id=sys_uuid.uuid4(),
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            credentials=types.SimpleNamespace(
                login="user@example.com",
                token="zulip-token",
            ),
        ),
    )
    stream = types.SimpleNamespace(uuid=sys_uuid.uuid4())
    message_info = {
        "message_id": 42,
        "sender_id": 24,
        "content": "see ![photo.png](/user_uploads/1/photo.png)",
    }

    class FakeZulipClient:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def download_file_with_api_key(self, login, token, url):
            raise RuntimeError("download boom")

    cache = agents.WorkspaceIntegrationBridgeCache()

    with mock.patch.object(
        agents.zulip_client,
        "ZulipClient",
        FakeZulipClient,
    ):
        result = cache.preprocess_zulip_message(
            external_account=external_account,
            stream=stream,
            message_info=message_info,
        )

    assert result["content"] == (
        "see ![photo.png]"
        "(urn:zulip-file:download-failed?"
        "name=photo.png&source_url=%2Fuser_uploads%2F1%2Fphoto.png&"
        "status=download_failed)"
    )
    assert cache.pop_file_import_errors() == [
        (
            "Failed to import Zulip file /user_uploads/1/photo.png "
            "for message 42: download boom"
        ),
    ]


def test_bridge_cache_uses_filetype_for_video_metadata():
    cache = agents.WorkspaceIntegrationBridgeCache()
    mp4_data = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"

    metadata = cache._get_zulip_file_metadata(
        file_name="clip.bin",
        header_content_type="application/octet-stream",
        data=mp4_data,
        url="/user_uploads/1/clip.bin?w=1920&h=1080",
    )

    assert metadata == {
        "content_type": "video/mp4",
        "width": 1920,
        "height": 1080,
        "size": len(mp4_data),
    }


def test_bridge_cache_updates_zulip_message_and_skips_outbound_event():
    project_id = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    external_account = types.SimpleNamespace(
        uuid=account_uuid,
        project_id=project_id,
        server_url="https://zulip.example.com",
    )
    message = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.uuid4(),
        stream_uuid=stream_uuid,
    )
    updated_message = types.SimpleNamespace(uuid=message_uuid)
    last_event = types.SimpleNamespace(epoch_version=70)
    update_event = types.SimpleNamespace(
        epoch_version=71,
        payload={"uuid": str(message_uuid)},
    )
    unrelated_event = types.SimpleNamespace(
        epoch_version=72,
        payload={"uuid": str(sys_uuid.uuid4())},
    )

    class FakeWorkspaceMessage:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=message),
        )

    class FakeWorkspaceEvent:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                side_effect=[
                    [last_event],
                    [update_event, unrelated_event],
                ],
            ),
        )

    FakeZulipOutboundEventState.inserted = []
    FakeZulipOutboundEventState.objects = types.SimpleNamespace(
        get_one_or_none=mock.Mock(return_value=None),
    )
    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=message_uuid),
    ), mock.patch.object(
        cache,
        "preprocess_zulip_message",
        mock.Mock(return_value={
            "message_id": 104,
            "sender_id": 8,
            "content": "edited workspace",
        }),
    ) as preprocess, mock.patch.object(
        agents.models,
        "WorkspaceMessage",
        FakeWorkspaceMessage,
    ), mock.patch.object(
        agents.models,
        "WorkspaceEvent",
        FakeWorkspaceEvent,
    ), mock.patch.object(
        agents.models,
        "ZulipOutboundEventState",
        FakeZulipOutboundEventState,
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "update_workspace_user_message",
        mock.Mock(return_value=updated_message),
    ) as update_workspace_message:
        result = cache.update_message(
            external_account=external_account,
            message_info={
                "message_id": 104,
                "sender_id": 8,
                "content": "edited zulip",
            },
        )

    assert result is updated_message
    preprocess.assert_called_once_with(
        external_account=external_account,
        stream=types.SimpleNamespace(uuid=stream_uuid),
        message_info={
            "message_id": 104,
            "sender_id": 8,
            "content": "edited zulip",
        },
    )
    update_workspace_message.assert_called_once()
    update_kwargs = update_workspace_message.call_args.kwargs
    assert update_kwargs["project_id"] == project_id
    assert update_kwargs["user_uuid"] == message.user_uuid
    assert update_kwargs["message_uuid"] == message_uuid
    assert update_kwargs["values"]["payload"].content == "edited workspace"
    cache_key = (
        project_id,
        "https://zulip.example.com",
        104,
    )
    assert cache._messages[cache_key] is updated_message
    assert len(FakeZulipOutboundEventState.inserted) == 1
    state = FakeZulipOutboundEventState.inserted[0]
    assert state.project_id == project_id
    assert state.epoch_version == 71
    assert state.external_account_uuid == account_uuid
    assert state.status == agents.OUTBOUND_SKIPPED_STATUS


def test_bridge_cache_deletes_zulip_message_and_skips_outbound_event():
    project_id = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    external_account = types.SimpleNamespace(
        uuid=account_uuid,
        project_id=project_id,
        server_url="https://zulip.example.com",
    )
    message = types.SimpleNamespace(
        uuid=message_uuid,
        user_uuid=sys_uuid.uuid4(),
    )
    last_event = types.SimpleNamespace(epoch_version=80)
    delete_event = types.SimpleNamespace(
        epoch_version=81,
        payload={"uuid": str(message_uuid)},
    )

    class FakeWorkspaceMessage:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=message),
        )

    class FakeWorkspaceEvent:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                side_effect=[
                    [last_event],
                    [delete_event],
                ],
            ),
        )

    FakeZulipOutboundEventState.inserted = []
    FakeZulipOutboundEventState.objects = types.SimpleNamespace(
        get_one_or_none=mock.Mock(return_value=None),
    )
    cache = agents.WorkspaceIntegrationBridgeCache()
    cache._messages[
        (
            project_id,
            "https://zulip.example.com",
            104,
        )
    ] = message
    with mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=message_uuid),
    ), mock.patch.object(
        agents.models,
        "WorkspaceMessage",
        FakeWorkspaceMessage,
    ), mock.patch.object(
        agents.models,
        "WorkspaceEvent",
        FakeWorkspaceEvent,
    ), mock.patch.object(
        agents.models,
        "ZulipOutboundEventState",
        FakeZulipOutboundEventState,
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "delete_workspace_user_message",
        mock.Mock(),
    ) as delete_workspace_message:
        results = cache.delete_messages(
            external_account=external_account,
            message_ids=[104],
        )

    assert results == [message]
    delete_workspace_message.assert_called_once_with(
        project_id=project_id,
        user_uuid=message.user_uuid,
        message_uuid=message_uuid,
    )
    cache_key = (
        project_id,
        "https://zulip.example.com",
        104,
    )
    assert cache_key not in cache._messages
    assert len(FakeZulipOutboundEventState.inserted) == 1
    state = FakeZulipOutboundEventState.inserted[0]
    assert state.project_id == project_id
    assert state.epoch_version == 81
    assert state.external_account_uuid == account_uuid
    assert state.status == agents.OUTBOUND_SKIPPED_STATUS


def test_bridge_cache_adds_zulip_reaction_and_skips_outbound_events():
    project_id = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    external_account = types.SimpleNamespace(
        uuid=account_uuid,
        project_id=project_id,
        server_url="https://zulip.example.com",
    )
    message = types.SimpleNamespace(uuid=message_uuid)
    reaction = types.SimpleNamespace(uuid=reaction_uuid)
    last_event = types.SimpleNamespace(epoch_version=90)
    reaction_event = types.SimpleNamespace(
        epoch_version=91,
        payload={"uuid": str(reaction_uuid)},
    )
    message_event = types.SimpleNamespace(
        epoch_version=92,
        payload={"uuid": str(message_uuid)},
    )

    class FakeWorkspaceMessage:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=message),
        )

    class FakeWorkspaceMessageReactions:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=None),
        )

    class FakeWorkspaceEvent:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                side_effect=[
                    [last_event],
                    [reaction_event],
                    [message_event],
                ],
            ),
        )

    FakeZulipOutboundEventState.inserted = []
    FakeZulipOutboundEventState.objects = types.SimpleNamespace(
        get_one_or_none=mock.Mock(return_value=None),
    )
    cache = agents.WorkspaceIntegrationBridgeCache()
    cache._get_required_zulip_user_uuid = mock.Mock(return_value=user_uuid)
    with mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=message_uuid),
    ), mock.patch.object(
        agents.models,
        "WorkspaceMessage",
        FakeWorkspaceMessage,
    ), mock.patch.object(
        agents.models,
        "WorkspaceMessageReactions",
        FakeWorkspaceMessageReactions,
    ), mock.patch.object(
        agents.models,
        "WorkspaceEvent",
        FakeWorkspaceEvent,
    ), mock.patch.object(
        agents.models,
        "ZulipOutboundEventState",
        FakeZulipOutboundEventState,
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "create_workspace_message_reaction",
        mock.Mock(return_value=reaction),
    ) as create_reaction:
        result = cache.add_message_reaction(
            external_account=external_account,
            reaction_info={
                "message_id": 104,
                "user_id": 24,
                "emoji_name": "thumbs_up",
            },
        )

    assert result is reaction
    create_reaction.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        emoji_name="thumbs_up",
    )
    assert [
        state.epoch_version
        for state in FakeZulipOutboundEventState.inserted
    ] == [91, 92]


def test_bridge_cache_removes_zulip_reaction_and_skips_outbound_events():
    project_id = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    external_account = types.SimpleNamespace(
        uuid=account_uuid,
        project_id=project_id,
        server_url="https://zulip.example.com",
    )
    message = types.SimpleNamespace(uuid=message_uuid)
    reaction = types.SimpleNamespace(uuid=reaction_uuid)
    last_event = types.SimpleNamespace(epoch_version=100)
    reaction_event = types.SimpleNamespace(
        epoch_version=101,
        payload={"uuid": str(reaction_uuid)},
    )
    message_event = types.SimpleNamespace(
        epoch_version=102,
        payload={"uuid": str(message_uuid)},
    )

    class FakeWorkspaceMessage:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=message),
        )

    class FakeWorkspaceMessageReactions:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=reaction),
        )

    class FakeWorkspaceEvent:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                side_effect=[
                    [last_event],
                    [reaction_event],
                    [message_event],
                ],
            ),
        )

    FakeZulipOutboundEventState.inserted = []
    FakeZulipOutboundEventState.objects = types.SimpleNamespace(
        get_one_or_none=mock.Mock(return_value=None),
    )
    cache = agents.WorkspaceIntegrationBridgeCache()
    cache._get_required_zulip_user_uuid = mock.Mock(return_value=user_uuid)
    with mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=message_uuid),
    ), mock.patch.object(
        agents.models,
        "WorkspaceMessage",
        FakeWorkspaceMessage,
    ), mock.patch.object(
        agents.models,
        "WorkspaceMessageReactions",
        FakeWorkspaceMessageReactions,
    ), mock.patch.object(
        agents.models,
        "WorkspaceEvent",
        FakeWorkspaceEvent,
    ), mock.patch.object(
        agents.models,
        "ZulipOutboundEventState",
        FakeZulipOutboundEventState,
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "delete_workspace_message_reaction",
        mock.Mock(),
    ) as delete_reaction:
        result = cache.remove_message_reaction(
            external_account=external_account,
            reaction_info={
                "message_id": 104,
                "user_id": 24,
                "emoji_name": "thumbs_up",
            },
        )

    assert result is reaction
    delete_reaction.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        reaction_uuid=reaction_uuid,
    )
    assert [
        state.epoch_version
        for state in FakeZulipOutboundEventState.inserted
    ] == [101, 102]


def test_outbound_event_filter_uses_author_event_only():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    author_event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="author-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=None),
        },
    )
    recipient_event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="recipient-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=None),
        },
    )
    native_event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="author-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "native",
            "source": types.SimpleNamespace(message_id=None),
        },
    )
    inbound_event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="author-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )
    reaction_event = types.SimpleNamespace(
        object_type="message_reaction",
        action="created",
        user_uuid="reaction-user",
        payload={
            "user_uuid": "reaction-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )
    recipient_reaction_event = types.SimpleNamespace(
        object_type="message_reaction",
        action="created",
        user_uuid="recipient-user",
        payload={
            "user_uuid": "reaction-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )

    assert worker._is_zulip_outbound_event(author_event)
    assert not worker._is_zulip_outbound_event(recipient_event)
    assert not worker._is_zulip_outbound_event(native_event)
    assert not worker._is_zulip_outbound_event(inbound_event)
    assert worker._is_zulip_outbound_event(reaction_event)
    assert not worker._is_zulip_outbound_event(recipient_reaction_event)


class FakeQueryResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def test_get_new_outbound_events_uses_candidate_json_filter():
    event_uuid = sys_uuid.uuid4()
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)
    session = types.SimpleNamespace(
        execute=mock.Mock(
            return_value=FakeQueryResult([
                {
                    "epoch_version": 56,
                    "uuid": event_uuid,
                    "project_id": project_id,
                    "user_uuid": user_uuid,
                    "payload": {
                        "author_uuid": str(user_uuid),
                        "source_name": "zulip",
                        "source": {"message_id": None},
                    },
                    "created_at": now,
                    "updated_at": now,
                    "schema_version": 1,
                    "object_type": "message",
                    "action": "created",
                },
            ]),
        ),
    )
    context = types.SimpleNamespace(get_session=mock.Mock(return_value=session))
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._last_outbound_event_epoch = 55

    with mock.patch.object(agents.contexts, "Context", return_value=context):
        events = worker._get_new_outbound_events()

    statement, values = session.execute.call_args.args
    assert "e.payload->>'source_name' = 'zulip'" in statement
    assert "e.user_uuid::text = e.payload->>'author_uuid'" in statement
    assert "e.object_type = 'message_reaction'" in statement
    assert "e.user_uuid::text = e.payload->>'user_uuid'" in statement
    assert "e.payload #>> '{source,message_id}' IS NULL" in statement
    assert "m_zulip_outbound_event_states" in statement
    assert values == (55, agents.DEFAULT_OUTBOUND_EVENTS_BATCH_LIMIT)
    assert len(events) == 1
    assert events[0].epoch_version == 56
    assert events[0].uuid == event_uuid


def test_dispatch_zulip_outbound_state_uses_author_worker_queue():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    command = object()
    state = FakeOutboundState()
    event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="author-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=None),
        },
    )
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="author-user",
        account_settings=types.SimpleNamespace(credentials=object()),
    )
    author_key = (
        "project",
        "https://zulip.example.com",
        "author-user",
    )
    other_key = (
        "project",
        "https://zulip.example.com",
        "other-user",
    )
    author_queue = queue.Queue()
    other_queue = queue.Queue()
    worker._worker_input_queues = {
        author_key: author_queue,
        other_key: other_queue,
    }

    with mock.patch.object(
        worker,
        "_get_zulip_outbound_event",
        mock.Mock(return_value=event),
    ), mock.patch.object(
        worker,
        "_get_zulip_outbound_account",
        mock.Mock(return_value=external_account),
    ), mock.patch.object(
        worker,
        "_build_zulip_outbound_command",
        mock.Mock(return_value=command),
    ):
        worker._dispatch_zulip_outbound_state(state)

    assert author_queue.get_nowait() is command
    assert other_queue.empty()
    assert state.status == "processing"
    assert state.attempts == 1
    assert state.last_error is None


def test_dispatch_zulip_reaction_outbound_state_uses_reactor_worker_queue():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    command = object()
    state = FakeOutboundState()
    event = types.SimpleNamespace(
        object_type="message_reaction",
        action="created",
        user_uuid="reactor-user",
        payload={
            "user_uuid": "reactor-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="reactor-user",
        account_settings=types.SimpleNamespace(credentials=object()),
    )
    reactor_queue = queue.Queue()
    author_queue = queue.Queue()
    worker._worker_input_queues = {
        ("project", "https://zulip.example.com", "reactor-user"): reactor_queue,
        ("project", "https://zulip.example.com", "author-user"): author_queue,
    }

    with mock.patch.object(
        worker,
        "_get_zulip_outbound_event",
        mock.Mock(return_value=event),
    ), mock.patch.object(
        worker,
        "_get_zulip_outbound_account",
        mock.Mock(return_value=external_account),
    ), mock.patch.object(
        worker,
        "_build_zulip_outbound_command",
        mock.Mock(return_value=command),
    ):
        worker._dispatch_zulip_outbound_state(state)

    assert reactor_queue.get_nowait() is command
    assert author_queue.empty()
    assert state.status == "processing"


def test_dispatch_zulip_outbound_state_retries_without_credentials():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    state = FakeOutboundState()
    event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="author-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=None),
        },
    )
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="author-user",
        account_settings=types.SimpleNamespace(credentials=None),
    )

    with mock.patch.object(
        worker,
        "_get_zulip_outbound_event",
        mock.Mock(return_value=event),
    ), mock.patch.object(
        worker,
        "_get_zulip_outbound_account",
        mock.Mock(return_value=external_account),
    ):
        worker._dispatch_zulip_outbound_state(state)

    assert state.status == "pending"
    assert state.attempts == 1
    assert state.next_retry_at is not None
    assert state.last_error == "Zulip external account credentials are missing"


def test_dispatch_zulip_outbound_state_skips_inbound_created_event():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    state = FakeOutboundState()
    event = types.SimpleNamespace(
        object_type="message",
        action="created",
        user_uuid="author-user",
        payload={
            "author_uuid": "author-user",
            "source_name": "zulip",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )

    with mock.patch.object(
        worker,
        "_get_zulip_outbound_event",
        mock.Mock(return_value=event),
    ), mock.patch.object(
        worker,
        "_get_zulip_outbound_account",
        mock.Mock(),
    ) as get_account:
        worker._dispatch_zulip_outbound_state(state)

    get_account.assert_not_called()
    assert state.status == "skipped"
    assert state.next_retry_at is None
    assert state.last_error is None


def test_save_zulip_processed_message_casts_message_uuid():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    message_uuid = sys_uuid.uuid4()
    command = types.SimpleNamespace(
        external_account=types.SimpleNamespace(
            project_id="project",
            server_url="https://zulip.example.com",
            user_uuid="author-user",
        ),
        message_uuid=str(message_uuid),
        zulip_message_id=12345,
    )

    with _patch_zulip_processed_entities():
        worker._save_zulip_processed_message(command)

    assert len(FakeZulipProcessedEntity.inserted) == 1
    processed = FakeZulipProcessedEntity.inserted[0]
    assert processed.workspace_uuid == message_uuid
    assert processed.entity_id == "12345"


def test_zulip_sent_result_error_marks_outbound_failed_without_retry():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    state = FakeOutboundState(status="processing", attempts=1)
    command = agents.workers.ZulipMessageSent(
        external_account=types.SimpleNamespace(
            project_id="project",
            server_url="https://zulip.example.com",
            user_uuid="author-user",
        ),
        epoch_version=55,
        message_uuid=str(sys_uuid.uuid4()),
        zulip_message_id=12345,
    )

    with mock.patch.object(
        worker,
        "_handle_zulip_message_sent",
        mock.Mock(side_effect=RuntimeError("local boom")),
    ), mock.patch.object(
        worker,
        "_get_outbound_state_for_command",
        mock.Mock(return_value=state),
    ):
        worker._execute_sync_command(command)

    assert state.status == "failed"
    assert state.attempts == 1
    assert state.next_retry_at is None
    assert state.last_error == "local boom"


def test_expired_processing_outbound_state_is_failed_not_redispatched():
    now = datetime.datetime.now(datetime.timezone.utc)
    expired_state = FakeOutboundState(
        status="processing",
        attempts=1,
        next_retry_at=now - datetime.timedelta(seconds=1),
    )
    pending_state = FakeOutboundState(
        epoch_version=56,
        status="pending",
        attempts=0,
        next_retry_at=None,
    )

    class FakeZulipOutboundEventState:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                side_effect=[
                    [expired_state],
                    [pending_state],
                ],
            ),
        )

    worker = agents.WorkspaceIntegrationBridgeWorker()
    with mock.patch.object(
        agents.models,
        "ZulipOutboundEventState",
        FakeZulipOutboundEventState,
    ):
        worker._fail_expired_outbound_processing_states()
        ready_states = worker._get_ready_outbound_event_states()

    assert expired_state.status == "failed"
    assert expired_state.next_retry_at is None
    assert expired_state.last_error == (
        "Zulip outbound command processing expired"
    )
    assert ready_states == [pending_state]


def test_build_update_zulip_message_command_skips_reaction_only_update():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    event = types.SimpleNamespace(
        epoch_version=56,
        payload={"uuid": "message-uuid"},
    )

    with mock.patch.object(
        worker,
        "_message_content_changed",
        mock.Mock(return_value=False),
    ), mock.patch.object(
        worker,
        "_get_workspace_message",
        mock.Mock(),
    ) as get_message:
        command = worker._build_update_zulip_message_command(event)

    assert command is None
    get_message.assert_not_called()


def test_build_zulip_reaction_outbound_commands():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    created_event = types.SimpleNamespace(
        object_type="message_reaction",
        action="created",
        epoch_version=61,
        payload={
            "message_uuid": "message-uuid",
            "emoji_name": "thumbs_up",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )
    deleted_event = types.SimpleNamespace(
        object_type="message_reaction",
        action="deleted",
        epoch_version=62,
        payload={
            "message_uuid": "message-uuid",
            "emoji_name": "thumbs_up",
            "source": types.SimpleNamespace(message_id=12345),
        },
    )
    updated_event = types.SimpleNamespace(
        object_type="message_reaction",
        action="updated",
        epoch_version=63,
        payload={
            "message_uuid": "message-uuid",
            "emoji_name": "thumbs_up",
            "source": types.SimpleNamespace(message_id=12345),
            "old_emoji_name": "heart",
            "old_source_name": "zulip",
            "old_source": types.SimpleNamespace(message_id=12344),
        },
    )

    created_command = worker._build_zulip_outbound_command(created_event)
    deleted_command = worker._build_zulip_outbound_command(deleted_event)
    updated_command = worker._build_zulip_outbound_command(updated_event)

    assert isinstance(created_command, agents.workers.AddZulipReaction)
    assert created_command.message_id == 12345
    assert created_command.emoji_name == "thumbs_up"
    assert isinstance(deleted_command, agents.workers.RemoveZulipReaction)
    assert deleted_command.message_id == 12345
    assert deleted_command.emoji_name == "thumbs_up"
    assert isinstance(updated_command, agents.workers.UpdateZulipReaction)
    assert updated_command.old_message_id == 12344
    assert updated_command.old_emoji_name == "heart"
    assert updated_command.message_id == 12345
    assert updated_command.emoji_name == "thumbs_up"


def test_build_send_zulip_message_command_uses_direct_private_recipient():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    event = types.SimpleNamespace(
        epoch_version=55,
        project_id="project",
        payload={
            "uuid": "message-uuid",
            "payload": {
                "content": "hello",
            },
        },
    )
    message = types.SimpleNamespace(
        stream_uuid="stream-uuid",
        topic_uuid="topic-uuid",
        source={
            "server_url": "https://zulip.example.com",
        },
    )
    stream = types.SimpleNamespace(
        private=True,
        direct_user_uuid="recipient-user",
    )
    recipient_account = types.SimpleNamespace(
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=42),
        ),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=recipient_account),
        )

    with mock.patch.object(
        worker,
        "_get_workspace_message",
        mock.Mock(return_value=message),
    ), mock.patch.object(
        worker,
        "_get_workspace_stream",
        mock.Mock(return_value=stream),
    ), mock.patch.object(
        worker,
        "_get_workspace_topic",
        mock.Mock(),
    ) as get_topic, mock.patch.object(
        agents.models,
        "ExternalAccount",
        FakeExternalAccount,
    ):
        command = worker._build_send_zulip_message_command(event)

    assert isinstance(command, agents.workers.SendZulipPrivateMessage)
    assert command.epoch_version == 55
    assert command.message_uuid == "message-uuid"
    assert command.recipient_ids == [42]
    assert command.content == "hello"
    get_topic.assert_not_called()
    filters = FakeExternalAccount.objects.get_one_or_none.call_args.kwargs[
        "filters"
    ]
    assert filters["project_id"].value == "project"
    assert filters["account_type"].value == "zulip"
    assert filters["server_url"].value == "https://zulip.example.com"
    assert filters["user_uuid"].value == "recipient-user"


def test_bridge_worker_shutdown_stops_workers_and_drains_output_queue():
    class FakeWorker:
        def __init__(self):
            self.stopped = False
            self.joined = False

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            self.joined = True

        def is_alive(self):
            return False

    bridge_worker = agents.WorkspaceIntegrationBridgeWorker()
    bridge_worker._enabled = True
    fake_worker = FakeWorker()
    worker_key = (
        "project",
        "https://zulip.example.com",
        "author-user",
    )
    input_queue = queue.Queue()
    bridge_worker._workers = {worker_key: fake_worker}
    bridge_worker._worker_input_queues = {worker_key: input_queue}
    bridge_worker._worker_accounts = {worker_key: object()}
    command = object()
    agents.workers.put_sync_response(bridge_worker._sync_queue, command)

    with mock.patch.object(
        bridge_worker,
        "_execute_sync_command",
        mock.Mock(),
    ) as execute_command:
        bridge_worker._shutdown_iteration()

    assert fake_worker.stopped
    assert fake_worker.joined
    assert isinstance(input_queue.get_nowait(), agents.workers.StopWorker)
    execute_command.assert_called_once_with(command)
    assert bridge_worker._workers == {}
    assert not bridge_worker._enabled


def test_bridge_worker_syncs_due_user_providers():
    provider = types.SimpleNamespace(sync=mock.Mock())

    class FakeExternalAccountUserSync:
        objects = types.SimpleNamespace(get_all=mock.Mock(return_value=[provider]))

    with mock.patch.object(
        agents.models,
        "ExternalAccountUserSync",
        FakeExternalAccountUserSync,
    ):
        agents.WorkspaceIntegrationBridgeWorker()._sync_zulip_users()

    FakeExternalAccountUserSync.objects.get_all.assert_called_once()
    filters = FakeExternalAccountUserSync.objects.get_all.call_args.kwargs[
        "filters"
    ]
    assert filters["account_type"].value == "zulip"
    provider.sync.assert_called_once_with()


def test_bridge_worker_syncs_due_iam_user_providers():
    provider = types.SimpleNamespace(sync=mock.Mock())

    class FakeExternalAccountUserSync:
        objects = types.SimpleNamespace(get_all=mock.Mock(return_value=[provider]))

    with mock.patch.object(
        agents.models,
        "ExternalAccountUserSync",
        FakeExternalAccountUserSync,
    ):
        agents.WorkspaceIntegrationBridgeWorker()._sync_iam_users()

    FakeExternalAccountUserSync.objects.get_all.assert_called_once()
    filters = FakeExternalAccountUserSync.objects.get_all.call_args.kwargs[
        "filters"
    ]
    assert filters["account_type"].value == "iam"
    provider.sync.assert_called_once_with()


def test_bridge_worker_starts_zulip_bridge_workers_by_user_uuid():
    credentials = object()
    first_user = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="first-user",
        account_settings=types.SimpleNamespace(credentials=credentials),
    )
    second_user = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="second-user",
        account_settings=types.SimpleNamespace(credentials=credentials),
    )
    skipped_user = types.SimpleNamespace(
        user_uuid="skipped-user",
        account_settings=types.SimpleNamespace(credentials=None),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                return_value=[first_user, second_user, skipped_user],
            ),
        )

    class FakeZulipBridgeWorker:
        instances = []

        def __init__(
            self,
            external_account,
            input_queue,
            output_queue,
        ):
            self.external_account = external_account
            self.input_queue = input_queue
            self.output_queue = output_queue
            self.start = mock.Mock()
            type(self).instances.append(self)

    worker = agents.WorkspaceIntegrationBridgeWorker()
    existing_worker = object()
    existing_input_queue = queue.Queue()
    first_worker_key = (
        "project",
        "https://zulip.example.com",
        "first-user",
    )
    second_worker_key = (
        "project",
        "https://zulip.example.com",
        "second-user",
    )
    worker._workers = {first_worker_key: existing_worker}
    worker._worker_input_queues = {first_worker_key: existing_input_queue}
    worker._worker_accounts = {first_worker_key: first_user}
    queue_state = types.SimpleNamespace(
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
    )

    with mock.patch.object(
        agents.models,
        "ExternalAccount",
        FakeExternalAccount,
    ):
        with mock.patch.object(
            agents.workers,
            "ZulipBridgeWorker",
            FakeZulipBridgeWorker,
        ):
            with mock.patch.object(
                worker,
                "_get_or_create_zulip_queue_state",
                mock.Mock(return_value=queue_state),
            ):
                worker._start_bridges()

    FakeExternalAccount.objects.get_all.assert_called_once()
    filters = FakeExternalAccount.objects.get_all.call_args.kwargs["filters"]
    assert filters["account_type"].value == "zulip"
    assert worker._workers[first_worker_key] is existing_worker
    assert worker._workers[second_worker_key] is (
        FakeZulipBridgeWorker.instances[0]
    )
    assert (
        "project",
        "https://zulip.example.com",
        "skipped-user",
    ) not in worker._workers
    assert FakeZulipBridgeWorker.instances[0].external_account is second_user
    assert FakeZulipBridgeWorker.instances[0].output_queue is (
        worker._sync_queue
    )
    assert worker._message_sync_worker_keys == {
        first_worker_key,
        second_worker_key,
    }
    first_sync_message = existing_input_queue.get_nowait()
    assert isinstance(first_sync_message, agents.workers.SyncMessages)
    assert first_sync_message.queue_id == "queue-1"
    assert first_sync_message.last_event_id == 42
    assert first_sync_message.last_message_id == 103
    assert first_sync_message.is_synced is True
    second_sync_message = FakeZulipBridgeWorker.instances[0].input_queue.get_nowait()
    assert isinstance(second_sync_message, agents.workers.SyncMessages)
    assert second_sync_message.queue_id == "queue-1"
    assert second_sync_message.last_event_id == 42
    first_sync_message.on_finished()
    assert worker._message_sync_worker_keys == {second_worker_key}
    FakeZulipBridgeWorker.instances[0].start.assert_called_once_with()

    worker._workers = {first_worker_key: existing_worker}
    worker._worker_input_queues = {first_worker_key: existing_input_queue}
    worker._worker_accounts = {first_worker_key: first_user}
    worker._message_sync_worker_keys = set()
    with mock.patch.object(
        agents.models,
        "ExternalAccount",
        FakeExternalAccount,
    ):
        with mock.patch.object(
            agents.workers,
            "ZulipBridgeWorker",
            FakeZulipBridgeWorker,
        ):
            with mock.patch.object(
                worker,
                "_get_or_create_zulip_queue_state",
                mock.Mock(return_value=queue_state),
            ):
                worker._start_bridges()

    sync_message = existing_input_queue.get_nowait()
    assert sync_message.queue_id == "queue-1"
    assert sync_message.last_event_id == 42
    assert sync_message.last_message_id == 103
    assert sync_message.is_synced is True


def test_bridge_cache_gets_existing_stream_once():
    existing_stream = types.SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
        source=types.SimpleNamespace(stream_id=3),
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [],
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[existing_stream]),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            stream = cache.get_or_create_stream(
                external_account=external_account,
                stream_info=stream_info,
            )
            cached_stream = cache.get_or_create_stream(
                external_account=external_account,
                stream_info=stream_info,
            )

    assert stream is existing_stream
    assert cached_stream is existing_stream
    FakeWorkspaceStream.objects.get_all.assert_called_once()
    filters = FakeWorkspaceStream.objects.get_all.call_args.kwargs["filters"]
    assert filters["project_id"].value == "project"
    assert filters["source_name"].value == "zulip"


def test_bridge_cache_skips_seen_stream_until_subscription_reset():
    existing_stream = types.SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
        name="old-general",
        description="Old description",
        invite_only=False,
        announce=False,
        is_archived=False,
        source=types.SimpleNamespace(stream_id=3),
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [],
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[existing_stream]),
            get_one_or_none=mock.Mock(return_value=existing_stream),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.messenger_dm_helpers,
                "update_workspace_user_stream",
                mock.Mock(),
            ) as update_stream:
                cache.get_or_create_stream(
                    external_account=external_account,
                    stream_info=stream_info,
                )
                cache.get_or_create_stream(
                    external_account=external_account,
                    stream_info=stream_info,
                )
                cache.reset_subscription_cache(external_account)
                cache.get_or_create_stream(
                    external_account=external_account,
                    stream_info=stream_info,
                )

    assert update_stream.call_count == 2


def test_bridge_cache_binds_existing_stream_users_once():
    existing_stream = types.SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
        source=types.SimpleNamespace(stream_id=3),
    )
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [24, 25],
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[existing_stream]),
        )

    creator_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="creator-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=24),
        ),
    )
    member_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="member-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=25),
        ),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[creator_account, member_account]),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_stream_bindings",
                    mock.Mock(),
                ) as get_or_create_bindings:
                    stream = cache.get_or_create_stream(
                        external_account=external_account,
                        stream_info=stream_info,
                    )
                    cached_stream = cache.get_or_create_stream(
                        external_account=external_account,
                        stream_info=stream_info,
                    )

    assert stream is existing_stream
    assert cached_stream is existing_stream
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="stream-uuid",
        who_uuid="creator-user",
        role_user_uuids={
            "member": [
                "member-user",
            ],
        },
    )


def test_bridge_cache_creates_missing_stream_once():
    created_stream = types.SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
    )
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [10, 24, 25],
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[]),
        )

    creator_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="creator-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=24),
        ),
    )
    bridge_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=10),
        ),
    )
    member_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="member-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=25),
        ),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                return_value=[
                    bridge_account,
                    creator_account,
                    member_account,
                ],
            ),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_user_stream",
                    mock.Mock(return_value=created_stream),
                ) as get_or_create_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_stream_bindings",
                        mock.Mock(),
                    ) as get_or_create_bindings:
                        stream = cache.get_or_create_stream(
                            external_account=external_account,
                            stream_info=stream_info,
                        )
                        cached_stream = cache.get_or_create_stream(
                            external_account=external_account,
                            stream_info=stream_info,
                        )

    assert stream is created_stream
    assert cached_stream is created_stream
    FakeWorkspaceStream.objects.get_all.assert_called_once()
    FakeExternalAccount.objects.get_all.assert_called_once()
    filters = FakeExternalAccount.objects.get_all.call_args.kwargs["filters"]
    assert filters["project_id"].value == "project"
    assert filters["account_type"].value == "zulip"
    assert filters["server_url"].value == "https://zulip.example.com"
    get_or_create_stream.assert_called_once()
    call_kwargs = get_or_create_stream.call_args.kwargs
    assert call_kwargs["project_id"] == "project"
    assert call_kwargs["user_uuid"] == "creator-user"
    assert call_kwargs["name"] == "general"
    assert call_kwargs["description"] == "General stream"
    assert call_kwargs["source_name"] == "zulip"
    assert call_kwargs["source"].kind == "zulip"
    assert call_kwargs["source"].stream_id == 3
    assert call_kwargs["invite_only"] is True
    assert call_kwargs["announce"] is True
    assert call_kwargs["is_archived"] is True
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="stream-uuid",
        who_uuid="creator-user",
        role_user_uuids={
            "member": [
                "bridge-user",
                "member-user",
            ],
        },
    )


def test_bridge_cache_creates_missing_private_stream_with_zulip_topic():
    public_stream_with_same_id = types.SimpleNamespace(
        uuid="public-stream-uuid",
        user_uuid="public-owner",
        private=False,
        source=types.SimpleNamespace(stream_id=79),
    )
    created_stream = types.SimpleNamespace(
        uuid="private-stream-uuid",
        user_uuid="gmelikov-user",
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "private",
        "stream_id": 79,
        "display_recipient": "admin, gmelikov",
        "description": "",
        "creator_id": 10,
        "timestamp": 1772202531,
        "invite_only": True,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [8, 10],
        "default_topic_name": "zulip",
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[public_stream_with_same_id]),
        )

    admin_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="admin-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=8),
        ),
    )
    gmelikov_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=10),
        ),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                return_value=[
                    admin_account,
                    gmelikov_account,
                ],
            ),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_user_stream",
                    mock.Mock(return_value=created_stream),
                ) as get_or_create_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_stream_bindings",
                        mock.Mock(),
                    ) as get_or_create_bindings:
                        stream = cache.get_or_create_stream(
                            external_account=external_account,
                            stream_info=stream_info,
                        )

    assert stream is created_stream
    get_or_create_stream.assert_called_once()
    call_kwargs = get_or_create_stream.call_args.kwargs
    assert call_kwargs["project_id"] == "project"
    assert call_kwargs["user_uuid"] == "gmelikov-user"
    assert call_kwargs["name"] == "admin, gmelikov"
    assert call_kwargs["description"] == ""
    assert call_kwargs["source_name"] == "zulip"
    assert call_kwargs["source"].kind == "zulip"
    assert call_kwargs["source"].stream_id == 79
    assert call_kwargs["invite_only"] is True
    assert call_kwargs["announce"] is False
    assert call_kwargs["direct_user_uuid"] == "admin-user"
    assert call_kwargs["is_archived"] is False
    assert call_kwargs["default_topic_name"] == "zulip"
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="private-stream-uuid",
        who_uuid="gmelikov-user",
        role_user_uuids={
            "member": [
                "admin-user",
            ],
        },
    )


def test_bridge_cache_creates_missing_private_group_stream():
    created_stream = types.SimpleNamespace(
        uuid="private-group-stream-uuid",
        user_uuid="admin-user",
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="admin-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "private",
        "stream_id": 20,
        "display_recipient": "admin, Eugene Frolov, gmelikov, Anton",
        "description": "",
        "creator_id": 8,
        "timestamp": 1772202531,
        "invite_only": True,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [8, 9, 10, 15],
        "default_topic_name": "zulip",
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[]),
        )

    admin_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="admin-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=8),
        ),
    )
    eugene_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="eugene-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=9),
        ),
    )
    gmelikov_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=10),
        ),
    )
    anton_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="anton-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=15),
        ),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(
                return_value=[
                    admin_account,
                    eugene_account,
                    gmelikov_account,
                    anton_account,
                ],
            ),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "create_workspace_private_group_stream",
                    mock.Mock(return_value=created_stream),
                ) as create_group_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_user_stream",
                        mock.Mock(),
                    ) as get_or_create_stream:
                        with mock.patch.object(
                            agents.messenger_dm_helpers,
                            "get_or_create_workspace_stream_bindings",
                            mock.Mock(),
                        ) as get_or_create_bindings:
                            stream = cache.get_or_create_stream(
                                external_account=external_account,
                                stream_info=stream_info,
                            )

    assert stream is created_stream
    get_or_create_stream.assert_not_called()
    create_group_stream.assert_called_once()
    call_kwargs = create_group_stream.call_args.kwargs
    assert call_kwargs["project_id"] == "project"
    assert call_kwargs["user_uuid"] == "admin-user"
    assert call_kwargs["name"] == "admin, Eugene Frolov, gmelikov, Anton"
    assert call_kwargs["description"] == ""
    assert call_kwargs["source_name"] == "zulip"
    assert call_kwargs["source"].kind == "zulip"
    assert call_kwargs["source"].stream_id == 20
    assert call_kwargs["invite_only"] is True
    assert call_kwargs["announce"] is False
    assert call_kwargs["is_archived"] is False
    assert call_kwargs["default_topic_name"] == "zulip"
    assert "direct_user_uuid" not in call_kwargs
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="private-group-stream-uuid",
        who_uuid="admin-user",
        role_user_uuids={
            "member": [
                "eugene-user",
                "gmelikov-user",
                "anton-user",
            ],
        },
    )


def test_bridge_cache_retries_private_stream_without_direct_user():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "private",
        "stream_id": 79,
        "display_recipient": "gmelikov",
        "description": "",
        "creator_id": 10,
        "timestamp": 1772202531,
        "invite_only": True,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [10],
        "default_topic_name": "zulip",
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(get_all=mock.Mock(return_value=[]))

    gmelikov_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
        account_settings=types.SimpleNamespace(
            user_info=types.SimpleNamespace(user_id=10),
        ),
    )

    class FakeExternalAccount:
        objects = types.SimpleNamespace(
            get_all=mock.Mock(return_value=[gmelikov_account]),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_user_stream",
                    mock.Mock(),
                ) as get_or_create_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_stream_bindings",
                        mock.Mock(),
                    ) as get_or_create_bindings:
                        try:
                            cache.get_or_create_stream(
                                external_account=external_account,
                                stream_info=stream_info,
                            )
                        except agents.RetryCommandLater:
                            pass
                        else:
                            assert False

    get_or_create_stream.assert_not_called()
    get_or_create_bindings.assert_not_called()


def test_bridge_cache_requests_stream_sync_for_missing_message_stream():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": False,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [24],
        "event_type": "message",
    }

    class FakeWorkspaceStream:
        objects = types.SimpleNamespace(get_all=mock.Mock(return_value=[]))

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            try:
                cache.get_or_create_stream(
                    external_account=external_account,
                    stream_info=stream_info,
                )
            except agents.SyncStreamsNeeded as exc:
                assert exc.external_account is external_account
                assert exc.stream_id == 3
            else:
                assert False


def test_bridge_cache_syncs_zulip_read_flag_for_each_external_account():
    first_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="first-user",
        server_url="https://zulip.example.com",
    )
    second_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="second-user",
        server_url="https://zulip.example.com",
    )
    stream = types.SimpleNamespace(uuid="stream-uuid", user_uuid="owner-user")
    topic = types.SimpleNamespace(uuid="topic-uuid")
    message = types.SimpleNamespace(uuid="message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
        "message_id": 100,
        "sender_id": 24,
        "content": "hello",
        "read": True,
        "created_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
        "updated_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
    }

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_required_zulip_user_uuid",
        mock.Mock(return_value="author-user"),
    ), mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=None),
    ), mock.patch.object(
        cache,
        "_save_processed_entity",
        mock.Mock(),
    ) as save_processed, mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(return_value=message),
    ) as get_or_create_message, mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_stream_binding",
        mock.Mock(),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "read_workspace_user_message",
        mock.Mock(),
    ) as read_message:
        result = cache.get_or_create_message(
            external_account=first_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )
        cached_result = cache.get_or_create_message(
            external_account=second_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )

    assert result is message
    assert cached_result is message
    get_or_create_message.assert_called_once()
    save_processed.assert_called_once()
    assert read_message.call_args_list == [
        mock.call(
            project_id="project",
            user_uuid="first-user",
            message_uuid="message-uuid",
        ),
        mock.call(
            project_id="project",
            user_uuid="second-user",
            message_uuid="message-uuid",
        ),
    ]


def test_bridge_cache_creates_missing_message_sender_from_zulip_payload():
    class FakeWorkspaceUser:
        inserted = []
        objects = types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=None),
        )

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def insert(self):
            type(self).inserted.append(self)

    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="reader-user",
        server_url="https://zulip.example.com",
    )
    stream = types.SimpleNamespace(uuid="stream-uuid", user_uuid="owner-user")
    topic = types.SimpleNamespace(uuid="topic-uuid")
    message = types.SimpleNamespace(uuid="message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
        "message_id": 100,
        "sender_id": 6,
        "sender_email": "notification-bot@zulip.com",
        "sender_full_name": "Notification Bot",
        "content": "hello",
        "read": True,
        "created_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
        "updated_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
    }

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_zulip_user_uuid",
        mock.Mock(return_value=None),
    ), mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=None),
    ), mock.patch.object(
        cache,
        "_save_processed_entity",
        mock.Mock(),
    ), mock.patch.object(
        agents.models,
        "WorkspaceUser",
        FakeWorkspaceUser,
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(return_value=message),
    ) as get_or_create_message, mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_stream_binding",
        mock.Mock(),
    ) as get_or_create_binding, mock.patch.object(
        agents.messenger_dm_helpers,
        "read_workspace_user_message",
        mock.Mock(),
    ):
        result = cache.get_or_create_message(
            external_account=external_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )

    assert result is message
    assert len(FakeWorkspaceUser.inserted) == 1
    sender = FakeWorkspaceUser.inserted[0]
    assert sender.username == "Notification Bot"
    assert sender.source == "zulip"
    assert sender.email == "notification-bot@zulip.com"
    get_or_create_binding.assert_called_once_with(
        project_id="project",
        stream_uuid="stream-uuid",
        user_uuid=sender.uuid,
        who_uuid="owner-user",
        role="member",
    )
    get_or_create_message.assert_called_once()
    assert get_or_create_message.call_args.kwargs["user_uuid"] == sender.uuid
    assert cache._user_uuids[
        (
            "project",
            "https://zulip.example.com",
            6,
        )
    ] == sender.uuid


def test_bridge_cache_preprocesses_existing_processed_message():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="reader-user",
        server_url="https://zulip.example.com",
    )
    stream = types.SimpleNamespace(uuid="stream-uuid", user_uuid="owner-user")
    topic = types.SimpleNamespace(uuid="topic-uuid")
    message = types.SimpleNamespace(uuid="message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
        "message_id": 100,
        "sender_id": 24,
        "content": "![photo.png](/user_uploads/1/photo.png)",
        "read": False,
        "created_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
        "updated_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
    }
    processed = types.SimpleNamespace(workspace_uuid="message-uuid")

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_required_zulip_user_uuid",
        mock.Mock(return_value="author-user"),
    ), mock.patch.object(
        cache,
        "preprocess_zulip_message",
        mock.Mock(return_value=message_info),
    ) as preprocess_message, mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(),
    ) as get_or_create_message, mock.patch.object(
        agents.models.WorkspaceUserMessage,
        "objects",
        types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=message),
        ),
    ) as user_messages:
        with _patch_zulip_processed_entities(processed):
            result = cache.get_or_create_message(
                external_account=external_account,
                stream=stream,
                topic=topic,
                stream_info=stream_info,
                topic_name="deploys",
                message_info=message_info,
            )

    assert result is message
    preprocess_message.assert_called_once_with(
        external_account=external_account,
        stream=stream,
        message_info=message_info,
    )
    get_or_create_message.assert_not_called()
    user_messages.get_one_or_none.assert_called_once()


def test_bridge_cache_rebinds_stale_processed_message():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="reader-user",
        server_url="https://zulip.example.com",
    )
    stream = types.SimpleNamespace(uuid="stream-uuid", user_uuid="owner-user")
    topic = types.SimpleNamespace(uuid="topic-uuid")
    message = types.SimpleNamespace(uuid="new-message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
        "message_id": 100,
        "sender_id": 24,
        "content": "hello",
        "read": False,
        "created_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
        "updated_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
    }
    processed = FakeZulipProcessedEntity(
        workspace_uuid="missing-message-uuid",
    )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_required_zulip_user_uuid",
        mock.Mock(return_value="author-user"),
    ), mock.patch.object(
        agents.models.WorkspaceUserMessage,
        "objects",
        types.SimpleNamespace(
            get_one_or_none=mock.Mock(return_value=None),
        ),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(return_value=message),
    ) as get_or_create_message, mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_stream_binding",
        mock.Mock(),
    ):
        with _patch_zulip_processed_entities(processed):
            result = cache.get_or_create_message(
                external_account=external_account,
                stream=stream,
                topic=topic,
                stream_info=stream_info,
                topic_name="deploys",
                message_info=message_info,
            )

    assert result is message
    get_or_create_message.assert_called_once()
    assert processed.workspace_uuid == "new-message-uuid"
    processed.update.assert_called_once_with()


def test_bridge_cache_does_not_sync_unread_zulip_message_as_unread():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="reader-user",
        server_url="https://zulip.example.com",
    )
    stream = types.SimpleNamespace(uuid="stream-uuid", user_uuid="owner-user")
    topic = types.SimpleNamespace(uuid="topic-uuid")
    message = types.SimpleNamespace(uuid="message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
        "message_id": 100,
        "sender_id": 24,
        "content": "hello",
        "read": False,
        "created_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
        "updated_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
    }

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_required_zulip_user_uuid",
        mock.Mock(return_value="author-user"),
    ), mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=None),
    ), mock.patch.object(
        cache,
        "_save_processed_entity",
        mock.Mock(),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(return_value=message),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_stream_binding",
        mock.Mock(),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "read_workspace_user_message",
        mock.Mock(),
    ) as read_message:
        cache.get_or_create_message(
            external_account=external_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )

    read_message.assert_not_called()


def test_bridge_worker_requeues_retryable_commands():
    command = types.SimpleNamespace(
        execute=mock.Mock(side_effect=agents.RetryCommandLater()),
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._put_sync_command(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_called_once_with(cache=worker._cache)
    assert worker._sync_queue.empty()
    assert len(worker._postponed_commands) == 1
    assert worker._postponed_commands[0][1] is command

    worker._postponed_commands[0] = (
        datetime.datetime.now(datetime.timezone.utc) -
        datetime.timedelta(seconds=1),
        command,
    )
    worker._release_postponed_commands()

    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is command


def test_bridge_worker_resets_stale_zulip_queue_subscription_version():
    external_account = types.SimpleNamespace(
        server_url="https://zulip.example.com",
    )
    queue_state = FakeZulipQueueState(
        queue_id="old-queue",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
        subscription_version=1,
    )

    result = (
        agents.WorkspaceIntegrationBridgeWorker()
        ._ensure_zulip_queue_subscription_version(
            external_account=external_account,
            queue_state=queue_state,
        )
    )

    assert result is queue_state
    assert queue_state.queue_id is None
    assert queue_state.last_event_id == -1
    assert queue_state.last_message_id == 103
    assert queue_state.is_synced is False
    assert queue_state.subscription_version == (
        agents.zulip_client.MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION
    )
    queue_state.update_dm.assert_called_once_with(
        values={
            "queue_id": None,
            "last_event_id": -1,
            "last_message_id": 103,
            "is_synced": False,
            "subscription_version": (
                agents.zulip_client.MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION
            ),
        },
    )
    queue_state.update.assert_called_once_with()


def test_bridge_worker_requests_queue_recreate_without_queue_id():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    input_queue = queue.Queue()
    queue_state = FakeZulipQueueState(
        queue_id=None,
        last_event_id=-1,
        last_message_id=103,
        is_synced=False,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._request_zulip_message_sync(event_owner)

    command = input_queue.get_nowait()
    assert isinstance(
        command,
        agents.workers.CreateZulipEventQueue,
    )
    assert worker._message_sync_worker_keys == set()
    assert worker._queue_recreate_worker_keys == {event_owner}


def test_bridge_worker_rebuilds_history_tasks_from_latest_message():
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    pending_task = types.SimpleNamespace(delete=mock.Mock())
    failed_task = types.SimpleNamespace(delete=mock.Mock())
    done_task = types.SimpleNamespace(delete=mock.Mock())
    FakeZulipHistorySyncTask.inserted = []
    FakeZulipHistorySyncTask.objects = types.SimpleNamespace(
        get_all=mock.Mock(side_effect=[
            [pending_task],
            [failed_task],
            [done_task],
        ]),
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        agents.models,
        "ZulipHistorySyncTask",
        FakeZulipHistorySyncTask,
    ), mock.patch.object(
        worker,
        "_get_zulip_latest_message_id",
        mock.Mock(return_value=250),
    ):
        created = worker._rebuild_zulip_history_sync_tasks(external_account)

    assert created == 3
    pending_task.delete.assert_called_once_with()
    failed_task.delete.assert_called_once_with()
    done_task.delete.assert_not_called()
    assert [
        (
            task.from_message_id,
            task.to_message_id,
            task.status,
        )
        for task in FakeZulipHistorySyncTask.inserted
    ] == [
        (0, 99, "pending"),
        (100, 199, "pending"),
        (200, 250, "pending"),
    ]


def test_bridge_worker_processes_new_queue_by_rebuilding_history_tasks():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    input_queue = queue.Queue()
    queue_state = FakeZulipQueueState(
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=0,
        is_synced=False,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account
    worker._cache = types.SimpleNamespace(
        reset_subscription_cache=mock.Mock(),
    )
    command = agents.workers.ZulipEventQueueCreated(
        external_account=external_account,
    )

    with mock.patch.object(
        worker,
        "_rebuild_zulip_history_sync_tasks",
        mock.Mock(return_value=3),
    ) as rebuild_tasks, mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._execute_sync_command(command)

    worker._cache.reset_subscription_cache.assert_called_once_with(
        external_account,
    )
    rebuild_tasks.assert_called_once_with(external_account=external_account)
    sync_command = input_queue.get_nowait()
    assert isinstance(sync_command, agents.workers.SyncMessages)
    assert sync_command.queue_id == "queue-1"
    assert worker._queue_recreate_worker_keys == set()
    assert worker._message_sync_worker_keys == {event_owner}


def test_bridge_worker_processes_one_history_task_newest_messages_first():
    task = FakeZulipHistorySyncTask(
        uuid="task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=100,
        to_message_id=199,
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        worker,
        "_get_next_zulip_history_sync_task",
        mock.Mock(return_value=task),
    ), mock.patch.object(
        worker,
        "_get_zulip_history_task_external_account",
        mock.Mock(return_value=external_account),
    ), mock.patch.object(
        worker,
        "_fetch_zulip_history_task_messages",
        mock.Mock(return_value=[
            {"id": 101},
            {"id": 199},
            {"id": 150},
        ]),
    ), mock.patch.object(
        worker,
        "_sync_zulip_history_task_message",
        mock.Mock(return_value=[]),
    ) as sync_message, mock.patch.object(
        worker,
        "_has_pending_zulip_history_sync_tasks",
        mock.Mock(return_value=False),
    ), mock.patch.object(
        worker,
        "_update_external_account_zulip_queue_state",
        mock.Mock(),
    ) as update_queue_state:
        worker._process_zulip_history_sync_task()

    assert [
        call.kwargs["message"]["id"]
        for call in sync_message.call_args_list
    ] == [199, 150, 101]
    assert task.status == "done"
    assert task.last_error is None
    task.update.assert_called_once_with()
    update_queue_state.assert_called_once_with(
        external_account=external_account,
        is_synced=True,
    )


def test_bridge_worker_fetches_only_history_task_range_size():
    task = FakeZulipHistorySyncTask(
        uuid="task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=5700,
        to_message_id=5709,
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        worker,
        "_fetch_zulip_messages",
        mock.Mock(return_value=[]),
    ) as fetch_messages:
        worker._fetch_zulip_history_task_messages(
            external_account=external_account,
            task=task,
        )

    fetch_messages.assert_called_once_with(
        external_account=external_account,
        message_filters={
            "anchor": 5699,
            "num_before": 0,
            "num_after": 10,
        },
    )


def test_bridge_worker_fetches_single_history_task_message_directly():
    task = FakeZulipHistorySyncTask(
        uuid="task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=5749,
        to_message_id=5749,
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        worker,
        "_fetch_zulip_message",
        mock.Mock(return_value={"id": 5749}),
    ) as fetch_message, mock.patch.object(
        worker,
        "_fetch_zulip_messages",
        mock.Mock(return_value=[]),
    ) as fetch_messages:
        messages = worker._fetch_zulip_history_task_messages(
            external_account=external_account,
            task=task,
        )

    assert messages == [{"id": 5749}]
    fetch_message.assert_called_once_with(
        external_account=external_account,
        message_id=5749,
    )
    fetch_messages.assert_not_called()


def test_bridge_worker_treats_invalid_single_history_message_as_empty_range():
    task = FakeZulipHistorySyncTask(
        uuid="task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=5749,
        to_message_id=5749,
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        worker,
        "_fetch_zulip_message",
        mock.Mock(return_value=None),
    ) as fetch_message, mock.patch.object(
        worker,
        "_fetch_zulip_messages",
        mock.Mock(return_value=[]),
    ) as fetch_messages:
        messages = worker._fetch_zulip_history_task_messages(
            external_account=external_account,
            task=task,
        )

    assert messages == []
    fetch_message.assert_called_once_with(
        external_account=external_account,
        message_id=5749,
    )
    fetch_messages.assert_not_called()


def test_bridge_worker_prefers_pending_history_tasks_without_error():
    errored_task = FakeZulipHistorySyncTask(
        uuid="errored-task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=5700,
        to_message_id=5700,
        status="pending",
        last_error="timeout",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        agents.models,
        "ZulipHistorySyncTask",
        FakeZulipHistorySyncTask,
    ):
        FakeZulipHistorySyncTask.objects = types.SimpleNamespace(
            get_all=mock.Mock(side_effect=[[], [errored_task]]),
        )
        result = worker._get_next_zulip_history_sync_task()

    assert result is errored_task
    first_filters = (
        FakeZulipHistorySyncTask.objects
        .get_all.call_args_list[0].kwargs["filters"]
    )
    assert first_filters["status"].value == "pending"
    assert first_filters["last_error"].value is None


def test_bridge_worker_keeps_retryable_history_task_pending():
    task = FakeZulipHistorySyncTask(
        uuid="task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=100,
        to_message_id=199,
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    error = agents.SyncStreamsNeeded(
        external_account=external_account,
        stream_id=3,
    )

    with mock.patch.object(
        worker,
        "_get_next_zulip_history_sync_task",
        mock.Mock(return_value=task),
    ), mock.patch.object(
        worker,
        "_get_zulip_history_task_external_account",
        mock.Mock(return_value=external_account),
    ), mock.patch.object(
        worker,
        "_fetch_zulip_history_task_messages",
        mock.Mock(return_value=[{"id": 199}]),
    ), mock.patch.object(
        worker,
        "_sync_zulip_history_task_message",
        mock.Mock(side_effect=error),
    ), mock.patch.object(
        worker,
        "_request_zulip_stream_sync",
        mock.Mock(),
    ) as request_stream_sync:
        worker._process_zulip_history_sync_task()

    request_stream_sync.assert_called_once_with(error)
    assert task.status == "pending"
    assert "stream 3" in task.last_error
    task.update.assert_called_once_with()


def test_bridge_worker_splits_network_error_history_task():
    task = FakeZulipHistorySyncTask(
        uuid="task-uuid",
        project_id="project",
        external_account_uuid="account-uuid",
        from_message_id=100,
        to_message_id=119,
    )
    external_account = types.SimpleNamespace(
        uuid="account-uuid",
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    error = agents.requests.exceptions.ReadTimeout("slow Zulip response")

    with mock.patch.object(
        worker,
        "_get_next_zulip_history_sync_task",
        mock.Mock(return_value=task),
    ), mock.patch.object(
        worker,
        "_get_zulip_history_task_external_account",
        mock.Mock(return_value=external_account),
    ), mock.patch.object(
        worker,
        "_fetch_zulip_history_task_messages",
        mock.Mock(side_effect=error),
    ), mock.patch.object(
        agents.models,
        "ZulipHistorySyncTask",
        FakeZulipHistorySyncTask,
    ):
        FakeZulipHistorySyncTask.inserted = []
        FakeZulipHistorySyncTask.deleted = []
        should_continue = worker._process_zulip_history_sync_task()

    assert should_continue is False
    assert FakeZulipHistorySyncTask.deleted == [task]
    assert [
        (
            created.from_message_id,
            created.to_message_id,
            created.status,
            created.last_error,
        )
        for created in FakeZulipHistorySyncTask.inserted
    ] == [
        (100, 109, "pending", None),
        (110, 119, "pending", None),
    ]
    task.update.assert_not_called()


def test_bridge_worker_processes_one_history_task_per_iteration():
    worker = agents.WorkspaceIntegrationBridgeWorker(
        history_sync_task_batch_limit=2,
    )

    with mock.patch.object(
        worker,
        "_process_zulip_history_sync_task",
        mock.Mock(side_effect=[True, True, True]),
    ) as process_task:
        worker._process_zulip_history_sync_tasks()

    assert process_task.call_count == 1


def test_bridge_worker_processes_one_history_task_even_on_retryable_error():
    worker = agents.WorkspaceIntegrationBridgeWorker(
        history_sync_task_batch_limit=20,
    )

    with mock.patch.object(
        worker,
        "_process_zulip_history_sync_task",
        mock.Mock(side_effect=[True, False, True]),
    ) as process_task:
        worker._process_zulip_history_sync_tasks()

    assert process_task.call_count == 1


def test_bridge_worker_recreates_queue_after_failed_queue_event():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    input_queue = queue.Queue()
    queue_state = FakeZulipQueueState(
        queue_id="dead-queue",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account
    worker._message_sync_worker_keys = {event_owner}
    worker._put_sync_command(
        agents.workers.ZulipQueueFailed(
            external_account=external_account,
        ),
    )

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
            worker._process_sync_queue()

    command = input_queue.get_nowait()
    assert isinstance(
        command,
        agents.workers.CreateZulipEventQueue,
    )
    assert worker._message_sync_worker_keys == set()
    assert worker._queue_recreate_worker_keys == {event_owner}
    assert queue_state.queue_id is None
    assert queue_state.is_synced is False
    queue_state.update.assert_called_once_with()


def test_bridge_worker_marks_queue_synced_on_catch_up_barrier():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    queue_state = FakeZulipQueueState(is_synced=False)
    worker = agents.WorkspaceIntegrationBridgeWorker()
    command = agents.workers.FinishZulipMessageCatchUp(
        external_account=external_account,
        last_message_id=104,
    )

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._execute_sync_command(command)

    assert queue_state.is_synced is True
    assert queue_state.last_message_id == 104
    queue_state.update.assert_called_once_with()


def test_bridge_worker_updates_message_anchor_without_clearing_queue_id():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    queue_state = FakeZulipQueueState(
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    command = agents.workers.UpdateZulipQueueState(
        external_account=external_account,
        last_message_id=104,
    )

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._execute_sync_command(command)

    assert queue_state.queue_id == "queue-1"
    assert queue_state.last_message_id == 104
    queue_state.update.assert_called_once_with()


def test_bridge_worker_requests_stream_sync_and_retries_command():
    external_account = types.SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    command = types.SimpleNamespace(
        external_account=external_account,
        event_owner=event_owner,
        execute=mock.Mock(
            side_effect=agents.SyncStreamsNeeded(
                external_account=external_account,
                stream_id=3,
            ),
        ),
    )
    input_queue = queue.Queue()
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account
    worker._put_sync_command(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_called_once_with(cache=worker._cache)
    assert worker._sync_queue.empty()
    assert len(worker._postponed_commands) == 1
    assert worker._postponed_commands[0][1] is command
    sync_streams = input_queue.get_nowait()
    assert isinstance(sync_streams, agents.workers.SyncStreams)
    assert sync_streams.event_owner == event_owner
    assert worker._stream_sync_event_owners == {event_owner}


def test_bridge_worker_defers_non_stream_commands_during_stream_sync():
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    command = types.SimpleNamespace(execute=mock.Mock(return_value="processed"))
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._stream_sync_event_owners.add(event_owner)
    worker._put_sync_command(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_not_called()
    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is command


def test_bridge_worker_processes_stream_commands_during_stream_sync():
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    external_account = types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="bridge-user",
    )
    stream_command = agents.workers.AddStream(
        external_account=external_account,
        stream={
            "stream_id": 3,
            "name": "general",
            "description": "General stream",
            "creator_id": 24,
            "date_created": 1776940760,
            "invite_only": False,
            "is_archived": False,
            "is_announcement_only": False,
        },
    )
    stream_command.execute = mock.Mock(return_value="stream")
    message_command = types.SimpleNamespace(
        execute=mock.Mock(return_value="message"),
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._stream_sync_event_owners.add(event_owner)
    worker._put_sync_command(message_command)
    worker._put_sync_command(stream_command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    stream_command.execute.assert_called_once_with(cache=worker._cache)
    message_command.execute.assert_not_called()
    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is (
        message_command
    )


def test_bridge_worker_releases_stream_sync_barrier_on_finished_response():
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    command = types.SimpleNamespace(execute=mock.Mock(return_value="processed"))
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._stream_sync_event_owners.add(event_owner)
    worker._put_sync_command(command)
    worker._put_sync_command(
        agents.workers.SyncStreamsFinished(event_owner=event_owner),
    )

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    assert worker._stream_sync_event_owners == set()
    command.execute.assert_called_once_with(cache=worker._cache)


def test_bridge_worker_drains_sync_queue_in_one_iteration():
    commands = [
        types.SimpleNamespace(execute=mock.Mock(return_value="processed"))
        for _ in range(3)
    ]
    worker = agents.WorkspaceIntegrationBridgeWorker(
        sync_queue_batch_limit=2,
    )
    for command in commands:
        worker._put_sync_command(command)

    worker._process_sync_queue()

    commands[0].execute.assert_called_once_with(cache=worker._cache)
    commands[1].execute.assert_called_once_with(cache=worker._cache)
    commands[2].execute.assert_called_once_with(cache=worker._cache)
    assert worker._sync_queue.empty()


def test_bridge_worker_uses_short_sync_queue_empty_wait():
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._sync_queue = types.SimpleNamespace(
        qsize=mock.Mock(return_value=0),
        get=mock.Mock(side_effect=queue.Empty),
    )

    worker._process_sync_queue()

    assert agents.SYNC_QUEUE_TIMEOUT == 0.01
    worker._sync_queue.get.assert_called_once_with(
        timeout=agents.SYNC_QUEUE_TIMEOUT,
    )


def test_bridge_worker_iteration_syncs_users():
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(
        worker,
        "_sync_iam_users",
    ) as sync_iam_users, mock.patch.object(
        worker,
        "_sync_zulip_users",
    ) as sync_zulip_users, mock.patch.object(
        worker,
        "_start_bridges",
    ) as start_bridges, mock.patch.object(
        worker,
        "_dispatch_zulip_outbound_events",
    ) as dispatch_zulip_outbound_events, mock.patch.object(
        worker,
        "_process_zulip_history_sync_tasks",
    ) as process_history_tasks, mock.patch.object(
        worker,
        "_process_sync_queue",
    ) as process_sync_queue:
        worker._run_iteration()

    sync_iam_users.assert_called_once_with()
    sync_zulip_users.assert_called_once_with()
    start_bridges.assert_called_once_with()
    dispatch_zulip_outbound_events.assert_called_once_with()
    process_history_tasks.assert_called_once_with()
    process_sync_queue.assert_called_once_with()
