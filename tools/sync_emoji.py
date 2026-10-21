# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Regenerate Unicode identities from Zulip's public catalog and Workspace UI data."""

import argparse
import collections
import json
import pathlib
import re
import urllib.request


def normalize_code(code):
    return "-".join(
        f"{int(point, 16):04x}" for point in code.split("-") if int(point, 16) != 0xFE0F
    )


def normalize_shortcode(name):
    name = re.sub(r"[\s-]+", "_", name.strip().lower().strip(":"))
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_+]", "", name)).strip("_")


def overrides(text, name):
    body = re.search(rf"export const {name}[^=]*= \{{(.*?)\}};", text, re.S)[1]
    return dict(re.findall(r'["\']?([a-zA-Z0-9_+\-]+)["\']?\s*:\s*"([^"]+)"', body))


def generate(zulip, dataset, ui_overrides, previous):
    aliases = collections.defaultdict(set)
    canonical = {}
    for code, names in dataset.items():
        code = normalize_code(code)
        names = [
            normalize_shortcode(name)
            for name in ([names] if isinstance(names, str) else names)
        ]
        canonical.setdefault(
            code,
            next(
                (
                    name
                    for name in names
                    if not name.startswith("face_with_") and not name.endswith("_face")
                ),
                names[0],
            ),
        )
        for name in names:
            aliases[name].add(code)
    for code, names in zulip.items():
        canonical.setdefault(code, names[0])
        for name in names:
            aliases[name].add(code)
    for name, code in overrides(
        ui_overrides, "ZULIP_SHORTCODE_TO_UNIFIED_OVERRIDES"
    ).items():
        aliases[normalize_shortcode(name)] = {normalize_code(code)}
    for code, name in overrides(
        ui_overrides, "ZULIP_CANONICAL_SHORTCODE_BY_UNIFIED_OVERRIDES"
    ).items():
        canonical[normalize_code(code)] = normalize_shortcode(name)
    # Existing Workspace identities win over provider names with another meaning.
    for code, names in previous.items():
        canonical[code] = names[0]
        for name in names:
            aliases[name] = {code}
    for code, name in canonical.items():
        aliases[name] = {code}
    assert len(set(canonical.values())) == len(canonical), "Duplicate canonical names"
    by_code = collections.defaultdict(list)
    for name, codes in aliases.items():
        if len(codes) == 1:
            by_code[next(iter(codes))].append(name)
    return {
        code: [name, *sorted(set(by_code[code]) - {name})]
        for code, name in sorted(canonical.items())
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--zulip-data", required=True, help="emoji_api.json path or URL"
    )
    parser.add_argument("--zulip-version", required=True)
    parser.add_argument(
        "--ui", required=True, type=pathlib.Path, help="Workspace UI checkout"
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("workspace/common/unicode_emoji.json"),
    )
    args = parser.parse_args()
    if args.zulip_data.startswith(("https://", "http://")):
        with urllib.request.urlopen(args.zulip_data, timeout=30) as response:
            source = json.load(response)
    else:
        source = json.loads(pathlib.Path(args.zulip_data).read_text())
    zulip = {
        normalize_code(code): names for code, names in source["code_to_names"].items()
    }
    emojibase = args.ui / "node_modules/emojibase-data"
    dataset = json.loads((emojibase / "en/shortcodes/emojibase.json").read_text())
    ui_overrides = (
        args.ui / "packages/web/src/shared/lib/emoji-shortcodes-overrides.data.ts"
    ).read_text()
    previous = json.loads(args.output.read_text())
    if "workspace" in previous:
        previous = previous["workspace"]
    else:
        previous = {code: [names[0], *names[2:]] for code, names in previous.items()}
    result = {
        "zulip_version": args.zulip_version,
        "emojibase_version": json.loads((emojibase / "package.json").read_text())[
            "version"
        ],
        "zulip": dict(sorted(zulip.items())),
        "workspace": generate(zulip, dataset, ui_overrides, previous),
    }
    # Keep each entry on one line so catalog updates remain reviewable.
    text = json.dumps(result, ensure_ascii=False, indent=2)
    text = re.sub(
        r"\[\n\s+((?:\"[^\"\n]*\",?\n\s*)+)\]",
        lambda match: "[" + ", ".join(re.findall(r'"[^"\n]*"', match[1])) + "]",
        text,
    )
    args.output.write_text(text + "\n")
    print(
        f"Zulip {args.zulip_version}: {len(zulip)} codes; Workspace: {len(result['workspace'])} codes"
    )


if __name__ == "__main__":
    main()
