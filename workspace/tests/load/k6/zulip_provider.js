/* Copyright 2026 Genesis Corporation. */

import encoding from "k6/encoding";
import crypto from "k6/crypto";
import http from "k6/http";
import exec from "k6/execution";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const EXECUTE = __ENV.WORKSPACE_LOAD_EXECUTE === "1";
const RUN_ID = __ENV.WORKSPACE_LOAD_RUN_ID || "dry-run";
const credentialBundle = EXECUTE
    ? JSON.parse(open(__ENV.WORKSPACE_PROVIDER_CREDENTIALS_FILE))
    : { accounts: [] };
const accounts = credentialBundle.accounts;
const routes = EXECUTE
    ? JSON.parse(open(__ENV.WORKSPACE_PROVIDER_ROUTES_FILE))
    : {};
const mappedAccounts = accounts.filter((row) => row.stream_uuid && row.topic_uuid);

if (EXECUTE) {
    if (!__ENV.WORKSPACE_PROVIDER_CREDENTIALS_FILE || !__ENV.WORKSPACE_PROVIDER_ROUTES_FILE) {
        throw new Error("execution requires credential and route files");
    }
    if (accounts.length !== 150 || new Set(accounts.map((row) => row.workspace_user_uuid)).size !== 150) {
        throw new Error("exactly 150 independent Workspace/Zulip accounts are required");
    }
    if (new Set(accounts.map((row) => row.zulip_email)).size !== 150) {
        throw new Error("every Workspace user requires a separate Zulip account");
    }
    if (mappedAccounts.length === 0) {
        throw new Error("at least one account requires an authorized provider projection");
    }
    if (mappedAccounts.some((row) => !Number.isInteger(row.cursor_ordinal_base) ||
        !Number.isInteger(row.cursor_ordinal_limit) || row.cursor_ordinal_base < 0 ||
        row.cursor_ordinal_limit <= row.cursor_ordinal_base)) {
        throw new Error("every mapped account requires a reserved cursor ordinal range");
    }
    if (!routes.workspace_base_url || !routes.zulip_message_url || !routes.zulip_history_url) {
        throw new Error("the E2E workload requires Workspace and Zulip routes");
    }
}

const e2eErrors = new Rate("provider_e2e_errors");
const e2eLatency = new Trend("provider_e2e_visible_latency_ms", true);
const inboundConverged = new Counter("provider_inbound_converged");
const outboundConverged = new Counter("provider_outbound_converged");
const reconciledWithoutResend = new Counter("provider_reconciled_without_resend");

export const options = EXECUTE
    ? {
        scenarios: {
            provider_e2e_messages: {
                executor: "ramping-arrival-rate",
                exec: "providerE2EMessage",
                startRate: 1,
                timeUnit: "1m",
                preAllocatedVUs: 50,
                maxVUs: 150,
                stages: [
                    { duration: "5m", target: 150 },
                    { duration: "30m", target: 150 },
                    { duration: "5m", target: 400 },
                    { duration: "10m", target: 150 },
                ],
            },
            burst_reconnect: {
                executor: "per-vu-iterations",
                exec: "reconnectAndBackfill",
                vus: 30,
                iterations: 1,
                startTime: "35m",
                maxDuration: "5m",
            },
        },
        thresholds: {
            provider_e2e_errors: ["rate<=0.001"],
            provider_e2e_visible_latency_ms: ["p(95)<=5000", "p(99)<=10000"],
        },
    }
    : {
        scenarios: {
            dry_run: {
                executor: "shared-iterations",
                exec: "dryRun",
                vus: 1,
                iterations: 1,
            },
        },
    };

function accountForIteration() {
    return mappedAccounts[exec.scenario.iterationInTest % mappedAccounts.length];
}

function render(template, account) {
    return template
        .replaceAll("{account_uuid}", account.external_account_uuid)
        .replaceAll("{chat_uuid}", account.external_chat_uuid || "")
        .replaceAll("{stream_uuid}", account.stream_uuid)
        .replaceAll("{topic_uuid}", account.topic_uuid);
}

function workspaceUrl(path) {
    return `${routes.workspace_base_url.replace(/\/$/, "")}/api/workspace/v1/messenger/${path}`;
}

function workspaceParams(account, extraHeaders = {}) {
    return {
        headers: {
            Authorization: `Bearer ${account.workspace_access_token}`,
            "Content-Type": "application/json",
            "X-Workspace-Project-Id": account.project_id,
            ...extraHeaders,
        },
        tags: { account_ordinal: String(account.ordinal), system: "workspace" },
    };
}

function zulipParams(account) {
    const basic = encoding.b64encode(`${account.zulip_email}:${account.zulip_api_key}`);
    return {
        headers: {
            Authorization: `Basic ${basic}`,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        tags: { account_ordinal: String(account.ordinal), system: "zulip" },
    };
}

function deterministicUuid(value) {
    const digest = crypto.sha256(value, "hex");
    return `${digest.slice(0, 8)}-${digest.slice(8, 12)}-5${digest.slice(13, 16)}` +
        `-a${digest.slice(17, 20)}-${digest.slice(20, 32)}`;
}

function operation(account) {
    const iteration = exec.scenario.iterationInTest;
    const accountSequence = Math.floor(iteration / mappedAccounts.length) + 1;
    const sequence = String(iteration);
    const content = `provider load ${RUN_ID}-${account.ordinal}-${sequence}`;
    const cursorOrdinal = account.cursor_ordinal_base + accountSequence;
    if (cursorOrdinal > account.cursor_ordinal_limit) {
        throw new Error("account cursor ordinal reservation is exhausted");
    }
    return {
        operation_uuid: deterministicUuid(`operation:${RUN_ID}:${account.ordinal}:${sequence}`),
        provider_event_uuid: deterministicUuid(`event:${RUN_ID}:${account.ordinal}:${sequence}`),
        account_uuid: account.external_account_uuid,
        owner_user_uuid: account.workspace_user_uuid,
        stream_uuid: account.stream_uuid,
        topic_uuid: account.topic_uuid,
        workspace_message_uuid: null,
        direction: null,
        payload_sha256: crypto.sha256(JSON.stringify({ kind: "markdown", content }), "hex"),
        cursor_scope: `zulip:${account.external_account_uuid}`,
        cursor_ordinal: cursorOrdinal,
        outbox_idempotency_key: deterministicUuid(`outbox:${RUN_ID}:${account.ordinal}:${sequence}`),
        content,
    };
}

function runExpectation(row) {
    if (!["inbound", "outbound"].includes(row.direction)) {
        throw new Error("provider expectation requires an explicit direction");
    }
    console.log("WORKSPACE_RUN_EXPECTED_V1 " + JSON.stringify({
        schema_version: "workspace.messenger.run-ledger/v1",
        run_id: RUN_ID,
        source: "k6.provider",
        operation_uuid: row.operation_uuid,
        operation_kind: `provider.message.${row.direction}`,
        account_uuid: row.account_uuid,
        owner_user_uuid: row.owner_user_uuid,
        stream_uuid: row.stream_uuid,
        topic_uuid: row.topic_uuid,
        provider_event_uuid: row.provider_event_uuid,
        payload_sha256: row.payload_sha256,
        cursor_scope: row.cursor_scope,
        cursor_ordinal: row.cursor_ordinal,
        idempotency_key: row.outbox_idempotency_key,
    }));
}

function visibilityDiagnostic(row, outcome, providerResultId) {
    console.log("WORKSPACE_RUN_DIAGNOSTIC_V1 " + JSON.stringify({
        schema_version: "workspace.messenger.run-diagnostic/v1",
        run_id: RUN_ID,
        source: "k6.provider",
        operation_uuid: row.operation_uuid,
        operation_kind: `provider.message.${row.direction}`,
        outcome,
        visible_result_id: providerResultId || null,
    }));
}

function collectionRows(response) {
    if (response.status !== 200) return [];
    const body = response.json();
    if (Array.isArray(body)) return body;
    return body.items || body.messages || body.data || [];
}

function findWorkspaceMessage(account, content) {
    const query = `messages/?stream_uuid=${encodeURIComponent(account.stream_uuid)}` +
        `&topic_uuid=${encodeURIComponent(account.topic_uuid)}&page_limit=100`;
    const response = http.get(workspaceUrl(query), workspaceParams(account));
    const matches = collectionRows(response).filter(
        (message) => message.payload && message.payload.content &&
            message.payload.content.includes(content),
    );
    return { response, matches };
}

function findZulipMessage(account, content) {
    const response = http.get(render(routes.zulip_history_url, account), zulipParams(account));
    if (response.status !== 200) return { response, matches: [] };
    const matches = (response.json("messages") || []).filter(
        (message) => String(message.content || "").includes(content),
    );
    return { response, matches };
}

function pollVisible(find, account, content) {
    const timeout = Number(__ENV.WORKSPACE_PROVIDER_VISIBILITY_TIMEOUT_SECONDS || "15");
    const interval = Number(__ENV.WORKSPACE_PROVIDER_POLL_INTERVAL_SECONDS || "0.5");
    const attempts = Math.max(1, Math.ceil(timeout / interval));
    let result = { response: { status: 0 }, matches: [] };
    for (let attempt = 0; attempt < attempts; attempt += 1) {
        result = find(account, content);
        if (result.matches.length > 0) return result;
        sleep(interval);
    }
    return result;
}

function postZulipWithReconciliation(account, content) {
    const payload = {
        type: account.zulip_message_type,
        to: account.zulip_to,
        topic: account.zulip_topic,
        content,
    };
    let response = http.post(render(routes.zulip_message_url, account), payload, zulipParams(account));
    if (response.status === 0 || response.status >= 500) {
        sleep(Number(__ENV.WORKSPACE_PROVIDER_RECONCILE_DELAY_SECONDS || "2"));
        const history = findZulipMessage(account, content);
        if (history.matches.length > 0) {
            reconciledWithoutResend.add(1);
            return { response: history.response, providerMessageId: history.matches[0].id };
        }
        response = http.post(render(routes.zulip_message_url, account), payload, zulipParams(account));
    }
    return {
        response,
        providerMessageId: response.status === 200 ? response.json("id") : null,
    };
}

function sendInboundE2E(account, row) {
    row.direction = "inbound";
    runExpectation(row);
    const started = Date.now();
    const source = postZulipWithReconciliation(account, row.content);
    if (source.response.status !== 200) {
        e2eErrors.add(true);
        visibilityDiagnostic(row, "failed", null);
        return;
    }
    const destination = pollVisible(findWorkspaceMessage, account, row.content);
    const visible = destination.matches.length === 1;
    row.workspace_message_uuid = visible ? destination.matches[0].uuid : null;
    e2eLatency.add(Date.now() - started);
    e2eErrors.add(!visible);
    check(destination.response, { "Zulip message converged once in Workspace": () => visible });
    if (visible) inboundConverged.add(1);
    visibilityDiagnostic(
        row,
        visible ? "succeeded" : "failed",
        row.workspace_message_uuid,
    );
}

function sendOutboundE2E(account, row) {
    row.direction = "outbound";
    runExpectation(row);
    const started = Date.now();
    const response = http.post(
        workspaceUrl("messages/"),
        JSON.stringify({
            stream_uuid: row.stream_uuid,
            topic_uuid: row.topic_uuid,
            payload: { kind: "markdown", content: row.content },
        }),
        workspaceParams(account),
    );
    if (response.status !== 201) {
        e2eErrors.add(true);
        visibilityDiagnostic(row, "failed", null);
        return;
    }
    row.workspace_message_uuid = response.json("uuid");
    const destination = pollVisible(findZulipMessage, account, row.content);
    const visible = destination.matches.length === 1;
    e2eLatency.add(Date.now() - started);
    e2eErrors.add(!visible);
    check(destination.response, { "Workspace message converged once in Zulip": () => visible });
    if (visible) outboundConverged.add(1);
    visibilityDiagnostic(
        row,
        visible ? "succeeded" : "failed",
        visible ? String(destination.matches[0].id) : null,
    );
}

export function dryRun() {
    console.log(JSON.stringify({
        dry_run: true,
        execute_flag: "WORKSPACE_LOAD_EXECUTE=1",
        required_accounts: 150,
        steady_messages_per_minute: "100-200 (target 150)",
        burst_messages_per_minute: 400,
        inbound_outbound_ratio: "60/40",
        path: "Zulip -> connector -> Workspace and Workspace -> connector -> Zulip",
        target_or_credentials_loaded: false,
    }));
}

export function providerE2EMessage() {
    const account = accountForIteration();
    const row = operation(account);
    if ((__ITER + __VU) % 5 < 3) sendInboundE2E(account, row);
    else sendOutboundE2E(account, row);
}

export function reconnectAndBackfill() {
    const account = accounts[(__VU - 1) % Math.min(accounts.length, 30)];
    const resourceUrl = workspaceUrl(`external_accounts/${account.external_account_uuid}`);
    const current = http.get(resourceUrl, workspaceParams(account));
    const etag = current.headers.ETag || current.headers.Etag;
    if (current.status !== 200 || !etag) {
        e2eErrors.add(true);
        return;
    }
    const response = http.post(
        `${resourceUrl}/actions/reconnect/invoke`,
        JSON.stringify({
            settings: {
                kind: "zulip",
                server_url: account.zulip_server_url,
                email: account.zulip_email,
                api_key: account.zulip_api_key,
            },
        }),
        workspaceParams(account, { "If-Match": etag }),
    );
    e2eErrors.add(response.status !== 200);
}

export function handleSummary(data) {
    if (!__ENV.K6_SUMMARY_PATH) {
        return { stdout: JSON.stringify({ dry_run: !EXECUTE, metrics: data.metrics }) + "\n" };
    }
    return { [__ENV.K6_SUMMARY_PATH]: JSON.stringify(data, null, 2) + "\n" };
}
