# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from unittest import mock

import pytest

from workspace.bridges.zulip import message_conversion
from workspace.messenger_api.dm import message_payloads


@pytest.fixture
def context():
    return {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/99",
        "current_message_uuid": "11111111-1111-4111-8111-111111111111",
    }


def assert_size_notice(payload, reusable, context):
    assert payload["kind"] == "markdown"
    assert payload["content"].startswith(
        "Not supported in Workspace: formatted message is too large\n\n"
    )
    assert payload["content"].endswith(
        f"[Original message](urn:url:{context['original_url']})"
    )
    assert not reusable
    assert len(payload["content"]) <= message_payloads.MARKDOWN_CONTENT_MAX_LENGTH


@pytest.mark.parametrize(
    "leaf", ["x" * 6000, "Ж" * 1200, "😀" * 1200], ids=["ascii", "unicode", "emoji"]
)
def test_nested_quotes_that_expand_past_the_limit_keep_an_original_link(context, leaf):
    source = leaf
    for depth in range(8):
        fence = "`" * (depth + 3)
        source = (
            "@_**Example|7** "
            f"[said](https://zulip.example.test/#narrow/near/{depth + 1}):\n"
            f"{fence}quote\n{source}\n{fence}"
        )
    assert len(source) < message_payloads.MARKDOWN_CONTENT_MAX_LENGTH

    payload, reusable = message_conversion.zulip_to_workspace_cached(
        {"id": 99, "content": source}, context
    )

    assert_size_notice(payload, reusable, context)
    fence = "`" * 11
    assert f"{fence}\n{source}\n{fence}" in payload["content"]
    assert "urn:forward:" not in payload["content"]


def test_oversized_plain_provider_content_keeps_an_original_link(context):
    source = "x" * (message_payloads.MARKDOWN_CONTENT_MAX_LENGTH + 1)

    payload, reusable = message_conversion.zulip_to_workspace_cached(
        {"id": 99, "content": source}, context
    )

    assert_size_notice(payload, reusable, context)
    assert source not in payload["content"]


def test_original_link_appendix_is_included_in_the_limit(context):
    short = message_conversion.zulip_to_workspace(
        {"id": 99, "content": "$$x$$"}, context
    )["content"]
    source = "x" * (message_payloads.MARKDOWN_CONTENT_MAX_LENGTH - len(short) + 1)
    source += "\n$$x$$"
    assert len(source) <= message_payloads.MARKDOWN_CONTENT_MAX_LENGTH

    payload, reusable = message_conversion.zulip_to_workspace_cached(
        {"id": 99, "content": source}, context
    )

    assert_size_notice(payload, reusable, context)


@pytest.mark.parametrize("length", [39_999, 40_000])
def test_plain_content_at_the_limit_stays_unchanged(context, length):
    source = "x" * length

    payload, reusable = message_conversion.zulip_to_workspace_cached(
        {"id": 99, "content": source}, context
    )

    assert payload == {"kind": "markdown", "content": source}
    assert reusable


def test_oversized_source_is_not_parsed(context):
    source = "x" * (message_payloads.MARKDOWN_CONTENT_MAX_LENGTH + 1)
    with mock.patch.object(
        message_conversion._ZulipToWorkspace, "body", side_effect=AssertionError
    ):
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, context
        )
    assert_size_notice(payload, reusable, context)


def test_snapshot_is_bounded_before_base64_encoding(context):
    source = "> " + "😀" * 8000
    with mock.patch.object(
        message_conversion.base64, "urlsafe_b64encode", side_effect=AssertionError
    ):
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, context
        )
    assert_size_notice(payload, reusable, context)
    assert source in payload["content"]


def test_many_inline_expansions_stop_before_processing_the_entire_source(context):
    source = "https://example.test/ " * 1500
    with mock.patch.object(
        message_conversion._ZulipToWorkspace,
        "target",
        wraps=message_conversion._ZulipToWorkspace({"id": 99}, context).target,
    ) as target:
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, context
        )
    assert_size_notice(payload, reusable, context)
    assert source in payload["content"]
    assert target.call_count < 1500


def test_unsupported_block_attachment_list_includes_separator_budget(context):
    source = "/poll\n" + "![x](https://example.test/p.png)\n" * 1000
    context["files"] = {"https://example.test/p.png": "urn:image:" + "1" * 36}
    with mock.patch.object(
        message_conversion._ZulipToWorkspace,
        "target",
        wraps=message_conversion._ZulipToWorkspace({"id": 99}, context).target,
    ) as target:
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, context
        )
    assert_size_notice(payload, reusable, context)
    assert source in payload["content"]
    assert target.call_count < 1000


def test_deep_quotes_stop_before_recursion_or_snapshot_growth(context):
    source = "x"
    for depth in range(80):
        fence = "`" * (depth + 3)
        source = (
            "@_**Example|7** "
            f"[said](https://zulip.example.test/#narrow/near/{depth + 1}):\n"
            f"{fence}quote\n{source}\n{fence}"
        )
    assert len(source) < message_payloads.MARKDOWN_CONTENT_MAX_LENGTH
    # Keep snapshots tiny: the depth guard must work independently of size.
    with mock.patch.object(
        message_conversion._ZulipToWorkspace, "snapshot", return_value="snapshot"
    ) as snapshot:
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, context
        )
    assert_size_notice(payload, reusable, context)
    assert source in payload["content"]
    snapshot.assert_not_called()


def test_fallback_fence_and_original_url_cannot_exceed_the_limit(context):
    for source, original in (
        ("`" * 39_000 + "\n" + "@**Example|7** " * 40, context["original_url"]),
        ("x" * 40_001, "https://example.test/" + "x" * 40_000),
    ):
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, {**context, "original_url": original}
        )
        assert payload["content"].startswith(
            "Not supported in Workspace: formatted message is too large"
        )
        assert len(payload["content"]) <= message_payloads.MARKDOWN_CONTENT_MAX_LENGTH
        assert not reusable


def test_snapshot_size_boundary_counts_base64_and_link_overhead(context):
    prefix = f"[unknown](urn:forward:{context['current_message_uuid']}?snapshot=)"
    available = message_payloads.MARKDOWN_CONTENT_MAX_LENGTH - len(prefix)
    length = available * 3 // 4
    for extra in (0, 1):
        source = "> " + "x" * (length + extra)
        payload, reusable = message_conversion.zulip_to_workspace_cached(
            {"id": 99, "content": source}, context
        )
        if extra:
            assert_size_notice(payload, reusable, context)
        else:
            assert payload["content"].startswith("[unknown](urn:forward:")
            assert (
                len(payload["content"]) <= message_payloads.MARKDOWN_CONTENT_MAX_LENGTH
            )
