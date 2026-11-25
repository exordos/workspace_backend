# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import base64
import collections.abc
import contextlib
import dataclasses
import email.message
import imaplib
import re
import smtplib
import socket
import ssl
import typing

from workspace.services import groupware_network


_LIST_RESPONSE = re.compile(rb'^\(([^)]*)\) (?:NIL|"(?:[^"\\]|\\.)*") (.*)$')
_UTF7_SHIFT = re.compile(rb"&([A-Za-z0-9+,]*)-")
_FETCH_UID = re.compile(rb"\bUID (\d+)\b", re.IGNORECASE)
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_MAIL_FOLDERS = 512
_MAX_RESPONSE_BYTES = MAX_MESSAGE_BYTES + 1024 * 1024
_MAX_RESPONSE_LINES = 4096
_MAX_SESSION_BYTES = 128 * 1024 * 1024
_MAX_SESSION_LINES = 65536
_FOLDER_KINDS = {
    b"\\archive": "archive",
    b"\\drafts": "drafts",
    b"\\junk": "spam",
    b"\\sent": "sent",
    b"\\trash": "trash",
}


@dataclasses.dataclass(frozen=True)
class Folder:
    mailbox: bytes
    name: str
    kind: str


@dataclasses.dataclass(frozen=True)
class Message:
    uid: int
    content: bytes = dataclasses.field(repr=False)
    read: bool
    starred: bool


class MessageTooLarge(Exception):
    def __init__(self, downloaded_bytes: int) -> None:
        self.downloaded_bytes = downloaded_bytes
        super().__init__("Mail message exceeds the download limit")


def _quote(value: bytes) -> str:
    if b"\r" in value or b"\n" in value or b"\x00" in value:
        raise ValueError("Invalid IMAP string")
    escaped = value.decode("ascii").replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _decode_name(mailbox: bytes) -> str:
    def decode_shift(match: re.Match[bytes]) -> bytes:
        encoded = match[1].replace(b",", b"/")
        if not encoded:
            return b"&"
        encoded += b"=" * (-len(encoded) % 4)
        return base64.b64decode(encoded, validate=True).decode("utf-16-be").encode()

    return _UTF7_SHIFT.sub(decode_shift, mailbox).decode()


def _parse_folder(row: bytes | tuple[bytes, bytes]) -> Folder | None:
    header, literal = row if isinstance(row, tuple) else (row, None)
    match = _LIST_RESPONSE.fullmatch(header)
    if match is None:
        raise imaplib.IMAP4.error("Invalid mailbox listing")
    flags = set(match[1].lower().split())
    if b"\\noselect" in flags or b"\\nonexistent" in flags:
        return None

    mailbox = match[2] if literal is None else literal
    if literal is None and mailbox.startswith(b'"') and mailbox.endswith(b'"'):
        mailbox = re.sub(rb"\\([\\\"])", rb"\1", mailbox[1:-1])
    if mailbox.upper() == b"INBOX":
        return Folder(mailbox, "INBOX", "inbox")
    kind = next(
        (kind for flag, kind in _FOLDER_KINDS.items() if flag in flags), "custom"
    )
    return Folder(mailbox, _decode_name(mailbox), kind)


class _IMAPConnection(imaplib.IMAP4):
    def __init__(
        self, endpoint: groupware_network.Endpoint, context: ssl.SSLContext
    ) -> None:
        self._endpoint = endpoint
        self._context = context
        self._remaining_bytes = _MAX_RESPONSE_BYTES
        self._remaining_lines = _MAX_RESPONSE_LINES
        self._remaining_session_bytes = _MAX_SESSION_BYTES
        self._remaining_session_lines = _MAX_SESSION_LINES
        super().__init__(
            endpoint.host, endpoint.port, timeout=groupware_network.TIMEOUT
        )

    def _create_socket(self, timeout: float | None) -> socket.socket:
        return self._endpoint.connect(timeout, self._context)

    def _command(self, name: str, *args: typing.Any) -> bytes:
        self._remaining_bytes = _MAX_RESPONSE_BYTES
        self._remaining_lines = _MAX_RESPONSE_LINES
        return super()._command(name, *args)  # type: ignore[misc]

    def _check_response_budget(self, size: int) -> None:
        if (
            size > min(self._remaining_bytes, self._remaining_session_bytes)
            or min(self._remaining_lines, self._remaining_session_lines) < 0
        ):
            self.state = "LOGOUT"
            with contextlib.suppress(OSError):
                self.shutdown()
            raise self.abort("IMAP response exceeds the configured limit")
        self._remaining_bytes -= size
        self._remaining_session_bytes -= size

    def read(self, size: int) -> bytes:
        self._check_response_budget(size)
        return super().read(size)

    def readline(self) -> bytes:
        self._remaining_lines -= 1
        self._remaining_session_lines -= 1
        line = super().readline()
        self._check_response_budget(len(line))
        return line


@contextlib.contextmanager
def open_imap(
    endpoint: groupware_network.Endpoint, login: str, password: str
) -> collections.abc.Iterator["Mailbox"]:
    quoted_login = _quote(login.encode("ascii"))
    if not password.isascii() or any(char in password for char in "\r\n\x00"):
        raise ValueError("Invalid IMAP password")
    context = ssl.create_default_context()
    with _IMAPConnection(endpoint, context) as client:
        if endpoint.tls == "starttls":
            client.starttls(context)
        client.login(quoted_login, password)
        yield Mailbox(client)


class Mailbox:
    def __init__(self, client: imaplib.IMAP4) -> None:
        self._client = client

    def folders(self) -> list[Folder]:
        status, rows = self._client.list()
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to list mail folders")
        if len(rows) > MAX_MAIL_FOLDERS:
            raise imaplib.IMAP4.error("IMAP folder count exceeds the configured limit")
        folders = []
        for row in rows:
            if row:
                folder = _parse_folder(row)
                if folder is not None:
                    folders.append(folder)
        return folders

    def select(self, folder: Folder) -> int:
        status, _ = self._client.select(_quote(folder.mailbox), readonly=True)
        _, values = self._client.response("UIDVALIDITY")
        if status != "OK" or not values or not values[0]:
            raise imaplib.IMAP4.error("Unable to select mail folder")
        return int(values[0])

    def uids(self, first_uid: int) -> list[int]:
        if first_uid < 1:
            raise ValueError("IMAP UIDs start at 1")
        status, rows = self._client.uid("SEARCH", "UID", f"{first_uid}:*")
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to find mail messages")
        # The wildcard can include the last existing UID even below first_uid.
        return sorted(
            uid for uid in map(int, (rows[0] or b"").split()) if uid >= first_uid
        )

    def fetch(self, uid: int) -> Message | None:
        status, rows = self._client.uid(
            "FETCH", str(uid), f"(UID FLAGS BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)"
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to fetch mail message")
        for index, row in enumerate(rows):
            if isinstance(row, tuple):
                header, content = row
                # FLAGS and UID can be present after the message literal.
                if index + 1 < len(rows) and isinstance(rows[index + 1], bytes):
                    header += b" " + rows[index + 1]
                identity = _FETCH_UID.search(header)
                if identity is None or int(identity[1]) != uid:
                    raise imaplib.IMAP4.error("Unexpected mail message UID")
                if len(content) > MAX_MESSAGE_BYTES:
                    raise MessageTooLarge(len(content))
                flags = tuple(flag.lower() for flag in imaplib.ParseFlags(header))
                return Message(uid, content, b"\\seen" in flags, b"\\flagged" in flags)
        return None


class _SMTPConnection(smtplib.SMTP):
    def __init__(
        self, endpoint: groupware_network.Endpoint, context: ssl.SSLContext
    ) -> None:
        self._endpoint = endpoint
        self._context = context
        super().__init__(
            endpoint.host, endpoint.port, timeout=groupware_network.TIMEOUT
        )

    def _get_socket(self, host: str, port: int, timeout: float | None) -> socket.socket:
        if (host, port) != (self._endpoint.host, self._endpoint.port):
            raise ValueError("Unexpected SMTP endpoint")
        return self._endpoint.connect(timeout, self._context)


@contextlib.contextmanager
def open_smtp(
    endpoint: groupware_network.Endpoint, login: str, password: str
) -> collections.abc.Iterator[smtplib.SMTP]:
    context = ssl.create_default_context()
    client = _SMTPConnection(endpoint, context)
    try:
        if endpoint.tls == "starttls":
            client.starttls(context=context)
        client.login(login, password)
        yield client
    finally:
        # Closing a connection cannot change acceptance of an earlier DATA command.
        with contextlib.suppress(OSError):
            client.close()


def send(
    client: smtplib.SMTP,
    outgoing: email.message.EmailMessage,
    sender: str,
    recipients: collections.abc.Sequence[str],
) -> frozenset[str]:
    """Return refused recipients after accepting mail for all remaining recipients."""
    return frozenset(client.send_message(outgoing, sender, recipients))
