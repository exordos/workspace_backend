# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from oslo_config import cfg

from workspace.common import mutation_journal_opts
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal

conversation = journal.conversation


def test_topic_deletion_repairs_the_surviving_stream_preview(api, db, conversation):
    stream, messages, _ = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    response = api.post(
        native.STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Removed latest topic"},
    )
    assert response.status_code == 201, response.text
    topic = response.json()
    response = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "payload": {
                "kind": "markdown",
                "content": "Newest message in removed topic",
            },
        },
    )
    assert response.status_code == 201, response.text
    native._drain()
    assert (
        api.get(f"{native.STREAMS}{stream['uuid']}").json()["last_message_uuid"]
        == response.json()["uuid"]
    )
    assert api.delete(f"{native.STREAM_TOPICS}{topic['uuid']}").status_code == 204
    native._drain()
    result = api.get(f"{native.STREAMS}{stream['uuid']}")
    assert result.status_code == 200, result.text
    assert result.json()["last_message_uuid"] == messages[-1]["uuid"]
