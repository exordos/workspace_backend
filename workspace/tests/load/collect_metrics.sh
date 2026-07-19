#!/usr/bin/env bash
# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

set -eu

interval_seconds=5
execute=${WORKSPACE_METRICS_EXECUTE:-0}

if [ "$execute" != "1" ]; then
    printf '%s\n' '{"dry_run":true,"execute_flag":"WORKSPACE_METRICS_EXECUTE=1","interval_seconds":5,"credentials_loaded":false}'
    exit 0
fi

: "${WORKSPACE_METRICS_OUTPUT_DIR:?set an artifact directory outside the repository}"
: "${WORKSPACE_METRICS_DURATION_SECONDS:?set the sampling duration explicitly}"

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
output_root=$(realpath -m "$WORKSPACE_METRICS_OUTPUT_DIR")
case "$output_root" in
    "$repository_root"|"$repository_root"/*)
        printf '%s\n' 'metrics artifacts must be written outside the repository' >&2
        exit 2
        ;;
esac

case "$WORKSPACE_METRICS_DURATION_SECONDS" in
    *[!0-9]*|'') printf '%s\n' 'WORKSPACE_METRICS_DURATION_SECONDS must be an integer' >&2; exit 2 ;;
esac

mkdir -p "$WORKSPACE_METRICS_OUTPUT_DIR"
system_csv="$WORKSPACE_METRICS_OUTPUT_DIR/system.csv"
postgres_csv="$WORKSPACE_METRICS_OUTPUT_DIR/postgresql.csv"
printf '%s\n' 'timestamp,load1,mem_available_bytes,process_count,process_rss_bytes,cgroup_memory_bytes,cgroup_cpu_usec,api_status,api_seconds,provider_status,provider_seconds,s3_status,s3_seconds' > "$system_csv"
printf '%s\n' 'timestamp,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,temp_files,deadlocks,statement_calls,statement_exec_ms' > "$postgres_csv"

curl_probe() {
    url=$1
    if [ -z "$url" ]; then
        printf '%s' 'disabled,0'
        return
    fi
    if [ -n "${WORKSPACE_CURL_CONFIG_FILE:-}" ]; then
        curl --config "$WORKSPACE_CURL_CONFIG_FILE" --silent --show-error \
            --output /dev/null --max-time 4 --write-out '%{http_code},%{time_total}' "$url" || printf '%s' 'error,0'
    else
        curl --silent --show-error --output /dev/null --max-time 4 \
            --write-out '%{http_code},%{time_total}' "$url" || printf '%s' 'error,0'
    fi
}

read_cgroup_value() {
    name=$1
    path=${WORKSPACE_CGROUP_PATH:-}
    if [ -z "$path" ] || [ ! -r "$path/$name" ]; then
        printf '%s' 0
        return
    fi
    if [ "$name" = 'cpu.stat' ]; then
        awk '$1 == "usage_usec" { print $2 }' "$path/$name"
    else
        head -n 1 "$path/$name"
    fi
}

sample_postgres() {
    timestamp=$1
    if [ -z "${WORKSPACE_PGSERVICE:-}" ]; then
        printf '%s,%s\n' "$timestamp" 'disabled,0,0,0,0,0,0,0,0' >> "$postgres_csv"
        return
    fi
    PGSERVICE="$WORKSPACE_PGSERVICE" psql -X -v ON_ERROR_STOP=1 -At -F ',' <<'SQL' | while IFS= read -r row; do
BEGIN READ ONLY;
SELECT now() AT TIME ZONE 'UTC', numbackends, xact_commit, xact_rollback,
       blks_read, blks_hit, temp_files, deadlocks,
       COALESCE((SELECT sum(calls)::bigint FROM pg_stat_statements), 0),
       COALESCE((SELECT round(sum(total_exec_time)::numeric, 3) FROM pg_stat_statements), 0)
FROM pg_stat_database
WHERE datname = current_database();
COMMIT;
SQL
        case "$row" in
            BEGIN|COMMIT) ;;
            *) printf '%s\n' "$row" >> "$postgres_csv" ;;
        esac
    done
}

started=$(date +%s)
while :; do
    now=$(date +%s)
    elapsed=$((now - started))
    if [ "$elapsed" -ge "$WORKSPACE_METRICS_DURATION_SECONDS" ]; then
        break
    fi

    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    load1=$(awk '{ print $1 }' /proc/loadavg)
    mem_available=$(awk '$1 == "MemAvailable:" { print $2 * 1024 }' /proc/meminfo)
    if [ -n "${WORKSPACE_PROCESS_PATTERN:-}" ]; then
        process_stats=$(ps -eo rss=,comm= | awk -v pattern="$WORKSPACE_PROCESS_PATTERN" '$2 ~ pattern { count += 1; rss += $1 } END { printf "%d,%d", count, rss * 1024 }')
    else
        process_stats='0,0'
    fi
    cgroup_memory=$(read_cgroup_value memory.current)
    cgroup_cpu=$(read_cgroup_value cpu.stat)
    api_probe=$(curl_probe "${WORKSPACE_API_HEALTH_URL:-}")
    provider_probe=$(curl_probe "${WORKSPACE_PROVIDER_HEALTH_URL:-}")
    s3_probe=$(curl_probe "${WORKSPACE_S3_METRICS_URL:-}")
    printf '%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$timestamp" "$load1" "$mem_available" "$process_stats" \
        "$cgroup_memory" "$cgroup_cpu" "$api_probe" "$provider_probe,$s3_probe" \
        >> "$system_csv"
    sample_postgres "$timestamp"
    sleep "$interval_seconds"
done
