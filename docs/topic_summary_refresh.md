# Topic summary refresh query plans

`messenger_refresh_topic_summary_state()` maintains each user's
`summary_has_new_messages` flag when topic metadata, previews or message
visibility change. Without a summary the flag is null. Otherwise it reports
whether that user has a visible Markdown message after the saved boundary,
ordered by `(created_at, uuid)`. A boundary absent from the live topic-message
view leaves every visible Markdown message eligible.

Migration 0293 keeps the boundary lookup inside a limited lateral subquery,
using the existing project and placement UUID keys. The placement UUID is
unique, so the limit preserves cardinality. The lookup is skipped when the
summary is null. This prevents stale project statistics after a history import
from expanding the entire boundary view before evaluating the null-summary
condition. The migration replaces only the function; it adds no indexes,
changes no planner settings and does not rewrite stored messages or flags.
Downgrade restores the previous function.

The boundary still comes from `messenger_topic_messages_v1`, including its
deleted-message/topic/stream checks. The new-message predicate still uses
`messenger_api_user_messages_v1`, preserving personal visibility, active
membership, project/user scope and Markdown filtering. The optional user array
continues to limit the bindings refreshed; null selects all users and an empty
array selects none.

## Validation

Use a dedicated PostgreSQL database: the integration harness recreates its
public schema. Set `WORKSPACE_TEST_DB_URL` to that database and
`WORKSPACE_REQUIRE_INTEGRATION_TESTS=1`, then run:

```bash
.tox/develop/bin/python -m pytest \
  workspace/tests/integration/test_topic_summary_stale_statistics.py \
  workspace/tests/integration/test_topic_summary_runtime.py \
  workspace/tests/integration/test_sparse_message_flags.py \
  workspace/tests/integration/test_read_latency_query_plans.py
```

The regression freezes statistics after a separate 4096-message project and
imports 30,000 messages for the current user/project without analyzing them.
It requires topic-binding creation and a real HTTP topic POST to succeed with
the original five-second SQL budget, and retains an analyzed control. Actual
plans for null summaries must examine no canonical-message or placement rows,
including when a stale non-null flag needs clearing. Timing alone is not the
regression assertion. With a saved boundary, the boundary lookup must examine
at most one placement and one canonical-message row.

Small fixtures compare the old and new functions against explicit expected
flags for equal-timestamp boundaries, missing and deleted boundaries, deleted
topics/streams, inactive membership, user selection and a wrong project. The
HTTP scenario also checks shared-topic reads, forbidden creation by nonmembers
and another project, and adjacent message creation/read/page APIs. Migration
round trips must preserve topic state.
