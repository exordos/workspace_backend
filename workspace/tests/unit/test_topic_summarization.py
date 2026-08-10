# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import contextlib
import json
import unittest.mock
import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.common import topic_summary_opts
from workspace.messenger_api import topic_summarization
from workspace.services.messenger_workers import agents


def test_default_system_prompt_uses_the_topics_primary_language():
    assert (
        "Write the summary in the primary language used in the topic."
        in topic_summarization.DEFAULT_SYSTEM_PROMPT
    )


def _endpoint(**overrides):
    values = {
        "uuid": sys_uuid.uuid4(),
        "name": "compatible",
        "base_url": "https://llm.example.invalid/v1",
        "model": "summary-model",
        "priority": 10,
        "supports_vision": False,
        "supports_reasoning": False,
        "temperature": 0.3,
        "max_output_tokens": 600,
        "top_p": 0.9,
        "presence_penalty": 0.2,
        "frequency_penalty": -0.1,
        "api_key": "provider-secret",
    }
    values.update(overrides)
    return topic_summarization.Endpoint(**values)


def _work(**overrides):
    message_uuid = sys_uuid.uuid4()
    values = {
        "topic_uuid": sys_uuid.uuid4(),
        "project_id": sys_uuid.uuid4(),
        "stream_uuid": sys_uuid.uuid4(),
        "actor_user_uuid": sys_uuid.uuid4(),
        "boundary_message_uuid": message_uuid,
        "previous_summary": "Earlier state.",
        "effective_prompt": "Summarize decisions.",
        "reasoning_effort": "medium",
        "prompt_fingerprint": "fingerprint",
        "messages": (
            topic_summarization.SummaryMessage(
                uuid=message_uuid,
                user_uuid=sys_uuid.uuid4(),
                content="Decision: ship on Friday.",
                image_uuids=(),
            ),
        ),
        "images": (),
        "requires_vision": False,
        "include_images": False,
        "topic_claim_token": sys_uuid.uuid4(),
        "endpoint_claim_token": sys_uuid.uuid4(),
        "endpoint": _endpoint(),
        "attempt": 1,
    }
    values.update(overrides)
    return topic_summarization.SummaryWork(**values)


def test_endpoint_api_key_envelope_round_trips_without_plaintext():
    endpoint_uuid = sys_uuid.uuid4()
    envelope = topic_summarization.encrypt_api_key(
        endpoint_uuid,
        "sensitive-provider-key",
        "server-side-secret",
    )

    assert "sensitive-provider-key" not in json.dumps(envelope)
    assert topic_summarization.decrypt_api_key(
        endpoint_uuid,
        envelope,
        "server-side-secret",
    ) == "sensitive-provider-key"

    tampered = dict(envelope)
    tampered["ciphertext"] = tampered["ciphertext"][:-2] + "AA"
    with pytest.raises(RuntimeError, match="Invalid endpoint credential envelope"):
        topic_summarization.decrypt_api_key(
            endpoint_uuid,
            tampered,
            "server-side-secret",
        )
    assert "provider-secret" not in repr(_endpoint())
    assert "provider-secret" not in repr(_work())


def test_endpoint_registry_order_uses_priority_then_uuid():
    first = unittest.mock.Mock(
        priority=10,
        uuid=sys_uuid.UUID("00000000-0000-4000-8000-000000000001"),
    )
    second = unittest.mock.Mock(
        priority=10,
        uuid=sys_uuid.UUID("00000000-0000-4000-8000-000000000002"),
    )
    last = unittest.mock.Mock(
        priority=20,
        uuid=sys_uuid.UUID("00000000-0000-4000-8000-000000000000"),
    )
    result = topic_summarization.order_endpoints([last, second, first])

    assert result == [first, second, last]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("temperature", 2.1),
        ("max_output_tokens", 0),
        ("top_p", -0.1),
        ("presence_penalty", 2.1),
        ("frequency_penalty", -2.1),
        ("priority", -1),
        ("supports_vision", "yes"),
    ),
)
def test_endpoint_generation_settings_reject_invalid_values(field, value):
    with pytest.raises(ra_exc.ValidationErrorException):
        topic_summarization.normalize_endpoint_values(
            {field: value},
            creating=False,
        )


def test_openai_request_applies_generation_and_capability_aware_reasoning():
    unsupported = _work()
    payload = topic_summarization.build_openai_request(unsupported)

    assert payload["temperature"] == 0.3
    assert payload["max_tokens"] == 600
    assert payload["top_p"] == 0.9
    assert payload["presence_penalty"] == 0.2
    assert payload["frequency_penalty"] == -0.1
    assert "reasoning_effort" not in payload
    assert payload["messages"][0] == {
        "role": "system",
        "content": "Summarize decisions.",
    }

    supported = _work(endpoint=_endpoint(supports_reasoning=True))
    assert topic_summarization.build_openai_request(supported)[
        "reasoning_effort"
    ] == "medium"


def test_openai_request_keeps_system_text_only_and_places_images_in_user_content():
    image_uuid = sys_uuid.uuid4()
    message = topic_summarization.SummaryMessage(
        uuid=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        content=f"Architecture diagram urn:image:{image_uuid}",
        image_uuids=(image_uuid,),
    )
    image = topic_summarization.ImageAttachment(
        uuid=image_uuid,
        content_type="image/png",
        size_bytes=3,
        storage_type="local",
        storage_object_id="object-id",
    )
    work = _work(
        messages=(message,),
        images=(image,),
        requires_vision=True,
        include_images=True,
        endpoint=_endpoint(supports_vision=True),
    )

    with unittest.mock.patch.object(
        topic_summarization.file_storage,
        "read_workspace_file",
        return_value=b"png",
    ):
        payload = topic_summarization.build_openai_request(work)

    assert isinstance(payload["messages"][0]["content"], str)
    user_content = payload["messages"][-1]["content"]
    assert user_content[0] == {"type": "text", "text": message.content}
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_openai_compatible_call_uses_chat_completions_and_validates_response():
    response = unittest.mock.Mock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "  Concise summary.  "}}]
    }
    work = _work()

    with unittest.mock.patch.object(
        topic_summarization.requests,
        "post",
        return_value=response,
    ) as post:
        result = topic_summarization.call_openai_compatible_endpoint(
            work,
            timeout_seconds=7,
        )

    assert result == "Concise summary."
    assert post.call_args.args == (
        "https://llm.example.invalid/v1/chat/completions",
    )
    assert post.call_args.kwargs["timeout"] == (30, 7)
    assert post.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer provider-secret"
    )


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    ((400, False), (408, True), (429, True), (503, True)),
)
def test_openai_compatible_call_classifies_retryable_statuses(
    status_code,
    retryable,
):
    response = unittest.mock.Mock(status_code=status_code)
    with (
        unittest.mock.patch.object(
            topic_summarization.requests,
            "post",
            return_value=response,
        ),
        pytest.raises(topic_summarization.ProviderCallError) as raised,
    ):
        topic_summarization.call_openai_compatible_endpoint(
            _work(),
            timeout_seconds=3,
        )

    assert raised.value.code == f"http_{status_code}"
    assert raised.value.retryable is retryable


def test_worker_calls_provider_outside_transactions_and_persists_afterward():
    active_sessions = []
    entered_sessions = []

    @contextlib.contextmanager
    def session_context():
        session = object()
        active_sessions.append(session)
        entered_sessions.append(session)
        try:
            yield session
        finally:
            active_sessions.remove(session)

    work = _work()

    def claim(session, **kwargs):
        del kwargs
        assert session in active_sessions
        return work

    def provider(current, **kwargs):
        del kwargs
        assert current is work
        assert active_sessions == []
        return "Generated summary."

    def complete(session, current, summary, **kwargs):
        del kwargs
        assert session in active_sessions
        assert current is work
        assert summary == "Generated summary."

    with (
        unittest.mock.patch.object(
            agents,
            "database_session_context",
            side_effect=session_context,
        ),
        unittest.mock.patch.object(
            topic_summarization,
            "claim_summary_work",
            side_effect=claim,
        ),
        unittest.mock.patch.object(
            topic_summarization,
            "call_openai_compatible_endpoint",
            side_effect=provider,
        ),
        unittest.mock.patch.object(
            topic_summarization,
            "complete_summary_work",
            side_effect=complete,
        ),
    ):
        worker = agents.MessengerWorkerAgent(summary_secret_key="secret")
        assert worker._summarize_one_topic() is True

    assert len(entered_sessions) == 2


def test_worker_defaults_allow_long_reasoning_and_keep_claims_alive():
    worker = agents.MessengerWorkerAgent(summary_secret_key="secret")

    assert worker._summary_connect_timeout_seconds == 30
    assert worker._summary_request_timeout_seconds == 25 * 60
    assert worker._summary_endpoint_claim_seconds == 30 * 60
    assert worker._summary_topic_claim_seconds == 90 * 60

    normalized = agents.MessengerWorkerAgent(
        summary_secret_key="secret",
        summary_request_timeout_seconds=20 * 60,
        summary_endpoint_claim_seconds=60,
        summary_topic_claim_seconds=60,
    )
    assert normalized._summary_endpoint_claim_seconds == 21 * 60
    assert normalized._summary_topic_claim_seconds == 63 * 60
    assert topic_summary_opts.CLAIM_GRACE_SECONDS == 60


def test_worker_retries_a_retryable_failure_on_claimed_fallback_endpoint():
    first = _work(endpoint=_endpoint(priority=10), attempt=1)
    fallback = _work(endpoint=_endpoint(priority=20), attempt=2)

    @contextlib.contextmanager
    def session_context():
        yield object()

    provider_error = topic_summarization.ProviderCallError(
        "http_503",
        retryable=True,
    )
    with (
        unittest.mock.patch.object(
            agents,
            "database_session_context",
            side_effect=session_context,
        ),
        unittest.mock.patch.object(
            topic_summarization,
            "claim_summary_work",
            return_value=first,
        ),
        unittest.mock.patch.object(
            topic_summarization,
            "call_openai_compatible_endpoint",
            side_effect=(provider_error, "Fallback summary."),
        ) as provider,
        unittest.mock.patch.object(
            topic_summarization,
            "fail_summary_work",
            return_value=fallback,
        ) as fail,
        unittest.mock.patch.object(
            topic_summarization,
            "complete_summary_work",
        ) as complete,
    ):
        worker = agents.MessengerWorkerAgent(summary_secret_key="secret")
        assert worker._summarize_one_topic() is True

    assert provider.call_args_list == [
        unittest.mock.call(
            first,
            connect_timeout_seconds=30,
            timeout_seconds=25 * 60,
        ),
        unittest.mock.call(
            fallback,
            connect_timeout_seconds=30,
            timeout_seconds=25 * 60,
        ),
    ]
    assert fail.call_args.args[1:3] == (first, provider_error)
    assert complete.call_args.args[1:3] == (fallback, "Fallback summary.")
