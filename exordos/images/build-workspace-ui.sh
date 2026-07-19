#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -euo pipefail

UI_PATH="${WORKSPACE_UI_PATH:-/opt/workspace-ui}"
DIST_PATH="$UI_PATH/packages/web/dist"

echo "Building Workspace UI from the resolved master source"
(
    cd "$UI_PATH"
    npm ci --include=dev
    VITE_MESSENGER_ONLY=true npm run build --workspace=web
)

node - "$DIST_PATH" <<'NODE'
const fs = require("node:fs");
const path = require("node:path");

const [distPath] = process.argv.slice(2);
const index = fs.readFileSync(path.join(distPath, "index.html"), "utf8");
const manifest = JSON.parse(
  fs.readFileSync(path.join(distPath, "manifest.webmanifest"), "utf8"),
);

if (!index.includes('src="/assets/')) {
  throw new Error("bundle assets do not use the root path");
}
if (manifest.scope !== "/" || manifest.start_url !== "/") {
  throw new Error("PWA manifest does not use the root path");
}
NODE

if [[ -f "$UI_PATH/.workspace-ui-ref" ]]; then
    cp "$UI_PATH/.workspace-ui-ref" "$DIST_PATH/build-ref.txt"
fi
rm -rf -- "$UI_PATH/node_modules"
