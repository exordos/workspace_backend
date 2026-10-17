# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
import logging

from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip.dm import models


LOG = logging.getLogger(__name__)


class HistoryImportService(basic.BasicService):
    def _process_task(self) -> None:
        for task in self._get_tasks():
            self._execute_task(task)

    def _get_tasks(self) -> list[models.Task]:
        now = datetime.datetime.now(datetime.timezone.utc)
        return models.Task.objects.get_all(
            filters=filters.AND(
                {
                    "status": filters.In(
                        [models.TaskStatus.NEW.value, models.TaskStatus.RETRY.value]
                    )
                },
                filters.OR(
                    {"not_before_at": filters.Is(None)},
                    {"not_before_at": filters.LE(now)},
                ),
                filters.OR(
                    {"next_retry_at": filters.Is(None)},
                    {"next_retry_at": filters.LE(now)},
                ),
                filters.OR(
                    {"lease_expires_at": filters.Is(None)},
                    {"lease_expires_at": filters.LE(now)},
                ),
            ),
            order_by={"priority": "desc", "created_at": "asc", "uuid": "asc"},
            limit=1,
            locked=True,
        )

    def _execute_task(self, task: models.Task) -> None:
        task.execute()
        LOG.info(
            "History import task %s finished with status %s", task.uuid, task.status
        )

    def _iteration(self) -> None:
        with contexts.Context().session_manager():
            self._process_task()
