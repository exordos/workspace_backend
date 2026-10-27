# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import base64
import contextlib
import urllib.parse
from unittest import mock

import psycopg
import pytest

from workspace.bridges.zulip import message_conversion
from workspace.services import zulip_history
from workspace.messenger_api.dm import models


def temporary_url(path, filename=None):
    signed = base64.b16encode(f"{path}:1:example-signature".encode()).decode()
    filename = filename or path.rsplit("/", 1)[-1]
    return f"/user_uploads/temporary/{signed}/{urllib.parse.quote(filename)}"


@pytest.mark.parametrize("name", ["file.pdf", "отчёт 100%.pdf", "a:b.pdf"])
def test_temporary_upload_uses_its_stable_authenticated_path(name):
    path = "1/ab/file-key/" + name
    assert message_conversion.zulip_upload_path(temporary_url(path)) == (
        "/user_uploads/" + urllib.parse.quote(path, safe="/")
    )


@pytest.mark.parametrize(
    "target",
    [
        "/user_uploads/temporary/not-hex/file.pdf",
        "/user_uploads/temporary/FF/file.pdf",
        "/user_uploads/temporary/00/file.pdf",
        temporary_url("https://foreign.example/file.pdf"),
        temporary_url("/1/key/file.pdf"),
        temporary_url("1/../file.pdf"),
        temporary_url("1/%2e%2e/file.pdf"),
        temporary_url("1/key/../../file.pdf"),
        temporary_url("1/key/file.pdf", "different.pdf"),
        temporary_url("1/key/file\x00.pdf"),
        temporary_url("1/key/file\\name.pdf"),
    ],
)
def test_invalid_temporary_upload_is_not_used_as_an_authenticated_path(target):
    assert message_conversion.zulip_upload_path(target) is None


def test_regular_upload_path_keeps_its_identity():
    path = "/user_uploads/1/key/file%20name.pdf"
    assert message_conversion.zulip_upload_path(path) == path


@pytest.mark.parametrize(
    "error,permanent",
    [
        (psycopg.errors.StringDataRightTruncation("too long"), True),
        (psycopg.errors.LockNotAvailable("lock timeout"), False),
        (psycopg.errors.SerializationFailure("concurrent change"), False),
    ],
)
def test_history_does_not_retry_a_permanent_length_failure(error, permanent):
    account = dict.fromkeys(
        ("project_id", "provider_uuid", "owner_user_uuid", "external_account_id"),
        "identity",
    )
    account["settings"] = {"server_url": "https://zulip.example.com"}
    intent = {
        "id": 1,
        "identity": zulip_history.identity(account),
        "payload": {"type": "message"},
        "expected": None,
        "fetched_at": None,
        "attempts": 20,
    }

    @contextlib.asynccontextmanager
    async def transaction():
        yield mock.AsyncMock()

    async def run():
        service = zulip_history.ZulipHistoryService("postgresql://unused")
        service.db = mock.Mock(transaction=transaction, sql=mock.AsyncMock())
        service.activity = mock.AsyncMock(return_value={"pending": False})
        service.prepare = mock.AsyncMock(side_effect=error)
        await service.process(account, intent)
        service.db.sql.assert_awaited_once_with(
            zulip_history.RETRY_INTENT_SQL, (str(error), permanent, 256, 1)
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,valid",
    [
        ("x" * 2048, True),
        ("x" * 2049, False),
        ("я" * 1024, True),
        ("я" * 1025, False),
        ("😀" * 512, True),
        ("😀" * 513, False),
    ],
    ids=[
        "ascii-boundary",
        "ascii-overflow",
        "cyrillic-boundary",
        "cyrillic-overflow",
        "emoji-boundary",
        "emoji-overflow",
    ],
)
def test_file_identity_is_bounded_by_utf8_bytes(value, valid):
    identity_type = models.WorkspaceFile.properties.properties[
        "provider_identity"
    ].get_property_type()
    assert identity_type.validate(value) is valid
