import base64
import contextlib
import dataclasses
import datetime
import email
import email.header
import email.message
import email.policy
import email.utils
import imaplib
import re
import smtplib
import ssl
from typing import Any


IMAP_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+"(?P<delimiter>[^"]*)"\s+(?P<name>.+)$'
)
UID_FLAGS_RE = re.compile(rb"UID\s+(?P<uid>\d+).*FLAGS\s+\((?P<flags>[^)]*)\)")
APPENDUID_RE = re.compile(
    rb"(?:^|\[)APPENDUID\s+(?P<uid_validity>\d+)\s+(?P<uid>\d+)(?:\]|\s|$)",
    re.IGNORECASE,
)
APPENDUID_DATA_RE = re.compile(rb"^\s*(?P<uid_validity>\d+)\s+(?P<uid>\d+)\s*$")
SPECIAL_USE_FLAGS = {
    b"\\Inbox": "inbox",
    b"\\Sent": "sent",
    b"\\Drafts": "drafts",
    b"\\Trash": "trash",
    b"\\Junk": "junk",
}


@dataclasses.dataclass(frozen=True)
class MailAttachment:
    name: str
    content_type: str
    content_id: str | None
    data: bytes


@dataclasses.dataclass(frozen=True)
class ParsedMail:
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    bcc_addresses: list[str]
    reply_to: str | None
    subject: str
    body_html: str | None
    body_text: str | None
    message_id: str | None
    references: str | None
    sent_at: datetime.datetime
    attachments: list[MailAttachment]


@dataclasses.dataclass(frozen=True)
class FolderInfo:
    path: str
    delimiter: str
    special_use: str | None
    display_path: str


@dataclasses.dataclass(frozen=True)
class FolderMetadata:
    uid_validity: int | None
    uid_next: int | None
    highest_modseq: int | None


@dataclasses.dataclass(frozen=True)
class AppendUid:
    uid_validity: int
    uid: int


def parse_append_uid(values: Any, response_code: bool = False) -> AppendUid | None:
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


def decode_header(value: str | None) -> str:
    if value is None:
        return ""
    return str(email.header.make_header(email.header.decode_header(value)))


def message_addresses(message: email.message.Message, name: str) -> list[str]:
    return [
        email.utils.formataddr((display_name, address))
        for display_name, address in email.utils.getaddresses(message.get_all(name, []))
    ]


def message_date(message: email.message.Message) -> datetime.datetime:
    try:
        value = email.utils.parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        value = None
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def decode_imap_utf7(value: str) -> str:
    chunks = []
    position = 0
    while position < len(value):
        marker = value.find("&", position)
        if marker < 0:
            chunks.append(value[position:])
            break
        chunks.append(value[position:marker])
        end = value.find("-", marker)
        if end < 0:
            chunks.append(value[marker:])
            break
        encoded = value[marker + 1 : end]
        if not encoded:
            chunks.append("&")
        else:
            padding = "=" * (-len(encoded) % 4)
            decoded = base64.b64decode(encoded.replace(",", "/") + padding).decode(
                "utf-16-be"
            )
            chunks.append(decoded)
        position = end + 1
    return "".join(chunks)


def parse_list_row(row: bytes) -> FolderInfo:
    match = IMAP_LIST_RE.match(row)
    if match is None:
        raise ValueError("Unsupported IMAP LIST response")
    raw_name = match.group("name")
    if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
        raw_name = raw_name[1:-1]
    path = raw_name.decode("ascii")
    delimiter = match.group("delimiter").decode("ascii") or "/"
    flags = match.group("flags").split()
    special_use = next(
        (value for flag, value in SPECIAL_USE_FLAGS.items() if flag in flags),
        None,
    )
    return FolderInfo(path, delimiter, special_use, decode_imap_utf7(path))


def parse_message(raw_message: bytes) -> ParsedMail:
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    body_html = None
    body_text = None
    attachments = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        data = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or filename is not None:
            attachments.append(
                MailAttachment(
                    decode_header(filename) or "attachment",
                    content_type,
                    part.get("Content-ID"),
                    data,
                )
            )
            continue
        decoded = data.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/html" and body_html is None:
            body_html = decoded
        elif content_type == "text/plain" and body_text is None:
            body_text = decoded
    return ParsedMail(
        from_address=decode_header(message.get("From")),
        to_addresses=message_addresses(message, "To"),
        cc_addresses=message_addresses(message, "Cc"),
        bcc_addresses=message_addresses(message, "Bcc"),
        reply_to=decode_header(message.get("Reply-To")) or None,
        subject=decode_header(message.get("Subject")),
        body_html=body_html,
        body_text=body_text,
        message_id=message.get("Message-ID"),
        references=message.get("References"),
        sent_at=message_date(message),
        attachments=attachments,
    )


def build_message(
    payload: dict[str, Any], attachments: list[tuple[str, str, bytes]]
) -> email.message.EmailMessage:
    message = email.message.EmailMessage()
    message["From"] = payload["from_address"]
    message["To"] = ", ".join(payload["to_addresses"])
    if payload.get("cc_addresses"):
        message["Cc"] = ", ".join(payload["cc_addresses"])
    if payload.get("bcc_addresses"):
        message["Bcc"] = ", ".join(payload["bcc_addresses"])
    message["Subject"] = payload.get("subject", "")
    message["Message-ID"] = payload.get("message_id") or email.utils.make_msgid()
    if payload.get("reply_to"):
        message["In-Reply-To"] = payload["reply_to"]
    if payload.get("references"):
        message["References"] = payload["references"]
    message.set_content(payload.get("body_text", ""))
    if payload.get("body_html") is not None:
        message.add_alternative(payload["body_html"], subtype="html")
    for name, content_type, data in attachments:
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=name,
        )
    return message


class ImapClient:
    def __init__(self, settings: dict[str, Any], timeout: float = 30.0):
        self.settings = settings
        self.timeout = timeout
        self.connection = None

    def __enter__(self) -> "ImapClient":
        security = self.settings["imap_security"]
        if security == "tls":
            self.connection = imaplib.IMAP4_SSL(
                self.settings["imap_host"],
                self.settings["imap_port"],
                timeout=self.timeout,
            )
        else:
            self.connection = imaplib.IMAP4(
                self.settings["imap_host"],
                self.settings["imap_port"],
                timeout=self.timeout,
            )
            if security == "starttls":
                self.connection.starttls(ssl_context=ssl.create_default_context())
        credentials = self.settings["credentials"]
        self.connection.login(credentials["username"], credentials["password"])
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            self.connection.logout()

    def list_folders(self) -> list[FolderInfo]:
        status, rows = self.connection.list()
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to list mail folders")
        return [parse_list_row(row) for row in rows if row is not None]

    @staticmethod
    def _response_int(connection, name: str) -> int | None:
        _, values = connection.response(name)
        if not values or values[0] is None:
            return None
        match = re.search(rb"\d+", values[0])
        return int(match.group()) if match is not None else None

    def select(self, path: str, readonly: bool = True) -> FolderMetadata:
        status, _ = self.connection.select(path, readonly=readonly)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to select {path}")
        return FolderMetadata(
            self._response_int(self.connection, "UIDVALIDITY"),
            self._response_int(self.connection, "UIDNEXT"),
            self._response_int(self.connection, "HIGHESTMODSEQ"),
        )

    def fetch_since(
        self, path: str, external_uid: int
    ) -> list[tuple[int, frozenset[bytes], bytes]]:
        self.select(path)
        status, rows = self.connection.uid("search", None, f"UID {external_uid + 1}:*")
        if status != "OK" or not rows or not rows[0]:
            return []
        messages = []
        for uid_value in rows[0].split():
            status, response = self.connection.uid("fetch", uid_value, "(RFC822 FLAGS)")
            if status != "OK" or not response or not isinstance(response[0], tuple):
                continue
            metadata, raw_message = response[0]
            messages.append(
                (int(uid_value), frozenset(imaplib.ParseFlags(metadata)), raw_message)
            )
        return messages

    def fetch_uids(
        self, path: str, uids: list[int]
    ) -> list[tuple[int, frozenset[bytes], bytes]]:
        self.select(path)
        messages = []
        for uid in uids:
            status, response = self.connection.uid("fetch", str(uid), "(RFC822 FLAGS)")
            if status != "OK" or not response or not isinstance(response[0], tuple):
                continue
            metadata, raw_message = response[0]
            messages.append((uid, frozenset(imaplib.ParseFlags(metadata)), raw_message))
        return messages

    def fetch_flags(self, path: str, start_uid: int = 1) -> dict[int, frozenset[bytes]]:
        self.select(path)
        status, response = self.connection.uid(
            "fetch",
            f"{max(1, start_uid)}:*",
            "(UID FLAGS)",
        )
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to fetch flags for {path}")
        result = {}
        for item in response:
            metadata = item[0] if isinstance(item, tuple) else item
            if not isinstance(metadata, bytes):
                continue
            match = UID_FLAGS_RE.search(metadata)
            if match is not None:
                result[int(match.group("uid"))] = frozenset(
                    imaplib.ParseFlags(metadata)
                )
        return result

    def update_flags(
        self,
        path: str,
        uid: int,
        seen: bool,
        flagged: bool,
        deleted: bool,
    ) -> None:
        self.select(path, readonly=False)
        flags = []
        if seen:
            flags.append("\\Seen")
        if flagged:
            flags.append("\\Flagged")
        if deleted:
            flags.append("\\Deleted")
        status, _ = self.connection.uid(
            "store",
            str(uid),
            "FLAGS",
            "(" + " ".join(flags) + ")",
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to update message flags")
        if deleted:
            self.connection.expunge()

    def move(self, source_path: str, target_path: str, uid: int) -> None:
        self.select(source_path, readonly=False)
        status, _ = self.connection.uid("copy", str(uid), target_path)
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to copy message")
        self.connection.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        self.connection.expunge()

    def append(
        self, path: str, raw_message: bytes, flags: tuple[str, ...] = ()
    ) -> AppendUid | None:
        flag_value = "(" + " ".join(flags) + ")" if flags else None
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

    def create_folder(self, path: str) -> None:
        status, _ = self.connection.create(path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to create {path}")

    def rename_folder(self, old_path: str, new_path: str) -> None:
        status, _ = self.connection.rename(old_path, new_path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to rename {old_path}")

    def delete_folder(self, path: str) -> None:
        status, _ = self.connection.delete(path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to delete {path}")


class SmtpClient:
    def __init__(self, settings: dict[str, Any], timeout: float = 30.0):
        self.settings = settings
        self.timeout = timeout

    def send(self, message: email.message.EmailMessage) -> None:
        security = self.settings["smtp_security"]
        if security == "tls":
            connection = smtplib.SMTP_SSL(
                self.settings["smtp_host"],
                self.settings["smtp_port"],
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = smtplib.SMTP(
                self.settings["smtp_host"],
                self.settings["smtp_port"],
                timeout=self.timeout,
            )
            if security == "starttls":
                connection.starttls(context=ssl.create_default_context())
        try:
            credentials = self.settings["credentials"]
            connection.login(credentials["username"], credentials["password"])
            connection.send_message(message)
        finally:
            connection.quit()
