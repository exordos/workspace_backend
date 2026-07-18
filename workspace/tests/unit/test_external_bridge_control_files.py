# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import hashlib
import json
import types
import urllib.parse
import uuid as sys_uuid
from unittest import mock

import pytest

from workspace.common import file_storage_opts
from workspace.external_bridge_control import files
from workspace.external_bridge_control import pki
from workspace.external_bridge_control import state
from workspace.messenger_api import file_storage


REALM_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
INSTANCE_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")


def _identity():
    return pki.BridgeIdentity(
        realm_uuid=REALM_UUID,
        provider_kind="zulip",
        bridge_instance_uuid=INSTANCE_UUID,
        identity_generation=1,
        uri_san="test",
    )


def _patch_storage(monkeypatch, path):
    conf = {
        file_storage_opts.DOMAIN: types.SimpleNamespace(
            default_type=file_storage_opts.STORAGE_TYPE_FILE,
            storage_path=str(path),
        ),
        file_storage_opts.S3_DOMAIN: types.SimpleNamespace(),
    }
    monkeypatch.setattr(file_storage, "CONF", conf)


def _manager(tmp_path, monkeypatch):
    storage_path = tmp_path / "objects"
    _patch_storage(monkeypatch, storage_path)
    repository = state.PersistentControlState(tmp_path / "state", REALM_UUID)
    repository.initialize()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    repository.upsert_resource(
        _identity(),
        {
            "resource_type": "external_account",
            "uuid": str(account_uuid),
            "generation": 1,
            "owner_user_uuid": str(owner_uuid),
            "settings": {
                "kind": "zulip",
                "server_url": "https://zulip.example.test",
                "selection_mode": "explicit",
                "history_depth": "30_days",
                "default_project_id": str(project_uuid),
            },
            "synchronization_enabled": True,
            "credential_envelope": None,
        },
    )
    repository.upsert_resource(
        _identity(),
        {
            "resource_type": "external_chat_assignment",
            "uuid": str(chat_uuid),
            "generation": 1,
            "external_account_uuid": str(account_uuid),
            "provider_chat": {
                "kind": "zulip",
                "chat_type": "channel",
                "provider_chat_key": "engineering",
            },
            "project_id": str(project_uuid),
            "projection_stream_uuid": str(stream_uuid),
            "selected": True,
        },
    )
    commit = mock.Mock()
    manager = files.ExternalFileTransferManager(
        repository,
        "https://workspace-bridge-control.example.test:21443",
        repository.signing_key(),
        commit_file_projection=commit,
    )
    return manager, commit, account_uuid, chat_uuid, storage_path


def _allocation_request(account_uuid, chat_uuid, data):
    return {
        "operation_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(chat_uuid),
        "name": "diagram.png",
        "size_bytes": len(data),
        "content_type": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _finalize_request(request, allocation):
    return {
        "operation_uuid": request["operation_uuid"],
        "allocation_generation": allocation["allocation_generation"],
        "size_bytes": request["size_bytes"],
        "content_type": request["content_type"],
        "sha256": request["sha256"],
    }


def test_local_incoming_transfer_is_single_object_and_commits_v2_sidecar(
    tmp_path, monkeypatch
):
    manager, commit, account_uuid, chat_uuid, storage_path = _manager(
        tmp_path, monkeypatch
    )
    file_uuid = sys_uuid.uuid4()
    data = b"provider image bytes"
    request = _allocation_request(account_uuid, chat_uuid, data)

    allocation, created = manager.allocate_incoming(_identity(), file_uuid, request)
    assert created is True
    assert allocation["upload"]["expires_in_seconds"] == 300
    token = urllib.parse.parse_qs(
        urllib.parse.urlsplit(allocation["upload"]["url"]).query
    )["token"][0]
    manager.put_presigned_object(
        _identity(), token, allocation["upload"]["headers"], data
    )
    result = manager.finalize_incoming(
        _identity(),
        file_uuid,
        _finalize_request(request, allocation),
    )

    assert result["file_urn"] == f"urn:image:{file_uuid}"
    assert file_storage.read_workspace_file(file_uuid) == data
    sidecar_path = storage_path / file_storage.get_workspace_file_metadata_object_id(
        file_uuid
    )
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == 2
    assert sidecar["origin"] == {
        "kind": "external_provider",
        "provider_kind": "zulip",
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(chat_uuid),
        "operation_uuid": request["operation_uuid"],
    }
    assert sidecar["acl"]["stream_uuid"] == sidecar["stream_uuid"]
    commit.assert_called_once()

    replay = manager.finalize_incoming(
        _identity(),
        file_uuid,
        _finalize_request(request, allocation),
    )
    assert replay == result
    commit.assert_called_once()


def test_incoming_finalize_rejects_hash_mismatch_without_projection(
    tmp_path, monkeypatch
):
    manager, commit, account_uuid, chat_uuid, storage_path = _manager(
        tmp_path, monkeypatch
    )
    file_uuid = sys_uuid.uuid4()
    data = b"expected"
    request = _allocation_request(account_uuid, chat_uuid, data)
    allocation, _ = manager.allocate_incoming(_identity(), file_uuid, request)
    token = urllib.parse.parse_qs(
        urllib.parse.urlsplit(allocation["upload"]["url"]).query
    )["token"][0]
    manager.put_presigned_object(
        _identity(), token, allocation["upload"]["headers"], b"tampered"
    )
    with pytest.raises(files.FileTransferError) as raised:
        manager.finalize_incoming(
            _identity(),
            file_uuid,
            _finalize_request(request, allocation),
        )
    assert raised.value.error == "file_integrity_mismatch"
    commit.assert_not_called()
    transfer = manager.control_state.file_transfer_get(f"incoming:{file_uuid}")
    assert transfer["status"] == "invalidated"
    assert transfer["phase"] == "invalidated"
    assert not (storage_path / transfer["object_id"]).exists()
    with pytest.raises(files.FileTransferError) as replay_error:
        manager.finalize_incoming(
            _identity(), file_uuid, _finalize_request(request, allocation)
        )
    assert replay_error.value.error == "allocation_not_pending"

    replacement, created = manager.allocate_incoming(_identity(), file_uuid, request)
    assert created is False
    assert replacement["allocation_generation"] == 2


@pytest.mark.parametrize(
    "failed_phase",
    [
        "final_object_saved",
        "sidecar_saved",
        "projection_committed",
        "pending_deleted",
    ],
)
def test_finalize_recovers_after_each_durable_side_effect(
    tmp_path, monkeypatch, failed_phase
):
    manager, commit, account_uuid, chat_uuid, _ = _manager(tmp_path, monkeypatch)
    file_uuid = sys_uuid.uuid4()
    data = b"fault tolerant provider bytes"
    request = _allocation_request(account_uuid, chat_uuid, data)
    allocation, _ = manager.allocate_incoming(_identity(), file_uuid, request)
    token = urllib.parse.parse_qs(
        urllib.parse.urlsplit(allocation["upload"]["url"]).query
    )["token"][0]
    manager.put_presigned_object(
        _identity(), token, allocation["upload"]["headers"], data
    )

    original_put = manager.control_state.file_transfer_put
    failed = False

    def fail_once(key, transfer):
        nonlocal failed
        if not failed and transfer.get("phase") == failed_phase:
            failed = True
            raise RuntimeError("simulated post-side-effect crash")
        return original_put(key, transfer)

    manager.control_state.file_transfer_put = fail_once
    with pytest.raises(RuntimeError, match="simulated"):
        manager.finalize_incoming(
            _identity(), file_uuid, _finalize_request(request, allocation)
        )

    result = manager.finalize_incoming(
        _identity(), file_uuid, _finalize_request(request, allocation)
    )
    assert result["file_urn"] == f"urn:image:{file_uuid}"
    assert file_storage.read_workspace_file(file_uuid) == data
    assert commit.call_count in (1, 2)


def test_outgoing_authorization_is_assignment_scoped_and_method_bound(
    tmp_path, monkeypatch
):
    manager, _, account_uuid, chat_uuid, _ = _manager(tmp_path, monkeypatch)
    file_uuid = sys_uuid.uuid4()
    content = b"workspace file"
    metadata = {
        "project_id": manager.control_state.assignment(
            _identity(), account_uuid, chat_uuid
        )["chat"]["project_id"],
        "stream_uuid": manager.control_state.assignment(
            _identity(), account_uuid, chat_uuid
        )["chat"]["projection_stream_uuid"],
        "name": "notes.txt",
        "content_type": "text/plain",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    metadata["acl"] = {
        "mode": "stream_members",
        "stream_uuid": metadata["stream_uuid"],
    }
    metadata["authorized_user_uuids"] = [
        manager.control_state.assignment(_identity(), account_uuid, chat_uuid)[
            "account"
        ]["owner_user_uuid"]
    ]
    file_storage.save_workspace_file(file_uuid, content)
    manager.resolve_workspace_file = lambda _: metadata
    request = {
        "operation_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(chat_uuid),
        "file_urn": f"urn:file:{file_uuid}",
    }
    authorization = manager.authorize_outgoing(_identity(), sys_uuid.uuid4(), request)
    token = urllib.parse.parse_qs(
        urllib.parse.urlsplit(authorization["download"]["url"]).query
    )["token"][0]
    assert manager.get_presigned_object(_identity(), token) == content
    with pytest.raises(files.FileTransferError):
        manager.put_presigned_object(_identity(), token, {}, content)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda metadata: metadata.update(stream_uuid=str(sys_uuid.uuid4())),
        lambda metadata: metadata.update(acl={"mode": "public"}),
        lambda metadata: metadata.pop("stream_uuid"),
    ],
    ids=("cross-stream-same-project", "wrong-acl-mode", "missing-stream"),
)
def test_outgoing_authorization_fails_closed_outside_exact_stream_acl(
    tmp_path, monkeypatch, mutate
):
    manager, _, account_uuid, chat_uuid, _ = _manager(tmp_path, monkeypatch)
    assignment = manager.control_state.assignment(_identity(), account_uuid, chat_uuid)
    file_uuid = sys_uuid.uuid4()
    metadata = {
        "project_id": assignment["chat"]["project_id"],
        "stream_uuid": assignment["chat"]["projection_stream_uuid"],
        "acl": {
            "mode": "stream_members",
            "stream_uuid": assignment["chat"]["projection_stream_uuid"],
        },
        "name": "private.txt",
        "content_type": "text/plain",
        "size_bytes": 7,
        "sha256": hashlib.sha256(b"private").hexdigest(),
        "authorized_user_uuids": [assignment["account"]["owner_user_uuid"]],
    }
    mutate(metadata)
    manager.resolve_workspace_file = lambda _: metadata
    with pytest.raises(files.FileTransferError) as raised:
        manager.authorize_outgoing(
            _identity(),
            sys_uuid.uuid4(),
            {
                "operation_uuid": str(sys_uuid.uuid4()),
                "external_account_uuid": str(account_uuid),
                "external_chat_uuid": str(chat_uuid),
                "file_urn": f"urn:file:{file_uuid}",
            },
        )
    assert raised.value.status == 403
    assert raised.value.error == "file_access_denied"


def test_outgoing_authorization_requires_current_owner_file_access(
    tmp_path, monkeypatch
):
    manager, _, account_uuid, chat_uuid, _ = _manager(tmp_path, monkeypatch)
    assignment = manager.control_state.assignment(_identity(), account_uuid, chat_uuid)
    file_uuid = sys_uuid.uuid4()
    stream_uuid = assignment["chat"]["projection_stream_uuid"]
    manager.resolve_workspace_file = lambda _: {
        "project_id": assignment["chat"]["project_id"],
        "stream_uuid": stream_uuid,
        "acl": {"mode": "stream_members", "stream_uuid": stream_uuid},
        "name": "removed-user.txt",
        "content_type": "text/plain",
        "size_bytes": 1,
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "authorized_user_uuids": [],
    }
    with pytest.raises(files.FileTransferError) as raised:
        manager.authorize_outgoing(
            _identity(),
            sys_uuid.uuid4(),
            {
                "operation_uuid": str(sys_uuid.uuid4()),
                "external_account_uuid": str(account_uuid),
                "external_chat_uuid": str(chat_uuid),
                "file_urn": f"urn:file:{file_uuid}",
            },
        )
    assert raised.value.error == "file_access_denied"
