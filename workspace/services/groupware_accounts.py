# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import dataclasses
import typing

from restalchemy.common import exceptions

from workspace.services import groupware_calendar
from workspace.services import groupware_credentials
from workspace.services import groupware_network


PROVIDERS: dict[str, groupware_credentials.Service] = {
    "imap_smtp": "mail",
    "caldav": "calendar",
}


def is_groupware(provider: object) -> bool:
    return isinstance(provider, str) and provider in PROVIDERS


def _string(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise exceptions.ValidationErrorException()
    return value


def endpoint(settings: object) -> groupware_network.Endpoint:
    if (
        not isinstance(settings, dict)
        or settings.keys() - {"host", "port", "tls"}
        or type(settings.get("port")) is not int
    ):
        raise exceptions.ValidationErrorException()
    try:
        return groupware_network.Endpoint(
            _string(settings.get("host"), 253),
            settings["port"],
            settings.get("tls", "implicit"),
        )
    except groupware_network.EndpointError as error:
        raise exceptions.ValidationErrorException() from error


def normalize(
    provider: str, settings: dict[str, typing.Any], *, require_password: bool
) -> tuple[dict[str, typing.Any], str | None]:
    if not isinstance(settings, dict):
        raise exceptions.ValidationErrorException()
    working = settings.copy()
    password = None
    if "password" in working:
        password = _string(working.pop("password"), 1024)
    if require_password and password is None:
        raise exceptions.ValidationErrorException()

    allowed = (
        {"email", "username", "imap", "smtp", "label"}
        if provider == "imap_smtp"
        else {"url", "username", "label"}
    )
    if working.keys() - allowed:
        raise exceptions.ValidationErrorException()
    if "label" in working:
        working["label"] = _string(working["label"], 256)

    if provider == "imap_smtp":
        address = _string(working.get("email"), 320)
        local, separator, domain = address.rpartition("@")
        if (
            not (local and separator and domain)
            or not address.isascii()
            or any(char.isspace() or char in "<>,;\\@" for char in local + domain)
        ):
            raise exceptions.ValidationErrorException()
        working["email"] = address
        working["username"] = _string(working.get("username", address), 512)
        if not working["username"].isascii() or (
            password is not None and not password.isascii()
        ):
            raise exceptions.ValidationErrorException()
        working["imap"] = dataclasses.asdict(endpoint(working.get("imap")))
        working["smtp"] = dataclasses.asdict(endpoint(working.get("smtp")))
    else:
        working["username"] = _string(working.get("username"), 512)
        working["url"] = _string(working.get("url"), 2048)
        try:
            groupware_calendar.validate_url(working["url"])
            working["username"].encode("latin-1")
            if password is not None:
                password.encode("latin-1")
        except ValueError as error:
            raise exceptions.ValidationErrorException() from error
    return working, password


def public_settings(
    provider: object, settings: dict[str, typing.Any]
) -> dict[str, typing.Any]:
    allowed = (
        {"email", "label", "username", "imap", "smtp"}
        if provider == "imap_smtp"
        else {"url", "username", "label"}
    )
    result = {name: value for name, value in settings.items() if name in allowed}
    for name in ("imap", "smtp"):
        if isinstance(result.get(name), dict):
            result[name] = {
                key: value
                for key, value in result[name].items()
                if key in {"host", "port", "tls"}
            }
    return result
