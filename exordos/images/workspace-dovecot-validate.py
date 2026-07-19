#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import re
import sys


EXPECTED_INDEX_PATH = (
    "/run/workspace/dovecot-indexes/%{user | domain}/%{user | username}"
)
INDEX_SETTING_RE = re.compile(r"^(?P<indent>\s*)mail_index_path\s*=\s*(?P<path>.*)$")
CONTROL_SETTING_RE = re.compile(r"^\s*mail_control_path\s*=\s*(?P<path>.*)$")
DERIVED_INDEX_SETTING_RE = re.compile(
    r"^\s*(?P<name>mail_cache_path|mail_index_private_path)\s*=\s*(?P<path>.*)$"
)


def main():
    matches = []
    control_paths = []
    derived_index_paths = []
    for line_number, line in enumerate(sys.stdin, 1):
        effective_line = line.rstrip("\n")
        match = INDEX_SETTING_RE.fullmatch(effective_line)
        if match is not None:
            matches.append((line_number, match.group("indent"), match.group("path")))
        control_match = CONTROL_SETTING_RE.fullmatch(effective_line)
        if control_match is not None and control_match.group("path").strip():
            control_paths.append((line_number, control_match.group("path")))
        derived_index_match = DERIVED_INDEX_SETTING_RE.fullmatch(effective_line)
        if (
            derived_index_match is not None
            and derived_index_match.group("path").strip()
        ):
            derived_index_paths.append(
                (
                    line_number,
                    derived_index_match.group("name"),
                    derived_index_match.group("path"),
                )
            )

    if control_paths:
        line_number, path = control_paths[0]
        print(
            "Effective Dovecot mail_control_path must remain unset so Maildir "
            f"control state stays persistent; got line {line_number}: {path}",
            file=sys.stderr,
        )
        return 1

    if derived_index_paths:
        line_number, name, path = derived_index_paths[0]
        print(
            f"Effective Dovecot {name} must remain unset so it inherits the "
            f"runtime index path; got line {line_number}: {path}",
            file=sys.stderr,
        )
        return 1

    if len(matches) != 1:
        print(
            "Effective Dovecot configuration must contain exactly one "
            "mail_index_path setting",
            file=sys.stderr,
        )
        return 1

    line_number, indent, path = matches[0]
    if indent or path != EXPECTED_INDEX_PATH:
        print(
            "Effective Dovecot mail_index_path must be the global runtime path "
            f"{EXPECTED_INDEX_PATH}; got line {line_number}: {path}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
