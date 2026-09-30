# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Import compatibility for published migration 0152 only."""

from migrations import legacy_identity_reconciliation as identity_linking

__all__ = ["identity_linking"]
