# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Owner-only Telegram login requests; the daemon owns MTProto connections."""

import datetime
import json
import uuid
import typing

from oslo_config import cfg
from restalchemy.common import exceptions

from workspace.bridges.telegram import conversion, credentials
from workspace.common import telegram_opts


def configuration() -> typing.Any:
    options = cfg.CONF[telegram_opts.DOMAIN]
    if not options.api_id or not options.api_hash or not options.secret_encryption_key:
        raise exceptions.ValidationErrorException()
    return options


def perform(session: typing.Any, account: typing.Any, values: dict) -> dict:
    if account.provider != "telegram":
        raise exceptions.ValidationErrorException()
    options = configuration()
    operation = values.get("operation", "status")
    if operation not in ("start", "status", "password", "cancel"):
        raise exceptions.ValidationErrorException()
    state = session.execute(
        "SELECT * FROM telegram_account_state WHERE external_account_uuid=%s FOR UPDATE",
        (account.uuid,),
    ).fetchone()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if operation == "start":
        generation = uuid.uuid4()
        if state and state["auth_state"] == "authorized":
            return snapshot(session, account, state, options.secret_encryption_key)
        state = session.execute(
            "INSERT INTO telegram_account_state(external_account_uuid,generation,auth_state,auth_expires_at,attempt_expires_at) "
            "VALUES(%s,%s,'starting',%s,%s) ON CONFLICT(external_account_uuid) DO UPDATE SET "
            "generation=EXCLUDED.generation,auth_state='starting',auth_expires_at=EXCLUDED.auth_expires_at,attempt_expires_at=EXCLUDED.attempt_expires_at,"
            "session_envelope=NULL,login_envelope=NULL,password_envelope=NULL,error_code=NULL,next_attempt_at=now() RETURNING *",
            (
                account.uuid,
                generation,
                now + datetime.timedelta(minutes=5),
                now + datetime.timedelta(minutes=5),
            ),
        ).fetchone()
    elif operation == "password":
        if (
            not state
            or str(state["generation"]) != values.get("generation")
            or state["auth_state"] != "password_required"
            or state["auth_expires_at"] <= now
            or not isinstance(values.get("password"), str)
            or not 1 <= len(values["password"]) <= 256
        ):
            raise exceptions.ValidationErrorException()
        encrypted = credentials.encrypt(
            account.uuid,
            {"password": values["password"], "generation": str(state["generation"])},
            options.secret_encryption_key,
        )
        state = session.execute(
            "UPDATE telegram_account_state SET password_envelope=%s::jsonb,next_attempt_at=now() "
            "WHERE external_account_uuid=%s RETURNING *",
            (json.dumps(encrypted), account.uuid),
        ).fetchone()
    elif operation == "cancel" and state and state["auth_state"] != "authorized":
        if str(state["generation"]) != values.get("generation"):
            raise exceptions.ValidationErrorException()
        state = session.execute(
            "UPDATE telegram_account_state SET generation=gen_random_uuid(),auth_state='disconnected',"
            "password_envelope=NULL,login_envelope=NULL,session_envelope=NULL,auth_expires_at=NULL,attempt_expires_at=NULL "
            "WHERE external_account_uuid=%s RETURNING *",
            (account.uuid,),
        ).fetchone()
    return snapshot(session, account, state, options.secret_encryption_key)


def snapshot(
    session: typing.Any, account: typing.Any, state: dict | None, key: str
) -> dict:
    if not state:
        return {
            "state": "disconnected",
            "generation": None,
            "dialogs": [],
            "history_enabled": True,
        }
    result = {
        "state": state["auth_state"],
        "generation": str(state["generation"]),
        "error_code": state["error_code"],
        "dialogs": [],
        "history_enabled": True,
        "catalog_error_code": state["catalog_error_code"],
        "retry_at": {
            name: conversion.timestamp(state[name]).replace("+00:00", "Z")
            if state[name]
            else None
            for name in ("history_retry_at", "catalog_retry_at", "common_retry_at")
        },
    }
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if state["auth_state"] == "qr_pending" and state["auth_expires_at"] > now:
        login = credentials.decrypt(account.uuid, state["login_envelope"], key)
        result["qr_url"] = login["url"]
        result["expires_at"] = state["auth_expires_at"].isoformat() + "Z"
    if state["auth_state"] == "authorized":
        peers = session.execute(
            "SELECT peer_key,title,peer_kind,can_send,forum,selected,history_done,"
            "imported_messages,imported_files,imported_bytes,history_started_at,first_message_at,"
            "history_finished_at,fetch_seconds,merge_seconds,file_seconds,history_retry_at,history_error_code,receive_retry_at,receive_error_code,media_retry_at,media_error_code "
            "FROM telegram_peer_state WHERE external_account_uuid=%s AND selected ORDER BY title,peer_key",
            (account.uuid,),
        ).fetchall()
        result["dialogs"] = [
            {
                name: conversion.timestamp(value).replace("+00:00", "Z")
                if isinstance(value, datetime.datetime)
                else value
                for name, value in dict(peer).items()
            }
            for peer in peers
        ]
    return result
