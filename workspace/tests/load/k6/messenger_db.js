/* Copyright 2026 Genesis Corporation. */

import http from "k6/http";
import ws from "k6/ws";
import crypto from "k6/crypto";
import exec from "k6/execution";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const EXECUTE = __ENV.WORKSPACE_LOAD_EXECUTE === "1";
const BASE_URL = __ENV.WORKSPACE_BASE_URL || "";
const RUN_ID = __ENV.WORKSPACE_LOAD_RUN_ID || "dry-run";
const manifest = EXECUTE
    ? JSON.parse(open(__ENV.WORKSPACE_FIXTURE_MANIFEST))
    : { live_users: [] };
const credentials = EXECUTE
    ? JSON.parse(open(__ENV.WORKSPACE_CREDENTIALS_FILE))
    : { users: [] };

if (EXECUTE) {
    if (!BASE_URL || !__ENV.WORKSPACE_FIXTURE_MANIFEST || !__ENV.WORKSPACE_CREDENTIALS_FILE) {
        throw new Error("execution requires target, fixture manifest, and credential file");
    }
    if (manifest.live_users.length !== 150 || credentials.users.length !== 150) {
        throw new Error("the full workload requires exactly 150 independent users");
    }
    if (credentials.users.some((row) => !row.workspace_user_uuid)) {
        throw new Error("every credential entry requires workspace_user_uuid");
    }
}

const apiErrors = new Rate("workspace_api_errors");
const sendAcceptance = new Trend("workspace_send_acceptance_ms", true);
const websocketReady = new Trend("workspace_websocket_ready_ms", true);
const websocketMessages = new Counter("workspace_websocket_messages");

const executeScenarios = {
    websocket_users: {
        executor: "ramping-vus",
        exec: "websocketUser",
        startVUs: 0,
        stages: [
            { duration: "5m", target: 150 },
            { duration: "30m", target: 150 },
            { duration: "5m", target: 150 },
            { duration: "10m", target: 150 },
        ],
        gracefulRampDown: "30s",
    },
    rest_reads: {
        executor: "constant-arrival-rate",
        exec: "restRead",
        rate: 100,
        timeUnit: "1s",
        duration: "50m",
        preAllocatedVUs: 30,
        maxVUs: 120,
    },
    native_mutations: {
        executor: "constant-arrival-rate",
        exec: "nativeMutation",
        rate: 20,
        timeUnit: "1s",
        duration: "50m",
        preAllocatedVUs: 20,
        maxVUs: 100,
    },
};

export const options = EXECUTE
    ? {
        scenarios: executeScenarios,
        thresholds: {
            workspace_api_errors: ["rate<=0.001"],
            workspace_send_acceptance_ms: ["p(95)<=1000"],
            http_req_duration: ["p(95)<=750", "p(99)<=1500"],
            http_req_failed: ["rate<=0.001"],
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

function userForIteration() {
    const index = (__VU + __ITER - 1) % credentials.users.length;
    return credentials.users[index];
}

function params(user) {
    return {
        headers: {
            Authorization: `Bearer ${user.access_token}`,
            "Content-Type": "application/json",
            "X-Workspace-Project-Id": user.project_id,
        },
        tags: { workspace_user_ordinal: String(user.ordinal) },
    };
}

function messengerUrl(path) {
    return `${BASE_URL.replace(/\/$/, "")}/api/workspace/v1/messenger/${path}`;
}

function deterministicUuid(value) {
    const digest = crypto.sha256(value, "hex");
    return `${digest.slice(0, 8)}-${digest.slice(8, 12)}-5${digest.slice(13, 16)}` +
        `-a${digest.slice(17, 20)}-${digest.slice(20, 32)}`;
}

function runLedger(prefix, row) {
    console.log(prefix + " " + JSON.stringify(row));
}

export function dryRun() {
    console.log(JSON.stringify({
        dry_run: true,
        execute_flag: "WORKSPACE_LOAD_EXECUTE=1",
        required_live_users: 150,
        rest_requests_per_second: 100,
        native_mutations_per_second: 20,
        target_or_credentials_loaded: false,
    }));
}

export function restRead() {
    const user = userForIteration();
    const paths = [
        "streams/?page_limit=100",
        "stream_topics/?page_limit=100",
        "messages/?page_limit=100",
        "folders/?page_limit=100",
    ];
    const response = http.get(messengerUrl(paths[__ITER % paths.length]), params(user));
    const ok = check(response, { "REST read is successful": (value) => value.status === 200 });
    apiErrors.add(!ok);
}

export function nativeMutation() {
    const user = userForIteration();
    if (!user.stream_uuid || !user.topic_uuid) {
        throw new Error("each credential entry requires an authorized stream_uuid/topic_uuid");
    }
    const operation = `${RUN_ID}:${user.ordinal}:${exec.scenario.iterationInTest}`;
    const operationUuid = deterministicUuid(`native:${operation}`);
    const expected = {
        schema_version: "workspace.messenger.run-ledger/v1",
        run_id: RUN_ID,
        source: "k6.native",
        operation_uuid: operationUuid,
        operation_kind: "native.message.create",
        account_uuid: null,
        owner_user_uuid: user.workspace_user_uuid,
        stream_uuid: user.stream_uuid,
        topic_uuid: user.topic_uuid,
        provider_event_uuid: null,
        payload_sha256: crypto.sha256(JSON.stringify({
            kind: "markdown",
            content: `load fixture ${operation}`,
        }), "hex"),
        cursor_scope: null,
        cursor_ordinal: null,
        idempotency_key: operationUuid,
    };
    runLedger("WORKSPACE_RUN_EXPECTED_V1", expected);
    const response = http.post(
        messengerUrl("messages/"),
        JSON.stringify({
            stream_uuid: user.stream_uuid,
            topic_uuid: user.topic_uuid,
            payload: { kind: "markdown", content: `load fixture ${operation}` },
        }),
        params(user),
    );
    sendAcceptance.add(response.timings.duration);
    const ok = check(response, { "message mutation is accepted": (value) => value.status === 201 });
    apiErrors.add(!ok);
    runLedger("WORKSPACE_RUN_OBSERVED_V1", {
        ...expected,
        evidence_source: "workspace_response",
        outcome: ok ? "succeeded" : "failed",
        result_id: ok ? response.json("uuid") : null,
    });
}

export function websocketUser() {
    const user = credentials.users[(__VU - 1) % 150];
    const httpBase = BASE_URL.replace(/\/$/, "");
    const socketBase = httpBase.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
    const cursor = user.epoch_version || 0;
    const generation = encodeURIComponent(user.epoch_generation || "");
    const url = `${socketBase}/api/workspace/v1/events/ws` +
        `?last_epoch_version=${cursor}&epoch_generation=${generation}`;
    const started = Date.now();
    const response = ws.connect(url, params(user), (socket) => {
        socket.on("open", () => websocketReady.add(Date.now() - started));
        socket.on("message", () => websocketMessages.add(1));
        socket.setInterval(() => socket.ping(), 10000);
        socket.setTimeout(() => socket.close(), 60000);
    });
    apiErrors.add(!response || response.status !== 101);
    sleep(0.1);
}

export function handleSummary(data) {
    if (!__ENV.K6_SUMMARY_PATH) {
        return { stdout: JSON.stringify({ dry_run: !EXECUTE, metrics: data.metrics }) + "\n" };
    }
    return { [__ENV.K6_SUMMARY_PATH]: JSON.stringify(data, null, 2) + "\n" };
}
