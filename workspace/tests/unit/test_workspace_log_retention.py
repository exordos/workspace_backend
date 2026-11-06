# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
import gzip
import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).parents[3]


def test_syslog_rotates_by_size_and_compresses_without_a_weekly_uncompressed_copy(
    tmp_path,
):
    rotator = shutil.which("logrotate")
    if rotator is None:
        pytest.skip("logrotate is required to exercise the guest retention policy")
    policy = (ROOT / "exordos/images/workspace-rsyslog.logrotate").read_text()
    # Exercise the actual policy on tiny private test files, never host logs.
    config = tmp_path / "logrotate.conf"
    config.write_text(
        policy.replace("/var/log/", str(tmp_path) + "/")
        .replace("maxsize 64M", "maxsize 1k")
        .replace("create 0640 syslog adm", "create 0600")
        .replace("/usr/lib/rsyslog/rsyslog-rotate", "true")
    )
    current = tmp_path / "syslog"
    message = b"Synthetic Workspace log entry\n" * 200
    for _ in range(10):
        current.write_bytes(message)
        subprocess.run(
            [rotator, "--state", str(tmp_path / "state"), str(config)],
            check=True,
            capture_output=True,
        )
        assert current.exists()
        assert not (tmp_path / "syslog.1").exists()
        assert gzip.decompress((tmp_path / "syslog.1.gz").read_bytes()) == message
    archives = list(tmp_path.glob("syslog.*.gz"))
    assert len(archives) == 7
    assert sum(path.stat().st_size for path in archives) < len(message)
    timer = (ROOT / "exordos/images/workspace-logrotate-timer.conf").read_text()
    assert "OnCalendar=\nOnCalendar=hourly" in timer
