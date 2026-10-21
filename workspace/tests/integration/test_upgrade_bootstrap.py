# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""The actual bootstrap must drain writers before DDL and serialize callers."""

import os
import pathlib
import shutil
import subprocess
import textwrap
import uuid

import psycopg
import pytest

from workspace.tests.integration import conftest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_bootstrap_quiesces_old_writers_before_migration_and_serializes_callers(
    tmp_path,
):
    if shutil.which("unshare") is None:
        pytest.skip("Linux PID namespaces are required")
    check = subprocess.run(
        ["unshare", "-Urpf", "--mount-proc", "true"], check=False, capture_output=True
    )
    if check.returncode:
        pytest.skip("Linux PID namespaces are unavailable")
    root = tmp_path
    gc = root / "runtime"
    bin_dir = gc / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (gc / "migrations").mkdir()
    (gc / "migrations" / "0001-fixture.py").write_text("# migration fixture\n")
    config = root / "config"
    config.mkdir()
    (config / "workspace.conf").write_text(
        "[db]\nconnection_url = " + conftest.TEST_DB_URL + "\n"
    )
    (bin_dir / "activate").write_text(
        "export PATH=" + str(bin_dir) + ":$PATH\ndeactivate() { :; }\n"
    )
    table = "cassi_upgrade_probe_" + uuid.uuid4().hex
    migration = bin_dir / "ra-apply-migration"
    migration.write_text(
        "#!"
        + str(pathlib.Path(os.sys.executable).absolute())
        + "\n"
        + textwrap.dedent("""
            import os,pathlib,psycopg
            with psycopg.connect(os.environ['PROBE_DATABASE_URL'],autocommit=True) as db:
                db.execute("SET lock_timeout='1000ms'")
                db.execute('ALTER TABLE '+os.environ['PROBE_TABLE']+' ADD COLUMN upgraded boolean')
            with pathlib.Path(os.environ['PROBE_ROOT'],'migrations.log').open('a') as out:
                out.write('applied\\n')
        """)
    )
    migration.chmod(0o755)
    writer = root / "workspace-messenger-api"
    writer.write_text(
        textwrap.dedent("""
        import os,pathlib,psycopg,time
        with psycopg.connect(os.environ['PROBE_DATABASE_URL']) as db:
            db.execute('SELECT * FROM '+os.environ['PROBE_TABLE']+' FOR SHARE')
            pathlib.Path(os.environ['PROBE_ROOT'],'writer-ready').touch()
            while True: time.sleep(1)
    """)
    )
    runner = root / "namespace_runner.py"
    runner.write_text(
        textwrap.dedent("""
        import json,os,pathlib,subprocess,sys,time
        root=pathlib.Path(os.environ['PROBE_ROOT'])
        writer=subprocess.Popen([sys.executable,str(root/'workspace-messenger-api')])
        try:
            deadline=time.monotonic()+10
            while not (root/'writer-ready').exists():
                assert writer.poll() is None
                if time.monotonic()>deadline: raise TimeoutError('writer startup')
                time.sleep(.02)
            jobs=[subprocess.Popen(['bash',os.environ['PROBE_BOOTSTRAP']],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) for _ in range(2)]
            results=[job.communicate(timeout=15)[0] for job in jobs]
            assert all(job.returncode==0 for job in jobs),results
            writer.wait(timeout=3)
            assert (root/'migrations.log').read_text().splitlines()==['applied']
            assert len((root/'run'/'bootstrap.ready').read_text().strip())==64
            print(json.dumps({'writer_drained':True,'concurrent_bootstraps':2,'migrations':1}))
        finally:
            if writer.poll() is None: writer.terminate()
            writer.wait(timeout=5)
    """)
    )
    env = {
        **os.environ,
        "WORKSPACE_PATH": str(gc),
        "WORKSPACE_CONFIG_DIR": str(config),
        "WORKSPACE_RUN_DIR": str(root / "run"),
        "WORKSPACE_FILES_PATH": str(root / "files"),
        "PROBE_DATABASE_URL": conftest.TEST_DB_URL,
        "PROBE_TABLE": table,
        "PROBE_ROOT": str(root),
        "PROBE_BOOTSTRAP": os.environ.get(
            "WORKSPACE_BOOTSTRAP_UNDER_TEST",
            str(ROOT / "exordos/images/backend-bootstrap.sh"),
        ),
    }
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as db:
        db.execute("CREATE TABLE " + table + "(id integer)")
        try:
            result = subprocess.run(
                ["unshare", "-Urpf", "--mount-proc", os.sys.executable, str(runner)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=35,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s AND column_name='upgraded'",
                (table,),
            ).fetchone() == ("upgraded",)
        finally:
            db.execute("DROP TABLE " + table)


def test_native_service_bootstrap_survives_the_default_start_timeout(tmp_path):
    """Exercise real systemd ExecStartPre timeout and the image's drop-in."""
    import pathlib
    import subprocess
    import uuid

    if shutil.which("sudo") is None:
        pytest.skip("Systemd with passwordless sudo is required")
    available = subprocess.run(
        ["sudo", "-n", "systemctl", "show", "--property=Version"],
        check=False,
        capture_output=True,
    )
    if available.returncode:
        pytest.skip("Systemd with passwordless sudo is required")
    prefix = "cassi-v5-bootstrap-" + uuid.uuid4().hex + "-"
    unit = prefix + "api.service"
    directory = pathlib.Path("/etc/systemd/system")
    unit_path = directory / unit
    dropin = directory / (prefix + ".service.d")
    installer = (
        pathlib.Path(__file__).resolve().parents[3]
        / "exordos/images/configure-bootstrap-timeout.sh"
    )
    helper = tmp_path / "configure-timeout.sh"
    helper.write_text(installer.read_text().replace("exordos_srv_workspace-", prefix))
    content = tmp_path / "unit.service"
    content.write_text(
        "[Service]\nType=oneshot\nTimeoutStartSec=100ms\nExecStartPre=/usr/bin/sleep 0.4\nExecStart=/usr/bin/true\n"
    )

    def command(*args, check=True):
        return subprocess.run(
            ["sudo", "-n", *args], check=check, capture_output=True, text=True
        )

    try:
        command("install", "-m", "0644", str(content), str(unit_path))
        command("systemctl", "daemon-reload")
        assert command("systemctl", "start", unit, check=False).returncode != 0
        assert (
            command("systemctl", "show", unit, "-p", "Result", "--value").stdout.strip()
            == "timeout"
        )
        command("bash", str(helper))
        command("systemctl", "daemon-reload")
        assert (
            command(
                "systemctl", "show", unit, "-p", "TimeoutStartUSec", "--value"
            ).stdout.strip()
            == "infinity"
        )
        command("systemctl", "start", unit)
        assert (
            command("systemctl", "show", unit, "-p", "Result", "--value").stdout.strip()
            == "success"
        )
    finally:
        command("systemctl", "stop", unit, check=False)
        command("rm", "-f", str(unit_path), str(dropin / "10-bootstrap-timeout.conf"))
        command("rmdir", str(dropin), check=False)
        command("systemctl", "daemon-reload")
        command("systemctl", "reset-failed", unit, check=False)
