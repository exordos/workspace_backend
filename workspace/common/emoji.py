# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Unicode shortcode identities shared with Zulip.

unicode_emoji.json combines Zulip 12.1's Apache-2.0 licensed
tools/setup/emoji/emoji_names.py with Workspace's emojibase shortcodes and
emoji-shortcodes-overrides.data.ts. Each entry contains the Workspace name,
Zulip name, and accepted aliases. The Unicode sequence identifies an emoji.
"""

import importlib.resources
import json


NAMES_BY_CODE = json.loads(
    importlib.resources.files("workspace.common")
    .joinpath("unicode_emoji.json")
    .read_text(encoding="utf-8")
)
CODE_BY_NAME = {
    name: code for code, names in NAMES_BY_CODE.items() for name in names[2:]
}
ZULIP_NAME_BY_CODE = {code: names[1] for code, names in NAMES_BY_CODE.items()}


def normalize_name(name: str) -> str:
    code = CODE_BY_NAME.get(name)
    return NAMES_BY_CODE[code][0] if code is not None else name


def name_for_code(code: str, name: str) -> str:
    names = NAMES_BY_CODE.get(code)
    return names[0] if names is not None else name
