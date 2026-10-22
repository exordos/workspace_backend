# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Preserve the history snapshot fences when topic-owned mutations are enabled."""

import datetime

import pytest
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import history
from workspace.common import mutation_journal_opts
from workspace.tests.integration import test_zulip_history_fingerprints as history_tests


if mutation_journal_opts.DOMAIN not in cfg.CONF:
    mutation_journal_opts.register_opts()


history_account = history_tests.history_account


@pytest.mark.parametrize("owned", [False, True])
def test_ready_history_snapshot_fences_older_flags_in_both_writer_modes(
    history_account, monkeypatch, owned
):
    cfg.CONF.set_override("enabled", owned, group=mutation_journal_opts.DOMAIN)
    try:
        with contexts.Context().session_manager() as session:
            history_tests.scan(history_account, [history_tests.message()], monkeypatch)
            history_tests.apply(history_account)
            old = datetime.datetime.now(datetime.timezone.utc)
            history_tests.scan(history_account, [history_tests.message()], monkeypatch)
            flags.record(history_account, [42], "read", True, old, event_id=10)
            row = session.execute(
                "SELECT value FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s AND message_id=42 AND flag='read'",
                (history_account.uuid,),
            ).fetchone()
            assert row["value"] is False
            assert history.batch(history_account, 1)["ready"]
    finally:
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)
