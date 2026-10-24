# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Unicode identities with separate Workspace and Zulip shortcode namespaces.

Regenerate the versioned catalog with tools/sync_emoji.py. Provider names can
mean different emoji in Workspace, so only Unicode codes cross that boundary.
"""

import importlib.resources
import json
import re
import typing


CATALOG = json.loads(
    importlib.resources.files("workspace.common")
    .joinpath("unicode_emoji.json")
    .read_text(encoding="utf-8")
)
NAMES_BY_CODE = CATALOG["workspace"]
CODE_BY_NAME = {name: code for code, names in NAMES_BY_CODE.items() for name in names}
CODE_BY_NAME.update(
    {
        "".join(chr(int(point, 16)) for point in code.split("-")): code
        for code in NAMES_BY_CODE
    }
)
ZULIP_NAME_BY_CODE = {code: names[0] for code, names in CATALOG["zulip"].items()}
ZULIP_CODE_BY_NAME = {
    name: code for code, names in CATALOG["zulip"].items() for name in names
}


def normalize_code(code: str) -> str:
    return "-".join(
        f"{int(point, 16):04x}" for point in code.split("-") if int(point, 16) != 0xFE0F
    )


def code_for_name(name: str) -> str | None:
    symbol = name.replace("\ufe0f", "")
    if symbol in CODE_BY_NAME:
        return CODE_BY_NAME[symbol]
    shortcode = re.sub(r"[\s-]+", "_", name.strip().lower().strip(":"))
    shortcode = re.sub(r"_+", "_", shortcode).strip("_")
    return CODE_BY_NAME.get(shortcode)


def normalize_name(name: str) -> str:
    code = code_for_name(name)
    return NAMES_BY_CODE[code][0] if code is not None else name


def name_for_code(code: str, name: str) -> str:
    code = normalize_code(code)
    names = NAMES_BY_CODE.get(code)
    if names is not None:
        return names[0]
    # Keep an unknown provider code distinct from known Workspace shortcodes.
    return "".join(chr(int(point, 16)) for point in code.split("-"))


def zulip_identity(
    name: str, observations: list[dict[str, typing.Any]]
) -> tuple[str, str] | None:
    code = code_for_name(name)
    for row in observations:
        if row["reaction_type"] == "unicode_emoji" and (
            (code is not None and normalize_code(row["emoji_code"]) == code)
            or name_for_code(row["emoji_code"], row["emoji_name"]) == name
        ):
            # Existing reactions retain the exact code and alias the server
            # supplied, including emoji introduced after our pinned catalog.
            return row["emoji_code"], row["emoji_name"]
    if code is not None and code in ZULIP_NAME_BY_CODE:
        return code, ZULIP_NAME_BY_CODE[code]
    return None
