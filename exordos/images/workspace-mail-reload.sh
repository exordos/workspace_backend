#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -o pipefail

if [ -s /etc/workspace/mail.conf ]; then
    /usr/local/bin/workspace-mail-bootstrap
fi
