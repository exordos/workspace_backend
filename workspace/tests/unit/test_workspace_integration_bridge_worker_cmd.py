#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from workspace.cmd import workspace_integration_bridge_worker
from workspace.common import file_storage_opts


def test_worker_command_registers_file_storage_options():
    conf = workspace_integration_bridge_worker.CONF

    assert (
        conf[file_storage_opts.DOMAIN].default_type ==
        file_storage_opts.STORAGE_TYPE_FILE
    )
