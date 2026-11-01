# Message page query plans

The public message list reads `messenger_api_user_messages_v1`. Personal read,
mention, pin and star flags come from the corresponding sparse fact tables.
Their `(project_id, user_uuid, placement_uuid)` primary keys guarantee that each
left join contributes at most one row. Missing unread facts mean `read=true`;
missing mention, pin or star facts mean the corresponding flag is false.

Migration 0289 exposes these facts as joins instead of correlated `EXISTS`
projections. Positive flag predicates can then participate in join planning,
before a large history scan or sort. Keep the sparse relations at the beginning
of the view's join tree: appending them after history joins can exceed the
default PostgreSQL `join_collapse_limit` and prevent that reordering.

The migration replaces only the view. It preserves column names and types,
project/user scope, active membership, deletion checks, public serialization and
`(created_at, uuid)` keyset ordering. It does not rewrite messages or flag facts,
add indexes, or change PostgreSQL settings. Its downgrade restores the previous
view definition without modifying data.

Outgoing Zulip reference resolution accepts both placement and canonical
message UUIDs. Use two indexed branches with `UNION ALL` and a final `LIMIT 1`,
keeping the same visibility conditions in each branch. An `OR` across those
identities belongs to different underlying relations and can scan unrelated
history even for a single missing reference.

## Validation

Use a dedicated database: the integration harness recreates its public schema.
Run the real HTTP/ORM integration tests with `WORKSPACE_TEST_DB_URL` pointing to
that database and `WORKSPACE_REQUIRE_INTEGRATION_TESTS=1`:

```bash
.tox/develop/bin/python -m pytest \
  workspace/tests/integration/test_messenger_mention_query_plan.py \
  workspace/tests/integration/test_sparse_message_flag_pages.py \
  workspace/tests/integration/test_zulip_reference_lookups.py
```

The regression assertions measure actual history rows examined, including rows
removed by filters and repeated scans. Checking returned flag rows alone can
miss unsuccessful probes across the whole history. Require bounded history work
for empty and nonempty mention pages and for present/missing message references.
Also verify all flag filter combinations, both pagination directions with equal
timestamps, membership/deletion visibility, and upgrade/downgrade row equality.

For a live incident, compare completed HTTP request durations with
`pg_stat_activity` wait events and blocking PIDs before attributing latency to
locks or background import. A bounded read-only query experiment is diagnostic
evidence; it does not mean the migration or application update was deployed.
