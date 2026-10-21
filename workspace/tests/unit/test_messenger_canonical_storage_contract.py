# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Keep retired storage and historical writers outside application code."""

import ast
import pathlib
import re


RETIRED_TABLES = (
    "m_folders",
    "m_folder_items",
    "m_workspace_messages",
    "m_workspace_message_reactions",
    "m_workspace_streams",
    "m_workspace_stream_bindings",
    "m_workspace_stream_topics",
    "m_workspace_user_message_flags",
    "m_workspace_user_topic_flags",
    "m_workspace_message_mentions_v1",
    "m_workspace_project_ingest_ranges_v2",
    "m_workspace_read_memberships_v1",
    "m_workspace_read_state_compaction_v1",
    "m_workspace_read_state_projects_v1",
    "m_workspace_topic_message_stats_v1",
    "m_workspace_user_read_chunks_v1",
    "m_workspace_user_read_revisions_v1",
    "m_workspace_user_topic_read_stats_v1",
)


def test_application_has_no_retired_sql_or_historical_test_imports():
    root = pathlib.Path(__file__).resolve().parents[3] / "workspace"
    tables = re.compile(r"\b(?:" + "|".join(RETIRED_TABLES) + r")\b")
    failures = []
    for path in root.rglob("*.py"):
        if "tests" in path.relative_to(root).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if tables.search(node.value):
                    failures.append(f"{path.relative_to(root)}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("workspace.tests"):
                    failures.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not failures, failures
