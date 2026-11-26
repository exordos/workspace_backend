# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import pathlib

from restalchemy.storage.sql import migrations


def test_sdk_push_migration_follows_current_master_without_rebase_joins():
    path = pathlib.Path(__file__).resolve().parents[3] / "migrations"
    engine = migrations.MigrationEngine(migrations_path=str(path))

    steps = engine.get_all_migrations()
    sdk = "0306-Use-SDK-events-for-native-mobile-push-537aee.py"
    legacy = "0307-Convert-legacy-mobile-push-device-arrays-5057f9.py"
    apns = "0308-Register-APNs-push-devices-71fa20.py"
    assert steps[apns]["depends"] == [legacy]
    assert steps[legacy]["depends"] == [sdk]
    assert steps[sdk]["depends"] == ["0305-Add-stream-key-sharing-policy-58fcbc.py"]
    assert not any(
        "Join-" in name and ("mobile-push" in name or "native-mobile" in name)
        for name in steps
    )
    assert not any("Add-encrypted-mobile-push-outbox" in name for name in steps)


def test_mail_and_calendar_migrations_extend_the_master_chain():
    path = pathlib.Path(__file__).resolve().parents[3] / "migrations"
    engine = migrations.MigrationEngine(migrations_path=str(path))
    steps = engine.get_all_migrations()
    mail = "0309-Add-canonical-Mail-API-resources-09e2dd.py"
    calendar = "0310-Add-personal-Calendar-resources-ecab4c.py"
    assert steps[mail]["depends"] == ["0308-Register-APNs-push-devices-71fa20.py"]
    assert steps[calendar]["depends"] == [mail]
    pending = [engine.get_latest_migration()]
    while pending:
        migration = pending.pop()
        if migration == calendar:
            break
        pending.extend(steps[migration]["depends"])
    else:
        raise AssertionError(
            "Calendar migration is outside the current migration chain"
        )
