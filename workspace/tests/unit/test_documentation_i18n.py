# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import pathlib
import re


PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DOCS_ROOT = PROJECT_ROOT / "docs"
LANGUAGES = ("en", "ru", "de", "zh")
DOCUMENT_SUFFIXES = {".md", ".puml", ".svg"}
FENCE_RE = re.compile(r"(?ms)^(```|~~~).*?^\1[ \t]*$")
INLINE_CODE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+")
TABLE_ROW_RE = re.compile(r"(?m)^\|.*\|[ \t]*$")


def _language_artifacts(language):
    root = DOCS_ROOT / language
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix in DOCUMENT_SUFFIXES
    }


def _markdown_contract(path):
    text = path.read_text()
    return {
        "headings": HEADING_RE.findall(text),
        "fences": FENCE_RE.findall(text),
        "fenced_blocks": [match.group(0) for match in FENCE_RE.finditer(text)],
        "links": LINK_RE.findall(text),
        "table_rows": len(TABLE_ROW_RE.findall(text)),
        "table_columns": [line.count("|") for line in TABLE_ROW_RE.findall(text)],
    }


def test_documentation_language_trees_have_identical_artifacts():
    english = _language_artifacts("en")

    assert english
    for language in LANGUAGES[1:]:
        assert _language_artifacts(language) == english


def test_markdown_structure_and_machine_values_are_identical():
    markdown_files = sorted((DOCS_ROOT / "en").rglob("*.md"))

    for english_path in markdown_files:
        relative = english_path.relative_to(DOCS_ROOT / "en")
        expected = _markdown_contract(english_path)
        machine_values = set(INLINE_CODE_RE.findall(english_path.read_text()))
        for language in LANGUAGES[1:]:
            translated_path = DOCS_ROOT / language / relative
            assert _markdown_contract(translated_path) == expected
            assert machine_values <= set(INLINE_CODE_RE.findall(translated_path.read_text()))


def test_each_plantuml_source_has_a_rendered_svg():
    for language in LANGUAGES:
        for source in (DOCS_ROOT / language).rglob("*.puml"):
            assert source.with_suffix(".svg").is_file()


def test_machine_readable_contracts_have_one_canonical_copy():
    expected = {
        "workspace_provider_api_v1.yaml",
        "workspace_provider_api_v2.yaml",
        "zulip_bridge_control_api_v1.yaml",
        "zulip_bridge_file_api_v1.yaml",
    }

    assert {path.name for path in DOCS_ROOT.glob("*.yaml")} == expected
    for language in LANGUAGES:
        assert not list((DOCS_ROOT / language).rglob("*.yaml"))


def test_local_documentation_links_resolve():
    failures = []
    for markdown in DOCS_ROOT.rglob("*.md"):
        for destination in LINK_RE.findall(markdown.read_text()):
            if destination.startswith(("http://", "https://", "mailto:", "urn:", "#", "/")):
                continue
            path = destination.split("#", 1)[0]
            if path and not (markdown.parent / path).resolve().exists():
                failures.append(f"{markdown.relative_to(PROJECT_ROOT)}: {destination}")

    assert not failures
