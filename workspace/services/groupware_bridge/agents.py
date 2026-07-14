# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import imaplib
import logging

import requests
from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import models
from workspace.services.groupware_bridge import calendar
from workspace.services.groupware_bridge import mail


LOG = logging.getLogger(__name__)
ACCESS_CHECK_INTERVAL = datetime.timedelta(minutes=5)


class WorkspaceGroupwareBridgeWorker(basic.BasicService):
    def __init__(self, mail_synchronizer=None, calendar_synchronizer=None, **kwargs):
        super().__init__(**kwargs)
        self.mail_synchronizer = mail_synchronizer or mail.MailSynchronizer()
        self.calendar_synchronizer = (
            calendar_synchronizer or calendar.CalendarSynchronizer()
        )

    @staticmethod
    def _accounts(account_type):
        return models.ExternalAccount.objects.get_all(
            filters={"account_type": dm_filters.EQ(account_type)},
            order_by={"created_at": "asc", "uuid": "asc"},
        )

    @staticmethod
    def _set_access(account, status, error=None):
        now = datetime.datetime.now(datetime.timezone.utc)
        values = {
            "access_status": status,
            "access_checked_at": now,
            "access_next_check_at": now + ACCESS_CHECK_INTERVAL,
            "access_last_error": error,
        }
        if status == models.ExternalAccountAccessStatus.CONFIRMED.value:
            values.update(
                {
                    "access_confirmed_at": now,
                    "access_last_error": None,
                    "status": models.ExternalAccountStatus.ACTIVE.value,
                }
            )
        account.update_dm(values=values)
        account.update()

    def _sync_account(self, account, synchronizer):
        if account.account_settings.credentials is None:
            self._set_access(
                account,
                models.ExternalAccountAccessStatus.MISSING_CREDENTIALS.value,
                "External account credentials are missing",
            )
            return
        try:
            synchronizer.sync(account)
        except (imaplib.IMAP4.error, requests.HTTPError) as exc:
            status = models.ExternalAccountAccessStatus.UNAVAILABLE.value
            if isinstance(exc, imaplib.IMAP4.error) or (
                isinstance(exc, requests.HTTPError)
                and exc.response is not None
                and exc.response.status_code in (401, 403)
            ):
                status = models.ExternalAccountAccessStatus.INVALID_CREDENTIALS.value
            self._set_access(account, status, str(exc))
            return
        except (OSError, requests.RequestException) as exc:
            self._set_access(
                account,
                models.ExternalAccountAccessStatus.UNAVAILABLE.value,
                str(exc),
            )
            return
        self._set_access(
            account,
            models.ExternalAccountAccessStatus.CONFIRMED.value,
        )

    def _run_iteration(self):
        for account in self._accounts(models.ExternalAccountType.MAIL.value):
            self._sync_account(account, self.mail_synchronizer)
        for account in self._accounts(models.ExternalAccountType.CALENDAR.value):
            self._sync_account(account, self.calendar_synchronizer)

    def _iteration(self):
        ctx = contexts.Context()
        with ctx.session_manager():
            self._run_iteration()
