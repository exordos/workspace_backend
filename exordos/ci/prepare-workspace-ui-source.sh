#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
REPOSITORY="${WORKSPACE_UI_REPOSITORY:-https://github.com/exordos/workspace_ui.git}"
OUTPUT_DIR="${WORKSPACE_UI_OUTPUT_DIR:-$PROJECT_ROOT/build/workspace-ui-source}"

if [[ "$(basename -- "$OUTPUT_DIR")" != "workspace-ui-source" ]]; then
    echo "Workspace UI output directory must end with workspace-ui-source" >&2
    exit 1
fi

OUTPUT_PARENT="$(dirname -- "$OUTPUT_DIR")"
mkdir -p "$OUTPUT_PARENT"
STAGING_DIR="$(mktemp -d "$OUTPUT_PARENT/.workspace-ui-source.XXXXXX")"
trap 'rm -rf -- "$STAGING_DIR"' EXIT

REPOSITORY_DIR="$STAGING_DIR/repository"
SOURCE_DIR="$STAGING_DIR/source"
git clone --quiet --filter=blob:none --no-checkout --no-tags \
    "$REPOSITORY" "$REPOSITORY_DIR"
git -C "$REPOSITORY_DIR" fetch --quiet --force --prune origin \
    "+refs/heads/master:refs/remotes/origin/master"

WORKSPACE_UI_SHA="$(
    git -C "$REPOSITORY_DIR" rev-parse "refs/remotes/origin/master^{commit}"
)"

mkdir -p "$SOURCE_DIR"
git -C "$REPOSITORY_DIR" archive "$WORKSPACE_UI_SHA" | tar -x -C "$SOURCE_DIR"
printf 'ref=master\ncommit=%s\n' \
    "$WORKSPACE_UI_SHA" > "$SOURCE_DIR/.workspace-ui-ref"
printf 'WORKSPACE_UI_REF=master\nWORKSPACE_UI_SHA=%s\n' \
    "$WORKSPACE_UI_SHA" > "$SOURCE_DIR/resolved-ref.env"

rm -rf -- "$OUTPUT_DIR"
mv "$SOURCE_DIR" "$OUTPUT_DIR"

cat "$OUTPUT_DIR/resolved-ref.env"
