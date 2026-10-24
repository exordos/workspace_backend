# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.


import pytest

from workspace.common import emoji


def test_unicode_aliases_preserve_workspace_canonical_shortcodes():
    assert emoji.normalize_name("+1") == emoji.normalize_name("like") == "thumbs_up"
    assert emoji.name_for_code("1f44d", "+1") == "thumbs_up"
    assert emoji.CODE_BY_NAME["thumbs_up"] == "1f44d"
    assert emoji.normalize_name("working_on_it") == "working_on_it"
    assert emoji.normalize_name("custom_emoji") == "custom_emoji"
    assert emoji.CODE_BY_NAME["party"] == "1f389"
    assert emoji.ZULIP_NAME_BY_CODE["1f389"] == "tada"


@pytest.mark.parametrize(
    "symbol,name",
    [
        ("👍", "thumbs_up"),
        ("👍\ufe0f", "thumbs_up"),
        ("❤", "heart"),
        ("❤️", "heart"),
        ("🇷🇺", "flag_ru"),
        ("👩\u200d💻", "woman_technologist"),
        ("#️⃣", "hash"),
        ("👍🏻", "+1_tone1"),
        ("👍🏿", "+1_tone5"),
        ("👩🏽\u200d💻", "woman_technologist_tone3"),
        ("🫱🏻\u200d🫲🏿", "handshake_tone1_5"),
        ("🕐", "clock1"),
        (":THUMBS-UP:", "thumbs_up"),
        ("👍🏻thumbs_up", "👍🏻thumbs_up"),
        ("not_an_emoji", "not_an_emoji"),
    ],
)
def test_unicode_symbols_use_the_same_identity_as_shortcodes(symbol, name):
    assert emoji.normalize_name(symbol) == name


def test_unicode_code_normalization_preserves_modifiers_and_keycap_padding():
    assert emoji.name_for_code("2764-FE0F", "red_heart") == "heart"
    assert emoji.name_for_code("31-fe0f-20e3", "keycap_1") == "one"
    assert (
        emoji.name_for_code("1F469-1F3FD-200D-1F4BB", "remote_alias")
        == "woman_technologist_tone3"
    )
    assert (
        emoji.normalize_code("1FAF1-1F3FB-200D-1FAF2-1F3FF")
        == "1faf1-1f3fb-200d-1faf2-1f3ff"
    )


def test_workspace_and_zulip_names_have_separate_meanings():
    assert emoji.CODE_BY_NAME["sunset"] == "1f307"
    assert emoji.ZULIP_CODE_BY_NAME["sunset"] == "1f306"
    assert emoji.CODE_BY_NAME["computer"] == "1f5a5"
    assert emoji.ZULIP_CODE_BY_NAME["computer"] == "1f4bb"
    assert emoji.ZULIP_NAME_BY_CODE["1f307"] != "sunset"


def test_catalog_round_trips_every_unicode_sequence_and_zulip_alias():
    assert emoji.CATALOG["zulip_version"] == "12.1.2"
    assert emoji.CATALOG["emojibase_version"] == "17.0.0"
    assert len(emoji.NAMES_BY_CODE) == 3979
    assert len(emoji.ZULIP_NAME_BY_CODE) == 1883
    for code, names in emoji.NAMES_BY_CODE.items():
        symbol = "".join(chr(int(point, 16)) for point in code.split("-"))
        assert emoji.normalize_name(symbol) == names[0]
        assert emoji.code_for_name(names[0]) == code
        for name in names:
            assert emoji.code_for_name(name) == code
    for name, code in emoji.ZULIP_CODE_BY_NAME.items():
        canonical = emoji.name_for_code(code, name)
        assert emoji.code_for_name(canonical) == code


def test_existing_remote_reaction_keeps_its_exact_identity():
    observed = [
        {
            "reaction_type": "unicode_emoji",
            "emoji_code": "1F44D-FE0F",
            "emoji_name": "old_alias",
        }
    ]
    assert emoji.zulip_identity("thumbs_up", observed) == (
        "1F44D-FE0F",
        "old_alias",
    )


def test_extended_unicode_does_not_claim_unsupported_zulip_names():
    assert emoji.code_for_name("👍🏻") == "1f44d-1f3fb"
    assert emoji.zulip_identity("+1_tone1", []) is None
    observed = [
        {
            "reaction_type": "unicode_emoji",
            "emoji_code": "1f44d-1f3fb",
            "emoji_name": "remote_skin_tone_alias",
        }
    ]
    assert emoji.zulip_identity("+1_tone1", observed) == (
        "1f44d-1f3fb",
        "remote_skin_tone_alias",
    )
    assert (
        emoji.zulip_identity(
            "custom_emoji",
            [
                {
                    "reaction_type": "realm_emoji",
                    "emoji_code": "123",
                    "emoji_name": "custom_emoji",
                }
            ],
        )
        is None
    )


def test_unknown_provider_code_cannot_reuse_another_emojis_shortcode():
    code = "1f600-200d-1f4bb"
    symbol = "😀‍💻"
    assert emoji.name_for_code(code, "thumbs_up") == symbol
    assert emoji.normalize_name(symbol) == symbol
    observed = [
        {
            "reaction_type": "unicode_emoji",
            "emoji_code": code,
            "emoji_name": "thumbs_up",
        }
    ]
    assert emoji.zulip_identity(symbol, observed) == (code, "thumbs_up")
    assert emoji.zulip_identity("thumbs_up", observed) == ("1f44d", "+1")
