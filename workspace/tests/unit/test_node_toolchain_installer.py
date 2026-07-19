# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import os
import pathlib
import subprocess


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
INSTALLER = PROJECT_ROOT / "exordos/images/install-node-toolchain.sh"


def _write_executable(path, body):
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _prepare_fake_commands(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("bash", "env", "mktemp", "rm", "tee"):
        (fake_bin / command).symlink_to(pathlib.Path("/usr/bin") / command)

    _write_executable(
        fake_bin / "sudo",
        """
if [[ "${1:-}" == "-E" ]]; then
    shift
fi
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "timeout",
        """
if [[ "${1:-}" == "--foreground" ]]; then
    shift
fi
shift
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "install",
        """
if [[ "${1:-}" == "-d" ]]; then
    /usr/bin/mkdir -p "${@: -1}"
    exit 0
fi
exec /usr/bin/install "$@"
""",
    )
    _write_executable(
        fake_bin / "curl",
        """
output=""
while (( $# > 0 )); do
    if [[ "$1" == "--output" ]]; then
        output=$2
        shift 2
    else
        shift
    fi
done
case "$MOCK_SETUP_MODE" in
    masked_failure)
        printf '#!/usr/bin/env bash\nexit 0\n' > "$output"
        ;;
    configure_repo)
        printf '%s\n' \
            '#!/usr/bin/env bash' \
            'set -euo pipefail' \
            '/usr/bin/mkdir -p "${WORKSPACE_NODESOURCE_LIST_PATH%/*}"' \
            'printf "deb mock nodesource\\n" > "$WORKSPACE_NODESOURCE_LIST_PATH"' \
            > "$output"
        ;;
    configure_deb822_repo)
        printf '%s\n' \
            '#!/usr/bin/env bash' \
            'set -euo pipefail' \
            '/usr/bin/mkdir -p "${WORKSPACE_NODESOURCE_SOURCES_PATH%/*}"' \
            'printf "Types: deb\\nURIs: https://deb.nodesource.com/node_22.x\\n" > "$WORKSPACE_NODESOURCE_SOURCES_PATH"' \
            > "$output"
        ;;
    *)
        exit 91
        ;;
esac
""",
    )
    _write_executable(
        fake_bin / "apt-get",
        """
printf '%s\n' "$*" >> "$MOCK_APT_LOG"
if [[ "${1:-}" == "-o" ]]; then
    shift 2
fi
if [[ "${1:-}" == "install" && "${MOCK_INSTALL_NODE:-0}" == "1" ]]; then
    printf '#!/usr/bin/env bash\nprintf "v22.99.0\\n"\n' > "$MOCK_BIN/node"
    /usr/bin/chmod 0755 "$MOCK_BIN/node"
fi
if [[ "${1:-}" == "install" && "${MOCK_INSTALL_NPM:-0}" == "1" ]]; then
    printf '#!/usr/bin/env bash\nprintf "10.99.0\\n"\n' > "$MOCK_BIN/npm"
    /usr/bin/chmod 0755 "$MOCK_BIN/npm"
fi
""",
    )
    return fake_bin


def _run_installer(tmp_path, *, setup_mode, install_node, install_npm):
    fake_bin = _prepare_fake_commands(tmp_path)
    apt_log = tmp_path / "apt.log"
    environment = {
        **os.environ,
        "PATH": str(fake_bin),
        "MOCK_APT_LOG": str(apt_log),
        "MOCK_BIN": str(fake_bin),
        "MOCK_INSTALL_NODE": "1" if install_node else "0",
        "MOCK_INSTALL_NPM": "1" if install_npm else "0",
        "MOCK_SETUP_MODE": setup_mode,
        "WORKSPACE_APT_CONFIG_DIR": str(tmp_path / "apt-config"),
        "WORKSPACE_NODESOURCE_LIST_PATH": str(tmp_path / "nodesource.list"),
        "WORKSPACE_NODESOURCE_SOURCES_PATH": str(tmp_path / "nodesource.sources"),
        "WORKSPACE_SUDO": str(fake_bin / "sudo"),
    }
    result = subprocess.run(
        [str(fake_bin / "bash"), str(INSTALLER)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    return result, apt_log


def test_node_toolchain_installer_rejects_masked_nodesource_failure(tmp_path):
    result, apt_log = _run_installer(
        tmp_path,
        setup_mode="masked_failure",
        install_node=False,
        install_npm=False,
    )

    assert result.returncode != 0
    assert "NodeSource repository was not configured" in result.stderr
    assert "install -y nodejs" not in apt_log.read_text(encoding="utf-8")


def test_node_toolchain_installer_rejects_success_without_npm(tmp_path):
    result, apt_log = _run_installer(
        tmp_path,
        setup_mode="configure_repo",
        install_node=True,
        install_npm=False,
    )

    assert result.returncode != 0
    assert "npm executable is missing" in result.stderr
    assert "install -y nodejs" in apt_log.read_text(encoding="utf-8")


def test_node_toolchain_installer_accepts_exact_supported_majors(tmp_path):
    result, apt_log = _run_installer(
        tmp_path,
        setup_mode="configure_repo",
        install_node=True,
        install_npm=True,
    )

    assert result.returncode == 0, result.stderr
    assert "DPkg::Lock::Timeout=600 update" in apt_log.read_text(encoding="utf-8")
    assert "DPkg::Lock::Timeout=600 install -y nodejs" in apt_log.read_text(
        encoding="utf-8"
    )


def test_node_toolchain_installer_accepts_deb822_nodesource_repository(tmp_path):
    result, apt_log = _run_installer(
        tmp_path,
        setup_mode="configure_deb822_repo",
        install_node=True,
        install_npm=True,
    )

    assert result.returncode == 0, result.stderr
    assert "install -y nodejs" in apt_log.read_text(encoding="utf-8")


def test_node_toolchain_installer_has_bounded_retry_and_postconditions():
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in installer
    assert "--retry 4" in installer
    assert "--retry-all-errors" in installer
    assert "--retry-max-time 600" in installer
    assert "--connect-timeout 30" in installer
    assert "--max-time 180" in installer
    assert 'test -s "$NODESOURCE_LIST_PATH"' in installer
    assert 'test -s "$NODESOURCE_SOURCES_PATH"' in installer
    assert "trap cleanup_setup_script EXIT" in installer
    assert "command -v node" in installer
    assert "command -v npm" in installer
