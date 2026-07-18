# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import datetime
import email
import email.message
import email.policy
import email.utils
import hashlib
import hmac
import json
import re
import unicodedata
import uuid as sys_uuid
from typing import Any, cast

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf import hkdf


PROTOCOL = "workspace-zulip-mail/1"
SCHEMA = "workspace.zulip_bridge.mail"
SCHEMA_VERSION = 1
MAX_ENCODED_BYTES = 512 * 1024
MAX_BODY_BYTES = 256 * 1024
MAIL_POLICY = email.policy.SMTP.clone(max_line_length=998)
_DIRECTIONS = frozenset({"workspace-to-zulip", "zulip-to-workspace"})
_KINDS = frozenset({"operation", "result"})
_RESULT_OUTCOMES = frozenset({"committed", "rejected", "expired", "cancelled"})
_SAFE_ERROR_CODES = frozenset(
    {
        "invalid_record",
        "unauthorized_account",
        "project_mismatch",
        "chat_not_selected",
        "capability_missing",
        "unsupported_operation",
        "not_found",
        "permission_denied",
        "conflict",
        "rate_limited",
        "provider_unavailable",
        "workspace_unavailable",
        "expired",
        "cancelled",
        "internal_error",
    }
)
_COMMON_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "record_kind",
        "record_uuid",
        "operation_uuid",
        "attempt",
        "operation_sha256",
        "account_uuid",
        "project_uuid",
        "origin",
        "causal_lane",
        "sequence",
        "predecessor_operation_uuid",
        "created_at",
        "expires_at",
    }
)
_HEADER_FIELDS = {
    "X-Workspace-Bridge-Protocol": lambda record: PROTOCOL,
    "X-Workspace-Bridge-Record-Kind": lambda record: record["record_kind"],
    "X-Workspace-Bridge-Record-UUID": lambda record: record["record_uuid"],
    "X-Workspace-Bridge-Operation-UUID": lambda record: record["operation_uuid"],
    "X-Workspace-Bridge-Attempt": lambda record: str(record["attempt"]),
    "X-Workspace-Bridge-Account-UUID": lambda record: record["account_uuid"],
    "X-Workspace-Bridge-Project-UUID": lambda record: record["project_uuid"],
    "X-Workspace-Bridge-Causal-Lane": lambda record: record["causal_lane"],
    "X-Workspace-Bridge-Sequence": lambda record: str(record["sequence"]),
    "X-Workspace-Bridge-Operation-SHA256": lambda record: record["operation_sha256"],
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LANE_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
_OPERATION_PAYLOAD_FIELDS = {
    "identity.upsert": {"display_name", "email", "avatar_urn", "active"},
    "stream.upsert": {
        "name",
        "description",
        "private",
        "chat_kind",
        "participant_uuids",
        "default_topic_uuid",
    },
    "stream.delete": {"stream_uuid"},
    "topic.upsert": {"stream_uuid", "name"},
    "topic.delete": {"stream_uuid", "topic_uuid"},
    "message.create": {
        "stream_uuid",
        "topic_uuid",
        "author_uuid",
        "payload",
        "reply_to_message_uuid",
    },
    "message.update": {"stream_uuid", "topic_uuid", "author_uuid", "payload"},
    "message.delete": {"stream_uuid", "topic_uuid", "author_uuid"},
    "read_state.set": {"stream_uuid", "topic_uuid", "reader_uuid", "read"},
}


class InvalidExternalBridgeRecord(ValueError):
    pass


def _validate_operation(operation: dict[str, Any]) -> None:
    if not isinstance(operation, dict) or set(operation) not in (
        {"kind", "entity_uuid", "actor_uuid", "occurred_at", "provider", "payload"},
        {
            "kind",
            "entity_uuid",
            "actor_uuid",
            "occurred_at",
            "provider",
            "payload",
            "extensions",
        },
    ):
        raise InvalidExternalBridgeRecord("Bridge operation schema is invalid")
    kind = operation["kind"]
    if kind not in _OPERATION_PAYLOAD_FIELDS:
        raise InvalidExternalBridgeRecord("Unsupported bridge operation")
    _uuid(operation["entity_uuid"], "operation entity_uuid")
    _uuid(operation["actor_uuid"], "operation actor_uuid")
    provider = operation["provider"]
    if (
        not isinstance(provider, dict)
        or set(provider) != {"kind", "chat_id", "entity_id", "revision"}
        or provider["kind"] != "zulip"
        or any(
            provider[name] is not None and not isinstance(provider[name], str)
            for name in ("chat_id", "entity_id", "revision")
        )
    ):
        raise InvalidExternalBridgeRecord("Bridge provider schema is invalid")
    payload = operation["payload"]
    payload_fields = set(payload) if isinstance(payload, dict) else set()
    expected_payload_fields = _OPERATION_PAYLOAD_FIELDS[kind]
    if kind == "read_state.set":
        selector_fields = payload_fields & {"through_message_uuid", "message_uuids"}
        valid_payload = (
            payload_fields - selector_fields == expected_payload_fields
            and len(selector_fields) == 1
        )
    else:
        valid_payload = payload_fields == expected_payload_fields
    if not isinstance(payload, dict) or not valid_payload:
        raise InvalidExternalBridgeRecord("Bridge operation payload is invalid")
    for name in (
        "stream_uuid",
        "topic_uuid",
        "author_uuid",
        "reader_uuid",
        "default_topic_uuid",
    ):
        if name in payload and payload[name] is not None:
            _uuid(payload[name], f"operation {name}")
    if "participant_uuids" in payload:
        if not isinstance(payload["participant_uuids"], list):
            raise InvalidExternalBridgeRecord("Invalid participant UUIDs")
        for participant_uuid in payload["participant_uuids"]:
            _uuid(participant_uuid, "participant_uuid")
    if "through_message_uuid" in payload:
        _uuid(payload["through_message_uuid"], "operation through_message_uuid")
    if "message_uuids" in payload:
        if (
            not isinstance(payload["message_uuids"], list)
            or not payload["message_uuids"]
        ):
            raise InvalidExternalBridgeRecord("Invalid message UUIDs")
        for message_uuid in payload["message_uuids"]:
            _uuid(message_uuid, "message_uuid")
    if kind.startswith("message.") and kind != "message.delete":
        markdown = payload["payload"]
        if (
            not isinstance(markdown, dict)
            or set(markdown) != {"kind", "content"}
            or markdown["kind"] != "markdown"
            or not isinstance(markdown["content"], str)
            or not markdown["content"].strip()
        ):
            raise InvalidExternalBridgeRecord("Invalid Markdown payload")
    if kind == "read_state.set" and not isinstance(payload["read"], bool):
        raise InvalidExternalBridgeRecord("Invalid read state")
    extensions = operation.get("extensions", {})
    if not isinstance(extensions, dict):
        raise InvalidExternalBridgeRecord("Bridge operation extensions are invalid")
    delivery_class = extensions.get("delivery_class")
    if delivery_class is not None and delivery_class not in {"live", "backfill"}:
        raise InvalidExternalBridgeRecord("Invalid bridge delivery class")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def canonical_json(value: dict[str, Any]) -> bytes:
    def normalized(item: Any) -> Any:
        if isinstance(item, str):
            return unicodedata.normalize("NFC", item)
        if isinstance(item, list):
            return [normalized(child) for child in item]
        if isinstance(item, dict):
            return {normalized(key): normalized(child) for key, child in item.items()}
        if item is None or isinstance(item, (bool, int)):
            return item
        raise InvalidExternalBridgeRecord("Canonical JSON does not allow this value")

    if not isinstance(value, dict):
        raise InvalidExternalBridgeRecord("Bridge body must be a JSON object")
    return json.dumps(
        normalized(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def operation_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "account_uuid": record["account_uuid"],
                "causal_lane": record["causal_lane"],
                "operation": record["operation"],
                "operation_uuid": record["operation_uuid"],
                "origin": record["origin"],
                "predecessor_operation_uuid": record["predecessor_operation_uuid"],
                "project_uuid": record["project_uuid"],
                "sequence": record["sequence"],
            }
        )
    ).hexdigest()


def derive_direction_key(
    enrollment_secret: str | bytes,
    realm_uuid: str | sys_uuid.UUID,
    bridge_instance_uuid: str | sys_uuid.UUID,
    identity_generation: int,
    direction: str,
) -> bytes:
    if direction not in _DIRECTIONS or identity_generation < 1:
        raise InvalidExternalBridgeRecord("Invalid bridge key context")
    realm_uuid = str(sys_uuid.UUID(str(realm_uuid)))
    bridge_instance_uuid = str(sys_uuid.UUID(str(bridge_instance_uuid)))
    secret = enrollment_secret
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    master_info = (
        "workspace-zulip-mail-v1/master/"
        f"{realm_uuid}/{bridge_instance_uuid}/{identity_generation}"
    ).encode("utf-8")
    master = hkdf.HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=master_info,
    ).derive(secret)
    return hkdf.HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=f"workspace-zulip-mail-v1/{direction}".encode("utf-8"),
    ).derive(master)


def _uuid(value: object, field: str) -> str:
    try:
        parsed = str(sys_uuid.UUID(cast(str, value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidExternalBridgeRecord(f"Invalid {field}") from exc
    if value != parsed:
        raise InvalidExternalBridgeRecord(f"Non-canonical {field}")
    return parsed


def _validate_record(record: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise InvalidExternalBridgeRecord("Bridge record must be an object")
    kind = record.get("record_kind")
    expected = _COMMON_FIELDS | (
        {"operation"}
        if kind == "operation"
        else {
            "in_reply_to_record_uuid",
            "result",
        }
    )
    if kind not in _KINDS or set(record) != expected:
        raise InvalidExternalBridgeRecord("Bridge record schema is invalid")
    if record["schema"] != SCHEMA or record["schema_version"] != SCHEMA_VERSION:
        raise InvalidExternalBridgeRecord("Unsupported bridge record schema")
    for field in ("record_uuid", "operation_uuid", "account_uuid", "project_uuid"):
        _uuid(record[field], field)
    for field in ("predecessor_operation_uuid", "expires_at"):
        if field == "predecessor_operation_uuid" and record[field] is not None:
            _uuid(record[field], field)
    if kind == "result":
        _uuid(record["in_reply_to_record_uuid"], "in_reply_to_record_uuid")
        result = record["result"]
        if not isinstance(result, dict) or set(result) != {
            "outcome",
            "committed_at",
            "provider_entity_id",
            "provider_revision",
            "safe_error",
            "manual_retry_allowed",
        }:
            raise InvalidExternalBridgeRecord("Bridge result schema is invalid")
        outcome = result["outcome"]
        if outcome not in _RESULT_OUTCOMES:
            raise InvalidExternalBridgeRecord("Unknown bridge result outcome")
        if not isinstance(result["manual_retry_allowed"], bool):
            raise InvalidExternalBridgeRecord("Invalid bridge retry flag")
        if (outcome == "committed") != (result["committed_at"] is not None):
            raise InvalidExternalBridgeRecord("Invalid committed result timestamp")
        for field in ("committed_at", "provider_entity_id", "provider_revision"):
            if result[field] is not None and not isinstance(result[field], str):
                raise InvalidExternalBridgeRecord(f"Invalid result {field}")
        safe_error = result["safe_error"]
        if outcome == "committed":
            if safe_error is not None:
                raise InvalidExternalBridgeRecord("Committed result has safe error")
        elif (
            not isinstance(safe_error, dict)
            or set(safe_error) != {"code", "message"}
            or safe_error["code"] not in _SAFE_ERROR_CODES
            or not isinstance(safe_error["message"], str)
        ):
            raise InvalidExternalBridgeRecord("Unknown bridge result reason")
    else:
        _validate_operation(record["operation"])
    if (
        isinstance(record["attempt"], bool)
        or not isinstance(record["attempt"], int)
        or record["attempt"] < 1
        or isinstance(record["sequence"], bool)
        or not isinstance(record["sequence"], int)
        or record["sequence"] < 1
    ):
        raise InvalidExternalBridgeRecord("Attempt and sequence must be positive")
    if (record["sequence"] == 1) != (record["predecessor_operation_uuid"] is None):
        raise InvalidExternalBridgeRecord("Predecessor does not match lane sequence")
    if record["origin"] not in {"workspace", "zulip"}:
        raise InvalidExternalBridgeRecord("Invalid operation origin")
    if _LANE_RE.fullmatch(record["causal_lane"]) is None:
        raise InvalidExternalBridgeRecord("Invalid causal lane")
    if _SHA256_RE.fullmatch(record["operation_sha256"]) is None:
        raise InvalidExternalBridgeRecord("Invalid operation digest")
    if kind == "operation" and operation_sha256(record) != record["operation_sha256"]:
        raise InvalidExternalBridgeRecord("Operation digest mismatch")
    canonical_json(record)


def _signature_input(
    record: dict[str, Any],
    direction: str,
    body_sha256: str,
) -> bytes:
    values = (
        "workspace-zulip-mail-v1",
        direction,
        record["record_kind"],
        record["record_uuid"],
        record["operation_uuid"],
        str(record["attempt"]),
        record["account_uuid"],
        record["project_uuid"],
        record["causal_lane"],
        str(record["sequence"]),
        record["predecessor_operation_uuid"] or "-",
        record.get("in_reply_to_record_uuid") or "-",
        record["operation_sha256"],
        body_sha256,
    )
    return ("\n".join(values) + "\n").encode("utf-8")


def build_message(
    record: dict[str, Any],
    direction: str,
    signing_key: bytes,
    sender: str,
    recipient: str,
) -> bytes:
    _validate_record(record)
    if direction not in _DIRECTIONS:
        raise InvalidExternalBridgeRecord("Invalid bridge direction")
    body = canonical_json(record)
    if len(body) > MAX_BODY_BYTES:
        raise InvalidExternalBridgeRecord("Bridge body is too large")
    body_sha256 = hashlib.sha256(body).hexdigest()
    signature = _b64url(
        hmac.new(
            signing_key,
            _signature_input(record, direction, body_sha256),
            hashlib.sha256,
        ).digest()
    )
    try:
        created_at = datetime.datetime.fromisoformat(
            record["created_at"].replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidExternalBridgeRecord("Invalid record created_at") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != datetime.timedelta(0):
        raise InvalidExternalBridgeRecord("Record created_at must be UTC")
    message = email.message.EmailMessage(policy=MAIL_POLICY)
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = email.utils.format_datetime(created_at)
    message["Message-ID"] = (
        f"<bridge-v1.{record['record_uuid']}@messenger.workspace.invalid>"
    )
    message["MIME-Version"] = "1.0"
    message["X-Workspace-Bridge-Protocol"] = PROTOCOL
    message["X-Workspace-Bridge-Direction"] = direction
    for name, getter in tuple(_HEADER_FIELDS.items())[1:]:
        message[name] = getter(record)
    message["X-Workspace-Bridge-Body-SHA256"] = body_sha256
    message["X-Workspace-Bridge-Signature"] = f"v1={signature}"
    message.set_type("application/json")
    message.set_param("charset", "utf-8")
    message["Content-Transfer-Encoding"] = "base64"
    message.set_payload(base64.encodebytes(body).decode("ascii"))
    raw_message = message.as_bytes(policy=MAIL_POLICY)
    if len(raw_message) > MAX_ENCODED_BYTES:
        raise InvalidExternalBridgeRecord("Bridge message is too large")
    return raw_message


def _single_header(message: email.message.Message, name: str) -> str:
    values = message.get_all(name, failobj=[])
    if len(values) != 1 or any(ord(char) < 32 for char in str(values[0])):
        raise InvalidExternalBridgeRecord(f"Invalid {name} header")
    return str(values[0])


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidExternalBridgeRecord("Duplicate JSON member")
        result[key] = value
    return result


def parse_message(
    raw_message: bytes,
    direction: str,
    signing_keys: tuple[bytes, ...] | list[bytes],
    sender: str,
    recipient: str,
) -> dict[str, Any]:
    if len(raw_message) > MAX_ENCODED_BYTES:
        raise InvalidExternalBridgeRecord("Bridge message is too large")
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    content_type_headers = message.get_all("Content-Type", failobj=[])
    content_type_parameters = message.get_params(
        header="Content-Type",
        failobj=[],
        unquote=True,
    )
    if (
        message.is_multipart()
        or len(content_type_headers) != 1
        or content_type_parameters
        != [
            ("application/json", ""),
            ("charset", "utf-8"),
        ]
    ):
        raise InvalidExternalBridgeRecord("Bridge message must be JSON-only MIME")
    if message.get_all("Content-Disposition", failobj=[]):
        raise InvalidExternalBridgeRecord(
            "Bridge message must not have content disposition"
        )
    if _single_header(message, "Content-Transfer-Encoding").lower() != "base64":
        raise InvalidExternalBridgeRecord(
            "Bridge body must use base64 transfer encoding"
        )
    if _single_header(message, "MIME-Version") != "1.0":
        raise InvalidExternalBridgeRecord("Invalid MIME version")
    try:
        message_date = email.utils.parsedate_to_datetime(
            _single_header(message, "Date")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidExternalBridgeRecord("Invalid Date header") from exc
    if message_date is None:
        raise InvalidExternalBridgeRecord("Invalid Date header")
    if (
        _single_header(message, "From") != sender
        or _single_header(message, "To") != recipient
    ):
        raise InvalidExternalBridgeRecord("Bridge mail route mismatch")
    if _single_header(message, "X-Workspace-Bridge-Direction") != direction:
        raise InvalidExternalBridgeRecord("Bridge direction mismatch")
    body_payload = message.get_payload(decode=True)
    if body_payload is None or len(body_payload) > MAX_BODY_BYTES:
        raise InvalidExternalBridgeRecord("Invalid bridge body")
    body = cast(bytes, body_payload)
    try:
        record = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_float=lambda value: (_ for _ in ()).throw(
                InvalidExternalBridgeRecord("Floating-point JSON is forbidden")
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                InvalidExternalBridgeRecord("Non-finite JSON is forbidden")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidExternalBridgeRecord("Malformed bridge JSON") from exc
    _validate_record(record)
    if canonical_json(record) != body:
        raise InvalidExternalBridgeRecord("Bridge JSON is not canonical")
    if _single_header(message, "Message-ID") != (
        f"<bridge-v1.{record['record_uuid']}@messenger.workspace.invalid>"
    ):
        raise InvalidExternalBridgeRecord("Bridge Message-ID mismatch")
    for name, getter in _HEADER_FIELDS.items():
        if _single_header(message, name) != getter(record):
            raise InvalidExternalBridgeRecord(f"Bridge {name} mismatch")
    body_sha256 = hashlib.sha256(body).hexdigest()
    if _single_header(message, "X-Workspace-Bridge-Body-SHA256") != body_sha256:
        raise InvalidExternalBridgeRecord("Bridge body digest mismatch")
    signature = _single_header(message, "X-Workspace-Bridge-Signature")
    if not signature.startswith("v1=") or not any(
        hmac.compare_digest(
            signature,
            "v1="
            + _b64url(
                hmac.new(
                    key,
                    _signature_input(record, direction, body_sha256),
                    hashlib.sha256,
                ).digest()
            ),
        )
        for key in signing_keys
    ):
        raise InvalidExternalBridgeRecord("Bridge signature mismatch")
    return record
