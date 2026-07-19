# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import importlib.util
import pathlib
import types
import uuid as sys_uuid

import pytest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "exordos/images/workspace-smtp-ingress-attester.py"
SPEC = importlib.util.spec_from_file_location("smtp_ingress_attester", SCRIPT)
smtp_ingress_attester = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smtp_ingress_attester)


def _config(tmp_path):
    return types.SimpleNamespace(
        hold_path=tmp_path / "smtp-hold.json",
        instance_id="smtp_ingress:mail-test",
        drain_timeout=30,
        poll_interval=5,
    )


def _gate():
    return {
        "project_id": sys_uuid.UUID("10000000-0000-0000-0000-000000000001"),
        "gate_id": sys_uuid.UUID("20000000-0000-0000-0000-000000000002"),
        "lease_expires_at": "2026-07-18 16:00:00+00",
    }


def test_attester_holds_and_drains_before_acknowledging(tmp_path, monkeypatch):
    gate = _gate()
    order = []

    class Database:
        heartbeat_calls = 0

        def heartbeat(self):
            self.heartbeat_calls += 1
            order.append(f"heartbeat:{self.heartbeat_calls}")
            return (gate,)

        def acknowledge(self, project_id, gate_id):
            order.append(f"ack:{project_id}:{gate_id}")

    class Exim:
        proof_calls = 0

        def prove_quiesced(self):
            self.proof_calls += 1
            order.append(f"proof:{self.proof_calls}")
            return self.proof_calls > 1

        def stop_and_drain(self, timeout):
            order.append(f"drain:{timeout}")

    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), Database(), Exim()
    )
    original_write_hold = attester._write_hold

    def record_hold(gates):
        order.append("persistent-hold")
        original_write_hold(gates)

    monkeypatch.setattr(attester, "_write_hold", record_hold)
    attester.run_once()

    assert attester._read_hold()["gate_ids"] == [str(gate["gate_id"])]
    assert order.index("persistent-hold") < order.index("drain:30")
    assert order.index("drain:30") < order.index("heartbeat:2")
    assert order.index("heartbeat:2") < next(
        index for index, value in enumerate(order) if value.startswith("ack:")
    )


def test_generation_change_during_drain_fails_without_ack(tmp_path):
    first = _gate()
    second = {**first, "gate_id": sys_uuid.uuid4()}

    class Database:
        calls = 0
        acknowledged = False

        def heartbeat(self):
            self.calls += 1
            return (first,) if self.calls == 1 else (second,)

        def acknowledge(self, project_id, gate_id):
            self.acknowledged = True

    class Exim:
        def prove_quiesced(self):
            return True

    database = Database()
    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), database, Exim()
    )

    with pytest.raises(
        smtp_ingress_attester.AttestationError,
        match="generation changed",
    ):
        attester.run_once()

    assert database.acknowledged is False


def test_existing_hold_generation_is_never_discarded_by_a_new_gate(tmp_path):
    old_gate = _gate()
    new_gate = {**old_gate, "gate_id": sys_uuid.uuid4()}

    class Database:
        def heartbeat(self):
            return (new_gate,)

        def acknowledge(self, project_id, gate_id):
            assert project_id == new_gate["project_id"]
            assert gate_id == new_gate["gate_id"]

    class Exim:
        def prove_quiesced(self):
            return True

    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), Database(), Exim()
    )
    attester._write_hold((old_gate,))

    attester.run_once()

    assert set(attester._read_hold()["gate_ids"]) == {
        str(old_gate["gate_id"]),
        str(new_gate["gate_id"]),
    }


def test_hold_replace_and_removal_fsync_the_parent_directory(tmp_path, monkeypatch):
    directory_descriptors = set()
    synced_descriptors = []
    real_open = smtp_ingress_attester.os.open
    real_fsync = smtp_ingress_attester.os.fsync

    def tracked_open(path, flags, mode=0o777):
        descriptor = real_open(path, flags, mode)
        if pathlib.Path(path) == tmp_path:
            directory_descriptors.add(descriptor)
        return descriptor

    def tracked_fsync(descriptor):
        synced_descriptors.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr(smtp_ingress_attester.os, "open", tracked_open)
    monkeypatch.setattr(smtp_ingress_attester.os, "fsync", tracked_fsync)
    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), object(), object()
    )

    attester._write_hold((_gate(),))
    assert attester._config.hold_path.stat().st_mode & 0o777 == 0o600
    assert directory_descriptors & set(synced_descriptors)

    directory_descriptors.clear()
    synced_descriptors.clear()
    attester._remove_hold()
    assert directory_descriptors & set(synced_descriptors)


def test_persistent_hold_and_live_gate_both_block_exim_prestart(tmp_path):
    class Database:
        live = False

        def any_live_closed_gate(self):
            return self.live

    database = Database()
    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), database, object()
    )
    attester._write_hold((_gate(),))
    with pytest.raises(smtp_ingress_attester.AttestationError, match="held"):
        attester.exim_prestart()
    attester._remove_hold()
    database.live = True
    with pytest.raises(smtp_ingress_attester.AttestationError, match="closed"):
        attester.exim_prestart()


def test_only_explicit_compatibility_prestart_allows_unprovisioned_gate(
    tmp_path, monkeypatch
):
    missing_config = tmp_path / "not-provisioned.conf"
    compatibility_marker = tmp_path / "compatibility"
    enforced_marker = tmp_path / "enforced"
    hold_path = tmp_path / "hold.json"
    compatibility_marker.write_text("compatibility-prestart-v1\n", encoding="utf-8")
    monkeypatch.setattr(
        smtp_ingress_attester, "COMPATIBILITY_MARKER", compatibility_marker
    )
    monkeypatch.setattr(smtp_ingress_attester, "ENFORCED_MARKER", enforced_marker)
    monkeypatch.setattr(
        smtp_ingress_attester, "DEFAULT_HOLD_PATH", str(hold_path)
    )

    assert smtp_ingress_attester.main(
        ["--config", str(missing_config), "exim-prestart"]
    ) == 0
    with pytest.raises(
        smtp_ingress_attester.AttestationError,
        match="configuration is absent",
    ):
        smtp_ingress_attester.main(["--config", str(missing_config), "run"])

    enforced_marker.write_text("enforced-prestart-v1\n", encoding="utf-8")
    assert smtp_ingress_attester.main(
        ["--config", str(missing_config), "exim-prestart"]
    ) == 1
    enforced_marker.unlink()
    hold_path.write_text("{}\n", encoding="utf-8")
    assert smtp_ingress_attester.main(
        ["--config", str(missing_config), "exim-prestart"]
    ) == 1

def test_resume_requires_every_exact_released_generation(tmp_path):
    first = _gate()
    second = {**first, "gate_id": sys_uuid.uuid4()}

    class Database:
        released = {first["gate_id"]}

        def exact_gate_is_released(self, gate_id):
            return gate_id in self.released

        def any_live_closed_gate(self):
            return False

    class Exim:
        started = False

        def start(self):
            self.started = True

    database = Database()
    exim = Exim()
    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), database, exim
    )
    attester._write_hold((first, second))

    with pytest.raises(smtp_ingress_attester.AttestationError, match="Every"):
        attester.resume(first["gate_id"])
    assert exim.started is False
    assert attester._read_hold() is not None

    database.released.add(second["gate_id"])
    attester.resume(first["gate_id"])
    assert exim.started is True
    assert attester._read_hold() is None


def test_release_lookup_accepts_current_or_append_only_history():
    source = SCRIPT.read_text()
    release_lookup = source.split("def exact_gate_is_released", 1)[1].split(
        "def any_live_closed_gate", 1
    )[0]

    assert "m_messenger_writer_gates_v1" in release_lookup
    assert "m_messenger_writer_gate_releases_v1" in release_lookup
    assert "SELECT EXISTS" in release_lookup


def test_failed_resume_restores_hold_and_stops_any_partial_listener(tmp_path):
    gate = _gate()

    class Database:
        def exact_gate_is_released(self, gate_id):
            return gate_id == gate["gate_id"]

        def any_live_closed_gate(self):
            return False

    class Exim:
        stopped = False

        def start(self):
            raise smtp_ingress_attester.AttestationError("start failed")

        def stop_accepting(self):
            self.stopped = True

    exim = Exim()
    attester = smtp_ingress_attester.SMTPIngressAttester(
        _config(tmp_path), Database(), exim
    )
    attester._write_hold((gate,))

    with pytest.raises(smtp_ingress_attester.AttestationError, match="start failed"):
        attester.resume(gate["gate_id"])

    assert exim.stopped is True
    assert attester._read_hold()["gate_ids"] == [str(gate["gate_id"])]


def test_exim_drain_uses_queue_and_process_proofs():
    calls = []
    queue_counts = iter(("1\n", "0\n"))

    def runner(command, **kwargs):
        calls.append(tuple(command))
        if tuple(command) == ("exim4", "-bpc"):
            return types.SimpleNamespace(
                returncode=0,
                stdout=next(queue_counts),
                stderr="",
            )
        if tuple(command) == ("exiwhat",):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if tuple(command) == (
            "systemctl",
            "is-active",
            "--quiet",
            "exim4.service",
        ):
            return types.SimpleNamespace(returncode=3, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    clock = iter((0.0, 0.1, 0.2, 0.3))
    boundary = smtp_ingress_attester.EximBoundary(
        runner=runner,
        monotonic=lambda: next(clock),
        sleeper=lambda _seconds: None,
    )
    boundary.stop_and_drain(10)

    assert calls[0] == ("systemctl", "stop", "exim4.service")
    assert ("exim4", "-qff") in calls
    assert calls.count(("exim4", "-bpc")) == 2
    assert calls.count(("exiwhat",)) == 2
    for index, call in enumerate(calls):
        if call == ("exim4", "-bpc"):
            assert calls[index - 1] == ("exiwhat",)


def test_exim_no_process_data_is_quiescent():
    def runner(command, **kwargs):
        del command, kwargs
        return types.SimpleNamespace(
            returncode=0,
            stdout="No exim process data\n",
            stderr="",
        )

    boundary = smtp_ingress_attester.EximBoundary(runner=runner)

    assert boundary.active_processes() == ()


def test_exim_listener_inspection_error_fails_closed():
    def runner(command, **kwargs):
        del command, kwargs
        return types.SimpleNamespace(returncode=4, stdout="", stderr="")

    boundary = smtp_ingress_attester.EximBoundary(runner=runner)

    with pytest.raises(
        smtp_ingress_attester.AttestationError,
        match="listener state",
    ):
        boundary.listener_is_inactive()


def test_database_client_keeps_password_out_of_psql_argv():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="f\n", stderr="")

    client = smtp_ingress_attester.PostgreSQLGateClient(
        "postgresql://gate-user:private-value@db.internal/workspace",
        "smtp_ingress:test",
        runner=runner,
    )
    assert client.any_live_closed_gate() is False

    command, kwargs = calls[0]
    assert "private-value" not in " ".join(command)
    assert kwargs["env"]["PGPASSWORD"] == "private-value"
    assert "BEGIN;" not in kwargs["input"]
