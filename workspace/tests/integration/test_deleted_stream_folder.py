# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from oslo_config import cfg

from workspace.common import mutation_journal_opts
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal
from workspace.tests.integration import test_zulip_incoming as provider

conversation = journal.conversation


def test_stream_without_topics_updates_linked_custom_folder(api, db, conversation):
    stream, _, _ = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    folder = api.post(native.FOLDERS, json={"title": "Removed empty stream"}).json()
    response = api.post(
        native.FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream["uuid"],
            "chat_type": "stream",
        },
    )
    assert response.status_code == 201, response.text
    native._drain()
    assert (
        api.delete(f"{native.STREAM_TOPICS}{stream['default_topic_uuid']}").status_code
        == 204
    )
    native._drain()
    provider.publish()
    before = db.execute(
        "SELECT coalesce(max(epoch_version),0) FROM m_workspace_visible_events WHERE project_id=%s AND user_uuid=%s",
        (api.project_id, api.user_uuid),
    ).fetchone()[0]
    assert api.delete(f"{native.STREAMS}{stream['uuid']}").status_code == 204
    native._drain()
    provider.publish()
    row = db.execute(
        "SELECT payload FROM m_workspace_visible_events WHERE project_id=%s AND user_uuid=%s AND epoch_version>%s AND payload->>'kind'='folder.updated' AND payload->>'uuid'=%s ORDER BY epoch_version DESC LIMIT 1",
        (api.project_id, api.user_uuid, before, folder["uuid"]),
    ).fetchone()
    assert row is not None, (
        "Deletion must publish the new folder snapshot after the stream is gone"
    )
    assert row[0]["folder_items"] == []
