# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One Telegram connection per account with durable realtime cursors."""

import asyncio
import base64
import contextlib
import datetime
import hashlib
import logging
import mimetypes
import signal
import time
import typing
import uuid

import telethon
from telethon import errors, events, sessions, utils
from telethon.extensions import binaryreader
from telethon.tl import functions, types

from workspace.bridges import messenger
from workspace.bridges.telegram import conversion, credentials, operations
from workspace.common import async_database
from workspace.common import emoji
from workspace.messenger_api import file_storage


LOG = logging.getLogger(__name__)
ACCOUNTS_SQL = """
SELECT account.* FROM m_external_accounts_v2 account
JOIN telegram_account_state state ON state.external_account_uuid=account.uuid
WHERE account.provider='telegram' AND (NOT account.disabled OR state.disconnect_requested)
  AND state.next_attempt_at<=now()
  AND (state.auth_state NOT IN ('disconnected','expired') OR state.disconnect_requested)
"""


def decode_object(encoded: str) -> typing.Any:
    with binaryreader.BinaryReader(base64.b64decode(encoded)) as reader:
        return reader.tgread_object()


def notification_group(peer: dict) -> str:
    return (
        "broadcasts"
        if peer["broadcast"]
        else "users"
        if peer["peer_kind"] == "user"
        else "chats"
    )


def notification_muted(until: datetime.datetime | None) -> bool:
    return until is not None and until > datetime.datetime.now(datetime.timezone.utc)


def posting_allowed(entity: typing.Any) -> bool:
    if isinstance(entity, types.User):
        return not bool(entity.deleted)
    if entity.left or (isinstance(entity, types.Chat) and entity.deactivated):
        return False
    if isinstance(entity, types.Channel) and entity.broadcast:
        return bool(
            entity.creator
            or (entity.admin_rights and entity.admin_rights.post_messages)
        )
    if entity.creator or entity.admin_rights:
        return True
    return not any(
        rights and (rights.view_messages or rights.send_messages or rights.send_plain)
        for rights in (
            entity.default_banned_rights,
            getattr(entity, "banned_rights", None),
        )
    )


class TelegramService:
    def __init__(
        self,
        db_url: str,
        api_id: int,
        api_hash: str,
        key: str,
        *,
        poll_interval: float = 1.0,
        client_factory: typing.Any = telethon.TelegramClient,
    ) -> None:
        self.db = async_database.Database(
            db_url, size=8, application_name="workspace-telegram"
        )
        self.api_id, self.api_hash, self.key = api_id, api_hash, key
        self.poll_interval = poll_interval
        self.client_factory = client_factory
        self.operations = operations.Operations(self.db)
        self.tasks: dict[uuid.UUID, asyncio.Task] = {}
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        await self.db.open()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        try:
            while not self.stopping.is_set():
                accounts = (
                    await self.db.sql(ACCOUNTS_SQL)
                    if self.api_id and self.api_hash and self.key
                    else []
                )
                identifiers = {item["uuid"] for item in accounts}
                for identifier in set(self.tasks) - identifiers:
                    removed = self.tasks.pop(identifier)
                    removed.cancel()
                    await asyncio.gather(removed, return_exceptions=True)
                for account in accounts:
                    task = self.tasks.get(account["uuid"])
                    if task is None or task.done():
                        if task is not None and not task.cancelled():
                            task.exception()
                        self.tasks[account["uuid"]] = asyncio.create_task(
                            self.account(account)
                        )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), self.poll_interval)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            await self.db.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    async def state(self, account: dict) -> dict | None:
        rows = await self.db.sql(
            "SELECT * FROM telegram_account_state WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        return rows[0] if rows else None

    async def current(self, connection: typing.Any, account: dict) -> bool:
        rows = await messenger.rows(
            connection,
            "SELECT * FROM m_external_accounts_v2 WHERE uuid=%s AND project_id=%s AND provider_uuid=%s AND owner_user_uuid=%s AND provider='telegram' AND NOT disabled FOR KEY SHARE",
            (
                account["uuid"],
                account["project_id"],
                account["provider_uuid"],
                account["owner_user_uuid"],
            ),
        )
        return bool(rows)

    async def account_snapshot(
        self, connection: typing.Any, account: dict, *, deleted: bool = False
    ) -> None:
        snapshot = {
            name: account[name]
            for name in (
                "uuid",
                "project_id",
                "provider",
                "provider_uuid",
                "external_account_id",
                "settings",
                "status",
                "error_description",
                "created_at",
                "updated_at",
                "disabled",
            )
        }
        await self.operations.publish(
            connection,
            account,
            "external_account.deleted" if deleted else "external_account.updated",
            account["uuid"],
            [{"uuid": account["uuid"], "snapshot": snapshot}],
            recipients=[account["owner_user_uuid"]],
        )

    async def status(
        self, account: dict, status: str, error: str | None = None
    ) -> None:
        async with self.db.transaction() as connection:
            changed = await messenger.rows(
                connection,
                "UPDATE m_external_accounts_v2 SET status=%s,error_description=%s,updated_at=now() WHERE uuid=%s AND (status,error_description) IS DISTINCT FROM (%s,%s) RETURNING *",
                (status, error, account["uuid"], status, error),
            )
            if changed:
                await self.account_snapshot(connection, changed[0])

    async def save_session(
        self, account: dict, client: typing.Any, generation: uuid.UUID
    ) -> None:
        encrypted = credentials.encrypt(
            account["uuid"],
            {"session": sessions.StringSession.save(client.session)},
            self.key,
        )
        await self.db.sql(
            "UPDATE telegram_account_state SET session_envelope=%s,updated_at=now() WHERE external_account_uuid=%s AND generation=%s",
            (messenger.json_value(encrypted), account["uuid"], generation),
        )

    async def account(self, account: dict) -> None:
        client = None
        initial_state = None
        lock = await self.db.connect()
        try:
            acquired = await messenger.rows(
                lock,
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired",
                (f"telegram:{account['uuid']}",),
            )
            if not acquired[0]["acquired"]:
                await asyncio.sleep(5)
                return
            state = await self.state(account)
            if state is None:
                return
            initial_state = state
            session = (
                credentials.decrypt(
                    account["uuid"], state["session_envelope"], self.key
                )["session"]
                if state["session_envelope"]
                else ""
            )
            client = self.client_factory(
                sessions.StringSession(session),
                self.api_id,
                self.api_hash,
                request_retries=0,
                flood_sleep_threshold=0,
                sequential_updates=True,
            )
            await client.connect()
            if state["disconnect_requested"]:
                await client.log_out()
                async with self.db.transaction() as connection:
                    await self.account_snapshot(connection, account, deleted=True)
                    await connection.execute(
                        "DELETE FROM m_external_accounts_v2 WHERE uuid=%s",
                        (account["uuid"],),
                    )
                return
            if not await client.is_user_authorized():
                if state["auth_state"] == "authorized":
                    await self.require_login(account)
                    return
                await self.authenticate(account, client)
            await self.initialize(account, client)
            wake = asyncio.Event()
            catalog_wake = asyncio.Event()
            notifications: asyncio.Queue = asyncio.Queue()
            channel_wake = asyncio.Event()
            dirty_channels: set[str] = set()

            async def incoming_wake(event: typing.Any) -> None:
                if isinstance(event, types.UpdateNotifySettings):
                    notifications.put_nowait(
                        (event, datetime.datetime.now(datetime.timezone.utc))
                    )
                remote_peer = getattr(event, "peer", None) or getattr(
                    getattr(event, "message", None), "peer_id", None
                )
                channel_id = getattr(event, "channel_id", None)
                if isinstance(remote_peer, types.PeerChannel):
                    channel_id = remote_peer.channel_id
                if channel_id is not None:
                    dirty_channels.add(f"channel:{channel_id}")
                    channel_wake.set()
                wake.set()

            client.add_event_handler(incoming_wake, events.Raw)
            jobs = [
                asyncio.create_task(handler)
                for handler in (
                    self.receive_loop(account, client, wake, catalog_wake),
                    self.channel_loop(
                        account, client, channel_wake, dirty_channels, catalog_wake
                    ),
                    self.replay_loop(account, client),
                    self.outgoing_loop(account, client),
                    self.catalog_loop(account, client, catalog_wake),
                    self.notification_loop(account, notifications),
                )
            ]
            try:
                await asyncio.gather(*jobs)
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except (
            errors.AuthKeyUnregisteredError,
            errors.SessionRevokedError,
            errors.UserDeactivatedError,
        ):
            await self.require_login(account)
        except errors.FloodWaitError as error:
            await self.db.sql(
                "UPDATE telegram_account_state SET next_attempt_at=now()+%s*interval '1 second',error_code='FLOOD_WAIT' WHERE external_account_uuid=%s",
                (error.seconds, account["uuid"]),
            )
            await asyncio.sleep(min(error.seconds, 60))
        except Exception as error:
            # RPC exception text can contain user data. Persist only its type.
            code = type(error).__name__
            LOG.warning("Telegram account %s stopped: %s", account["uuid"], code)
            await self.status(account, "error", code)
            await asyncio.sleep(5)
        finally:
            if client is not None:
                # A canceled attempt must not leave a newly authorized session alive.
                if initial_state and initial_state["auth_state"] != "authorized":
                    latest = await self.state(account)
                    if (
                        not latest
                        or latest["generation"] != initial_state["generation"]
                        or latest["auth_state"] in ("disconnected", "expired")
                    ):
                        with contextlib.suppress(Exception):
                            if await client.is_user_authorized():
                                await client.log_out()
                await client.disconnect()
            await lock.close()

    async def require_login(self, account: dict) -> None:
        await self.db.sql(
            "UPDATE telegram_account_state SET auth_state='expired',session_envelope=NULL,login_envelope=NULL,password_envelope=NULL,error_code='AUTH_REQUIRED' WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await self.status(account, "error", "AUTH_REQUIRED")

    async def authenticate(self, account: dict, client: typing.Any) -> None:
        changed = asyncio.Event()

        async def token_update(event: typing.Any) -> None:
            changed.set()

        client.add_event_handler(token_update, events.Raw(types.UpdateLoginToken))
        expires = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        generation = None
        try:
            while True:
                state = await self.state(account)
                if not state or state["auth_state"] in ("disconnected", "expired"):
                    raise asyncio.CancelledError()
                if state["attempt_expires_at"] and state[
                    "attempt_expires_at"
                ] <= datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None):
                    await self.db.sql(
                        "UPDATE telegram_account_state SET auth_state='expired',login_envelope=NULL,password_envelope=NULL,session_envelope=NULL WHERE external_account_uuid=%s AND generation=%s",
                        (account["uuid"], state["generation"]),
                    )
                    raise asyncio.CancelledError()
                if state["generation"] != generation:
                    if generation is not None:
                        raise asyncio.CancelledError()
                    generation = state["generation"]
                    expires = datetime.datetime.min.replace(
                        tzinfo=datetime.timezone.utc
                    )
                assert generation is not None
                if state["auth_state"] == "password_required":
                    if state["auth_expires_at"] <= datetime.datetime.now(
                        datetime.timezone.utc
                    ).replace(tzinfo=None):
                        await self.db.sql(
                            "UPDATE telegram_account_state SET auth_state='expired',password_envelope=NULL WHERE external_account_uuid=%s AND generation=%s",
                            (account["uuid"], generation),
                        )
                        raise asyncio.CancelledError()
                    if state["password_envelope"]:
                        claimed = await self.db.sql(
                            "UPDATE telegram_account_state SET password_envelope=NULL WHERE external_account_uuid=%s AND generation=%s AND auth_state='password_required' AND auth_expires_at>now() RETURNING generation",
                            (account["uuid"], generation),
                        )
                        if not claimed:
                            raise asyncio.CancelledError()
                        secret = credentials.decrypt(
                            account["uuid"], state["password_envelope"], self.key
                        )
                        try:
                            await client.sign_in(password=secret["password"])
                        except errors.PasswordHashInvalidError:
                            await self.db.sql(
                                "UPDATE telegram_account_state SET error_code='PASSWORD_INVALID' WHERE external_account_uuid=%s AND generation=%s",
                                (account["uuid"], generation),
                            )
                        else:
                            await self.save_session(account, client, generation)
                            account["_auth_generation"] = generation
                            return
                    if state["auth_expires_at"] <= datetime.datetime.now(
                        datetime.timezone.utc
                    ).replace(tzinfo=None):
                        await self.db.sql(
                            "UPDATE telegram_account_state SET auth_state='expired',password_envelope=NULL WHERE external_account_uuid=%s AND generation=%s",
                            (account["uuid"], generation),
                        )
                        raise asyncio.CancelledError()
                elif (
                    changed.is_set()
                    or datetime.datetime.now(datetime.timezone.utc) >= expires
                ):
                    changed.clear()
                    try:
                        response = await client(
                            functions.auth.ExportLoginTokenRequest(
                                self.api_id, self.api_hash, []
                            )
                        )
                        if isinstance(response, types.auth.LoginTokenMigrateTo):
                            await client._switch_dc(response.dc_id)
                            response = await client(
                                functions.auth.ImportLoginTokenRequest(response.token)
                            )
                        if isinstance(response, types.auth.LoginTokenSuccess):
                            await client._on_login(response.authorization.user)
                            await self.save_session(account, client, generation)
                            account["_auth_generation"] = generation
                            return
                        expires = response.expires
                        token = (
                            base64.urlsafe_b64encode(response.token)
                            .decode()
                            .rstrip("=")
                        )
                        envelope = credentials.encrypt(
                            account["uuid"],
                            {"url": "tg://login?token=" + token},
                            self.key,
                        )
                        await self.save_session(account, client, generation)
                        await self.db.sql(
                            "UPDATE telegram_account_state SET auth_state='qr_pending',login_envelope=%s,auth_expires_at=%s,error_code=NULL WHERE external_account_uuid=%s AND generation=%s",
                            (
                                messenger.json_value(envelope),
                                expires.replace(tzinfo=None),
                                account["uuid"],
                                generation,
                            ),
                        )
                    except errors.SessionPasswordNeededError:
                        await self.save_session(account, client, generation)
                        await self.db.sql(
                            "UPDATE telegram_account_state SET auth_state='password_required',login_envelope=NULL,auth_expires_at=least(attempt_expires_at,now()+interval '5 minutes') WHERE external_account_uuid=%s AND generation=%s",
                            (account["uuid"], generation),
                        )
                await asyncio.sleep(self.poll_interval)
        finally:
            client.remove_event_handler(token_update)

    async def initialize(self, account: dict, client: typing.Any) -> None:
        me = await client.get_me()
        state = await self.state(account)
        if state is None:
            raise asyncio.CancelledError()
        if state["auth_state"] in ("disconnected", "expired") or (
            account.get("_auth_generation")
            and state["generation"] != account["_auth_generation"]
        ):
            await client.log_out()
            raise asyncio.CancelledError()
        if account["external_account_id"] is not None and account[
            "external_account_id"
        ] != str(me.id):
            raise ValueError("Telegram account identity changed")
        current_state = (
            await client(functions.updates.GetStateRequest())
            if state["pts"] is None
            else None
        )
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            locked = await messenger.rows(
                connection,
                "SELECT generation,auth_state FROM telegram_account_state WHERE external_account_uuid=%s FOR UPDATE",
                (account["uuid"],),
            )
            if (
                not locked
                or locked[0]["generation"] != state["generation"]
                or locked[0]["auth_state"] in ("disconnected", "expired")
            ):
                await client.log_out()
                raise asyncio.CancelledError()
            existing = await messenger.rows(
                connection,
                "SELECT linked.*,person.source AS user_source,state.auth_state FROM m_external_accounts_v2 linked JOIN m_workspace_users person ON person.uuid=linked.owner_user_uuid LEFT JOIN telegram_account_state state ON state.external_account_uuid=linked.uuid WHERE linked.project_id=%s AND linked.provider='telegram' AND linked.provider_uuid=%s AND linked.external_account_id=%s AND linked.uuid<>%s FOR UPDATE OF linked",
                (
                    account["project_id"],
                    account["provider_uuid"],
                    str(me.id),
                    account["uuid"],
                ),
            )
            if existing:
                imported = existing[0]
                if (
                    imported["user_source"] != "telegram"
                    or imported["auth_state"] is not None
                ):
                    raise ValueError(
                        "Telegram identity is already linked to a Workspace account"
                    )
                await connection.execute(
                    "DELETE FROM m_external_accounts_v2 WHERE uuid=%s",
                    (imported["uuid"],),
                )
            rows = await messenger.rows(
                connection,
                "UPDATE m_external_accounts_v2 SET external_account_id=%s,status='active',error_description=NULL,updated_at=now() WHERE uuid=%s RETURNING *",
                (str(me.id), account["uuid"]),
            )
            account.update(rows[0])
            await self.operations.user(connection, account, me.id)
            await connection.execute(
                "UPDATE telegram_account_state SET auth_state='authorized',login_envelope=NULL,password_envelope=NULL,error_code=NULL,authorized_at=coalesce(authorized_at,now()) WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
            if current_state is not None:
                await connection.execute(
                    "UPDATE telegram_account_state SET pts=%s,qts=%s,cursor_date=%s WHERE external_account_uuid=%s",
                    (
                        current_state.pts,
                        current_state.qts,
                        current_state.date,
                        account["uuid"],
                    ),
                )
            await self.account_snapshot(connection, account)

    async def contacts(self, account: dict, client: typing.Any) -> None:
        response = await client(functions.contacts.GetContactsRequest(hash=0))
        if isinstance(response, types.contacts.ContactsNotModified):
            return
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            for person in response.users:
                if isinstance(person, types.User):
                    await self.operations.person(connection, account, person)

    async def catalog_loop(
        self, account: dict, client: typing.Any, wake: asyncio.Event | None = None
    ) -> None:
        initial = True
        retrying = False
        while True:
            if not initial and not retrying:
                if wake is None:
                    await asyncio.sleep(60)
                else:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(wake.wait(), 60)
                    wake.clear()
            await self.wait_retry(account, "catalog")
            await self.selected(account)
            try:
                if initial:
                    await self.contacts(account, client)
                await self.notification_defaults(account, client)
                await self.catalog(account, client)
                initial = False
                retrying = False
                await self.db.sql(
                    "UPDATE telegram_account_state SET catalog_error_code=NULL WHERE external_account_uuid=%s",
                    (account["uuid"],),
                )
            except errors.FloodWaitError as error:
                await self.defer_worker(account, "catalog", error.seconds)
                retrying = True
            except (
                errors.AuthKeyUnregisteredError,
                errors.SessionRevokedError,
                errors.UserDeactivatedError,
            ):
                raise
            except Exception as error:
                code = type(error).__name__
                LOG.warning("Telegram catalog %s paused: %s", account["uuid"], code)
                await self.db.sql(
                    "UPDATE telegram_account_state SET catalog_error_code=%s WHERE external_account_uuid=%s",
                    (code, account["uuid"]),
                )
                await self.defer_worker(account, "catalog", 30)
                retrying = True

    async def wait_retry(self, account: dict, worker: str) -> None:
        field = {
            "history": "history_retry_at",
            "catalog": "catalog_retry_at",
            "common": "common_retry_at",
        }[worker]
        while True:
            retry = await self.db.sql(
                f"SELECT EXTRACT(epoch FROM ({field}-now())) AS seconds FROM telegram_account_state WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
            if not retry or retry[0]["seconds"] is None or retry[0]["seconds"] <= 0:
                return
            await asyncio.sleep(float(retry[0]["seconds"]))

    async def defer_worker(self, account: dict, worker: str, seconds: int) -> None:
        field = {
            "history": "history_retry_at",
            "catalog": "catalog_retry_at",
            "common": "common_retry_at",
        }[worker]
        await self.db.sql(
            f"UPDATE telegram_account_state SET {field}=GREATEST({field},now()+%s*interval '1 second'),updated_at=now() WHERE external_account_uuid=%s",
            (seconds, account["uuid"]),
        )

    async def catalog(self, account: dict, client: typing.Any) -> None:
        seen = []
        observed_at = datetime.datetime.now(datetime.timezone.utc)
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if getattr(entity, "left", False) or getattr(entity, "kicked", False):
                continue
            if isinstance(entity, types.User):
                async with self.db.transaction() as connection:
                    if not await self.current(connection, account):
                        raise asyncio.CancelledError()
                    await self.operations.person(connection, account, entity)
                peer, kind = types.PeerUser(entity.id), "user"
                can_send, forum, pts = posting_allowed(entity), False, None
            elif isinstance(entity, types.Chat):
                peer, kind = types.PeerChat(entity.id), "chat"
                can_send, forum, pts = (
                    posting_allowed(entity),
                    False,
                    None,
                )
            elif isinstance(entity, types.Channel):
                peer, kind = types.PeerChannel(entity.id), "channel"
                can_send = posting_allowed(entity)
                forum, pts = bool(entity.forum), dialog.dialog.pts
            else:
                continue
            key = conversion.peer_key(peer)
            seen.append(key)
            envelope = credentials.encrypt(
                account["uuid"],
                {"peer": base64.b64encode(bytes(dialog.input_entity)).decode()},
                self.key,
            )
            saved = await self.db.sql(
                "INSERT INTO telegram_peer_state(external_account_uuid,peer_key,title,input_peer_envelope,peer_kind,can_send,forum,pts,read_max_id,selected,broadcast) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s) ON CONFLICT(external_account_uuid,peer_key) DO UPDATE SET title=EXCLUDED.title,input_peer_envelope=EXCLUDED.input_peer_envelope,can_send=EXCLUDED.can_send,forum=EXCLUDED.forum,read_max_id=EXCLUDED.read_max_id,broadcast=EXCLUDED.broadcast,selected=true,updated_at=now() RETURNING *",
                (
                    account["uuid"],
                    key,
                    dialog.title or key,
                    messenger.json_value(envelope),
                    kind,
                    can_send,
                    forum,
                    pts,
                    dialog.dialog.read_inbox_max_id,
                    isinstance(entity, types.Channel) and bool(entity.broadcast),
                ),
            )
            if saved[0]["stream_uuid"] is not None:
                # A newly verified counterpart must also update an already imported DM.
                async with self.db.transaction() as connection:
                    if not await self.current(connection, account):
                        raise asyncio.CancelledError()
                    await self.operations.stream(connection, account, saved[0])
            settings = getattr(dialog.dialog, "notify_settings", None)
            if settings is not None:
                await self.notify_settings(account, saved[0], settings, observed_at)

        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            missing = await messenger.rows(
                connection,
                "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s AND selected "
                "AND NOT(peer_key=ANY(%s::text[])) ORDER BY peer_key FOR UPDATE",
                (account["uuid"], seen),
            )
            for peer in missing:
                await self.lock_peer(connection, account, peer["peer_key"])
                await self.operations.revoke(connection, account, peer)

    async def notification_defaults(self, account: dict, client: typing.Any) -> None:
        defaults = {}
        observed_at = datetime.datetime.now(datetime.timezone.utc)
        for group, peer in (
            ("users", types.InputNotifyUsers()),
            ("chats", types.InputNotifyChats()),
            ("broadcasts", types.InputNotifyBroadcasts()),
        ):
            settings = await client(functions.account.GetNotifySettingsRequest(peer))
            defaults[group] = notification_muted(getattr(settings, "mute_until", None))
        await self.db.sql(
            "UPDATE telegram_account_state SET notification_defaults=%s,notification_defaults_at=%s WHERE external_account_uuid=%s AND (notification_defaults_at IS NULL OR notification_defaults_at<=%s)",
            (messenger.json_value(defaults), observed_at, account["uuid"], observed_at),
        )

    async def notify_settings(
        self,
        account: dict,
        peer: dict,
        settings: typing.Any,
        observed_at: datetime.datetime,
    ) -> None:
        until = settings.mute_until
        state = await self.state(account)
        if state is None:
            raise asyncio.CancelledError()
        inherited = until is None
        muted = (
            state["notification_defaults"].get(notification_group(peer), False)
            if inherited
            else notification_muted(until)
        )
        await self.notify_remote(
            account,
            peer,
            muted,
            until=until,
            inherited=inherited,
            observed_at=observed_at,
        )

    async def notify_remote(
        self,
        account: dict,
        peer: dict,
        muted: bool,
        *,
        until: datetime.datetime | None = None,
        inherited: bool = False,
        observed_at: datetime.datetime | None = None,
    ) -> None:
        observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            await self.lock_peer(connection, account, peer["peer_key"])
            latest = (
                await messenger.rows(
                    connection,
                    "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s AND peer_key=%s",
                    (account["uuid"], peer["peer_key"]),
                )
            )[0]
            if (
                latest["notification_checked_at"] is not None
                and observed_at < latest["notification_checked_at"]
            ):
                return
            if await messenger.rows(
                connection,
                "SELECT id FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND peer_key=%s AND entity_type='notifications' AND status IN ('new','checking') LIMIT 1",
                (account["uuid"], peer["peer_key"]),
            ):
                return
            await connection.execute(
                "UPDATE telegram_peer_state SET notification_muted=%s,notification_inherited=%s,notification_mute_until=%s,notification_checked_at=%s WHERE external_account_uuid=%s AND peer_key=%s",
                (
                    muted,
                    inherited,
                    until,
                    observed_at,
                    account["uuid"],
                    peer["peer_key"],
                ),
            )
            if latest["notification_muted"] == muted or latest["stream_uuid"] is None:
                return
            changed = await messenger.rows(
                connection,
                "UPDATE messenger_stream_bindings SET notification_mode=%s,notification_updated_at=now(),updated_at=now() WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s AND active AND notification_mode IS DISTINCT FROM %s RETURNING uuid",
                (
                    "muted" if muted else "all_messages",
                    account["project_id"],
                    latest["stream_uuid"],
                    account["owner_user_uuid"],
                    "muted" if muted else "all_messages",
                ),
            )
            if changed:
                await self.operations.snapshot(
                    connection,
                    account,
                    "stream",
                    latest["stream_uuid"],
                    "updated",
                    latest["stream_uuid"],
                )

    async def notification_loop(self, account: dict, queue: asyncio.Queue) -> None:
        while True:
            update, observed_at = await queue.get()
            peers = await self.selected(account)
            if isinstance(update.peer, types.NotifyPeer):
                key = conversion.peer_key(update.peer.peer)
                for peer in peers:
                    if peer["peer_key"] == key:
                        await self.notify_settings(
                            account, peer, update.notify_settings, observed_at
                        )
            else:
                group = next(
                    (
                        name
                        for model, name in (
                            (types.NotifyUsers, "users"),
                            (types.NotifyChats, "chats"),
                            (types.NotifyBroadcasts, "broadcasts"),
                        )
                        if isinstance(update.peer, model)
                    ),
                    None,
                )
                if group is None:
                    continue
                muted = notification_muted(update.notify_settings.mute_until)
                await self.db.sql(
                    "UPDATE telegram_account_state SET notification_defaults=jsonb_set(notification_defaults,ARRAY[%s],%s),notification_defaults_at=%s WHERE external_account_uuid=%s AND (notification_defaults_at IS NULL OR notification_defaults_at<=%s)",
                    (
                        group,
                        messenger.json_value(muted),
                        observed_at,
                        account["uuid"],
                        observed_at,
                    ),
                )
                for peer in peers:
                    if (
                        peer["notification_inherited"]
                        and notification_group(peer) == group
                    ):
                        await self.notify_remote(
                            account,
                            peer,
                            muted,
                            inherited=True,
                            observed_at=observed_at,
                        )

    def input_peer(self, account: dict, peer: dict) -> typing.Any:
        return decode_object(
            credentials.decrypt(account["uuid"], peer["input_peer_envelope"], self.key)[
                "peer"
            ]
        )

    async def selected(self, account: dict) -> list[dict]:
        current = await self.db.sql(
            "SELECT settings,disabled FROM m_external_accounts_v2 WHERE uuid=%s AND owner_user_uuid=%s AND project_id=%s",
            (account["uuid"], account["owner_user_uuid"], account["project_id"]),
        )
        state = await self.state(account)
        if (
            not current
            or current[0]["disabled"]
            or not state
            or state["disconnect_requested"]
        ):
            raise asyncio.CancelledError()
        account["settings"] = current[0]["settings"]
        return await self.db.sql(
            "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s AND selected ORDER BY peer_key",
            (account["uuid"],),
        )

    async def prepare_files(
        self,
        account: dict,
        peer: dict,
        message: typing.Any,
        data: dict,
        client: typing.Any,
    ) -> tuple[int, int]:
        data["files"] = []
        if not message.media or isinstance(
            message.media, (types.MessageMediaEmpty, types.MessageMediaWebPage)
        ):
            return 0, 0
        stream_uuid = await self.operations.identity(
            account,
            "stream",
            conversion.stream_identity(
                account["external_account_id"], peer["peer_key"]
            ),
        )
        file_uuid = uuid.uuid5(stream_uuid, f"{data['identity']}:file")
        existing = await self.db.sql(
            "SELECT uuid,name,created_at,content_type,size_bytes,hash,storage_type,storage_id,storage_object_id FROM m_workspace_files WHERE uuid=%s AND project_id=%s",
            (file_uuid, account["project_id"]),
        )
        if existing:
            data["files"] = existing
            return 0, 0
        if not isinstance(
            message.media, (types.MessageMediaPhoto, types.MessageMediaDocument)
        ):
            data["text"] += "\n\n[Unsupported Telegram attachment]"
            return 0, 0
        content = await client.download_media(message, bytes)
        if content is None:
            raise ValueError("Telegram attachment is unavailable")
        media_file = message.file
        name = media_file.name or f"telegram-{message.id}{media_file.ext or ''}"
        mime = (
            media_file.mime_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        storage = await asyncio.to_thread(
            file_storage.save_workspace_file, file_uuid, content
        )
        digest = hashlib.sha256(content).hexdigest()
        metadata = file_storage.WorkspaceFileMetadata(
            file_uuid,
            account["project_id"],
            stream_uuid,
            account["owner_user_uuid"],
            name,
            "",
            mime,
            len(content),
            digest,
            message.date,
        )
        await asyncio.to_thread(
            file_storage.save_workspace_file_metadata, metadata, storage.storage_type
        )
        data["files"] = [
            {
                "uuid": file_uuid,
                "name": name,
                "content_type": mime,
                "size_bytes": len(content),
                "hash": digest,
                "storage_type": storage.storage_type,
                "storage_id": storage.storage_id,
                "storage_object_id": storage.storage_object_id,
                "created_at": message.date,
            }
        ]
        return 1, len(content)

    async def prepare(
        self,
        account: dict,
        peer: dict,
        messages: list,
        client: typing.Any,
        people: dict | None = None,
        observed_at: datetime.datetime | None = None,
    ) -> tuple[list[dict], int, int, float]:
        started = time.monotonic()
        observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
        prepared, files, size = [], 0, 0
        for message in messages:
            if not isinstance(message, types.Message):
                continue
            message._client = client
            mentions = {}
            remote_mentions = {
                entity.user_id
                for entity in message.entities or []
                if isinstance(entity, types.MessageEntityMentionName)
            }
            if remote_mentions:
                async with self.db.transaction() as connection:
                    if not await self.current(connection, account):
                        raise asyncio.CancelledError()
                    for remote in remote_mentions:
                        mentions[remote] = await self.operations.user(
                            connection, account, remote
                        )
            data = conversion.normalize_message(
                account["external_account_id"],
                peer["peer_key"],
                message,
                mentions=mentions,
            )
            data["observed_at"] = observed_at.isoformat()
            sender = (people or {}).get(data["sender_id"])
            if sender is not None:
                data["sender_name"] = utils.get_display_name(sender)
            if peer["forum"] and data["topic_id"]:
                topics = await client(
                    functions.messages.GetForumTopicsByIDRequest(
                        peer=self.input_peer(account, peer), topics=[data["topic_id"]]
                    )
                )
                data["topic_title"] = next(
                    (
                        item.title
                        for item in topics.topics
                        if isinstance(item, types.ForumTopic)
                    ),
                    f"Topic {data['topic_id']}",
                )
            added_files, added_size = await self.prepare_files(
                account, peer, message, data, client
            )
            files += added_files
            size += added_size
            prepared.append(data)
        return prepared, files, size, time.monotonic() - started

    async def lock_peer(
        self, connection: typing.Any, account: dict, peer_key: str
    ) -> None:
        scope = conversion.stream_identity(account["external_account_id"], peer_key)
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"telegram:{account['project_id']}:{account['provider_uuid']}:{scope}",),
        )

    async def merge_batch(
        self,
        account: dict,
        peer: dict,
        prepared: list[dict],
        *,
        cursor: dict | None = None,
    ) -> int:
        created = 0
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            selected = await messenger.rows(
                connection,
                "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s AND peer_key=%s AND selected FOR UPDATE",
                (account["uuid"], peer["peer_key"]),
            )
            if not selected:
                raise asyncio.CancelledError()
            await connection.execute(
                "SELECT set_config('workspace.outgoing_actor','',true)"
            )
            await self.lock_peer(connection, account, peer["peer_key"])
            stream = await self.operations.stream(connection, account, peer)
            for data in prepared:
                event_key = f"{data['identity']}:{data['edited_at']}:{hashlib.sha256(data['text'].encode()).hexdigest()}"
                observed = (
                    datetime.datetime.fromisoformat(data["observed_at"]).replace(
                        tzinfo=None
                    )
                    if "observed_at" in data
                    else datetime.datetime.now(datetime.timezone.utc).replace(
                        tzinfo=None
                    )
                )
                latest = await messenger.rows(
                    connection,
                    "SELECT max(event.received_at) AS observed_at FROM telegram_incoming_events event JOIN m_external_accounts_v2 source ON source.uuid=event.external_account_uuid WHERE source.project_id=%s AND source.provider_uuid=%s AND event.payload->>'identity'=%s",
                    (
                        account["project_id"],
                        account["provider_uuid"],
                        data["identity"],
                    ),
                )
                if latest[0]["observed_at"] and latest[0]["observed_at"] > observed:
                    continue
                await connection.execute(
                    "INSERT INTO telegram_incoming_events(external_account_uuid,event_key,payload,received_at) VALUES(%s,%s,%s,%s) ON CONFLICT(external_account_uuid,event_key) DO UPDATE SET received_at=EXCLUDED.received_at,payload=EXCLUDED.payload",
                    (
                        account["uuid"],
                        event_key,
                        messenger.json_value(data),
                        observed,
                    ),
                )
                bound = await messenger.rows(
                    connection,
                    "SELECT uuid FROM messenger_messages WHERE project_id=%s AND provider_uuid=%s AND source_name='telegram' AND provider_identity=%s",
                    (
                        account["project_id"],
                        account["provider_uuid"],
                        data["identity"],
                    ),
                )
                if bound:
                    pending_edit = await messenger.rows(
                        connection,
                        "SELECT id FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_uuid=%s AND (status IN ('new','checking') OR error_code='SEND_UNCERTAIN') LIMIT 1",
                        (account["uuid"], bound[0]["uuid"]),
                    )
                    if pending_edit:
                        continue
                elif data["out"]:
                    uncertain = await messenger.rows(
                        connection,
                        "SELECT id FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND peer_key=%s AND entity_type='message' AND operation='insert' AND (status IN ('new','checking') OR error_code='SEND_UNCERTAIN') AND created_at<=%s::timestamp+interval '2 seconds' AND NOT(coalesce(request,'{}'::jsonb) ? 'receipt_id') LIMIT 1",
                        (account["uuid"], peer["peer_key"], data["date"]),
                    )
                    if uncertain:
                        continue
                added, _identifier = await self.operations.message(
                    connection, account, peer, data, stream=stream
                )
                created += int(added)
                await connection.execute(
                    "UPDATE telegram_incoming_events SET processed_at=now() WHERE external_account_uuid=%s AND event_key=%s",
                    (account["uuid"], event_key),
                )
            if cursor is not None:
                if "pts" in cursor:
                    await connection.execute(
                        "UPDATE telegram_peer_state SET pts=%s,receive_retry_at=NULL,receive_error_code=NULL WHERE external_account_uuid=%s AND peer_key=%s",
                        (cursor["pts"], account["uuid"], peer["peer_key"]),
                    )
        return created

    async def replay_pending(
        self, account: dict, peer: dict, client: typing.Any
    ) -> None:
        pending = await self.db.sql(
            "SELECT id,payload FROM telegram_incoming_events WHERE external_account_uuid=%s AND processed_at IS NULL AND payload->>'peer'=%s ORDER BY id LIMIT 100",
            (account["uuid"], peer["peer_key"]),
        )
        if not pending:
            return
        query = "SELECT id FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND peer_key=%s AND (status IN ('new','checking') OR error_code='SEND_UNCERTAIN') LIMIT 1"
        parameters = (account["uuid"], peer["peer_key"])
        if await self.db.sql(query, parameters):
            return
        identifiers = list({item["payload"]["id"] for item in pending})
        messages = await client.get_messages(
            self.input_peer(account, peer), ids=identifiers
        )
        prepared, *_ = await self.prepare(account, peer, messages, client)
        await self.merge_batch(account, peer, prepared)
        # Re-fetch after the last acknowledgement instead of applying an older
        # echo over a newer local edit. A crash before this mark safely repeats it.
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            await self.lock_peer(connection, account, peer["peer_key"])
            if await messenger.rows(connection, query, parameters):
                return
            present = {
                message.id for message in messages if isinstance(message, types.Message)
            }
            await self.operations.delete(
                connection,
                account,
                peer,
                [value for value in identifiers if value not in present],
            )
            await connection.execute(
                "UPDATE telegram_incoming_events SET processed_at=now() WHERE external_account_uuid=%s AND id=ANY(%s::bigint[])",
                (account["uuid"], [item["id"] for item in pending]),
            )

    async def receive_loop(
        self,
        account: dict,
        client: typing.Any,
        wake: asyncio.Event,
        catalog_wake: asyncio.Event | None = None,
    ) -> None:
        while True:
            wake.clear()
            await self.wait_retry(account, "common")
            peers = await self.selected(account)
            try:
                await self.receive_common(account, peers, client, catalog_wake)
            except errors.FloodWaitError as error:
                await self.defer_worker(account, "common", error.seconds)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                await self.defer_worker(account, "common", 3)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), self.poll_interval)

    async def defer_peer(
        self,
        account: dict,
        peer: dict,
        field: str,
        seconds: int,
        *,
        code: str | None = None,
    ) -> None:
        assert field in ("receive_retry_at", "media_retry_at", "history_retry_at")
        await self.db.sql(
            f"UPDATE telegram_peer_state SET {field}=now()+%s*interval '1 second',{field.replace('retry_at', 'error_code')}=%s WHERE external_account_uuid=%s AND peer_key=%s",
            (seconds, code, account["uuid"], peer["peer_key"]),
        )
        peer[field] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=seconds
        )

    async def channel_loop(
        self,
        account: dict,
        client: typing.Any,
        wake: asyncio.Event,
        dirty: set[str],
        catalog_wake: asyncio.Event | None = None,
    ) -> None:
        async def receive(peer: dict) -> None:
            if peer["receive_retry_at"] is not None and peer[
                "receive_retry_at"
            ] > datetime.datetime.now(datetime.timezone.utc):
                return
            try:
                await self.receive_channel(account, peer, client)
            except errors.ChannelPrivateError:
                async with self.db.transaction() as connection:
                    if not await self.current(connection, account):
                        raise asyncio.CancelledError()
                    selected = await messenger.rows(
                        connection,
                        "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s AND peer_key=%s AND selected FOR UPDATE",
                        (account["uuid"], peer["peer_key"]),
                    )
                    if selected:
                        await self.lock_peer(connection, account, peer["peer_key"])
                        await self.operations.revoke(connection, account, selected[0])
            except errors.FloodWaitError as error:
                await self.defer_peer(
                    account, peer, "receive_retry_at", error.seconds, code="FLOOD_WAIT"
                )
            except (
                errors.AuthKeyUnregisteredError,
                errors.SessionRevokedError,
                errors.UserDeactivatedError,
            ):
                raise
            except Exception as error:
                code = type(error).__name__
                LOG.warning(
                    "Telegram channel %s %s paused: %s",
                    account["uuid"],
                    peer["peer_key"],
                    code,
                )
                await self.defer_peer(account, peer, "receive_retry_at", 30, code=code)

        while True:
            wake.clear()
            peers = {
                peer["peer_key"]: peer
                for peer in await self.selected(account)
                if peer["peer_kind"] == "channel"
            }
            for peer in peers.values():
                if dirty:
                    key = dirty.pop()
                    if key not in peers:
                        if catalog_wake is not None:
                            catalog_wake.set()
                    elif key != peer["peer_key"]:
                        await receive(peers[key])
                dirty.discard(peer["peer_key"])
                await receive(peer)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), self.poll_interval)

    async def replay_loop(self, account: dict, client: typing.Any) -> None:
        while True:
            peers = await self.selected(account)
            pending = await self.db.sql(
                "SELECT payload->>'peer' AS peer_key FROM telegram_incoming_events WHERE external_account_uuid=%s AND processed_at IS NULL GROUP BY payload->>'peer' ORDER BY min(id)",
                (account["uuid"],),
            )
            by_key = {peer["peer_key"]: peer for peer in peers}
            for item in pending:
                peer = by_key.get(item["peer_key"])
                if peer is None or (
                    peer["media_retry_at"] is not None
                    and peer["media_retry_at"]
                    > datetime.datetime.now(datetime.timezone.utc)
                ):
                    continue
                try:
                    await self.replay_pending(account, peer, client)
                    if peer["media_retry_at"] is not None:
                        await self.db.sql(
                            "UPDATE telegram_peer_state SET media_retry_at=NULL,media_error_code=NULL WHERE external_account_uuid=%s AND peer_key=%s",
                            (account["uuid"], peer["peer_key"]),
                        )
                except errors.FloodWaitError as error:
                    await self.defer_peer(
                        account,
                        peer,
                        "media_retry_at",
                        error.seconds,
                        code="FLOOD_WAIT",
                    )
                except (
                    errors.AuthKeyUnregisteredError,
                    errors.SessionRevokedError,
                    errors.UserDeactivatedError,
                ):
                    raise
                except Exception as error:
                    code = type(error).__name__
                    LOG.warning(
                        "Telegram replay %s %s paused: %s",
                        account["uuid"],
                        peer["peer_key"],
                        code,
                    )
                    await self.defer_peer(
                        account, peer, "media_retry_at", 30, code=code
                    )
            await asyncio.sleep(self.poll_interval)

    async def defer_media(self, account: dict, messages: list) -> list:
        ready = []
        deferred = [
            message
            for message in messages
            if isinstance(message, types.Message)
            and message.media is not None
            and not isinstance(
                message.media, (types.MessageMediaEmpty, types.MessageMediaWebPage)
            )
        ]
        if deferred:
            async with self.db.transaction() as connection:
                if not await self.current(connection, account):
                    raise asyncio.CancelledError()
                for message in deferred:
                    data = conversion.normalize_message(
                        account["external_account_id"],
                        conversion.peer_key(message.peer_id),
                        message,
                    )
                    await connection.execute(
                        "INSERT INTO telegram_incoming_events(external_account_uuid,event_key,payload) VALUES(%s,%s,%s) ON CONFLICT(external_account_uuid,event_key) DO NOTHING",
                        (
                            account["uuid"],
                            "media:" + hashlib.sha256(bytes(message)).hexdigest(),
                            messenger.json_value(data),
                        ),
                    )
        for message in messages:
            if message not in deferred:
                ready.append(message)
        return ready

    async def receipts(self, account: dict, updates: list) -> None:
        for update in updates:
            if not isinstance(update, types.UpdateMessageID):
                continue
            intents = await self.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND random_id=%s AND status IN ('new','checking')",
                (account["uuid"], update.random_id),
            )
            for intent in intents:
                async with self.db.transaction() as connection:
                    await self.lock_peer(connection, account, intent["peer_key"])
                    await connection.execute(
                        "UPDATE telegram_outgoing_intents SET request=coalesce(request,'{}'::jsonb)||jsonb_build_object('receipt_id',%s::bigint) WHERE id=%s",
                        (update.id, intent["id"]),
                    )
                    identity = conversion.message_identity(
                        account["external_account_id"], intent["peer_key"], update.id
                    )
                    await connection.execute(
                        "UPDATE messenger_messages SET provider_identity=%s,status='active' WHERE project_id=%s AND uuid=%s AND source_name='telegram' AND provider_uuid=%s",
                        (
                            identity,
                            account["project_id"],
                            intent["entity_uuid"],
                            account["provider_uuid"],
                        ),
                    )

    async def apply_updates(
        self, account: dict, peers: list[dict], updates: list, client: typing.Any
    ) -> None:
        await self.receipts(account, updates)
        by_key = {peer["peer_key"]: peer for peer in peers}
        for update in updates:
            if isinstance(
                update, (types.UpdateEditMessage, types.UpdateEditChannelMessage)
            ):
                key = conversion.peer_key(update.message.peer_id)
                if key in by_key:
                    prepared, *_ = await self.prepare(
                        account,
                        by_key[key],
                        await self.defer_media(account, [update.message]),
                        client,
                    )
                    await self.merge_batch(account, by_key[key], prepared)
            elif isinstance(
                update, (types.UpdateDeleteMessages, types.UpdateDeleteChannelMessages)
            ):
                targets = (
                    [by_key[f"channel:{update.channel_id}"]]
                    if isinstance(update, types.UpdateDeleteChannelMessages)
                    and f"channel:{update.channel_id}" in by_key
                    else [peer for peer in peers if peer["peer_kind"] != "channel"]
                    if isinstance(update, types.UpdateDeleteMessages)
                    else []
                )
                async with self.db.transaction() as connection:
                    if not await self.current(connection, account):
                        raise asyncio.CancelledError()
                    for peer in targets:
                        await self.operations.delete(
                            connection, account, peer, update.messages
                        )
            elif isinstance(
                update, (types.UpdateReadHistoryInbox, types.UpdateReadChannelInbox)
            ):
                key = (
                    f"channel:{update.channel_id}"
                    if isinstance(update, types.UpdateReadChannelInbox)
                    else conversion.peer_key(update.peer)
                )
                if key in by_key:
                    await self.read_remote(account, by_key[key], update.max_id)
            elif isinstance(update, types.UpdateMessageReactions):
                key = conversion.peer_key(update.peer)
                if key in by_key:
                    message = await client.get_messages(
                        self.input_peer(account, by_key[key]), ids=update.msg_id
                    )
                    if isinstance(message, types.Message):
                        prepared, *_ = await self.prepare(
                            account,
                            by_key[key],
                            await self.defer_media(account, [message]),
                            client,
                        )
                        await self.merge_batch(account, by_key[key], prepared)

    async def read_remote(self, account: dict, peer: dict, maximum: int) -> None:
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            await connection.execute(
                "UPDATE telegram_peer_state SET read_max_id=greatest(read_max_id,%s) WHERE external_account_uuid=%s AND peer_key=%s",
                (maximum, account["uuid"], peer["peer_key"]),
            )
            changed = await messenger.rows(
                connection,
                "DELETE FROM messenger_user_message_unreads flag USING messenger_message_placements placement,messenger_messages message "
                "WHERE flag.project_id=%s AND flag.user_uuid=%s AND placement.project_id=flag.project_id AND placement.uuid=flag.placement_uuid "
                "AND message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.source_name='telegram' "
                "AND message.provider_uuid=%s AND message.provider_identity LIKE %s AND split_part(message.provider_identity,':message:',2)::bigint<=%s RETURNING placement.*",
                (
                    account["project_id"],
                    account["owner_user_uuid"],
                    account["provider_uuid"],
                    conversion.stream_identity(
                        account["external_account_id"], peer["peer_key"]
                    )
                    + ":message:%",
                    maximum,
                ),
            )
            for placement in changed:
                await self.operations.snapshot(
                    connection,
                    account,
                    "message",
                    placement["uuid"],
                    "read",
                    placement["stream_uuid"],
                )
                await self.operations.counters(
                    connection, account, placement, user=account["owner_user_uuid"]
                )

    async def receive_common(
        self,
        account: dict,
        peers: list[dict],
        client: typing.Any,
        catalog_wake: asyncio.Event | None = None,
    ) -> None:
        state = await self.state(account)
        if state is None:
            raise asyncio.CancelledError()
        response = await client(
            functions.updates.GetDifferenceRequest(
                pts=state["pts"],
                date=state["cursor_date"],
                qts=state["qts"],
                pts_total_limit=10000,
            )
        )
        if isinstance(response, types.updates.DifferenceTooLong):
            # An expired server journal needs a bounded authoritative rescan.
            await self.db.sql(
                "UPDATE telegram_peer_state SET history_done=false,history_before_id=0 WHERE external_account_uuid=%s AND selected",
                (account["uuid"],),
            )
            await self.db.sql(
                "UPDATE telegram_account_state SET pts=%s WHERE external_account_uuid=%s",
                (response.pts, account["uuid"]),
            )
            return
        if isinstance(response, types.updates.DifferenceEmpty):
            await self.db.sql(
                "UPDATE telegram_account_state SET cursor_date=%s WHERE external_account_uuid=%s",
                (response.date, account["uuid"]),
            )
            return
        updates = response.other_updates
        if catalog_wake is not None:
            known = {peer["peer_key"] for peer in peers}
            if any(
                isinstance(message, types.Message)
                and conversion.peer_key(message.peer_id) not in known
                for message in response.new_messages
            ):
                catalog_wake.set()
        await self.receipts(account, updates)
        people = {
            utils.get_peer_id(person): person
            for person in response.users + response.chats
        }
        for peer in peers:
            messages = [
                message
                for message in response.new_messages
                if isinstance(message, types.Message)
                and conversion.peer_key(message.peer_id) == peer["peer_key"]
            ]
            if messages:
                messages = await self.defer_media(account, messages)
                prepared, *_ = await self.prepare(
                    account, peer, messages, client, people
                )
                await self.merge_batch(account, peer, prepared)
        await self.apply_updates(account, peers, updates, client)
        new_state = (
            response.intermediate_state
            if isinstance(response, types.updates.DifferenceSlice)
            else response.state
        )
        await self.db.sql(
            "UPDATE telegram_account_state SET pts=%s,qts=%s,cursor_date=%s WHERE external_account_uuid=%s",
            (new_state.pts, new_state.qts, new_state.date, account["uuid"]),
        )

    async def receive_channel(
        self, account: dict, peer: dict, client: typing.Any
    ) -> None:
        response = await client(
            functions.updates.GetChannelDifferenceRequest(
                channel=utils.get_input_channel(self.input_peer(account, peer)),
                filter=types.ChannelMessagesFilterEmpty(),
                pts=peer["pts"] or 1,
                limit=100,
            )
        )
        if isinstance(response, types.updates.ChannelDifferenceEmpty):
            await self.db.sql(
                "UPDATE telegram_peer_state SET pts=%s,receive_retry_at=NULL,receive_error_code=NULL WHERE external_account_uuid=%s AND peer_key=%s",
                (response.pts, account["uuid"], peer["peer_key"]),
            )
            peer["pts"] = response.pts
            return
        too_long = isinstance(response, types.updates.ChannelDifferenceTooLong)
        messages = await self.defer_media(
            account, response.messages if too_long else response.new_messages
        )
        prepared, *_ = await self.prepare(
            account,
            peer,
            messages,
            client,
            {
                utils.get_peer_id(person): person
                for person in response.users + response.chats
            },
        )
        if not too_long:
            await self.apply_updates(account, [peer], response.other_updates, client)
        pts = response.dialog.pts if too_long else response.pts
        await self.merge_batch(account, peer, prepared, cursor={"pts": pts})
        peer["pts"] = pts
        if too_long:
            await self.db.sql(
                "UPDATE telegram_peer_state SET history_done=false,history_before_id=0 WHERE external_account_uuid=%s AND peer_key=%s",
                (account["uuid"], peer["peer_key"]),
            )

    async def outgoing_loop(self, account: dict, client: typing.Any) -> None:
        while True:
            await self.selected(account)
            intents = await self.db.sql(
                "SELECT intent.* FROM telegram_outgoing_intents intent WHERE external_account_uuid=%s AND status IN ('new','checking') AND next_attempt_at<=now() "
                "AND NOT EXISTS(SELECT 1 FROM telegram_outgoing_intents earlier WHERE earlier.external_account_uuid=intent.external_account_uuid AND earlier.entity_uuid=intent.entity_uuid AND earlier.id<intent.id AND (earlier.status IN ('new','checking') OR earlier.error_code='SEND_UNCERTAIN')) ORDER BY id LIMIT 1",
                (account["uuid"],),
            )
            if not intents:
                await asyncio.sleep(self.poll_interval)
                continue
            intent = intents[0]
            try:
                await self.send(account, intent, client)
            except errors.FloodWaitError as error:
                await self.db.sql(
                    "UPDATE telegram_outgoing_intents SET next_attempt_at=now()+%s*interval '1 second',error_code='FLOOD_WAIT',attempts=attempts+1 WHERE id=%s",
                    (error.seconds, intent["id"]),
                )
            except (ConnectionError, OSError, asyncio.TimeoutError):
                await self.db.sql(
                    "UPDATE telegram_outgoing_intents SET next_attempt_at=now()+interval '3 seconds',error_code='TRANSPORT_UNCERTAIN',attempts=attempts+1 WHERE id=%s",
                    (intent["id"],),
                )
            except errors.RandomIdDuplicateError:
                await self.db.sql(
                    "UPDATE telegram_outgoing_intents SET next_attempt_at=now()+interval '3 seconds',error_code='SEND_UNCERTAIN',attempts=attempts+1,status=CASE WHEN attempts>=5 THEN 'error' ELSE status END WHERE id=%s",
                    (intent["id"],),
                )
            except Exception as error:
                code = type(error).__name__
                await self.db.sql(
                    "UPDATE telegram_outgoing_intents SET status='error',error_code=%s,attempts=attempts+1 WHERE id=%s",
                    (code, intent["id"]),
                )
                await self.db.sql(
                    "UPDATE messenger_messages SET status='error' WHERE project_id=%s AND uuid=%s AND source_name='telegram'",
                    (account["project_id"], intent["entity_uuid"]),
                )

    async def send(self, account: dict, intent: dict, client: typing.Any) -> None:
        peers = await self.db.sql(
            "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s AND peer_key=%s AND selected",
            (account["uuid"], intent["peer_key"]),
        )
        if not peers:
            raise ValueError("Telegram chat is not selected")
        peer = peers[0]
        target = self.input_peer(account, peer)
        if intent["entity_type"] == "notifications":
            muted = intent["data"]["notification_mode"] != "all_messages"
            notify_peer = types.InputNotifyPeer(target)
            current = await client(
                functions.account.GetNotifySettingsRequest(notify_peer)
            )
            await client(
                functions.account.UpdateNotifySettingsRequest(
                    peer=notify_peer,
                    settings=types.InputPeerNotifySettings(
                        show_previews=current.show_previews,
                        silent=current.silent,
                        mute_until=2147483647 if muted else 0,
                        sound=current.other_sound,
                        stories_muted=current.stories_muted,
                        stories_hide_sender=current.stories_hide_sender,
                        stories_sound=current.stories_other_sound,
                    ),
                )
            )
            await self.finish_send(account, intent, None, notify_muted=muted)
            return
        messages = await self.db.sql(
            "SELECT * FROM messenger_messages WHERE project_id=%s AND uuid=%s AND provider_uuid=%s AND source_name='telegram'",
            (account["project_id"], intent["entity_uuid"], account["provider_uuid"]),
        )
        if not messages:
            remote_id = None
            if intent["entity_type"] == "message" and intent["operation"] == "delete":
                identity = intent["data"]["provider_identity"]
                if identity:
                    remote_id = int(identity.rsplit(":message:", 1)[1])
                else:
                    delivered = await self.db.sql(
                        "SELECT (request->>'receipt_id')::bigint AS remote_id FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_uuid=%s AND peer_key=%s AND id<%s AND request->>'receipt_id' IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (
                            account["uuid"],
                            intent["entity_uuid"],
                            intent["peer_key"],
                            intent["id"],
                        ),
                    )
                    remote_id = delivered[0]["remote_id"] if delivered else None
                if remote_id is not None:
                    await client.delete_messages(target, [remote_id], revoke=True)
            await self.finish_send(account, intent, remote_id)
            return
        message = messages[0]
        identity = message["provider_identity"] or intent["data"]["provider_identity"]
        remote_id = int(identity.rsplit(":message:", 1)[1]) if identity else None
        request = intent["request"] or {}
        if request.get("receipt_id"):
            await self.finish_send(account, intent, request["receipt_id"])
            return
        if (
            intent["entity_type"] == "message"
            and message["deleted_at"]
            and remote_id is None
        ):
            await self.finish_send(account, intent, None)
            return
        if intent["entity_type"] == "read":
            if remote_id is not None:
                if peer["peer_kind"] == "channel":
                    await client(
                        functions.channels.ReadHistoryRequest(
                            channel=utils.get_input_channel(target), max_id=remote_id
                        )
                    )
                else:
                    await client(
                        functions.messages.ReadHistoryRequest(
                            peer=target, max_id=remote_id
                        )
                    )
                await self.read_remote(account, peer, remote_id)
            await self.finish_send(account, intent, remote_id)
            return
        if intent["entity_type"] == "reaction":
            if remote_id is None:
                raise ValueError("Telegram message has no provider identity")
            facts = await self.db.sql(
                "SELECT emoji_name FROM messenger_message_reaction_facts WHERE project_id=%s AND canonical_message_uuid=%s AND user_uuid=%s ORDER BY emoji_name",
                (
                    account["project_id"],
                    intent["entity_uuid"],
                    account["owner_user_uuid"],
                ),
            )
            reactions = []
            for fact in facts:
                code = emoji.code_for_name(fact["emoji_name"])
                value = (
                    "".join(chr(int(point, 16)) for point in code.split("-"))
                    if code
                    else fact["emoji_name"]
                )
                reactions.append(types.ReactionEmoji(value))
            await client(
                functions.messages.SendReactionRequest(
                    peer=target, msg_id=remote_id, reaction=reactions
                )
            )
            await self.finish_send(account, intent, remote_id)
            return
        if not peer["can_send"] and remote_id is None:
            raise PermissionError("Telegram chat is read-only")
        if intent["operation"] == "delete":
            if remote_id is not None:
                await client.delete_messages(target, [remote_id], revoke=True)
            await self.finish_send(account, intent, remote_id)
            return
        text = intent["data"]["payload"]["content"]
        references: dict = {}
        plain, entities = conversion.to_telegram(text.strip(), references=references)
        for mention in references.get("mentions", []):
            mapping = await self.db.sql(
                "SELECT external_account_id FROM m_external_accounts_v2 WHERE project_id=%s AND %s IN (owner_user_uuid,messenger_uuid_v5(provider_uuid,'user:'||external_account_id)) AND provider='telegram' AND provider_uuid=%s AND external_account_id IS NOT NULL",
                (
                    account["project_id"],
                    uuid.UUID(mention["user_uuid"]),
                    account["provider_uuid"],
                ),
            )
            if not mapping or int(mapping[0]["external_account_id"]) <= 0:
                raise ValueError("Mention has no Telegram user identity")
            person = await client.get_input_entity(
                types.PeerUser(int(mapping[0]["external_account_id"]))
            )
            entities.append(
                types.InputMessageEntityMentionName(
                    offset=mention["offset"],
                    length=mention["length"],
                    user_id=utils.get_input_user(person),
                )
            )
        file_ids = references.get("files", [])
        if len(file_ids) > 1:
            raise ValueError("Telegram messages support one outgoing attachment")
        topic_identity = intent["data"].get("topic_identity")
        topic_id = int(topic_identity.rsplit(":topic:", 1)[1]) if topic_identity else 0
        reply = (
            types.InputReplyToMessage(reply_to_msg_id=topic_id) if topic_id else None
        )
        quoted = references.get("reply")
        if quoted:
            linked = await self.db.sql(
                "SELECT message.provider_identity FROM messenger_message_placements placement JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid WHERE placement.project_id=%s AND placement.uuid=%s AND placement.stream_uuid=%s AND message.provider_uuid=%s AND message.source_name='telegram'",
                (
                    account["project_id"],
                    uuid.UUID(quoted),
                    peer["stream_uuid"],
                    account["provider_uuid"],
                ),
            )
            if linked and linked[0]["provider_identity"]:
                reply = types.InputReplyToMessage(
                    reply_to_msg_id=int(
                        linked[0]["provider_identity"].rsplit(":message:", 1)[1]
                    )
                )
        media = None
        if file_ids:
            files = await self.db.sql(
                "SELECT file.* FROM m_workspace_files file JOIN m_workspace_visible_files_v1 visible ON visible.uuid=file.uuid WHERE file.uuid=%s AND file.project_id=%s AND visible.viewer_user_uuid=%s",
                (
                    uuid.UUID(file_ids[0]),
                    account["project_id"],
                    account["owner_user_uuid"],
                ),
            )
            if not files:
                raise PermissionError("Telegram attachment access denied")
            file = files[0]
            content = await asyncio.to_thread(
                file_storage.read_workspace_file,
                file["uuid"],
                file["storage_type"],
                file["storage_object_id"],
            )
            if hashlib.sha256(content).hexdigest() != file["hash"]:
                raise ValueError("Telegram attachment digest mismatch")
            uploaded = await client.upload_file(content, file_name=file["name"])
            media = (
                types.InputMediaUploadedPhoto(uploaded)
                if file["content_type"] in ("image/jpeg", "image/png")
                else types.InputMediaUploadedDocument(
                    uploaded,
                    file["content_type"],
                    [types.DocumentAttributeFilename(file["name"])],
                )
            )
        # Persist the immutable send identifier before any remote message write.
        await self.db.sql(
            "UPDATE telegram_outgoing_intents SET status='checking',request=coalesce(request,%s),attempts=attempts+1 WHERE id=%s",
            (
                messenger.json_value({"text": plain, "random_id": intent["random_id"]}),
                intent["id"],
            ),
        )
        if remote_id is not None:
            response = await client(
                functions.messages.EditMessageRequest(
                    peer=target,
                    id=remote_id,
                    message=plain,
                    entities=entities,
                    media=media,
                )
            )
        elif media is not None:
            response = await client(
                functions.messages.SendMediaRequest(
                    peer=target,
                    media=media,
                    message=plain,
                    random_id=intent["random_id"],
                    entities=entities,
                    reply_to=reply,
                )
            )
        else:
            response = await client(
                functions.messages.SendMessageRequest(
                    peer=target,
                    message=plain,
                    random_id=intent["random_id"],
                    entities=entities,
                    reply_to=reply,
                )
            )
        updates = getattr(response, "updates", [])
        await self.receipts(account, updates)
        if remote_id is None:
            if isinstance(response, types.UpdateShortSentMessage):
                remote_id = response.id
            else:
                remote_id = next(
                    (
                        update.id
                        for update in updates
                        if isinstance(update, types.UpdateMessageID)
                        and update.random_id == intent["random_id"]
                    ),
                    None,
                )
                if remote_id is None:
                    remote_id = next(
                        (
                            update.message.id
                            for update in updates
                            if isinstance(
                                update,
                                (types.UpdateNewMessage, types.UpdateNewChannelMessage),
                            )
                        ),
                        None,
                    )
        if remote_id is None:
            raise ConnectionError("Telegram send has no authoritative receipt")
        await self.finish_send(account, intent, remote_id)

    async def finish_send(
        self,
        account: dict,
        intent: dict,
        remote_id: int | None,
        *,
        notify_muted: bool | None = None,
    ) -> None:
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError()
            await self.lock_peer(connection, account, intent["peer_key"])
            if notify_muted is not None:
                await connection.execute(
                    "UPDATE telegram_peer_state SET notification_muted=%s,notification_inherited=false,notification_mute_until=to_timestamp(%s),notification_checked_at=now() WHERE external_account_uuid=%s AND peer_key=%s",
                    (
                        notify_muted,
                        2147483647 if notify_muted else 0,
                        account["uuid"],
                        intent["peer_key"],
                    ),
                )
            await connection.execute(
                "UPDATE telegram_outgoing_intents SET status='done',completed_at=now(),error_code=NULL,request=coalesce(request,'{}'::jsonb)||jsonb_build_object('receipt_id',%s::bigint) WHERE id=%s",
                (remote_id, intent["id"]),
            )
            if remote_id is not None and intent["entity_type"] == "message":
                identity = conversion.message_identity(
                    account["external_account_id"], intent["peer_key"], remote_id
                )
                await connection.execute(
                    "UPDATE messenger_messages SET provider_identity=%s,status='active' WHERE project_id=%s AND uuid=%s AND source_name='telegram' AND provider_uuid=%s",
                    (
                        identity,
                        account["project_id"],
                        intent["entity_uuid"],
                        account["provider_uuid"],
                    ),
                )
                placements = await messenger.rows(
                    connection,
                    "SELECT * FROM messenger_message_placements WHERE project_id=%s AND message_uuid=%s",
                    (account["project_id"], intent["entity_uuid"]),
                )
                for placement in placements:
                    await self.operations.snapshot(
                        connection,
                        account,
                        "message",
                        placement["uuid"],
                        "updated",
                        placement["stream_uuid"],
                    )
