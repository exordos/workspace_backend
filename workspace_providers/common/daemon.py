import abc
import datetime
import logging
import threading
import time
import uuid

from workspace_providers import __version__
from workspace_providers.common import backoff
from workspace_providers.common import client
from workspace_providers.common import models
from workspace_providers.common import redaction
from workspace_providers.common import state


LOG = logging.getLogger(__name__)


class ProviderDaemon(abc.ABC):
    provider_kind: models.ProviderKind
    provider_domain: models.ProviderDomain

    def __init__(
        self,
        provider_uuid: uuid.UUID,
        name: str,
        service_client: client.WorkspaceServiceClient,
        repository: state.PostgresStateRepository,
        poll_interval: float = 5.0,
        sleep=time.sleep,
        command_retry_policy: backoff.RetryPolicy | None = None,
        now=None,
    ):
        self.provider_uuid = provider_uuid
        self.name = name
        self.client = service_client
        self.repository = repository
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.command_retry_policy = command_retry_policy or backoff.RetryPolicy()
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._stopped = threading.Event()
        self._bootstrapped = False

    def stop(self) -> None:
        self._stopped.set()

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        self.repository.bootstrap()
        self.repository.save_runtime(self.provider_uuid, self.client.base_url)
        registration = models.ProviderRegistration(
            provider_uuid=self.provider_uuid,
            name=self.name,
            kinds=(self.provider_kind,),
            version=__version__,
        )
        self.client.register(registration)
        self._bootstrapped = True

    def run_once(self) -> None:
        self.bootstrap()
        accounts = self.client.list_external_accounts(self.provider_kind)
        for account in accounts:
            try:
                self.sync_account(account)
            except Exception as exc:
                error = redaction.safe_error(exc)
                LOG.exception(
                    "Provider account synchronization failed for %s: %s",
                    account.uuid,
                    error,
                )
                self.repository.save_account(
                    account.uuid,
                    account.settings,
                    "failed",
                    error,
                )
                self.client.report_external_account_status(
                    account.uuid,
                    "unavailable",
                    safe_error=error,
                )
        for command in self.client.poll_commands(self.provider_domain):
            self.process_command(command, accounts)

    def run(self) -> None:
        while not self._stopped.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                LOG.exception("Provider iteration failed")
            elapsed = time.monotonic() - started
            self._stopped.wait(max(0.0, self.poll_interval - elapsed))

    def process_command(
        self,
        command: models.ProviderCommand,
        accounts: list[models.ExternalAccount],
    ) -> None:
        """Process a command with persistent at-least-once retry semantics.

        The local pending row is written before the remote side effect.  A
        process crash therefore leaves work eligible for replay.  Transient
        errors remain pending with capped exponential backoff; explicit failed
        results and permanent client errors are terminal.
        """
        existing = self.repository.get_command(command.uuid)
        if existing is not None and existing["status"] in (
            models.DeliveryStatus.DELIVERED.value,
            models.DeliveryStatus.FAILED.value,
        ):
            result = models.CommandResult(
                status=models.DeliveryStatus(existing["status"]),
                provider_external_id=existing["external_id"],
                error=existing["last_error"],
            )
            self.client.report_command_result(command.uuid, command.domain, result)
            return
        now = self.now()
        if existing is not None and not self._command_retry_due(existing, now):
            return
        attempts = 1 if existing is None else existing["attempts"] + 1
        self.repository.save_command(
            command.uuid,
            models.DeliveryStatus.PENDING.value,
            attempts,
        )
        try:
            result = self.handle_command(command, accounts)
        except Exception as exc:
            error = redaction.safe_error(exc)
            if not self._is_permanent_command_error(exc):
                retry_at = now + datetime.timedelta(
                    seconds=self.command_retry_policy.delay(attempts),
                )
                self.repository.save_command(
                    command.uuid,
                    models.DeliveryStatus.PENDING.value,
                    attempts,
                    error=error,
                    next_retry_at=retry_at,
                )
                return
            result = models.CommandResult(models.DeliveryStatus.FAILED, error=error)
        self.repository.save_command(
            command.uuid,
            result.status.value,
            attempts,
            external_id=result.provider_external_id,
            error=result.error,
        )
        if result.status is not models.DeliveryStatus.PENDING:
            self.client.report_command_result(command.uuid, command.domain, result)

    @staticmethod
    def _command_retry_due(existing, now):
        retry_at = existing.get("next_retry_at")
        if retry_at is None:
            return True
        if isinstance(retry_at, str):
            retry_at = datetime.datetime.fromisoformat(
                retry_at.replace("Z", "+00:00"),
            )
        return retry_at <= now

    def _is_permanent_command_error(self, exc):
        if isinstance(exc, ValueError):
            return True
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if status_code is None:
            return False
        return (
            400 <= status_code < 500
            and status_code not in self.command_retry_policy.retry_statuses
        )

    @abc.abstractmethod
    def sync_account(self, account: models.ExternalAccount) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def handle_command(
        self,
        command: models.ProviderCommand,
        accounts: list[models.ExternalAccount],
    ) -> models.CommandResult:
        raise NotImplementedError
