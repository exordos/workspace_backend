# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import dataclasses
import datetime
import hashlib
import json
import re
import secrets
import typing
import urllib.parse
import uuid as sys_uuid

from cryptography.hazmat.primitives.ciphers import aead
from cryptography import exceptions as cryptography_exceptions
from oslo_config import cfg
import requests
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters

from workspace.common import topic_summary_opts
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models


DEFAULT_SYSTEM_PROMPT = (
    "Summarize the topic briefly. Preserve decisions, owners, unresolved "
    "questions, and important constraints. Write the summary in the primary "
    "language used in the topic."
)
ENDPOINT_MANAGE_PERMISSION = "workspace.topic_summary_endpoint.manage"
SETTINGS_MANAGE_PERMISSION = "workspace.topic_summary_settings.manage"
MAX_MESSAGES_PER_SUMMARY = 100
MAX_IMAGES_PER_SUMMARY = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PROVIDER_ATTEMPTS = 3
RETRY_BASE_SECONDS = 30
_IMAGE_URN_RE = re.compile(
    r"urn:image:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})(?:\?[^\s)\]>]*)?"
)
_ENDPOINT_DEFAULTS: dict[str, object] = {
    "enabled": True,
    "priority": 100,
    "supports_vision": False,
    "supports_reasoning": False,
    "temperature": 0.2,
    "max_output_tokens": 512,
    "top_p": 1.0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
}
_ENDPOINT_REQUIRED_FIELDS = {"uuid", "name", "base_url", "model", "api_key"}
_ENDPOINT_MUTABLE_FIELDS = {
    "name",
    "base_url",
    "model",
    "enabled",
    "priority",
    "supports_vision",
    "supports_reasoning",
    "temperature",
    "max_output_tokens",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "api_key",
}
_ENDPOINT_FLOAT_FIELDS = {
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
}


@dataclasses.dataclass(frozen=True)
class SummaryMessage:
    uuid: sys_uuid.UUID
    user_uuid: sys_uuid.UUID
    content: str
    image_uuids: tuple[sys_uuid.UUID, ...]


@dataclasses.dataclass(frozen=True)
class ImageAttachment:
    uuid: sys_uuid.UUID
    content_type: str
    size_bytes: int
    storage_type: str
    storage_object_id: str


@dataclasses.dataclass(frozen=True)
class Endpoint:
    uuid: sys_uuid.UUID
    name: str
    base_url: str
    model: str
    priority: int
    supports_vision: bool
    supports_reasoning: bool
    temperature: float
    max_output_tokens: int
    top_p: float
    presence_penalty: float
    frequency_penalty: float
    api_key: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class SummaryWork:
    topic_uuid: sys_uuid.UUID
    project_id: sys_uuid.UUID
    stream_uuid: sys_uuid.UUID
    actor_user_uuid: sys_uuid.UUID
    boundary_message_uuid: sys_uuid.UUID
    previous_summary: str | None
    effective_prompt: str
    reasoning_effort: str | None
    prompt_fingerprint: str
    messages: tuple[SummaryMessage, ...]
    images: tuple[ImageAttachment, ...]
    requires_vision: bool
    include_images: bool
    topic_claim_token: sys_uuid.UUID
    endpoint_claim_token: sys_uuid.UUID
    endpoint: Endpoint
    attempt: int


class ProviderCallError(Exception):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def configured_secret_key() -> str:
    key = cfg.CONF[topic_summary_opts.DOMAIN].secret_encryption_key
    if not key:
        raise RuntimeError("Topic summary credential encryption is not configured")
    return typing.cast(str, key)


def _encryption_key(key_material: str) -> bytes:
    return hashlib.sha256(key_material.encode("utf-8")).digest()


def encrypt_api_key(
    endpoint_uuid: object,
    api_key: str,
    key_material: str,
) -> dict[str, str]:
    nonce = secrets.token_bytes(12)
    ciphertext = aead.AESGCM(_encryption_key(key_material)).encrypt(
        nonce,
        api_key.encode("utf-8"),
        str(endpoint_uuid).encode("ascii"),
    )
    return {
        "algorithm": "AES-256-GCM",
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def decrypt_api_key(
    endpoint_uuid: object,
    envelope: dict[str, str],
    key_material: str,
) -> str:
    if set(envelope) != {"algorithm", "nonce", "ciphertext"}:
        raise RuntimeError("Invalid endpoint credential envelope")
    if envelope["algorithm"] != "AES-256-GCM":
        raise RuntimeError("Unsupported endpoint credential envelope")
    try:
        plaintext = aead.AESGCM(_encryption_key(key_material)).decrypt(
            base64.urlsafe_b64decode(envelope["nonce"]),
            base64.urlsafe_b64decode(envelope["ciphertext"]),
            str(endpoint_uuid).encode("ascii"),
        )
    except (cryptography_exceptions.InvalidTag, ValueError, TypeError) as exc:
        raise RuntimeError("Invalid endpoint credential envelope") from exc
    return plaintext.decode("utf-8")


def _normalize_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ra_exc.ValidationErrorException()
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(value) > 2048
    ):
        raise ra_exc.ValidationErrorException()
    return value


def normalize_endpoint_values(
    values: dict[str, typing.Any],
    *,
    creating: bool,
) -> tuple[dict[str, typing.Any], str | None]:
    names = set(values)
    if creating:
        if not _ENDPOINT_REQUIRED_FIELDS <= names:
            raise ra_exc.ValidationErrorException()
        if not names <= _ENDPOINT_REQUIRED_FIELDS | set(_ENDPOINT_DEFAULTS):
            raise ra_exc.ValidationErrorException()
    elif not names or not names <= _ENDPOINT_MUTABLE_FIELDS:
        raise ra_exc.ValidationErrorException()

    result = dict(_ENDPOINT_DEFAULTS) if creating else {}
    result.update({name: value for name, value in values.items() if name != "api_key"})
    if "uuid" in result:
        try:
            result["uuid"] = sys_uuid.UUID(str(result["uuid"]))
        except (TypeError, ValueError) as exc:
            raise ra_exc.ValidationErrorException() from exc
    for name in ("name", "model"):
        if name not in result:
            continue
        value = result[name]
        maximum = 255
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ra_exc.ValidationErrorException()
        result[name] = value.strip()
    if "base_url" in result:
        result["base_url"] = _normalize_base_url(result["base_url"])
    for name in ("enabled", "supports_vision", "supports_reasoning"):
        if name in result and not isinstance(result[name], bool):
            raise ra_exc.ValidationErrorException()
    if "priority" in result and (
        isinstance(result["priority"], bool)
        or not isinstance(result["priority"], int)
        or not 0 <= result["priority"] <= 1_000_000
    ):
        raise ra_exc.ValidationErrorException()
    if "max_output_tokens" in result and (
        isinstance(result["max_output_tokens"], bool)
        or not isinstance(result["max_output_tokens"], int)
        or not 1 <= result["max_output_tokens"] <= 32768
    ):
        raise ra_exc.ValidationErrorException()
    float_ranges = {
        "temperature": (0.0, 2.0),
        "top_p": (0.0, 1.0),
        "presence_penalty": (-2.0, 2.0),
        "frequency_penalty": (-2.0, 2.0),
    }
    for name in _ENDPOINT_FLOAT_FIELDS & set(result):
        float_value = result[name]
        if isinstance(float_value, bool) or not isinstance(
            float_value, (int, float)
        ):
            raise ra_exc.ValidationErrorException()
        minimum_float, maximum_float = float_ranges[name]
        normalized_float = float(float_value)
        if not minimum_float <= normalized_float <= maximum_float:
            raise ra_exc.ValidationErrorException()
        result[name] = normalized_float
    api_key = values.get("api_key")
    if api_key is not None and (
        not isinstance(api_key, str) or not api_key or len(api_key) > 8192
    ):
        raise ra_exc.ValidationErrorException()
    return result, api_key


def get_topic_summary_settings(
    session: typing.Any,
    project_id: object,
) -> models.WorkspaceTopicSummarySettings:
    session.execute(
        """
        INSERT INTO m_workspace_topic_summary_project_settings (
            project_id, enabled, created_at, updated_at
        ) VALUES (%s, FALSE, NOW(), NOW())
        ON CONFLICT (project_id) DO NOTHING
        """,
        (project_id,),
    )
    return models.WorkspaceTopicSummarySettings.objects.get_one(
        filters={"project_id": dm_filters.EQ(project_id)},
        session=session,
    )


def update_topic_summary_settings(
    session: typing.Any,
    project_id: object,
    values: dict[str, typing.Any],
) -> models.WorkspaceTopicSummarySettings:
    if set(values) != {"global_enabled", "project_enabled"} or any(
        not isinstance(values[name], bool) for name in values
    ):
        raise ra_exc.ValidationErrorException()
    session.execute(
        """
        UPDATE m_workspace_topic_summary_global_settings
        SET enabled = %s, updated_at = NOW()
        WHERE singleton = TRUE
        """,
        (values["global_enabled"],),
    )
    session.execute(
        """
        INSERT INTO m_workspace_topic_summary_project_settings (
            project_id, enabled, created_at, updated_at
        ) VALUES (%s, %s, NOW(), NOW())
        ON CONFLICT (project_id) DO UPDATE
        SET enabled = EXCLUDED.enabled, updated_at = NOW()
        """,
        (project_id, values["project_enabled"]),
    )
    return get_topic_summary_settings(session, project_id)


def create_endpoint(
    session: typing.Any,
    values: dict[str, typing.Any],
    key_material: str,
) -> models.WorkspaceLLMEndpoint:
    endpoint_values, api_key = normalize_endpoint_values(values, creating=True)
    endpoint_values["credential_present"] = True
    endpoint = models.WorkspaceLLMEndpoint(**endpoint_values)
    endpoint.insert(session=session)
    secret = models.WorkspaceLLMEndpointSecret(
        uuid=endpoint.uuid,
        endpoint_uuid=endpoint.uuid,
        envelope=encrypt_api_key(endpoint.uuid, typing.cast(str, api_key), key_material),
    )
    secret.insert(session=session)
    return endpoint


def list_endpoints(session: typing.Any) -> list[models.WorkspaceLLMEndpoint]:
    endpoints = models.WorkspaceLLMEndpoint.objects.get_all(
        order_by={"priority": "asc"},
        session=session,
    )
    return order_endpoints(endpoints)


def order_endpoints(
    endpoints: typing.Iterable[models.WorkspaceLLMEndpoint],
) -> list[models.WorkspaceLLMEndpoint]:
    return sorted(endpoints, key=lambda endpoint: (endpoint.priority, endpoint.uuid))


def update_endpoint(
    session: typing.Any,
    endpoint: models.WorkspaceLLMEndpoint,
    values: dict[str, typing.Any],
    key_material: str,
) -> models.WorkspaceLLMEndpoint:
    endpoint_values, api_key = normalize_endpoint_values(values, creating=False)
    for name, value in endpoint_values.items():
        endpoint.properties[name].set_value_force(value)
    endpoint.update(session=session)
    if api_key is not None:
        envelope = encrypt_api_key(endpoint.uuid, api_key, key_material)
        session.execute(
            """
            INSERT INTO m_workspace_llm_endpoint_secrets (
                uuid, endpoint_uuid, envelope
            ) VALUES (%s, %s, %s)
            ON CONFLICT (endpoint_uuid) DO UPDATE
            SET envelope = EXCLUDED.envelope
            """,
            (endpoint.uuid, endpoint.uuid, json.dumps(envelope)),
        )
    return endpoint


def _row_payload(row: typing.Any) -> dict[str, typing.Any]:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid stored message payload")
    return payload


def _prompt_fingerprint(prompt: str, reasoning_effort: str | None) -> str:
    content = f"{prompt}\x1f{reasoning_effort or ''}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load_summary_messages(
    session: typing.Any,
    candidate: typing.Any,
) -> tuple[SummaryMessage, ...]:
    rows = session.execute(
        """
        WITH previous_boundary AS (
            SELECT created_at, uuid
            FROM m_workspace_messages
            WHERE uuid = %s AND project_id = %s AND topic_uuid = %s
        ), selected AS (
            SELECT message.uuid, message.user_uuid, message.payload,
                   message.created_at
            FROM m_workspace_messages AS message
            LEFT JOIN previous_boundary ON TRUE
            WHERE message.project_id = %s
              AND message.topic_uuid = %s
              AND (message.created_at, message.uuid) <= (
                    SELECT created_at, uuid
                    FROM m_workspace_messages
                    WHERE uuid = %s
              )
              AND (
                    previous_boundary.uuid IS NULL
                    OR (message.created_at, message.uuid) >
                       (previous_boundary.created_at, previous_boundary.uuid)
              )
            ORDER BY message.created_at, message.uuid
            LIMIT %s
        )
        SELECT uuid, user_uuid, payload
        FROM selected
        ORDER BY created_at, uuid
        """,
        (
            candidate["summary_last_message_uuid"],
            candidate["project_id"],
            candidate["topic_uuid"],
            candidate["project_id"],
            candidate["topic_uuid"],
            candidate["snapshot_boundary_message_uuid"],
            MAX_MESSAGES_PER_SUMMARY,
        ),
    ).fetchall()
    messages = []
    for row in rows:
        payload = _row_payload(row)
        if payload.get("kind") != "markdown" or not isinstance(
            payload.get("content"), str
        ):
            raise RuntimeError("Unsupported stored message payload")
        content = payload["content"]
        image_uuids = tuple(
            dict.fromkeys(
                sys_uuid.UUID(match.group(1)) for match in _IMAGE_URN_RE.finditer(content)
            )
        )
        messages.append(
            SummaryMessage(
                uuid=sys_uuid.UUID(str(row["uuid"])),
                user_uuid=sys_uuid.UUID(str(row["user_uuid"])),
                content=content,
                image_uuids=image_uuids,
            )
        )
    return tuple(messages)


def _load_images(
    session: typing.Any,
    candidate: typing.Any,
    messages: tuple[SummaryMessage, ...],
) -> tuple[ImageAttachment, ...]:
    image_uuids = tuple(
        dict.fromkeys(
            image_uuid
            for message in messages
            for image_uuid in message.image_uuids
        )
    )[:MAX_IMAGES_PER_SUMMARY]
    if not image_uuids:
        return ()
    rows = session.execute(
        """
        SELECT uuid, content_type, size_bytes, storage_type, storage_object_id
        FROM m_workspace_files
        WHERE project_id = %s
          AND stream_uuid = %s
          AND uuid = ANY(%s)
          AND content_type LIKE 'image/%%'
          AND size_bytes <= %s
        """,
        (
            candidate["project_id"],
            candidate["stream_uuid"],
            list(image_uuids),
            MAX_IMAGE_BYTES,
        ),
    ).fetchall()
    by_uuid = {sys_uuid.UUID(str(row["uuid"])): row for row in rows}
    return tuple(
        ImageAttachment(
            uuid=image_uuid,
            content_type=by_uuid[image_uuid]["content_type"],
            size_bytes=by_uuid[image_uuid]["size_bytes"],
            storage_type=by_uuid[image_uuid]["storage_type"],
            storage_object_id=by_uuid[image_uuid]["storage_object_id"],
        )
        for image_uuid in image_uuids
        if image_uuid in by_uuid
    )


def _endpoint_from_row(
    row: typing.Any,
    key_material: str,
) -> Endpoint:
    envelope = row["envelope"]
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    return Endpoint(
        uuid=sys_uuid.UUID(str(row["uuid"])),
        name=row["name"],
        base_url=row["base_url"],
        model=row["model"],
        priority=row["priority"],
        supports_vision=row["supports_vision"],
        supports_reasoning=row["supports_reasoning"],
        temperature=float(row["temperature"]),
        max_output_tokens=row["max_output_tokens"],
        top_p=float(row["top_p"]),
        presence_penalty=float(row["presence_penalty"]),
        frequency_penalty=float(row["frequency_penalty"]),
        api_key=decrypt_api_key(row["uuid"], envelope, key_material),
    )


def _claim_endpoint(
    session: typing.Any,
    *,
    now: datetime.datetime,
    claim_seconds: int,
    key_material: str,
    require_vision: bool,
    after_priority: int | None = None,
    after_uuid: sys_uuid.UUID | None = None,
) -> tuple[Endpoint, sys_uuid.UUID] | None:
    after_clause = ""
    params: list[object] = [require_vision, now]
    if after_priority is not None and after_uuid is not None:
        after_clause = "AND (endpoint.priority, endpoint.uuid) > (%s, %s)"
        params.extend((after_priority, after_uuid))
    row = session.execute(
        f"""
        SELECT endpoint.*, secret.envelope
        FROM m_workspace_llm_endpoints AS endpoint
        JOIN m_workspace_llm_endpoint_secrets AS secret
          ON secret.endpoint_uuid = endpoint.uuid
        WHERE endpoint.enabled = TRUE
          AND (%s = FALSE OR endpoint.supports_vision = TRUE)
          AND (
                endpoint.claim_expires_at IS NULL
                OR endpoint.claim_expires_at <= %s
          )
          {after_clause}
        ORDER BY endpoint.priority, endpoint.uuid
        FOR UPDATE OF endpoint SKIP LOCKED
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    claim_token = sys_uuid.uuid4()
    session.execute(
        """
        UPDATE m_workspace_llm_endpoints
        SET claim_token = %s, claim_expires_at = %s, updated_at = NOW()
        WHERE uuid = %s
        """,
        (
            claim_token,
            now + datetime.timedelta(seconds=claim_seconds),
            row["uuid"],
        ),
    )
    return _endpoint_from_row(row, key_material), claim_token


def _queue_waiting_topic(
    session: typing.Any,
    candidate: typing.Any,
    boundary_message_uuid: sys_uuid.UUID,
    prompt: str,
    reasoning_effort: str | None,
    prompt_fingerprint: str,
    now: datetime.datetime,
    error_code: str,
) -> None:
    session.execute(
        """
        INSERT INTO m_workspace_topic_summary_jobs (
            topic_uuid, project_id, status, attempt, boundary_message_uuid,
            effective_prompt, reasoning_effort, prompt_fingerprint,
            next_attempt_at, last_error_code, created_at, updated_at
        ) VALUES (
            %s, %s, 'waiting_endpoint', 0, %s, %s, %s, %s,
            %s, %s, NOW(), NOW()
        )
        ON CONFLICT (topic_uuid) DO UPDATE
        SET project_id = EXCLUDED.project_id,
            status = 'waiting_endpoint',
            attempt = CASE
                WHEN m_workspace_topic_summary_jobs.boundary_message_uuid
                        IS DISTINCT FROM EXCLUDED.boundary_message_uuid
                  OR m_workspace_topic_summary_jobs.prompt_fingerprint
                        IS DISTINCT FROM EXCLUDED.prompt_fingerprint
                THEN 0
                ELSE m_workspace_topic_summary_jobs.attempt
            END,
            boundary_message_uuid = EXCLUDED.boundary_message_uuid,
            effective_prompt = EXCLUDED.effective_prompt,
            reasoning_effort = EXCLUDED.reasoning_effort,
            prompt_fingerprint = EXCLUDED.prompt_fingerprint,
            next_attempt_at = EXCLUDED.next_attempt_at,
            last_error_code = EXCLUDED.last_error_code,
            claim_token = NULL,
            claim_expires_at = NULL,
            endpoint_uuid = NULL,
            endpoint_claim_token = NULL,
            updated_at = NOW()
        """,
        (
            candidate["topic_uuid"],
            candidate["project_id"],
            boundary_message_uuid,
            prompt,
            reasoning_effort,
            prompt_fingerprint,
            now + datetime.timedelta(seconds=3),
            error_code,
        ),
    )


def claim_summary_work(
    session: typing.Any,
    *,
    now: datetime.datetime,
    key_material: str,
    topic_claim_seconds: int,
    endpoint_claim_seconds: int,
) -> SummaryWork | None:
    candidate = session.execute(
        """
        SELECT
            topic.uuid AS topic_uuid,
            topic.project_id,
            topic.stream_uuid,
            topic.summary,
            topic.summary_last_message_uuid,
            topic.summary_system_prompt,
            topic.summary_reasoning_effort,
            snapshot.uuid AS snapshot_boundary_message_uuid,
            actor.user_uuid AS actor_user_uuid,
            job.status AS job_status,
            job.attempt AS job_attempt,
            job.boundary_message_uuid AS job_boundary_message_uuid,
            job.prompt_fingerprint AS job_prompt_fingerprint
        FROM m_workspace_stream_topics AS topic
        JOIN m_workspace_topic_summary_project_settings AS project_settings
          ON project_settings.project_id = topic.project_id
         AND project_settings.enabled = TRUE
        JOIN m_workspace_topic_summary_global_settings AS global_settings
          ON global_settings.singleton = TRUE
         AND global_settings.enabled = TRUE
         AND topic.summary_enabled = TRUE
        JOIN LATERAL (
            SELECT bounded.uuid
            FROM (
                SELECT message.uuid, message.created_at
                FROM m_workspace_messages AS message
                LEFT JOIN m_workspace_messages AS previous_boundary
                  ON previous_boundary.uuid = topic.summary_last_message_uuid
                WHERE message.project_id = topic.project_id
                  AND message.topic_uuid = topic.uuid
                  AND (
                        previous_boundary.uuid IS NULL
                        OR (message.created_at, message.uuid) > (
                            previous_boundary.created_at,
                            previous_boundary.uuid
                        )
                  )
                ORDER BY message.created_at, message.uuid
                LIMIT %s
            ) AS bounded
            ORDER BY bounded.created_at DESC, bounded.uuid DESC
            LIMIT 1
        ) AS snapshot ON TRUE
        JOIN LATERAL (
            SELECT binding.user_uuid
            FROM m_workspace_stream_bindings AS binding
            WHERE binding.project_id = topic.project_id
              AND binding.stream_uuid = topic.stream_uuid
            ORDER BY
                CASE binding.role
                    WHEN 'owner' THEN 0
                    WHEN 'administrator' THEN 1
                    ELSE 2
                END,
                binding.user_uuid
            LIMIT 1
        ) AS actor ON TRUE
        LEFT JOIN m_workspace_topic_summary_jobs AS job
          ON job.topic_uuid = topic.uuid
        WHERE (
                job.topic_uuid IS NULL
                OR job.boundary_message_uuid IS DISTINCT FROM snapshot.uuid
                OR job.effective_prompt IS DISTINCT FROM
                   COALESCE(topic.summary_system_prompt, %s)
                OR job.reasoning_effort IS DISTINCT FROM
                   topic.summary_reasoning_effort
                OR (
                    job.status IN ('retry_wait', 'waiting_endpoint')
                    AND job.next_attempt_at <= %s
                    AND job.attempt < %s
                )
                OR (
                    job.status = 'running'
                    AND job.claim_expires_at <= %s
                )
          )
        ORDER BY COALESCE(job.next_attempt_at, topic.updated_at), topic.uuid
        FOR UPDATE OF topic SKIP LOCKED
        LIMIT 1
        """,
        (
            MAX_MESSAGES_PER_SUMMARY,
            DEFAULT_SYSTEM_PROMPT,
            now,
            MAX_PROVIDER_ATTEMPTS,
            now,
        ),
    ).fetchone()
    if candidate is None:
        return None

    prompt = candidate["summary_system_prompt"] or DEFAULT_SYSTEM_PROMPT
    reasoning_effort = candidate["summary_reasoning_effort"]
    prompt_fingerprint = _prompt_fingerprint(prompt, reasoning_effort)
    messages = _load_summary_messages(session, candidate)
    images = _load_images(session, candidate, messages)
    if not messages:
        return None
    boundary_message_uuid = messages[-1].uuid
    has_image = bool(images)
    vision_exists = session.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM m_workspace_llm_endpoints
            WHERE enabled = TRUE AND supports_vision = TRUE
        ) AS present
        """
    ).fetchone()["present"]
    requires_vision = bool(has_image and vision_exists)
    claimed_endpoint = _claim_endpoint(
        session,
        now=now,
        claim_seconds=endpoint_claim_seconds,
        key_material=key_material,
        require_vision=requires_vision,
    )
    if claimed_endpoint is None:
        error_code = (
            "vision_endpoint_busy"
            if requires_vision
            else "endpoint_unavailable"
        )
        _queue_waiting_topic(
            session,
            candidate,
            boundary_message_uuid,
            prompt,
            reasoning_effort,
            prompt_fingerprint,
            now,
            error_code,
        )
        return None

    endpoint, endpoint_claim_token = claimed_endpoint
    same_snapshot = (
        candidate["job_boundary_message_uuid"] == boundary_message_uuid
        and candidate["job_prompt_fingerprint"] == prompt_fingerprint
    )
    attempt = (candidate["job_attempt"] or 0) + 1 if same_snapshot else 1
    topic_claim_token = sys_uuid.uuid4()
    session.execute(
        """
        INSERT INTO m_workspace_topic_summary_jobs (
            topic_uuid, project_id, status, attempt, boundary_message_uuid,
            effective_prompt, reasoning_effort, prompt_fingerprint,
            claim_token, claim_expires_at, endpoint_uuid,
            endpoint_claim_token, created_at, updated_at
        ) VALUES (
            %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s, %s, %s,
            NOW(), NOW()
        )
        ON CONFLICT (topic_uuid) DO UPDATE
        SET project_id = EXCLUDED.project_id,
            status = 'running',
            attempt = EXCLUDED.attempt,
            boundary_message_uuid = EXCLUDED.boundary_message_uuid,
            effective_prompt = EXCLUDED.effective_prompt,
            reasoning_effort = EXCLUDED.reasoning_effort,
            prompt_fingerprint = EXCLUDED.prompt_fingerprint,
            claim_token = EXCLUDED.claim_token,
            claim_expires_at = EXCLUDED.claim_expires_at,
            endpoint_uuid = EXCLUDED.endpoint_uuid,
            endpoint_claim_token = EXCLUDED.endpoint_claim_token,
            next_attempt_at = NULL,
            last_error_code = NULL,
            updated_at = NOW()
        """,
        (
            candidate["topic_uuid"],
            candidate["project_id"],
            attempt,
            boundary_message_uuid,
            prompt,
            reasoning_effort,
            prompt_fingerprint,
            topic_claim_token,
            now + datetime.timedelta(seconds=topic_claim_seconds),
            endpoint.uuid,
            endpoint_claim_token,
        ),
    )
    return SummaryWork(
        topic_uuid=sys_uuid.UUID(str(candidate["topic_uuid"])),
        project_id=sys_uuid.UUID(str(candidate["project_id"])),
        stream_uuid=sys_uuid.UUID(str(candidate["stream_uuid"])),
        actor_user_uuid=sys_uuid.UUID(str(candidate["actor_user_uuid"])),
        boundary_message_uuid=boundary_message_uuid,
        previous_summary=candidate["summary"],
        effective_prompt=prompt,
        reasoning_effort=reasoning_effort,
        prompt_fingerprint=prompt_fingerprint,
        messages=messages,
        images=images,
        requires_vision=requires_vision,
        include_images=bool(has_image and endpoint.supports_vision),
        topic_claim_token=topic_claim_token,
        endpoint_claim_token=endpoint_claim_token,
        endpoint=endpoint,
        attempt=attempt,
    )


def _image_parts(
    work: SummaryWork,
    message: SummaryMessage,
) -> list[dict[str, typing.Any]]:
    by_uuid = {image.uuid: image for image in work.images}
    parts: list[dict[str, typing.Any]] = [
        {"type": "text", "text": message.content}
    ]
    for image_uuid in message.image_uuids:
        image = by_uuid.get(image_uuid)
        if image is None:
            continue
        data = file_storage.read_workspace_file(
            image.uuid,
            storage_type=image.storage_type,
            storage_object_id=image.storage_object_id,
        )
        if len(data) > MAX_IMAGE_BYTES:
            continue
        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.content_type};base64,{encoded}",
                },
            }
        )
    return parts


def build_openai_request(work: SummaryWork) -> dict[str, typing.Any]:
    messages: list[dict[str, typing.Any]] = [
        {
            "role": "system",
            "content": work.effective_prompt,
        }
    ]
    if work.previous_summary is not None:
        messages.append(
            {
                "role": "user",
                "content": f"Previous summary:\n{work.previous_summary}",
            }
        )
    for message in work.messages:
        content: object = message.content
        if work.include_images and message.image_uuids:
            content = _image_parts(work, message)
        messages.append(
            {
                "role": "user",
                "content": content,
            }
        )
    payload: dict[str, typing.Any] = {
        "model": work.endpoint.model,
        "messages": messages,
        "temperature": work.endpoint.temperature,
        "max_tokens": work.endpoint.max_output_tokens,
        "top_p": work.endpoint.top_p,
        "presence_penalty": work.endpoint.presence_penalty,
        "frequency_penalty": work.endpoint.frequency_penalty,
    }
    if work.reasoning_effort is not None and work.endpoint.supports_reasoning:
        payload["reasoning_effort"] = work.reasoning_effort
    return payload


def call_openai_compatible_endpoint(
    work: SummaryWork,
    *,
    timeout_seconds: int,
    connect_timeout_seconds: int = (
        topic_summary_opts.DEFAULT_CONNECT_TIMEOUT_SECONDS
    ),
) -> str:
    try:
        response = requests.post(
            f"{work.endpoint.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {work.endpoint.api_key}",
                "Content-Type": "application/json",
            },
            json=build_openai_request(work),
            timeout=(connect_timeout_seconds, timeout_seconds),
        )
    except requests.RequestException as exc:
        raise ProviderCallError("network_error", retryable=True) from exc
    if response.status_code >= 400:
        retryable = response.status_code in {408, 409, 425, 429} or (
            response.status_code >= 500
        )
        raise ProviderCallError(
            f"http_{response.status_code}",
            retryable=retryable,
        )
    try:
        body = response.json()
        choices = body["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderCallError("invalid_response", retryable=False) from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderCallError("invalid_response", retryable=False)
    return content.strip()[:4096].rstrip()


def _release_endpoint(
    session: typing.Any,
    work: SummaryWork,
    *,
    now: datetime.datetime,
    error_code: str | None,
) -> None:
    if error_code is None:
        session.execute(
            """
            UPDATE m_workspace_llm_endpoints
            SET claim_token = NULL,
                claim_expires_at = NULL,
                last_success_at = %s,
                last_error_code = NULL,
                updated_at = NOW()
            WHERE uuid = %s AND claim_token = %s
            """,
            (now, work.endpoint.uuid, work.endpoint_claim_token),
        )
        return
    session.execute(
        """
        UPDATE m_workspace_llm_endpoints
        SET claim_token = NULL,
            claim_expires_at = NULL,
            last_failure_at = %s,
            failure_count = failure_count + 1,
            last_error_code = %s,
            updated_at = NOW()
        WHERE uuid = %s AND claim_token = %s
        """,
        (now, error_code, work.endpoint.uuid, work.endpoint_claim_token),
    )


def _cancel_endpoint_claim(
    session: typing.Any,
    work: SummaryWork,
) -> None:
    session.execute(
        """
        UPDATE m_workspace_llm_endpoints
        SET claim_token = NULL,
            claim_expires_at = NULL,
            updated_at = NOW()
        WHERE uuid = %s AND claim_token = %s
        """,
        (work.endpoint.uuid, work.endpoint_claim_token),
    )


def complete_summary_work(
    session: typing.Any,
    work: SummaryWork,
    summary: str,
    *,
    now: datetime.datetime,
) -> None:
    current = session.execute(
        """
        SELECT job.claim_token, topic.summary_enabled
        FROM m_workspace_topic_summary_jobs AS job
        JOIN m_workspace_stream_topics AS topic
          ON topic.uuid = job.topic_uuid
        WHERE job.topic_uuid = %s
        FOR UPDATE OF job, topic
        """,
        (work.topic_uuid,),
    ).fetchone()
    if (
        current is None
        or current["claim_token"] != work.topic_claim_token
        or not current["summary_enabled"]
    ):
        _cancel_endpoint_claim(session, work)
        return
    helpers.set_workspace_user_stream_topic_summary(
        work.project_id,
        work.actor_user_uuid,
        work.topic_uuid,
        summary,
        work.boundary_message_uuid,
        session=session,
    )
    _release_endpoint(session, work, now=now, error_code=None)
    session.execute(
        """
        UPDATE m_workspace_topic_summary_jobs
        SET status = 'succeeded',
            claim_token = NULL,
            claim_expires_at = NULL,
            endpoint_claim_token = NULL,
            completed_at = %s,
            last_error_code = NULL,
            updated_at = NOW()
        WHERE topic_uuid = %s AND claim_token = %s
        """,
        (now, work.topic_uuid, work.topic_claim_token),
    )


def fail_summary_work(
    session: typing.Any,
    work: SummaryWork,
    error: ProviderCallError,
    *,
    now: datetime.datetime,
    key_material: str,
    endpoint_claim_seconds: int,
) -> SummaryWork | None:
    current = session.execute(
        """
        SELECT claim_token
        FROM m_workspace_topic_summary_jobs
        WHERE topic_uuid = %s
        FOR UPDATE
        """,
        (work.topic_uuid,),
    ).fetchone()
    if current is None or current["claim_token"] != work.topic_claim_token:
        return None
    _release_endpoint(session, work, now=now, error_code=error.code)
    if error.retryable and work.attempt < MAX_PROVIDER_ATTEMPTS:
        claimed_endpoint = _claim_endpoint(
            session,
            now=now,
            claim_seconds=endpoint_claim_seconds,
            key_material=key_material,
            require_vision=work.requires_vision,
            after_priority=work.endpoint.priority,
            after_uuid=work.endpoint.uuid,
        )
        if claimed_endpoint is not None:
            endpoint, endpoint_claim_token = claimed_endpoint
            session.execute(
                """
                UPDATE m_workspace_topic_summary_jobs
                SET attempt = attempt + 1,
                    endpoint_uuid = %s,
                    endpoint_claim_token = %s,
                    last_error_code = %s,
                    updated_at = NOW()
                WHERE topic_uuid = %s AND claim_token = %s
                """,
                (
                    endpoint.uuid,
                    endpoint_claim_token,
                    error.code,
                    work.topic_uuid,
                    work.topic_claim_token,
                ),
            )
            return dataclasses.replace(
                work,
                endpoint=endpoint,
                endpoint_claim_token=endpoint_claim_token,
                include_images=bool(
                    any(message.image_uuids for message in work.messages)
                    and endpoint.supports_vision
                ),
                attempt=work.attempt + 1,
            )
    if error.retryable and work.attempt < MAX_PROVIDER_ATTEMPTS:
        status = "retry_wait"
        next_attempt_at = now + datetime.timedelta(
            seconds=RETRY_BASE_SECONDS * (2 ** (work.attempt - 1))
        )
    else:
        status = "failed"
        next_attempt_at = None
    session.execute(
        """
        UPDATE m_workspace_topic_summary_jobs
        SET status = %s,
            claim_token = NULL,
            claim_expires_at = NULL,
            endpoint_claim_token = NULL,
            next_attempt_at = %s,
            last_error_code = %s,
            updated_at = NOW()
        WHERE topic_uuid = %s AND claim_token = %s
        """,
        (
            status,
            next_attempt_at,
            error.code,
            work.topic_uuid,
            work.topic_claim_token,
        ),
    )
    return None
