#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -o pipefail

STORE_PATH="${1:-/var/lib/workspace/external-bridge-control}"
STORE_OWNER="${2:-root:root}"
EXPECTED_SIZE_GIB="${3:-}"
FILESYSTEM_LABEL="ws-bridge-ctrl"
FSTAB_PATH="${FSTAB_PATH:-/etc/fstab}"

mounted_target() {
    findmnt -rn -o TARGET --target "$STORE_PATH" 2>/dev/null || true
}

labelled_device() {
    local devices=()

    mapfile -t devices < <(
        lsblk -rnp -o NAME,LABEL | awk -v label="$FILESYSTEM_LABEL" \
            '$2 == label {print $1}'
    )
    if [[ "${#devices[@]}" -gt 1 ]]; then
        echo "Multiple external bridge control filesystems found" >&2
        return 1
    fi
    if [[ "${#devices[@]}" -eq 1 ]]; then
        printf '%s\n' "${devices[0]}"
    fi
}

blank_device() {
    local expected_size_bytes=$((EXPECTED_SIZE_GIB * 1024 * 1024 * 1024))
    local candidates=()
    local device
    local device_type
    local size

    while read -r device device_type size; do
        [[ "$device_type" == "disk" ]] || continue
        [[ "$size" == "$expected_size_bytes" ]] || continue
        [[ -z "$(blkid -p -s TYPE -o value "$device" 2>/dev/null)" ]] || continue
        [[ "$(lsblk -nrpo NAME "$device" | wc -l)" -eq 1 ]] || continue
        [[ -z "$(wipefs -n "$device" 2>/dev/null)" ]] || continue
        candidates+=("$device")
    done < <(lsblk -bdnrpo NAME,TYPE,SIZE)

    if [[ "${#candidates[@]}" -ne 1 ]]; then
        echo "Expected exactly one blank ${EXPECTED_SIZE_GIB} GiB control disk; found ${#candidates[@]}" >&2
        return 1
    fi
    printf '%s\n' "${candidates[0]}"
}

directory_has_payload() {
    find "$1" -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit \
        | grep -q .
}

cleanup_migration_mount() {
    local migration_mount="$1"

    if mountpoint -q "$migration_mount"; then
        umount "$migration_mount"
    fi
    rmdir "$migration_mount"
}

migrate_existing_store() {
    local device="$1"
    local migration_mount

    if ! directory_has_payload "$STORE_PATH"; then
        return
    fi

    migration_mount="$(mktemp -d)"
    chmod 0700 "$migration_mount"
    if ! mount "$device" "$migration_mount"; then
        rmdir "$migration_mount"
        return 1
    fi
    if directory_has_payload "$migration_mount"; then
        cleanup_migration_mount "$migration_mount"
        echo "Both the root filesystem and control disk contain bridge state" >&2
        return 1
    fi
    if ! cp -a "$STORE_PATH"/. "$migration_mount"/; then
        cleanup_migration_mount "$migration_mount"
        return 1
    fi
    if ! sync -f "$migration_mount"; then
        cleanup_migration_mount "$migration_mount"
        return 1
    fi
    cleanup_migration_mount "$migration_mount"
}

prepare_filesystem() {
    local device
    local filesystem_uuid

    if [[ "$(mounted_target)" == "$STORE_PATH" ]]; then
        return
    fi

    device="$(labelled_device)"
    if [[ -z "$device" ]]; then
        device="$(blank_device)"
        mkfs.ext4 -L "$FILESYSTEM_LABEL" "$device"
    fi
    migrate_existing_store "$device"
    filesystem_uuid="$(blkid -s UUID -o value "$device")"
    if [[ -z "$filesystem_uuid" ]]; then
        echo "External bridge control filesystem has no UUID" >&2
        return 1
    fi

    if ! grep -Eq "^UUID=${filesystem_uuid}[[:space:]]" "$FSTAB_PATH"; then
        printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' \
            "$filesystem_uuid" "$STORE_PATH" >>"$FSTAB_PATH"
    fi
    mount "$STORE_PATH"
    if [[ "$(mounted_target)" != "$STORE_PATH" ]]; then
        echo "External bridge control filesystem was not mounted" >&2
        return 1
    fi
}

if [ -L "$STORE_PATH" ]; then
    echo "External bridge control store must not be a symlink" >&2
    exit 1
fi

mkdir -p "$STORE_PATH"
if [[ -n "$EXPECTED_SIZE_GIB" ]]; then
    prepare_filesystem
fi
chown "$STORE_OWNER" "$STORE_PATH"
chmod 0700 "$STORE_PATH"

test "$(stat -c '%U:%G' "$STORE_PATH")" = "$STORE_OWNER"
test "$(stat -c '%a' "$STORE_PATH")" = "700"
