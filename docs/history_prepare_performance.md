# History conversion context

History preparation resolves its conversion catalog once per page with
`incoming_operations.conversion_context_base`. Mention, stream, topic and message
reference mappings hold preformatted UUIDs. Each message receives a small context
with layered mapping views, a private attachment mapping and its own identity.
The catalog maps are read-only. The batch-local UUID cache also serves placement
construction, reaction actors, repeated attachments and newly encountered
destination topics.

Precedence and insertion order match single-message conversion: the current
stream overrides catalog streams, while catalog topics and message references
override the current message's fallback identities. Single-event calls without
an explicit base still return ordinary nested dictionaries, preserving content
cache serialization. The converter flags and emitted Markdown are unchanged.

The topic fallback index retains the first matching `(stream_uuid, name, is_done)`
row, matching the former sequential search. New destinations do not rebuild or
modify the resolver snapshot. Catalog construction and topic selection take
`O(catalog + page)` work; this does not bound arbitrary Markdown conversion,
attachment I/O, SQL merge time or source HTTP requests.

## Reproducing the CPU benchmark

The benchmark module is introduced by this change. Before measuring an older
revision, copy **only the harness** into a separate baseline checkout; leave its
`workspace/` sources unchanged. For example, from the checkout containing this
change, prepare a comparison with release 3.0.10:

```shell
git worktree add --detach ../cassi-history-before 3.0.10
mkdir -p ../cassi-history-before/tools
cp tools/benchmark_history_prepare.py ../cassi-history-before/tools/
```

Create the development environment in each checkout with `tox -e develop`.
Confirm `.tox/develop/bin/python --version` matches in both; if necessary select
the same installed interpreter with tox's
`-x testenv:develop.base_python=/path/to/python` override. Run the following
commands from **each checkout's root**, on the same test machine:

```shell
.tox/develop/bin/python -m tools.benchmark_history_prepare --repeats 5
.tox/develop/bin/python -m tools.benchmark_history_prepare --repeats 5 --new-topics
```

The default dataset contains 1,000 messages, 512 people, 512 streams and 8,192
topics. `--messages`, `--people`, `--streams` and `--topics` change those sizes.
The benchmark executes the real `prepare_batch` and converter with synthetic
SQL responses and deterministic content UUIDs. Dataset creation and row checksum
calculation are outside the timed interval. JSON output includes every sample,
the median, Python version and a checksum of all prepared database rows.
Checksums must match between revisions. There is no external I/O or deployment.

Tests compare contexts with the frozen release implementation, including key
order, collisions, done topics, direct messages, mentions and quotes. They also
bound catalog UUID formatting and fallback scans, compare complete prepared
rows, and verify persisted message relations on PostgreSQL.

No schema, page size, scheduling or cursor change is required. A release deploy
must measure the history worker's phase timings and completed pages under its
actual workload; CPU benchmark ratios are not measured end-to-end throughput.
