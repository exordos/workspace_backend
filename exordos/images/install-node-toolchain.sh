#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation
#
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

set -euo pipefail

EXPECTED_NODE_MAJOR="${WORKSPACE_NODE_MAJOR:-22}"
EXPECTED_NPM_MAJOR="${WORKSPACE_NPM_MAJOR:-10}"
APT_LOCK_TIMEOUT_SECONDS="${WORKSPACE_APT_LOCK_TIMEOUT_SECONDS:-600}"
APT_CONFIG_DIR="${WORKSPACE_APT_CONFIG_DIR:-/etc/apt/apt.conf.d}"
NODESOURCE_LIST_PATH="${WORKSPACE_NODESOURCE_LIST_PATH:-/etc/apt/sources.list.d/nodesource.list}"
NODESOURCE_SOURCES_PATH="${WORKSPACE_NODESOURCE_SOURCES_PATH:-/etc/apt/sources.list.d/nodesource.sources}"
NODESOURCE_SETUP_URL="${WORKSPACE_NODESOURCE_SETUP_URL:-https://deb.nodesource.com/setup_${EXPECTED_NODE_MAJOR}.x}"
SUDO="${WORKSPACE_SUDO:-sudo}"
SETUP_SCRIPT=""

cleanup_setup_script() {
    if [[ -n "$SETUP_SCRIPT" ]]; then
        rm -f "$SETUP_SCRIPT"
        SETUP_SCRIPT=""
    fi
}

trap cleanup_setup_script EXIT

fail() {
    printf 'Node toolchain installation failed: %s\n' "$*" >&2
    return 1
}

run_with_apt_lock_timeout() {
    timeout --foreground "${APT_LOCK_TIMEOUT_SECONDS}s" \
        "$SUDO" env DEBIAN_FRONTEND=noninteractive \
        apt-get \
        -o "DPkg::Lock::Timeout=${APT_LOCK_TIMEOUT_SECONDS}" \
        "$@"
}

configure_apt_lock_timeout() {
    "$SUDO" install -d -m 0755 "$APT_CONFIG_DIR"
    printf 'DPkg::Lock::Timeout "%s";\n' "$APT_LOCK_TIMEOUT_SECONDS" \
        | "$SUDO" tee "$APT_CONFIG_DIR/99-workspace-lock-timeout" >/dev/null
}

read_major() {
    local command_name=$1
    local version

    version="$("$command_name" --version)"
    version="${version#v}"
    printf '%s\n' "${version%%.*}"
}

verify_supported_toolchain() {
    local node_major
    local npm_major

    command -v node >/dev/null 2>&1 \
        || fail "node executable is missing"
    command -v npm >/dev/null 2>&1 \
        || fail "npm executable is missing"

    node_major="$(read_major node)"
    npm_major="$(read_major npm)"
    [[ "$node_major" == "$EXPECTED_NODE_MAJOR" ]] \
        || fail "expected Node major ${EXPECTED_NODE_MAJOR}, got ${node_major}"
    [[ "$npm_major" == "$EXPECTED_NPM_MAJOR" ]] \
        || fail "expected npm major ${EXPECTED_NPM_MAJOR}, got ${npm_major}"
}

nodesource_repository_configured() {
    test -s "$NODESOURCE_LIST_PATH" \
        || test -s "$NODESOURCE_SOURCES_PATH"
}

install_nodesource_toolchain() {
    SETUP_SCRIPT="$(mktemp)"

    curl \
        --fail \
        --location \
        --silent \
        --show-error \
        --connect-timeout 30 \
        --max-time 180 \
        --retry 4 \
        --retry-all-errors \
        --retry-delay 5 \
        --retry-max-time 600 \
        --output "$SETUP_SCRIPT" \
        "$NODESOURCE_SETUP_URL"
    test -s "$SETUP_SCRIPT" \
        || fail "NodeSource setup script is empty"

    timeout --foreground "${APT_LOCK_TIMEOUT_SECONDS}s" \
        "$SUDO" -E bash "$SETUP_SCRIPT"
    nodesource_repository_configured \
        || fail "NodeSource repository was not configured"

    run_with_apt_lock_timeout install -y nodejs
    cleanup_setup_script
}

main() {
    configure_apt_lock_timeout
    run_with_apt_lock_timeout update

    if ! verify_supported_toolchain >/dev/null 2>&1; then
        install_nodesource_toolchain
    fi
    verify_supported_toolchain
}

main "$@"
