# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

from workspace.cmd import workspace_v3_worker
from workspace.services.workspace_v3_workers import agents


def test_capture_has_one_paced_process_independent_of_projection_worker_count():
    domain = workspace_v3_worker.DOMAIN
    workspace_v3_worker.CONF.set_override("workers", 4, group=domain)
    workspace_v3_worker.CONF.set_override(
        "baseline_page_interval_seconds", 0.2, group=domain
    )
    try:
        services = workspace_v3_worker.build_worker_services()
    finally:
        workspace_v3_worker.CONF.clear_override("workers", group=domain)
        workspace_v3_worker.CONF.clear_override(
            "baseline_page_interval_seconds", group=domain
        )
    baseline = [s for s in services if isinstance(s, agents.WorkspaceV3BaselineAgent)]
    projections = [
        s for s in services if isinstance(s, agents.WorkspaceV3ProjectionAgent)
    ]
    assert len(baseline) == 1
    assert baseline[0]._iter_min_period == 0.2
    assert baseline[0]._iter_pause > 0
    assert len(projections) == 4
    assert all(s._iter_min_period == 0 for s in projections)
