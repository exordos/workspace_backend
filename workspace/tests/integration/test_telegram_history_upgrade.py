"""Adding backfill preserves progress from an existing realtime/private deployment."""

import importlib.util
import pathlib

import pytest

from workspace.tests.integration import test_telegram_runtime

telegram_runtime = test_telegram_runtime.telegram_runtime


@pytest.mark.parametrize("prepared", [False, True])
def test_adding_history_preserves_existing_cursors_and_prepared_pages(
    telegram_runtime, prepared
):
    _api, db, run = telegram_runtime

    async def exercise(service, account, peer):
        return account["uuid"], peer["peer_key"]

    identifier, peer_key = run(exercise)
    db.execute(
        "UPDATE telegram_peer_state SET history_before_id=4321,history_done=false,"
        "imported_messages=987,imported_files=12,imported_bytes=65536 "
        "WHERE external_account_uuid=%s AND peer_key=%s",
        (identifier, peer_key),
    )
    if prepared:
        db.execute(
            "INSERT INTO telegram_history_batches VALUES(%s,%s,4321,'[]','{}')",
            (identifier, peer_key),
        )
    else:
        db.execute("DROP TABLE telegram_history_batches")

    path = next((pathlib.Path(__file__).parents[3] / "migrations").glob("0300-*.py"))
    spec = importlib.util.spec_from_file_location("history_upgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.migration_step.upgrade(db)

    assert db.execute(
        "SELECT history_before_id,history_done,imported_messages,imported_files,imported_bytes "
        "FROM telegram_peer_state WHERE external_account_uuid=%s AND peer_key=%s",
        (identifier, peer_key),
    ).fetchone() == (4321, False, 987, 12, 65536)
    assert db.execute(
        "SELECT before_id,prepared,info FROM telegram_history_batches "
        "WHERE external_account_uuid=%s AND peer_key=%s",
        (identifier, peer_key),
    ).fetchall() == ([(4321, [], {})] if prepared else [])
