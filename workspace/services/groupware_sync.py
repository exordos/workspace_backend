# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import collections.abc
import contextlib
import dataclasses
import datetime
import hmac
import imaplib
import logging
import smtplib
import typing

import requests
from cryptography import exceptions as crypto_exceptions
from gcl_looper.services import basic
from oslo_config import cfg
from restalchemy.dm import filters
from restalchemy.storage.sql import engines

from workspace.common import groupware_opts
from workspace.messenger_api import events
from workspace.messenger_api.dm import external_models
from workspace.services import groupware_accounts
from workspace.services import groupware_calendar
from workspace.services import groupware_credentials
from workspace.services import groupware_delivery
from workspace.services import groupware_mail
from workspace.services import groupware_projection


LOG = logging.getLogger(__name__)
MAIL_BATCH_PER_FOLDER = 25
MAIL_BYTES_PER_SYNC = 64 * 1024 * 1024
CALENDAR_SYNC_SPAN = datetime.timedelta(days=365)


@dataclasses.dataclass(frozen=True)
class Connection:
    account: external_models.ExternalAccount
    service: groupware_credentials.Service
    password: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class MailBatch:
    folder: groupware_mail.Folder
    validity: int
    last_uid: int
    messages: list[groupware_mail.Message]
    more: bool


def _update_state(
    session: typing.Any,
    account: external_models.ExternalAccount,
    status: str,
    error: str | None,
) -> None:
    if (account.status, account.error_description) == (status, error):
        return
    account.status = status
    account.error_description = error
    account.update(session=session)
    events.create_external_resource_event(
        account.project_id,
        account.owner_user_uuid,
        account,
        events.EXTERNAL_ACCOUNT_UPDATED_EVENT,
        hidden_fields=("owner_user_uuid",),
        session=session,
    )


class Synchronizer:
    def __init__(
        self,
        engine: typing.Any,
        store: groupware_credentials.CredentialStore,
        interval: int,
    ) -> None:
        self._engine = engine
        self._store = store
        self._interval = interval
        self._sender = groupware_delivery.Sender(engine, store)

    def _claim(self) -> Connection | None:
        with self._engine.session_manager() as session:
            row = session.execute(
                """
                SELECT credentials.external_account_uuid, credentials.service
                FROM groupware_account_credentials credentials
                JOIN m_external_accounts_v2 account ON account.uuid = credentials.external_account_uuid
                WHERE NOT account.disabled AND credentials.next_sync_at <= now()
                  AND account.provider IN ('imap_smtp', 'caldav')
                ORDER BY credentials.next_sync_at, credentials.external_account_uuid
                LIMIT 1 FOR UPDATE OF credentials SKIP LOCKED
                """
            ).fetchone()
            if row is None:
                return None
            account_uuid = row["external_account_uuid"]
            service = row["service"]
            session.execute(
                """
                UPDATE groupware_account_credentials
                SET next_sync_at = now() + %s * interval '1 second'
                WHERE external_account_uuid = %s AND service = %s
                """,
                (self._interval, account_uuid, service),
            )
            account = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(account_uuid)}, session=session
            )
            try:
                password = self._store.load(session, account_uuid, service)
            except crypto_exceptions.InvalidTag:
                LOG.error("Unable to decrypt an external groupware credential")
                return None
            if password is None or account.disabled:
                return None
            return Connection(account, service, password)

    def _current(
        self, session: typing.Any, connection: Connection
    ) -> external_models.ExternalAccount | None:
        account = external_models.ExternalAccount.objects.get_one_or_none(
            filters={
                "uuid": filters.EQ(connection.account.uuid),
                "disabled": filters.EQ(False),
            },
            session=session,
            locked=True,
        )
        if account is None:
            return None
        password = self._store.load(session, account.uuid, connection.service)
        if password is None or not hmac.compare_digest(
            password.encode(), connection.password.encode()
        ):
            return None
        return account

    def _read_mail(
        self, connection: Connection
    ) -> collections.abc.Generator[MailBatch, None, None]:
        account = connection.account
        settings = account.settings
        with self._engine.session_manager() as session:
            rows = session.execute(
                """
                SELECT folder.provider_identity, progress.uid_validity, progress.last_uid
                FROM groupware_mailbox_progress progress
                JOIN m_mail_folders folder ON folder.uuid = progress.folder_uuid
                WHERE folder.project_id = %s AND folder.owner_user_uuid = %s
                  AND folder.external_account_uuid = %s
                """,
                (account.project_id, account.owner_user_uuid, account.uuid),
            ).fetchall()
        checkpoints = {
            row["provider_identity"]: (row["uid_validity"], row["last_uid"])
            for row in rows
        }
        downloaded = 0
        with groupware_mail.open_imap(
            groupware_accounts.endpoint(settings["imap"]),
            settings["username"],
            connection.password,
        ) as mailbox:
            for folder in mailbox.folders():
                validity = mailbox.select(folder)
                previous_validity, last_uid = checkpoints.get(
                    folder.mailbox.decode("ascii"), (None, 0)
                )
                if previous_validity != validity:
                    last_uid = 0
                uids = mailbox.uids(last_uid + 1)[:MAIL_BATCH_PER_FOLDER]
                messages = []
                batch_size = 0
                for uid in uids:
                    try:
                        message = mailbox.fetch(uid)
                    except groupware_mail.MessageTooLarge as error:
                        downloaded += error.downloaded_bytes
                    else:
                        if message is not None:
                            batch_size += len(message.content)
                            downloaded += len(message.content)
                            if batch_size > groupware_mail.MAX_MESSAGE_BYTES:
                                break
                            messages.append(message)
                    last_uid = uid
                    if downloaded >= MAIL_BYTES_PER_SYNC:
                        break
                yield MailBatch(
                    folder,
                    validity,
                    last_uid,
                    messages,
                    downloaded >= MAIL_BYTES_PER_SYNC
                    or bool(uids)
                    and (len(uids) == MAIL_BATCH_PER_FOLDER or last_uid != uids[-1]),
                )
                if downloaded >= MAIL_BYTES_PER_SYNC:
                    return

    def _apply_mail(self, connection: Connection, batch: MailBatch) -> bool:
        with self._engine.session_manager() as session:
            account = self._current(session, connection)
            if account is None:
                return False
            projection = groupware_projection.AccountProjection(session, account)
            folder = projection.mail_folder(batch.folder)
            for message in batch.messages:
                projection.mail_message(folder, batch.validity, message)
            session.execute(
                """
                INSERT INTO groupware_mailbox_progress (folder_uuid, uid_validity, last_uid)
                VALUES (%s, %s, %s)
                ON CONFLICT (folder_uuid) DO UPDATE SET
                    uid_validity = excluded.uid_validity, last_uid = excluded.last_uid
                """,
                (folder.uuid, batch.validity, batch.last_uid),
            )
            return True

    def _sync_mail(self, connection: Connection) -> None:
        more = False
        with contextlib.closing(self._read_mail(connection)) as batches:
            for batch in batches:
                if not self._apply_mail(connection, batch):
                    return
                more |= batch.more

        settings = connection.account.settings
        error = None
        try:
            with groupware_mail.open_smtp(
                groupware_accounts.endpoint(settings["smtp"]),
                settings["username"],
                connection.password,
            ):
                pass
        except (smtplib.SMTPException, OSError, ValueError):
            error = "Mail is being imported, but the SMTP server is unavailable."

        with self._engine.session_manager() as session:
            account = self._current(session, connection)
            if account is None:
                return
            if more:
                session.execute(
                    """
                    UPDATE groupware_account_credentials SET next_sync_at = now() + interval '10 seconds'
                    WHERE external_account_uuid = %s AND service = 'mail'
                    """,
                    (account.uuid,),
                )
            _update_state(session, account, "error" if error else "active", error)

    def _sync_calendars(self, connection: Connection) -> None:
        settings = connection.account.settings
        now = datetime.datetime.now(datetime.timezone.utc)
        start = now - CALENDAR_SYNC_SPAN
        end = now + CALENDAR_SYNC_SPAN
        with requests.Session() as http_session:
            client = groupware_calendar.Client(
                http_session, settings["url"], settings["username"], connection.password
            )
            results = [
                (calendar, client.events(calendar, start, end))
                for calendar in client.calendars()
            ]
        with self._engine.session_manager() as session:
            account = self._current(session, connection)
            if account is None:
                return
            projection = groupware_projection.AccountProjection(session, account)
            for source, incoming in results:
                calendar = projection.calendar(source)
                projection.reconcile_calendar(
                    calendar, incoming, start, end, source.timezone, now
                )
            _update_state(session, account, "active", None)

    def sync_next(self) -> bool:
        connection = self._claim()
        if connection is None:
            return False
        try:
            if connection.service == "mail":
                self._sync_mail(connection)
            else:
                self._sync_calendars(connection)
        except (
            imaplib.IMAP4.error,
            smtplib.SMTPException,
            OSError,
            ValueError,
            groupware_calendar.CalendarError,
            requests.RequestException,
        ):
            # Remote error strings can include account credentials or message data.
            with self._engine.session_manager() as session:
                account = self._current(session, connection)
                if account is not None:
                    _update_state(
                        session,
                        account,
                        "error",
                        "Unable to connect or synchronize with the IMAP server."
                        if connection.service == "mail"
                        else "Unable to connect or synchronize with the external server.",
                    )
        return True

    def deliver_next(self) -> bool:
        with self._engine.session_manager() as session:
            row = session.execute(
                """
                SELECT intent.external_account_uuid FROM mail_outgoing_intents intent
                JOIN m_external_accounts_v2 account ON account.uuid = intent.external_account_uuid
                JOIN groupware_account_credentials credentials
                  ON credentials.external_account_uuid = account.uuid AND credentials.service = 'mail'
                WHERE ((intent.status = 'new' AND intent.next_attempt_at <= now())
                    OR (intent.status = 'checking' AND intent.updated_at < now() - interval '10 minutes'))
                  AND account.provider = 'imap_smtp' AND NOT account.disabled
                ORDER BY intent.id LIMIT 1
                """
            ).fetchone()
        return (
            self._sender.send_next(row["external_account_uuid"])
            if row is not None
            else False
        )


class GroupwareAgent(basic.BasicService):
    def _iteration(self) -> None:
        store = groupware_credentials.configured_store()
        if store is None:
            return
        sync = Synchronizer(
            engines.engine_factory.get_engine(),
            store,
            cfg.CONF[groupware_opts.DOMAIN].sync_interval_seconds,
        )
        try:
            sync.deliver_next()
            sync.sync_next()
        except Exception as error:
            LOG.error("External groupware iteration failed (%s)", type(error).__name__)
