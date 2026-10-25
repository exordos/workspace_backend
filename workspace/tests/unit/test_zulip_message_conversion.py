# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import base64
import copy
import json
import pathlib
import re

import pytest

from workspace.bridges.zulip import message_conversion
from workspace.bridges.zulip import content_cache


EXAMPLES = json.loads(
    (pathlib.Path(__file__).parents[1] / "fixtures" / "zulip_messages.json").read_text()
)
MESSAGES = {str(example["message"]["id"]): example["message"] for example in EXAMPLES}
FILE_COUNTS = {
    "fenced_literal_markup": 1,
    "uploaded_document": 1,
    "multiple_attachments": 3,
    "image_markdown": 1,
    "image_link": 2,
    "animated_image": 1,
    "video_attachment": 1,
    "audio_attachment": 1,
    "mixed_formatting": 2,
}


@pytest.mark.parametrize(
    "content,reusable",
    [("Plain **text**", True), ("> quoted text", False), ("```math\nx+y\n```", False)],
)
def test_conversion_cache_excludes_current_message_identity(content, reusable):
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/71",
        "current_message_uuid": "11111111-1111-4111-8111-111111111111",
    }
    message = {"id": 71, "content": content}
    payload, actual = message_conversion.zulip_to_workspace_cached(message, context)
    assert actual == reusable
    assert payload == message_conversion.zulip_to_workspace(message, context)


def test_conversion_cache_key_is_scoped_to_provider_source_and_resolved_context():
    provider = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    context = {"files": {"/upload": "urn:file:" + provider}}
    original = content_cache.key(provider, "incoming", "Source", context)
    assert original == content_cache.key(provider, "incoming", "Source", context)
    assert original != content_cache.key(other, "incoming", "Source", context)
    assert original != content_cache.key(provider, "outgoing", "Source", context)
    assert original != content_cache.key(provider, "incoming", "Edited", context)
    assert original != content_cache.key(
        provider, "incoming", "Source", {"files": {"/upload": "urn:file:" + other}}
    )


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("suffix", ["", "|8"])
def test_silent_user_references_keep_the_clickable_identity(mapped, suffix):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "mention_uuids": (
            {"id:8": identifier, "name:Admin [Ops]": identifier} if mapped else {}
        ),
    }
    payload = message_conversion.zulip_to_workspace(
        {
            "id": 2,
            "content": f"@_**Admin [Ops]{suffix}** пометил/а тему как нерешенную.",
        },
        context,
    )

    if mapped:
        assert payload == {
            "kind": "markdown",
            "content": rf'[Admin \[Ops\]](urn:user:{identifier} "silent") пометил/а тему как нерешенную.',
        }
        assert message_conversion.workspace_to_zulip(payload, context) == (
            "@_**Admin [Ops]|8** пометил/а тему как нерешенную."
        )
    else:
        assert (
            "Not supported in Workspace: unresolved user Admin [Ops]"
            in payload["content"]
        )


@pytest.mark.parametrize("mapped_source", [False, True])
def test_silent_name_inside_a_bot_quote_stays_readable(mapped_source):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "strict_identities": True,
        "current_message_uuid": "22222222-2222-4222-8222-222222222222",
        "message_uuids": {"1": identifier} if mapped_source else {},
        "messages": {"1": {"content": "A longer original notification."}},
        "mention_uuids": {"id:8": identifier},
    }
    payload = message_conversion.zulip_to_workspace(
        {
            "id": 2,
            "content": (
                "@_**Notification Bot|6** [сказал/а](https://zulip.example.test/#narrow/near/1):\n"
                "```quote\n@_**Admin Example|8** пометил/а тему как нерешенную.\n```"
            ),
        },
        context,
    )
    snapshot = re.search(r"snapshot=([^)]*)", payload["content"])[1]
    assert message_conversion.parse_snapshot("snapshot=" + snapshot) == (
        f'[Admin Example](urn:user:{identifier} "silent") пометил/а тему как нерешенную.'
    )
    assert (
        f"urn:forward:{identifier}" in payload["content"]
        if mapped_source
        else "urn:forward:" in payload["content"]
    )
    assert "Not supported" not in payload["content"]
    assert "Original message" not in payload["content"]


@pytest.mark.parametrize("title", ['"silent"', "'silent'", "(silent)"])
@pytest.mark.parametrize("strict", [False, True])
def test_silent_user_link_exports_without_turning_into_an_active_mention(title, strict):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "strict_identities": strict,
        "mention_uuids": {"id:8": identifier},
        "user_ids": {identifier: ("8", "Admin Example")},
    }
    assert (
        message_conversion.workspace_to_zulip(
            {
                "kind": "markdown",
                "content": f"[Admin Example](urn:user:{identifier} {title})",
            },
            context,
        )
        == "@_**Admin Example|8**"
    )


@pytest.mark.parametrize(
    "source", ["@_**|8**", "@_**Admin Example|8**", "@_**Admin Example**"]
)
def test_silent_reference_variants_resolve_the_same_profile(source):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "mention_uuids": {"id:8": identifier, "name:Admin Example": identifier},
        "user_ids": {identifier: ("8", "Admin Example")},
    }
    assert message_conversion.zulip_to_workspace(
        {"id": 2, "content": source}, context
    ) == {
        "kind": "markdown",
        "content": f'[Admin Example](urn:user:{identifier} "silent")',
    }


def test_id_only_reference_without_a_persisted_name_keeps_the_original_source():
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "mention_uuids": {"id:8": "11111111-1111-4111-8111-111111111111"},
    }
    payload = message_conversion.zulip_to_workspace(
        {"id": 2, "content": "@_**|8**"}, context
    )
    assert "unresolved user |8" in payload["content"]
    assert "[Original message]" in payload["content"]
    assert "urn:user:" not in payload["content"]


@pytest.mark.parametrize("syntax", ["@**Admin Example|8**", "@_**Admin Example|8**"])
@pytest.mark.parametrize("wrapper", ["> {}", "```quote\n{}\n```", "> > {}"])
def test_all_user_mentions_in_quotes_are_silent(syntax, wrapper):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "mention_uuids": {"id:8": identifier},
    }
    payload = message_conversion.zulip_to_workspace(
        {"id": 2, "content": wrapper.format(syntax)}, context
    )
    snapshot = re.search(r"snapshot=([^)]*)", payload["content"])[1]
    content = message_conversion.parse_snapshot("snapshot=" + snapshot)
    while "snapshot=" in content:
        snapshot = re.search(r"snapshot=([^)]*)", content)[1]
        content = message_conversion.parse_snapshot("snapshot=" + snapshot)
    assert (
        content.removeprefix("> ") == f'[Admin Example](urn:user:{identifier} "silent")'
    )
    assert "Not supported" not in payload["content"]


@pytest.mark.parametrize(
    "source",
    [
        r"\@_**Admin Example|8**",
        "`@_**Admin Example|8**`",
        "```text\n@_**Admin Example|8**\n```",
    ],
)
def test_literal_silent_mention_is_not_a_user_reference(source):
    payload = message_conversion.zulip_to_workspace({"id": 2, "content": source}, {})
    assert payload == {"kind": "markdown", "content": source}


@pytest.mark.parametrize("name", ["Engineering", "role:administrators", "Missing"])
def test_silent_group_mentions_are_explicitly_unsupported(name):
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "user_group_ids": {"Engineering": 101, "role:administrators": 102},
    }
    payload = message_conversion.zulip_to_workspace(
        {"id": 2, "content": f"@_*{name}*"}, context
    )
    assert "Not supported in Workspace:" in payload["content"]
    assert name in payload["content"]
    assert (
        "[Original message](urn:url:https://zulip.example.test/#narrow/near/2)"
        in payload["content"]
    )
    assert "urn:user:" not in payload["content"]


@pytest.fixture(params=EXAMPLES, ids=lambda example: example["name"])
def example(request):
    return copy.deepcopy(request.param)


@pytest.fixture
def context(example):
    return {**example["conversion_context"], "messages": copy.deepcopy(MESSAGES)}


def test_zulip_to_workspace_matches_examples(example, context):
    before = copy.deepcopy((example, context))

    actual = message_conversion.zulip_to_workspace(example["message"], context)

    assert actual == example["expected_payload"]
    assert (example, context) == before


def test_workspace_to_zulip_matches_examples(example, context):
    before = copy.deepcopy((example, context))

    actual = message_conversion.workspace_to_zulip(example["expected_payload"], context)

    assert actual == example["expected_zulip_content"]
    assert (example, context) == before


def test_canonical_zulip_round_trip_preserves_workspace_payload(example, context):
    content = message_conversion.workspace_to_zulip(
        example["expected_payload"], context
    )

    actual = message_conversion.zulip_to_workspace(
        {**example["message"], "content": content}, context
    )

    assert actual == example["expected_payload"]


def test_zulip_files_match_examples(example, context):
    files = message_conversion.get_zulip_files(example["message"], context)

    assert len(files) == FILE_COUNTS.get(example["name"], 0)
    assert files == list(context.get("files", {}))


def test_workspace_files_match_examples(example, context):
    files = message_conversion.get_workspace_files(example["expected_payload"])

    assert len(files) == FILE_COUNTS.get(example["name"], 0)
    assert files == list(context.get("files", {}).values())


def test_conversion_preserves_file_count_and_identity(example, context):
    source_files = message_conversion.get_zulip_files(example["message"], context)
    payload = message_conversion.zulip_to_workspace(example["message"], context)
    converted_files = message_conversion.get_workspace_files(payload)

    assert len(source_files) == len(converted_files)
    assert converted_files == [context["files"][url] for url in source_files]
    content = message_conversion.workspace_to_zulip(payload, context)
    assert (
        message_conversion.get_zulip_files({"content": content}, context)
        == source_files
    )


@pytest.mark.parametrize(
    "content,expected",
    [
        ("/user_uploads/1/abc/file.pdf", ["/user_uploads/1/abc/file.pdf"]),
        ("(/user_uploads/1/abc/file.pdf).", ["/user_uploads/1/abc/file.pdf"]),
        (
            "https://zulip.example.test/user_uploads/1/abc/file.pdf, next",
            ["https://zulip.example.test/user_uploads/1/abc/file.pdf"],
        ),
        (
            "https://zulip.example.test/user_uploads/1/abc/file.pdf",
            ["https://zulip.example.test/user_uploads/1/abc/file.pdf"],
        ),
        (
            "[file](https://zulip.example.test/user_uploads/1/abc/file.pdf?download=1)",
            ["https://zulip.example.test/user_uploads/1/abc/file.pdf?download=1"],
        ),
        (
            "[file](https://[2001:db8::1]:8443/user_uploads/1/abc/file.pdf)",
            ["https://[2001:db8::1]:8443/user_uploads/1/abc/file.pdf"],
        ),
        (
            "![image](https://images.example.test/preview)",
            ["https://images.example.test/preview"],
        ),
        (
            "```text\n[file](/user_uploads/1/abc/file.pdf)\n```",
            ["/user_uploads/1/abc/file.pdf"],
        ),
        ("`[file](/user_uploads/1/abc/file.pdf)`", ["/user_uploads/1/abc/file.pdf"]),
        (
            "[nested [caption]](/user_uploads/1/abc/file_(v2).pdf)",
            ["/user_uploads/1/abc/file_(v2).pdf"],
        ),
        ("[site](https://example.test/?next=/user_uploads/1/abc/file.pdf)", []),
        ("[/user_uploads/1/abc/file.pdf](https://example.test)", []),
        ("https://example.test/docs/user_uploads/file.pdf", []),
        (r"\![image](https://images.example.test/preview)", []),
        ("[documentation](https://docs.example.test/guide)", []),
        ("", []),
    ],
)
def test_zulip_file_extraction(content, expected):
    assert message_conversion.get_zulip_files({"content": content}, {}) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://[broken/user_uploads/file.pdf",
        "https://broken]/user_uploads/file.pdf",
        "https://[not-an-ip]/user_uploads/file.pdf",
        "https://example.test／user_uploads/file.pdf",
    ],
)
@pytest.mark.parametrize(
    "formatting", ["{url}", "<{url}>", "[file]({url})", '![image]({url} "Image")']
)
def test_malformed_urls_preserve_text_and_other_references(url, formatting):
    source = formatting.format(url=url)
    user_uuid = "11111111-1111-4111-8111-111111111111"
    file_url = "/user_uploads/1/abc/report.pdf"
    file_urn = "urn:file:22222222-2222-4222-8222-222222222222"
    context = {
        "server_url": "https://zulip.example.test",
        "mention_uuids": {"id:12": user_uuid},
        "user_ids": {user_uuid: (12, "Bob")},
        "files": {file_url: file_urn},
    }
    content = f"{source}\n@**Bob|12** [report]({file_url})"
    message = {"id": 1, "content": content}

    assert message_conversion.get_zulip_files(
        message, context, include_quotes=True
    ) == [file_url]
    payload = message_conversion.zulip_to_workspace(message, context)
    assert payload["content"] == (
        f"{source}\n[Bob](urn:user:{user_uuid}) [report]({file_urn})"
    )
    assert message_conversion.workspace_to_zulip(payload, context) == content


@pytest.mark.parametrize("kind", ["file", "image", "video"])
@pytest.mark.parametrize(
    "formatting",
    [
        "[{urn}]({urn})",
        "![media]({urn})",
        "`[file]({urn})`",
        "```text\n[file]({urn})\n```",
    ],
)
def test_workspace_file_extraction(kind, formatting):
    urn = f"urn:{kind}:11111111-1111-4111-8111-111111111111?name=report.pdf&size=123"
    payload = {"kind": "markdown", "content": formatting.format(urn=urn)}

    assert message_conversion.get_workspace_files(payload) == [urn]


@pytest.mark.parametrize(
    "content",
    [
        "No attachments",
        "[document](urn:url:https://files.example.test/report.pdf)",
        "[user](urn:user:11111111-1111-4111-8111-111111111111)",
        "[invalid](urn:file:not-a-uuid)",
        "[invalid](urn:file:11111111-1111-4111-8111-111111111111-extra)",
        "[site](urn:url:https://example.test/?next=urn:file:11111111-1111-4111-8111-111111111111)",
        "[urn:file:11111111-1111-4111-8111-111111111111](urn:url:https://example.test)",
        r"\[file](urn:file:11111111-1111-4111-8111-111111111111)",
        "[quote](urn:forward:11111111-1111-4111-8111-111111111111?snapshot=invalid!)",
        "[quote](urn:quote:11111111-1111-4111-8111-111111111111)",
        "",
    ],
)
def test_workspace_file_extraction_ignores_other_links_and_quotes(content):
    assert (
        message_conversion.get_workspace_files({"kind": "markdown", "content": content})
        == []
    )


def test_workspace_file_extraction_ignores_encrypted_payloads():
    payload = {
        "kind": "e2ee",
        "content": "[file](urn:file:11111111-1111-4111-8111-111111111111)",
    }

    assert message_conversion.get_workspace_files(payload) == []


@pytest.mark.parametrize("depth", [1, 3])
def test_workspace_file_extraction_ignores_nested_forward_snapshots(depth):
    content = "[report](urn:file:11111111-1111-4111-8111-111111111111)"
    for _ in range(depth):
        encoded = base64.urlsafe_b64encode(content.encode()).decode().rstrip("=")
        content = f"[unknown](urn:forward:22222222-2222-4222-8222-222222222222?snapshot={encoded})"
    content += "\n\n[own file](urn:file:33333333-3333-4333-8333-333333333333)"

    assert message_conversion.get_workspace_files(
        {"kind": "markdown", "content": content}
    ) == ["urn:file:33333333-3333-4333-8333-333333333333"]


@pytest.mark.parametrize(
    "quote",
    [
        "> {file}",
        "```quote\n{file}\n```",
        "@_**Alice|9** [said](https://zulip.example.test/#narrow/near/1):\n```quote\n{file}\n```",
        "````quote\n```quote\n{file}\n```\n````",
    ],
)
def test_file_extraction_excludes_quotes_before_and_after_conversion(quote):
    own_url = "/user_uploads/1/own/report.pdf"
    quoted_url = "/user_uploads/1/quoted/report.pdf"
    own_urn = "urn:file:11111111-1111-4111-8111-111111111111"
    quoted_urn = "urn:file:22222222-2222-4222-8222-222222222222"
    context = {
        "server_url": "https://zulip.example.test",
        "message_uuids": {"1": "33333333-3333-4333-8333-333333333333"},
        "files": {own_url: own_urn, quoted_url: quoted_urn},
    }
    quoted = quote.format(file=f"[quoted file]({quoted_url})")
    message = {"id": 2, "content": f"{quoted}\n\n[own file]({own_url})"}

    payload = message_conversion.zulip_to_workspace(message, context)

    assert message_conversion.get_zulip_files(message, context) == [own_url]
    assert message_conversion.get_workspace_files(payload) == [own_urn]


def test_repeated_file_references_preserve_count_and_order():
    url = "/user_uploads/1/abc/report.pdf"
    urn = "urn:file:11111111-1111-4111-8111-111111111111"
    context = {"server_url": "https://zulip.example.test", "files": {url: urn}}
    message = {"id": 1, "content": f"[one]({url}) and [two]({url})"}

    payload = message_conversion.zulip_to_workspace(message, context)

    assert message_conversion.get_zulip_files(message, context) == [url, url]
    assert message_conversion.get_workspace_files(payload) == [urn, urn]


@pytest.mark.parametrize(
    "source,expected",
    [
        ("[report.pdf]({path})", "report.pdf ({notice})"),
        ('![preview](<{url}> "Preview")', "preview ({notice})"),
        ("{url}", r"report\_old\.pdf ({notice})"),
        ("<{url}>", r"report\_old\.pdf ({notice})"),
        ("{path}", r"report\_old\.pdf ({notice})"),
        ("({url}).", r"(report\_old\.pdf ({notice}))."),
    ],
)
@pytest.mark.parametrize(
    "context_key,notice",
    [("deleted_files", "file deleted"), ("inaccessible_files", "access denied")],
)
def test_unavailable_files_become_text_without_affecting_other_elements(
    source, expected, context_key, notice
):
    path = "/user_uploads/1/missing/report_old.pdf"
    url = "https://zulip.example.test" + path
    kept = "/user_uploads/1/kept/report.pdf"
    urn = "urn:file:11111111-1111-4111-8111-111111111111"
    user = "22222222-2222-4222-8222-222222222222"
    context = {
        "server_url": "https://zulip.example.test",
        context_key: {path, url},
        "files": {kept: urn},
        "mention_uuids": {"id:7": user},
    }
    message = {
        "id": 1,
        "content": source.format(path=path, url=url) + f" [kept]({kept}) @**Bob|7**",
    }

    payload = message_conversion.zulip_to_workspace(message, context)

    assert payload["content"] == (
        f"{expected.format(notice=notice)} [kept]({urn}) [Bob](urn:user:{user})"
    )
    assert message_conversion.get_workspace_files(payload) == [urn]


@pytest.mark.parametrize(
    "prefix", ["/poll Choose ", "/me attaches ", "Check $$x^2$$: "]
)
@pytest.mark.parametrize(
    "context_key,notice",
    [("deleted_files", "file deleted"), ("inaccessible_files", "access denied")],
)
def test_unavailable_files_remain_visible_in_unsupported_content(
    prefix, context_key, notice
):
    path = "/user_uploads/1/missing/report.pdf"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/1",
        context_key: {path},
    }

    payload = message_conversion.zulip_to_workspace(
        {"id": 1, "content": f"{prefix}[report]({path})"}, context
    )

    assert rf"report\.pdf ({notice})" in payload["content"]
    assert "/user_uploads/" not in payload["content"]


@pytest.mark.parametrize(
    "url",
    ["https://external.example.test/photo.png", "/user_uploads/1/offline/photo.png"],
)
def test_unsupported_content_retains_files_without_a_local_copy(url):
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/1",
    }

    payload = message_conversion.zulip_to_workspace(
        {"id": 1, "content": f"/poll Choose ![photo]({url})"}, context
    )

    absolute = url if url.startswith("https://") else context["server_url"] + url
    assert f"[photo\\.png](urn:url:{absolute})" in payload["content"]
    assert message_conversion.get_workspace_files(payload) == []


@pytest.mark.parametrize(
    "context_key,notice",
    [("deleted_files", "file deleted"), ("inaccessible_files", "access denied")],
)
def test_unavailable_files_are_marked_inside_quoted_snapshots(context_key, notice):
    path = "/user_uploads/1/missing/report.pdf"
    context = {
        "server_url": "https://zulip.example.test",
        context_key: {path},
    }

    payload = message_conversion.zulip_to_workspace(
        {"id": 1, "content": f"> [report]({path})"}, context
    )

    snapshot = payload["content"].partition("?snapshot=")[2].removesuffix(")")
    assert base64.urlsafe_b64decode(snapshot + "=" * (-len(snapshot) % 4)).decode() == (
        f"report ({notice})"
    )


@pytest.mark.parametrize(
    "quote", ["> {file}", "```quote\n{file}\n```", "```spoiler Details\n> {file}\n```"]
)
def test_workspace_file_extraction_excludes_markdown_quotes(quote):
    content = quote.format(
        file="[quoted](urn:file:11111111-1111-4111-8111-111111111111)"
    )
    content += "\n\n[own](urn:file:22222222-2222-4222-8222-222222222222)"

    assert message_conversion.get_workspace_files(
        {"kind": "markdown", "content": content}
    ) == ["urn:file:22222222-2222-4222-8222-222222222222"]


@pytest.mark.parametrize(
    "prefix", ["Check $$x^2$$: ", "/poll Choose ", "/todo Review ", "/me attaches "]
)
def test_unsupported_content_keeps_this_messages_attachments(prefix):
    url = "/user_uploads/1/abc/report.pdf"
    urn = "urn:file:11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/1",
        "files": {url: urn},
    }
    message = {"id": 1, "content": f"{prefix}[report.pdf]({url})"}

    payload = message_conversion.zulip_to_workspace(message, context)

    assert message_conversion.get_zulip_files(message, context) == [url]
    assert message_conversion.get_workspace_files(payload) == [urn]


@pytest.mark.parametrize(
    "content,expected",
    [
        (
            "[nested [label]](https://example.test/a_(b_(c)))",
            "[nested [label]](urn:url:https://example.test/a_(b_(c)))",
        ),
        (
            '[guide](https://example.test/a_(b) "A guide")',
            '[guide](urn:url:https://example.test/a_(b) "A guide")',
        ),
        (
            "Read https://example.test/a_(b).",
            "Read [https://example.test/a_(b)](urn:url:https://example.test/a_(b)).",
        ),
        (
            "[guide](https://[2001:db8::1]:8443/a)",
            "[guide](urn:url:https://[2001:db8::1]:8443/a)",
        ),
        (
            "https://[::1]/a",
            "[https://[::1]/a](urn:url:https://[::1]/a)",
        ),
        (r"\@**Bob|12**", r"\@**Bob|12**"),
        (r"\\@**Bob|12**", r"\\[Bob](urn:user:11111111-1111-4111-8111-111111111111)"),
        (
            r"\[guide](https://example.test)",
            r"\[guide]([https://example.test](urn:url:https://example.test))",
        ),
    ],
)
def test_markdown_boundaries(content, expected):
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/1",
        "mention_uuids": {"id:12": "11111111-1111-4111-8111-111111111111"},
    }

    assert message_conversion.zulip_to_workspace(
        {"id": 1, "content": content}, context
    ) == {"kind": "markdown", "content": expected}


def test_same_message_id_on_another_server_is_an_external_link():
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/1",
        "message_uuids": {"1001": "11111111-1111-4111-8111-111111111111"},
    }
    message = {
        "id": 1,
        "content": "[Other realm](https://other.example.test/#narrow/near/1001)",
    }

    assert message_conversion.zulip_to_workspace(message, context)["content"] == (
        "[Other realm](urn:url:https://other.example.test/#narrow/near/1001)"
    )


@pytest.mark.parametrize(
    "origin", ["https://zulip.example.test:9443", "http://zulip.example.test"]
)
def test_quote_on_another_origin_does_not_reuse_a_local_message_id(origin):
    context = {
        "server_url": "https://zulip.example.test",
        "strict_identities": True,
        "current_message_uuid": "22222222-2222-4222-8222-222222222222",
        "message_uuids": {"1001": "11111111-1111-4111-8111-111111111111"},
        "messages": {"1001": {"content": "Quoted text"}},
    }
    url = f"{origin}/#narrow/near/1001"
    payload = message_conversion.zulip_to_workspace(
        {
            "id": 2,
            "content": f"@_**Alice|7** [said]({url}):\n```quote\nQuoted text\n```",
        },
        context,
    )
    assert "urn:quote:" not in payload["content"]
    assert "urn:forward:" in payload["content"]
    assert f"urn:url:{url}" in payload["content"]


def test_malformed_quote_links_preserve_the_source_and_converted_snapshot():
    user_uuid = "11111111-1111-4111-8111-111111111111"
    url = "https://[broken/#narrow/near/1001"
    context = {
        "server_url": "https://zulip.example.test",
        "strict_identities": True,
        "current_message_uuid": "22222222-2222-4222-8222-222222222222",
        "message_uuids": {"1001": user_uuid},
        "mention_uuids": {"id:12": user_uuid},
    }
    snapshot = (
        base64.urlsafe_b64encode(
            f'https://[broken/ [Bob](urn:user:{user_uuid} "silent")'.encode()
        )
        .decode()
        .rstrip("=")
    )
    message = {
        "id": 2,
        "content": f"@_**Alice|7** [said]({url}):\n```quote\nhttps://[broken/ @**Bob|12**\n```",
    }

    assert (
        message_conversion.get_zulip_files(message, context, include_quotes=True) == []
    )
    assert message_conversion.zulip_to_workspace(message, context)["content"] == (
        f"[unknown](urn:forward:{context['current_message_uuid']}?snapshot={snapshot})"
        f"\n@_**Alice|7** [said]({url}):"
    )


def test_partial_quote_without_original_message_keeps_quoted_text():
    example = next(
        case for case in EXAMPLES if case["name"] == "partial_quoted_message_reference"
    )

    assert (
        message_conversion.zulip_to_workspace(
            example["message"], example["conversion_context"]
        )
        == example["expected_payload"]
    )


@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_quote_references_stay_separate_from_adjacent_comments(separator):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "message_uuids": {"1": identifier},
        "messages": {"1": {"content": "Original leaf."}},
    }
    message = {
        "id": 2,
        "content": (
            f"Before.{separator}"
            "@_**Alice|9** [said](https://zulip.example.test/#narrow/near/1):\n"
            f"```quote\nOriginal leaf.\n```{separator}After."
        ),
    }

    assert message_conversion.zulip_to_workspace(message, context) == {
        "kind": "markdown",
        "content": f"Before.\n\n[Alice](urn:quote:{identifier})\n\nAfter.",
    }


@pytest.mark.parametrize("original_available", [True, False])
def test_partial_nested_quote_preserves_blocks_in_a_snapshot(original_available):
    inner_uuid = "11111111-1111-4111-8111-111111111111"
    outer_uuid = "22222222-2222-4222-8222-222222222222"
    current_uuid = "33333333-3333-4333-8333-333333333333"
    inner = (
        "@_**Alice|9** [said](https://zulip.example.test/#narrow/near/1):\n"
        "```quote\nOriginal leaf.\n```\nImmediate comment."
    )
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/3",
        "current_message_uuid": current_uuid,
        "strict_identities": True,
        "message_uuids": {"1": inner_uuid, "2": outer_uuid},
        "mention_uuids": {"id:9": "44444444-4444-4444-8444-444444444444"},
        "messages": {"1": {"content": "Original leaf."}},
    }
    if original_available:
        context["messages"]["2"] = {"content": inner + "\n\nLater edit."}
    message = {
        "id": 3,
        "content": (
            "@_**Bob|12** [wrote](https://zulip.example.test/#narrow/near/2):\n"
            f"````quote\n{inner}\n````\n\nOutside reply."
        ),
    }
    snapshot = (
        base64.urlsafe_b64encode(
            f"[Alice](urn:quote:{inner_uuid})\n\nImmediate comment.".encode()
        )
        .decode()
        .rstrip("=")
    )

    assert message_conversion.zulip_to_workspace(message, context) == {
        "kind": "markdown",
        "content": (
            f"[Bob](urn:forward:{outer_uuid}?snapshot={snapshot})\n\nOutside reply."
        ),
    }


@pytest.mark.parametrize(
    "kind", ["user", "stream", "topic", "message", "file", "image", "video"]
)
def test_reverse_conversion_requires_identity_and_attachment_mappings(kind):
    payload = {
        "kind": "markdown",
        "content": f"[Reference](urn:{kind}:11111111-1111-4111-8111-111111111111)",
    }

    with pytest.raises(ValueError, match="No Zulip"):
        message_conversion.workspace_to_zulip(
            payload, {"server_url": "https://zulip.example.test"}
        )


@pytest.mark.parametrize(
    "target",
    [
        "https://[broken/",
        "urn:url:https://[broken/",
        "urn:unknown:value",
        "urn:user:not-a-uuid",
        "urn:stream:" + "-" * 36,
        "urn:topic:" + "a" * 36,
        "urn:message:" + "a" * 36,
        "urn:quote:" + "-" * 36,
        "urn:file:not-a-uuid",
        "urn:forward:11111111-1111-4111-8111-111111111111",
        "urn:forward:11111111-1111-4111-8111-111111111111?snapshot=",
        "urn:forward:11111111-1111-4111-8111-111111111111?snapshot=invalid!",
        "urn:forward:11111111-1111-4111-8111-111111111111?snapshot=_w",
    ],
)
def test_unparseable_workspace_elements_remain_text(target):
    user_uuid = "22222222-2222-4222-8222-222222222222"
    original = f'[broken](<{target}> "Keep this")'
    payload = {
        "kind": "markdown",
        "content": f"{original} [Bob](urn:user:{user_uuid})",
    }
    context = {"mention_uuids": {"id:12": user_uuid}}

    assert (
        message_conversion.workspace_reference_text(payload["content"])
        == (payload["content"])
    )
    assert message_conversion.get_workspace_files(payload, include_quotes=True) == []
    assert message_conversion.workspace_to_zulip(payload, context) == (
        f"{original} @**Bob|12**"
    )


def test_unparseable_element_inside_a_forward_does_not_block_other_references():
    user_uuid = "22222222-2222-4222-8222-222222222222"
    malformed = (
        "[broken](urn:forward:11111111-1111-4111-8111-111111111111?snapshot=invalid!)"
    )
    snapshot = f"{malformed} [Bob](urn:user:{user_uuid})"
    encoded = base64.urlsafe_b64encode(snapshot.encode()).decode().rstrip("=")
    content = f"[quote](urn:forward:{user_uuid}?snapshot={encoded})"
    context = {"mention_uuids": {"id:12": user_uuid}}

    assert message_conversion.workspace_reference_text(content) == (
        content + "\n" + snapshot
    )
    assert (
        message_conversion.workspace_to_zulip(
            {"kind": "markdown", "content": content}, context
        )
        == f"```quote\n{malformed} @**Bob|12**\n```"
    )


def test_reverse_conversion_rejects_encrypted_content():
    payload = {
        "kind": "e2ee",
        "key_uuid": "11111111-1111-4111-8111-111111111111",
        "content": "Encrypted",
    }

    with pytest.raises(ValueError, match="Only Markdown"):
        message_conversion.workspace_to_zulip(payload, {})


@pytest.mark.parametrize("slug", ["42-general", "42-old-name", "42"])
def test_channel_links_use_stream_id_even_after_a_rename(slug):
    context = {
        "server_url": "https://zulip.example.test",
        "stream_ids": {"renamed channel": 42},
        "topic_uuids": {
            "renamed channel>Roadmap": "11111111-1111-4111-8111-111111111111"
        },
    }
    message = {"id": 1, "content": f"[Plan](#narrow/channel/{slug}/topic/Roadmap)"}

    assert message_conversion.zulip_to_workspace(message, context)["content"] == (
        "[Plan](urn:topic:11111111-1111-4111-8111-111111111111)"
    )


def test_unmapped_message_link_keeps_its_anchor():
    context = {
        "server_url": "https://zulip.example.test",
        "topic_uuids": {"general>Roadmap": "11111111-1111-4111-8111-111111111111"},
    }
    url = "https://zulip.example.test/#narrow/channel/42-general/topic/Roadmap/near/999"

    assert (
        message_conversion.zulip_to_workspace(
            {"id": 1, "content": f"[Not imported yet]({url})"}, context
        )["content"]
        == f"[Not imported yet](urn:url:{url})"
    )


def test_topic_permalink_with_message_id_becomes_a_message_reference():
    context = {
        "server_url": "https://zulip.example.test",
        "message_uuids": {"1001": "11111111-1111-4111-8111-111111111111"},
    }
    content = "[Message](#narrow/channel/42-general/topic/Roadmap/with/1001)"

    assert message_conversion.zulip_to_workspace(
        {"id": 1, "content": content}, context
    )["content"] == ("[Message](urn:message:11111111-1111-4111-8111-111111111111)")


def test_custom_topic_label_and_zulip_hash_escaping_round_trip():
    context = {
        "server_url": "https://zulip.example.test",
        "stream_ids": {"team chat": 42},
        "topic_uuids": {"team chat>Café (v2).": "11111111-1111-4111-8111-111111111111"},
    }
    payload = {
        "kind": "markdown",
        "content": "[Plan](urn:topic:11111111-1111-4111-8111-111111111111)",
    }

    content = message_conversion.workspace_to_zulip(payload, context)

    assert (
        content
        == "[Plan](https://zulip.example.test/#narrow/channel/42-team-chat/topic/Caf.C3.A9.20.28v2.29.2E)"
    )
    assert (
        message_conversion.zulip_to_workspace({"id": 1, "content": content}, context)
        == payload
    )


def test_reverse_file_mapping_uses_identity_not_optional_metadata():
    context = {
        "files": {
            "/user_uploads/1/file/report.pdf": "urn:file:11111111-1111-4111-8111-111111111111?name=old.pdf"
        },
    }
    payload = {
        "kind": "markdown",
        "content": "[Report](urn:file:11111111-1111-4111-8111-111111111111?name=report.pdf&size=123)",
    }

    assert (
        message_conversion.workspace_to_zulip(payload, context)
        == "[Report](/user_uploads/1/file/report.pdf)"
    )


@pytest.mark.parametrize(
    "content", ["\nFirst line.  \nSecond line.\n", "  Quoted text.  "]
)
def test_full_quote_preserves_source_whitespace(content):
    source = {
        "id": 1,
        "type": "private",
        "sender_id": 9,
        "sender_full_name": "Alice",
        "content": content,
    }
    context = {
        "server_url": "https://zulip.example.test",
        "message_uuids": {"1": "11111111-1111-4111-8111-111111111111"},
        "messages": {"1": source},
    }
    payload = {
        "kind": "markdown",
        "content": "[Alice](urn:quote:11111111-1111-4111-8111-111111111111)",
    }

    quoted = message_conversion.workspace_to_zulip(payload, context)

    assert (
        quoted
        == f"@_**Alice|9** [said](https://zulip.example.test/#narrow/near/1):\n```quote\n{content}\n```"
    )
    assert (
        message_conversion.zulip_to_workspace({"id": 2, "content": quoted}, context)
        == payload
    )


@pytest.mark.parametrize("original_available", [True, False])
@pytest.mark.parametrize(
    "suffix",
    [
        " to @_**Bob**",
        " to @_**Bob|12** and @_**Zoë|13**",
        " in [#**general>topic**](https://zulip.example.test/#narrow/near/999)",
    ],
)
def test_contextual_quote_keeps_the_first_message_identity(suffix, original_available):
    identifier = "11111111-1111-4111-8111-111111111111"
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/2",
        "strict_identities": True,
        "message_uuids": {"1": identifier},
        "messages": {"1": {"content": "Original leaf."}} if original_available else {},
    }
    message = {
        "id": 2,
        "content": (
            "@_**Alice|9** [said](https://zulip.example.test/#narrow/near/1)"
            f"{suffix}:\n```quote\nOriginal leaf.\n```\nImmediate reply."
        ),
    }
    target = f"urn:quote:{identifier}"
    if not original_available:
        target += "?text=Original%20leaf."
    assert message_conversion.zulip_to_workspace(message, context) == {
        "kind": "markdown",
        "content": f"[Alice]({target})\n\nImmediate reply.",
    }
