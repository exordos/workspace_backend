# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import dataclasses
import datetime
import smtplib
import typing
import uuid

from restalchemy.dm import filters

from workspace.mail_api import application_services
from workspace.mail_api import events as mail_events
from workspace.mail_api.dm import models as mail_models
from workspace.mail_api.dm import payloads
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.services import groupware_accounts
from workspace.services import groupware_content
from workspace.services import groupware_credentials
from workspace.services import groupware_mail


@dataclasses.dataclass(frozen=True)
class Delivery:
    intent_id: int
    message_uuid: uuid.UUID
    spec: dict[str, typing.Any] = dataclasses.field(repr=False)
    attachments: list[groupware_content.Attachment] = dataclasses.field(repr=False)
    secret: str = dataclasses.field(repr=False)


def _attachments(
    session: typing.Any,
    account: external_models.ExternalAccount,
    urns: list[str],
) -> list[groupware_content.Attachment]:
    files = []
    for urn in urns:
        match = application_services.FILE_URN_RE.fullmatch(urn)
        if match is None:
            raise ValueError("Invalid outgoing attachment reference")
        file_uuid = uuid.UUID(match.group(1))
        row = session.execute(
            """
            SELECT name, content_type, storage_type, storage_object_id
            FROM m_workspace_visible_files_v1
            WHERE uuid = %s AND project_id = %s
              AND (viewer_user_uuid = %s OR viewer_user_uuid IS NULL)
            LIMIT 1
            """,
            (file_uuid, account.project_id, account.owner_user_uuid),
        ).fetchone()
        if row is None:
            raise ValueError("Outgoing attachment is unavailable")
        data = file_storage.read_workspace_file(
            file_uuid, row["storage_type"], row["storage_object_id"]
        )
        files.append(
            groupware_content.Attachment(row["name"], row["content_type"], data)
        )
    return files


class Sender:
    def __init__(
        self, engine: typing.Any, secrets: groupware_credentials.CredentialStore
    ) -> None:
        self._engine = engine
        self._secrets = secrets

    def _finish(
        self,
        session: typing.Any,
        account: external_models.ExternalAccount,
        message_uuid: uuid.UUID,
        accepted: bool,
        error: str | None,
    ) -> None:
        resource = application_services.get_message(
            session,
            account.project_id,
            account.owner_user_uuid,
            message_uuid,
            locked=True,
        )
        resource.properties["delivery_status"].set_value_force(
            "sent" if accepted else "failed"
        )
        resource.properties["delivery_error"].set_value_force(error)
        if accepted:
            resource.properties["sent_at"].set_value_force(
                datetime.datetime.now(datetime.timezone.utc)
            )
            sent_folder = mail_models.MailFolder.objects.get_one_or_none(
                filters={
                    "project_id": filters.EQ(account.project_id),
                    "owner_user_uuid": filters.EQ(account.owner_user_uuid),
                    "external_account_uuid": filters.EQ(account.uuid),
                    "kind": filters.EQ("sent"),
                    "deleted": filters.EQ(False),
                },
                session=session,
            )
            if sent_folder is not None:
                resource.folder_uuid = sent_folder.uuid
        resource.update(session=session)
        mail_events.create_mail_event(
            resource, mail_events.MAIL_MESSAGE_UPDATED_EVENT, session
        )

    def _claim(
        self, account_uuid: uuid.UUID
    ) -> tuple[external_models.ExternalAccount, Delivery] | None:
        with self._engine.session_manager() as session:
            account = external_models.ExternalAccount.objects.get_one_or_none(
                filters={
                    "uuid": filters.EQ(account_uuid),
                    "disabled": filters.EQ(False),
                },
                session=session,
                locked=True,
            )
            if account is None:
                return None
            secret = self._secrets.load(session, account.uuid, "mail")
            if secret is None:
                return None
            row = session.execute(
                """
                SELECT id, message_uuid, payload, status FROM mail_outgoing_intents
                WHERE project_id = %s AND external_account_uuid = %s
                  AND ((status = 'new' AND next_attempt_at <= now())
                    OR (status = 'checking' AND updated_at < now() - interval '10 minutes'))
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
                """,
                (account.project_id, account.uuid),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "checking":
                self._record_error(
                    session,
                    account,
                    row["id"],
                    row["message_uuid"],
                    "Workspace could not confirm the result of an earlier SMTP attempt.",
                )
                return None
            spec = row["payload"]
            if spec["from_address"] != account.settings.get("email"):
                error = "The outgoing sender no longer matches the connected account."
            else:
                try:
                    attachments = _attachments(
                        session, account, spec["attachment_urns"]
                    )
                except (OSError, ValueError):
                    error = "An outgoing attachment is unavailable."
                else:
                    session.execute(
                        """
                        UPDATE mail_outgoing_intents SET status = 'checking',
                            attempts = attempts + 1, updated_at = now() WHERE id = %s
                        """,
                        (row["id"],),
                    )
                    return account, Delivery(
                        row["id"], row["message_uuid"], spec, attachments, secret
                    )
            self._record_error(session, account, row["id"], row["message_uuid"], error)
            return None

    def _record_error(
        self,
        session: typing.Any,
        account: external_models.ExternalAccount,
        intent_id: int,
        message_uuid: uuid.UUID,
        error: str,
    ) -> None:
        session.execute(
            """
            UPDATE mail_outgoing_intents SET status = 'error', error = %s,
                updated_at = now(), completed_at = now() WHERE id = %s
            """,
            (error, intent_id),
        )
        self._finish(session, account, message_uuid, accepted=False, error=error)

    def send_next(self, account_uuid: uuid.UUID) -> bool:
        claimed = self._claim(account_uuid)
        if claimed is None:
            return False
        account, delivery = claimed
        spec = delivery.spec
        sender = spec["from_address"]
        recipients = list(
            dict.fromkeys(
                spec["to_addresses"] + spec["cc_addresses"] + spec["bcc_addresses"]
            )
        )
        error: str | None
        try:
            outgoing = groupware_content.compose_mail(
                delivery.message_uuid,
                sender,
                spec["to_addresses"],
                spec["cc_addresses"],
                spec["subject"],
                payloads.PAYLOAD_TYPE.from_simple_type(spec["payload"]),
                delivery.attachments,
            )
            with groupware_mail.open_smtp(
                groupware_accounts.endpoint(account.settings["smtp"]),
                account.settings.get("username", sender),
                delivery.secret,
            ) as transport:
                refused = groupware_mail.send(transport, outgoing, sender, recipients)
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPResponseException,
            ValueError,
        ):
            error = "The SMTP server did not accept the outgoing message."
        except (smtplib.SMTPException, OSError):
            error = "The SMTP connection ended before Workspace could confirm delivery."
        else:
            error = (
                "The SMTP server declined one or more recipients." if refused else None
            )

        with self._engine.session_manager() as session:
            current = session.execute(
                "SELECT status FROM mail_outgoing_intents WHERE id = %s FOR UPDATE",
                (delivery.intent_id,),
            ).fetchone()
            if current is None or current["status"] != "checking":
                return True
            if error is None:
                session.execute(
                    """
                    UPDATE mail_outgoing_intents SET status = 'done',
                        completed_at = now(), updated_at = now() WHERE id = %s
                    """,
                    (delivery.intent_id,),
                )
                self._finish(
                    session, account, delivery.message_uuid, accepted=True, error=None
                )
            else:
                self._record_error(
                    session, account, delivery.intent_id, delivery.message_uuid, error
                )
        return True
