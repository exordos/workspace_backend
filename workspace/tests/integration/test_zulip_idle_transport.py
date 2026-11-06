# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest


def test_long_poll_survives_idle_network_state_expiry():
    required = ("unshare", "ip", "nft")
    if any(shutil.which(command) is None for command in required):
        reason = "Linux network namespaces, iproute2 and nftables are required"
        if os.environ.get("WORKSPACE_REQUIRE_INTEGRATION_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    namespace = ["unshare", "-Urn"]
    if os.environ.get("WORKSPACE_PRIVILEGED_NAMESPACE_TEST") == "1":
        namespace = ["sudo", "-n", "unshare", "-n"]
    check = subprocess.run(namespace + ["true"], capture_output=True)
    if check.returncode:
        reason = "A private Linux network namespace is unavailable"
        if os.environ.get("WORKSPACE_REQUIRE_INTEGRATION_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    helper = pathlib.Path(__file__).parent / "helpers" / "zulip_idle_transport.py"
    result = subprocess.run(
        namespace + [sys.executable, "-B", str(helper)],
        cwd=helper.parents[4],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"delivered": True}
