# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Exercise the bootstrap's actual config parser against libpq URI semantics."""

import pathlib
import re
import shlex
import subprocess
import sys

import psycopg.conninfo
import pytest


def test_image_exports_every_manifest_workspace_service(tmp_path):
    root = pathlib.Path(__file__).resolve().parents[3]
    script = (root / "exordos/images/backend-install.sh").read_text()
    binaries = script.split("WORKSPACE_BINARIES=(", 1)[1].split(")", 1)[0]
    manifest = (root / "exordos/manifests/workspace.yaml.j2").read_text()
    commands = set(re.findall(r"path: /usr/bin/(workspace-[\w-]+)\b", manifest))
    venv = tmp_path / "venv"
    exported = tmp_path / "exported"
    (venv / "bin").mkdir(parents=True)
    exported.mkdir()
    for command in commands:
        target = venv / "bin" / command
        target.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(command) + "\n")
        target.chmod(0o755)
    export_loop = script.split('for binary in "${WORKSPACE_BINARIES[@]}"; do', 1)[
        1
    ].split("\ndone", 1)[0]
    export_loop = export_loop.replace('"/usr/bin/$binary"', '"$EXPORT_PATH/$binary"')
    subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            "WORKSPACE_BINARIES=("
            + binaries
            + ")\n"
            + "VENV_PATH="
            + shlex.quote(str(venv))
            + "\n"
            + "EXPORT_PATH="
            + shlex.quote(str(exported))
            + "\n"
            + 'sudo() { "$@"; }\nfor binary in "${WORKSPACE_BINARIES[@]}"; do'
            + export_loop
            + "\ndone",
        ],
        check=True,
    )
    for command in commands:
        assert (exported / command).is_file(), command
        assert subprocess.check_output([exported / command], text=True) == command


@pytest.mark.parametrize(
    "query",
    [
        "user=user+name&password=p+q&host=/tmp/pg+socket&dbname=db+name",
        "user=&password=&host=&port=&dbname=",
        "user=user%2Bname&password=p%20q%26r%3Ds&dbname=db%2Bname",
        "password=%24%28touch%20never%29%3B%27%22%60%0A&dbname=test",
    ],
)
def test_bootstrap_query_values_match_libpq(tmp_path, query):
    uri = "postgresql://old-user:old-password@localhost:5432/old-db?" + query
    config = tmp_path / "workspace.conf"
    config.write_text("[db]\nconnection_url = " + uri + "\n")
    script = (
        pathlib.Path(__file__).resolve().parents[3]
        / "exordos/images/backend-bootstrap.sh"
    ).read_text()
    parser = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    result = subprocess.run(
        [sys.executable, "-", str(config)],
        input=parser,
        text=True,
        capture_output=True,
        check=True,
    )
    # Decode shell assignments as data; never execute their contents.
    values = dict(value.split("=", 1) for value in shlex.split(result.stdout))
    expected = psycopg.conninfo.conninfo_to_dict(uri)
    for field, option in (
        ("WORKSPACE_PG_ENDPOINT", "host"),
        ("WORKSPACE_PG_PORT", "port"),
        ("WORKSPACE_PG_USER", "user"),
        ("WORKSPACE_PG_PASS", "password"),
        ("WORKSPACE_PG_DB", "dbname"),
    ):
        assert values[field] == expected[option]
