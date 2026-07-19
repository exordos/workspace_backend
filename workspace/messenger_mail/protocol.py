# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import dataclasses
import email.message
import imaplib
import re
import smtplib
import ssl
import typing


APPENDUID_RE = re.compile(
    rb"(?:^|\[)APPENDUID\s+(?P<uid_validity>\d+)\s+(?P<uid>\d+)(?:\]|\s|$)",
    re.IGNORECASE,
)
APPENDUID_DATA_RE = re.compile(rb"^\s*(?P<uid_validity>\d+)\s+(?P<uid>\d+)\s*$")
UID_RE = re.compile(rb"(?:^|[\s(])UID\s+(?P<uid>\d+)(?:\s|$)")
IMAP_FLAG_RE = re.compile(r"^[\\$A-Za-z0-9._-]+$")
MESSAGE_ID_RE = re.compile(r"^<[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+>$")
SECURITY_MODES = frozenset({"plain", "starttls", "tls"})
STORE_MODES = {"replace": "FLAGS", "add": "+FLAGS", "remove": "-FLAGS"}


@dataclasses.dataclass(frozen=True)
class Credentials:
    username: str
    password: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class ImapSettings:
    host: str
    port: int
    credentials: Credentials
    security: str = "plain"
    ca_file: str | None = None
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.security not in SECURITY_MODES:
            raise ValueError("Unsupported IMAP security mode")
        if self.timeout <= 0:
            raise ValueError("IMAP timeout must be positive")


@dataclasses.dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    security: str = "plain"
    credentials: Credentials | None = None
    ca_file: str | None = None
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.security not in SECURITY_MODES:
            raise ValueError("Unsupported SMTP security mode")
        if self.timeout <= 0:
            raise ValueError("SMTP timeout must be positive")


@dataclasses.dataclass(frozen=True)
class MailboxMetadata:
    uid_validity: int | None
    uid_next: int | None
    highest_modseq: int | None


@dataclasses.dataclass(frozen=True)
class AppendUid:
    uid_validity: int
    uid: int


@dataclasses.dataclass(frozen=True)
class FetchedMessage:
    uid: int
    flags: frozenset[str]
    raw_message: bytes


def parse_append_uid(
    values: typing.Iterable[object] | None,
    response_code: bool = False,
) -> AppendUid | None:
    pattern = APPENDUID_DATA_RE if response_code else APPENDUID_RE
    for value in values or ():
        if isinstance(value, str):
            value = value.encode("ascii", errors="ignore")
        if not isinstance(value, bytes):
            continue
        match = pattern.fullmatch(value) if response_code else pattern.search(value)
        if match is None:
            continue
        uid_validity = int(match.group("uid_validity"))
        uid = int(match.group("uid"))
        if uid_validity > 0 and uid > 0:
            return AppendUid(uid_validity, uid)
    return None


def _response_int(connection: typing.Any, name: str) -> int | None:
    _, values = connection.response(name)
    if not values or values[0] is None:
        return None
    value = values[0]
    if isinstance(value, str):
        value = value.encode("ascii", errors="ignore")
    match = re.search(rb"\d+", value)
    return int(match.group()) if match is not None else None


def _flags(metadata: bytes) -> frozenset[str]:
    return frozenset(value.decode("ascii") for value in imaplib.ParseFlags(metadata))


def _has_response_code(values: typing.Iterable[object] | None, code: str) -> bool:
    marker = f"[{code}]".encode("ascii")
    for value in values or ():
        if isinstance(value, str):
            value = value.encode("ascii", errors="ignore")
        if isinstance(value, bytes) and marker in value.upper():
            return True
    return False


class ImapClient:
    def __init__(self, settings: ImapSettings):
        self.settings = settings
        self.connection: typing.Any = None

    def __enter__(self) -> "ImapClient":
        ssl_context = ssl.create_default_context(cafile=self.settings.ca_file)
        if self.settings.security == "tls":
            self.connection = imaplib.IMAP4_SSL(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout,
                ssl_context=ssl_context,
            )
        else:
            self.connection = imaplib.IMAP4(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout,
            )
            if self.settings.security == "starttls":
                self.connection.starttls(ssl_context=ssl_context)
        self.connection.login(
            self.settings.credentials.username,
            self.settings.credentials.password,
        )
        return self

    def __exit__(
        self,
        exc_type: typing.Any,
        exc: typing.Any,
        traceback: typing.Any,
    ) -> None:
        if self.connection is not None:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                self.connection.logout()
            self.connection = None

    def select(self, path: str, readonly: bool = True) -> MailboxMetadata:
        status, _ = self.connection.select(path, readonly=readonly)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to select {path}")
        return MailboxMetadata(
            _response_int(self.connection, "UIDVALIDITY"),
            _response_int(self.connection, "UIDNEXT"),
            _response_int(self.connection, "HIGHESTMODSEQ"),
        )

    def search(self, criteria: str = "ALL") -> list[int]:
        status, rows = self.connection.uid("search", None, criteria)
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to search mailbox")
        if not rows or not rows[0]:
            return []
        return [int(value) for value in rows[0].split()]

    def fetch(self, uids: list[int]) -> list[FetchedMessage]:
        result = []
        for uid in uids:
            status, rows = self.connection.uid("fetch", str(uid), "(UID FLAGS RFC822)")
            if status != "OK":
                raise imaplib.IMAP4.error(f"Unable to fetch message {uid}")
            for row in rows or ():
                if not isinstance(row, tuple) or not isinstance(row[0], bytes):
                    continue
                metadata, raw_message = row
                match = UID_RE.search(metadata)
                if match is None or not isinstance(raw_message, bytes):
                    continue
                result.append(
                    FetchedMessage(
                        uid=int(match.group("uid")),
                        flags=_flags(metadata),
                        raw_message=raw_message,
                    )
                )
                break
        return result

    def append(
        self,
        path: str,
        raw_message: bytes,
        flags: tuple[str, ...] = (),
        keywords: tuple[str, ...] = (),
    ) -> AppendUid | None:
        flag_value = _flag_list(flags, keywords) if flags or keywords else None
        status, values = self.connection.append(path, flag_value, None, raw_message)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to append message to {path}")
        append_uid = parse_append_uid(values)
        if append_uid is not None:
            return append_uid
        response_name, response_values = self.connection.response("APPENDUID")
        if response_name != "APPENDUID":
            return None
        return parse_append_uid(response_values, response_code=True)

    def create_mailbox(self, path: str, exist_ok: bool = False) -> bool:
        status, values = self.connection.create(path)
        if status == "OK":
            return True
        if exist_ok and _has_response_code(values, "ALREADYEXISTS"):
            return False
        raise imaplib.IMAP4.error(f"Unable to create {path}")

    def ensure_mailbox(self, path: str) -> bool:
        status, values = self.connection.select(path, readonly=True)
        if status == "OK":
            return False
        can_create = _has_response_code(values, "TRYCREATE") or _has_response_code(
            values, "NONEXISTENT"
        )
        if not can_create:
            raise imaplib.IMAP4.error(f"Unable to select {path}")
        return self.create_mailbox(path, exist_ok=True)

    def store_flags(
        self,
        uid: int,
        flags: tuple[str, ...] = (),
        keywords: tuple[str, ...] = (),
        mode: str = "replace",
    ) -> None:
        operation = STORE_MODES[mode]
        status, _ = self.connection.uid(
            "store",
            str(uid),
            operation,
            _flag_list(flags, keywords),
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to store message flags")

    def expunge_uids(self, uids: list[int]) -> None:
        if not uids:
            return
        uid_set = ",".join(str(uid) for uid in sorted(set(uids)))
        status, _ = self.connection.uid("expunge", uid_set)
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to UID EXPUNGE messages")

    def delete_uids(self, path: str, uids: list[int]) -> None:
        if not uids:
            return
        self.select(path, readonly=False)
        for uid in uids:
            self.store_flags(uid, flags=("\\Deleted",), mode="add")
        self.expunge_uids(uids)

    def delete_by_message_id(self, path: str, message_id: str) -> list[int]:
        if MESSAGE_ID_RE.fullmatch(message_id) is None:
            raise ValueError("Invalid Message-ID")
        self.select(path, readonly=False)
        uids = self.search(f'HEADER Message-ID "{message_id}"')
        self.delete_uids(path, uids)
        return uids


def _flag_list(flags: tuple[str, ...], keywords: tuple[str, ...]) -> str:
    values = flags + keywords
    if any(IMAP_FLAG_RE.fullmatch(value) is None for value in values):
        raise ValueError("Invalid IMAP flag or keyword")
    return "(" + " ".join(values) + ")"


class SmtpClient:
    def __init__(self, settings: SmtpSettings):
        self.settings = settings
        self.connection: typing.Any = None

    def __enter__(self) -> "SmtpClient":
        ssl_context = ssl.create_default_context(cafile=self.settings.ca_file)
        if self.settings.security == "tls":
            self.connection = smtplib.SMTP_SSL(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout,
                context=ssl_context,
            )
        else:
            self.connection = smtplib.SMTP(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout,
            )
            if self.settings.security == "starttls":
                self.connection.starttls(context=ssl_context)
        if self.settings.credentials is not None:
            self.connection.login(
                self.settings.credentials.username,
                self.settings.credentials.password,
            )
        return self

    def __exit__(
        self,
        exc_type: typing.Any,
        exc: typing.Any,
        traceback: typing.Any,
    ) -> None:
        if self.connection is not None:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                self.connection.quit()
            self.connection = None

    def send(self, message: email.message.EmailMessage) -> None:
        if self.connection is None:
            with self:
                connection: typing.Any = self.connection
                connection.send_message(message)
            return
        self.connection.send_message(message)
