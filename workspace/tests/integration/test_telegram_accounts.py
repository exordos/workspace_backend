import uuid
import logging

from workspace.bridges import messenger
from workspace.bridges.telegram import credentials

import pytest


PERMISSIONS = tuple(
    f"workspace.external_account.{action}"
    for action in ("read", "create", "update", "delete")
)


def test_telegram_authentication_is_owner_and_project_scoped(telegram_account):
    api, root = telegram_account
    path = root + "/actions/telegram_auth/invoke"
    started = api.post(path, json={"operation": "start"}, permissions=PERMISSIONS)
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "starting"
    assert started.json()["generation"]
    assert (
        api.post(
            path,
            json={"operation": "status"},
            permissions=PERMISSIONS,
            user=str(uuid.uuid4()),
        ).status_code
        == 404
    )
    assert (
        api.post(
            path,
            json={"operation": "status"},
            permissions=PERMISSIONS,
            project=str(uuid.uuid4()),
        ).status_code
        == 404
    )
    assert api.post(path, json={"operation": "status"}).status_code == 403
    assert (
        api.post(
            path,
            json={
                "operation": "password",
                "generation": str(uuid.uuid4()),
                "password": "synthetic",
            },
            permissions=PERMISSIONS,
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "field", ["api_hash", "session", "password", "auth_key", "qr_url"]
)
def test_telegram_public_settings_reject_credentials(telegram_account, field):
    api, root = telegram_account
    response = api.put(
        root, json={"settings": {field: "synthetic-secret"}}, permissions=PERMISSIONS
    )
    assert response.status_code == 400, response.text
    saved = api.get(root, permissions=PERMISSIONS).json()
    assert "synthetic-secret" not in str(saved)


def test_telegram_configuration_accepts_full_history_without_zulip_credentials(
    telegram_account,
):
    api, root = telegram_account
    response = api.put(
        root,
        json={"settings": {"history_depth": "all", "peer_keys": ["channel:123"]}},
        permissions=PERMISSIONS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {}


def test_stale_form_cannot_cancel_a_new_login_attempt(telegram_account):
    api, root = telegram_account
    path = root + "/actions/telegram_auth/invoke"
    first = api.post(path, json={"operation": "start"}, permissions=PERMISSIONS).json()
    second = api.post(path, json={"operation": "start"}, permissions=PERMISSIONS).json()
    assert first["generation"] != second["generation"]
    stale = api.post(
        path,
        json={"operation": "cancel", "generation": first["generation"]},
        permissions=PERMISSIONS,
    )
    assert stale.status_code == 400
    assert (
        api.post(path, json={"operation": "status"}, permissions=PERMISSIONS).json()[
            "generation"
        ]
        == second["generation"]
    )
    canceled = api.post(
        path,
        json={"operation": "cancel", "generation": second["generation"]},
        permissions=PERMISSIONS,
    )
    assert canceled.json()["state"] == "disconnected"


def test_authentication_password_and_qr_are_not_written_to_debug_logs(
    telegram_account, db, caplog
):
    api, root = telegram_account
    identifier = uuid.UUID(root.rsplit("/", 1)[1])
    path = root + "/actions/telegram_auth/invoke"
    started = api.post(
        path, json={"operation": "start"}, permissions=PERMISSIONS
    ).json()
    db.execute(
        "UPDATE telegram_account_state SET auth_state='password_required' WHERE external_account_uuid=%s",
        (identifier,),
    )
    with caplog.at_level(logging.DEBUG):
        response = api.post(
            path,
            json={
                "operation": "password",
                "generation": started["generation"],
                "password": "sensitive-synthetic-password",
            },
            permissions=PERMISSIONS,
        )
    assert response.status_code == 200, response.text
    assert "sensitive-synthetic-password" not in caplog.text
    assert "no-store" in response.headers.get("Cache-Control", "")
    envelope = credentials.encrypt(
        identifier,
        {"url": "tg://login?token=sensitive-synthetic-qr"},
        "synthetic-encryption-key",
    )
    db.execute(
        "UPDATE telegram_account_state SET auth_state='qr_pending',login_envelope=%s WHERE external_account_uuid=%s",
        (messenger.json_value(envelope), identifier),
    )
    with caplog.at_level(logging.DEBUG):
        response = api.post(path, json={"operation": "status"}, permissions=PERMISSIONS)
    assert response.json()["qr_url"] == "tg://login?token=sensitive-synthetic-qr"
    assert "sensitive-synthetic-qr" not in caplog.text


def test_telegram_discards_legacy_history_and_chat_selection(telegram_account):
    api, root = telegram_account
    response = api.put(
        root,
        json={
            "settings": {
                "history_depth": "30_days",
                "selection_mode": "explicit",
                "peer_keys": ["user:8"],
            }
        },
        permissions=PERMISSIONS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {}
    assert api.get(root, permissions=PERMISSIONS).json()["settings"] == {}
