# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from unittest import mock
import datetime
import uuid

import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip.dm import history
from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import v2_models
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_history_fingerprints as history_tests
from workspace.tests.integration import test_zulip_messages as messages_tests
from workspace.tests.integration import test_zulip_outgoing as outgoing_tests
from workspace.tests.integration import test_zulip_flags as flags_tests


outgoing = outgoing_tests.outgoing


def test_shared_native_observation_updates_canonical_without_invalidating_peer_snapshot(
    outgoing, monkeypatch, db
):
    _api, account, _channel, remote, zulip, _constructor = outgoing
    outgoing_tests.create(outgoing)
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"cassi-history-peer-{owner}")
    with contexts.Context().session_manager():
        peer = external_models.ExternalAccount(
            project_id=account.project_id,
            owner_user_uuid=owner,
            provider="zulip",
            external_account_id="8",
            settings=account.settings,
        )
        peer.insert()
        messages_tests._channel(peer)
        try:
            placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
                filters={"project_id": filters.EQ(account.project_id)}
            )
            canonical = v2_models.WorkspaceMessage.objects.get_one(
                filters={"project_id": filters.EQ(account.project_id)}
            )
            delivery = models.MessageDelivery.route(
                account.project_id, placement, canonical
            )
            delivery.zulip_message_id = remote["id"]
            delivery.update()
            data = {**remote, "reactions": []}
            first_id = (remote["id"] - 1) // 1000 * 1000 + 1
            for observer in (account, peer):
                history_tests.scan(observer, [data], monkeypatch, last_id=remote["id"])
                history_tests.apply(observer)
            changed = {
                **data,
                "content": data["content"] + " changed by provider",
                "last_edit_timestamp": data["timestamp"] + 10,
            }
            history_tests.scan(account, [changed], monkeypatch, last_id=remote["id"])
            history_tests.apply(account)
            current = v2_models.WorkspaceMessage.objects.get_one(
                filters={"uuid": filters.EQ(canonical.uuid)}
            )
            assert "changed by provider" in current.payload.content
            assert history.batch(peer, first_id)["ready"]
            # The peer's unchanged old observation does not undo the newer
            # shared canonical edit. A genuinely fresh snapshot is detected.
            history_tests.scan(peer, [data], monkeypatch, last_id=remote["id"])
            assert history_tests.pending(peer) == []
            history_tests.scan(peer, [changed], monkeypatch, last_id=remote["id"])
            assert len(history_tests.pending(peer)) == 1
            history_tests.apply(peer)
            delivery = models.MessageDelivery.objects.get_one(
                filters={"uuid": filters.EQ(delivery.uuid)}
            )
            delivery.observe(
                {
                    **changed,
                    "content": changed["content"] + " again",
                    "last_edit_timestamp": data["timestamp"] + 20,
                },
                observer=account,
                zulip=zulip,
            )
            assert all(
                not history.batch(observer, first_id)["ready"]
                for observer in (account, peer)
            )
        finally:
            peer.delete()


@pytest.mark.parametrize("component", ["flags", "reactions", "metadata"])
def test_native_delivery_hashes_skip_unchanged_and_unrelated_conversion(
    outgoing, monkeypatch, component
):
    _api, account, _channel, remote, _zulip, _constructor = outgoing
    outgoing_tests.create(outgoing)
    data = {**remote, "reactions": []}
    with contexts.Context().session_manager():
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        canonical = v2_models.WorkspaceMessage.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        delivery = models.MessageDelivery.route(
            account.project_id, placement, canonical
        )
        delivery.zulip_message_id = remote["id"]
        delivery.update()
        history_tests.scan(account, [data], monkeypatch, last_id=remote["id"])
        assert len(history_tests.apply(account)) == 1
        assert history.batch(account, 12001)["ready"]
    monkeypatch.setattr(
        content_conversion,
        "ConversionContext",
        mock.Mock(side_effect=AssertionError("unchanged native body converted")),
    )
    with contexts.Context().session_manager():
        history_tests.scan(account, [data], monkeypatch, last_id=remote["id"])
        assert history_tests.pending(account) == []
    if component == "flags":
        data = {**data, "flags": ["starred"]}
    elif component == "metadata":
        data = {**data, "last_edit_timestamp": remote["timestamp"] + 1}
    else:
        data = {
            **data,
            "reactions": [
                {
                    "user_id": 7,
                    "reaction_type": "unicode_emoji",
                    "emoji_code": "1f44d",
                    "emoji_name": "thumbs_up",
                }
            ],
        }
    with contexts.Context().session_manager():
        history_tests.scan(account, [data], monkeypatch, last_id=remote["id"])
        (task,) = history_tests.pending(account)
        assert task.content.snapshot[0]["components"] == [component]
        history_tests.apply(account)
        assert history.batch(account, 12001)["ready"]
        assert (
            models.ZulipMessage.objects.get_all(
                filters={"project_id": filters.EQ(account.project_id)}
            )
            == []
        )
        assert (
            v2_models.WorkspaceMessage.objects.get_one(
                filters={"project_id": filters.EQ(account.project_id)}
            ).uuid
            == canonical.uuid
        )


def test_global_read_all_respects_equal_snapshot_barriers_and_later_snapshot_changes(
    outgoing, monkeypatch
):
    api, account, _channel, remote, _zulip, _constructor = outgoing
    public = outgoing_tests.create(outgoing)
    data = {**remote, "flags": [], "reactions": []}
    with contexts.Context().session_manager():
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        canonical = v2_models.WorkspaceMessage.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        delivery = models.MessageDelivery.route(
            account.project_id, placement, canonical
        )
        delivery.zulip_message_id = remote["id"]
        delivery.update()
        history_tests.scan(account, [data], monkeypatch, last_id=remote["id"])
        history_tests.apply(account)
    flags_tests.reconcile()
    old = datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        history_tests.scan(account, [data], monkeypatch, last_id=remote["id"])
        flags.record(account, [0], "read", True, old, event_id=10)
    flags_tests.reconcile()
    assert api.get(f"/v1/messenger/messages/{public['uuid']}").json()["read"] is False
    with contexts.Context().session_manager():
        flags.record(
            account,
            [0],
            "read",
            True,
            datetime.datetime.now(datetime.timezone.utc),
            event_id=11,
        )
    flags_tests.reconcile()
    assert api.get(f"/v1/messenger/messages/{public['uuid']}").json()["read"] is True
    monkeypatch.setattr(
        content_conversion,
        "ConversionContext",
        mock.Mock(side_effect=AssertionError("flags converted text")),
    )
    with contexts.Context().session_manager():
        history_tests.scan(account, [data], monkeypatch, last_id=remote["id"])
        history_tests.apply(account)
    flags_tests.reconcile()
    assert api.get(f"/v1/messenger/messages/{public['uuid']}").json()["read"] is False
