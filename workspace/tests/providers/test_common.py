import datetime
import json
import uuid

import pytest
import requests

from workspace_providers.common import backoff
from workspace_providers.common import cli
from workspace_providers.common import client
from workspace_providers.common import daemon
from workspace_providers.common import models
from workspace_providers.common import reconciliation
from workspace_providers.common import redaction
from workspace_providers.common import state


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content if content is not None else json.dumps(payload).encode()
        self.text = self.content.decode(errors="replace")
        self.reason = "fake"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_provider_cli_accepts_short_deployment_name(monkeypatch):
    monkeypatch.setenv("WORKSPACE_PROVIDER_NAME", "Mail.ru")

    args = cli.parser("Mail provider", "Mail").parse_args([])

    assert args.provider_name == "Mail.ru"
    assert (
        cli.parser("Mail provider", "Mail")
        .parse_args(["--provider-name", "Corporate Mail"])
        .provider_name
        == "Corporate Mail"
    )


def test_service_client_uses_strict_provider_contract():
    provider_uuid = uuid.uuid4()
    account_uuid = uuid.uuid4()
    blob_uuid = uuid.uuid4()
    entity_payload = {"urn": f"urn:message:{uuid.uuid4()}", "uuid": str(uuid.uuid4())}
    session = FakeSession(
        [
            FakeResponse(payload={"uuid": str(provider_uuid)}),
            FakeResponse(
                payload=[
                    {
                        "uuid": str(account_uuid),
                        "kind": "mail",
                        "settings": {"credentials": {"password": "secret"}},
                    }
                ]
            ),
            FakeResponse(payload=entity_payload),
            FakeResponse(payload={"status": "confirmed"}),
            FakeResponse(
                payload={
                    "uuid": str(blob_uuid),
                    "urn": f"urn:file:{blob_uuid}",
                }
            ),
            FakeResponse(payload=entity_payload),
            FakeResponse(content=b"blob-content"),
            FakeResponse(
                payload=[
                    {
                        "uuid": str(uuid.uuid4()),
                        "external_account_uuid": str(account_uuid),
                        "operation": "message.send",
                        "entity_urn": f"urn:mail-message:{uuid.uuid4()}",
                        "payload": {"subject": "Hello"},
                    }
                ]
            ),
            FakeResponse(payload={"status": "delivered"}),
            FakeResponse(payload=None, content=b""),
        ]
    )
    service = client.WorkspaceServiceClient(
        "https://workspace.example.com",
        provider_uuid,
        session=session,
        retry_policy=backoff.RetryPolicy(attempts=1),
    )
    service.register(
        models.ProviderRegistration(
            provider_uuid,
            "Mail",
            (models.ProviderKind.MAIL,),
            "1.0",
        )
    )
    accounts = service.list_external_accounts(models.ProviderKind.MAIL)
    reference = service.upsert_mail(
        "messages",
        "folder/42",
        account_uuid,
        {"subject": "Hello"},
    )
    service.report_external_account_status(account_uuid, "confirmed")
    blob = service.upload_blob(
        account_uuid, "note.txt", "text/plain", b"content", "sha256"
    )
    attachment = {
        "urn": blob.urn,
        "name": "note.txt",
        "content_type": "text/plain",
        "content_id": None,
        "size_bytes": 7,
        "hash": "sha256",
    }
    service.upsert_mail(
        "messages",
        "folder/43",
        account_uuid,
        {"subject": "With attachment", "attachments": [attachment]},
    )
    assert service.download_blob(blob.urn) == b"blob-content"
    command = service.poll_commands(models.ProviderDomain.MAIL)[0]
    service.report_command_result(
        command.uuid,
        command.domain,
        models.CommandResult(
            models.DeliveryStatus.DELIVERED,
            provider_external_id="remote-42",
        ),
    )
    service.delete_entity(
        models.ProviderDomain.MAIL, "messages", "folder/42", account_uuid
    )
    assert accounts[0].uuid == account_uuid
    assert reference.urn == entity_payload["urn"]
    registration = session.calls[0]
    assert registration[1].endswith(
        f"/api/workspace-service/v1/providers/{provider_uuid}"
    )
    assert registration[2]["json"] == {
        "name": "Mail",
        "supported_kinds": ["mail"],
        "version": "1.0",
    }
    assert session.calls[1][1].endswith(
        f"/providers/{provider_uuid}/external_accounts/"
    )
    assert session.calls[1][0] == "GET"
    assert session.calls[1][2]["params"] == {
        "account_type": "mail",
        "page_limit": 200,
    }
    entity_call = session.calls[2]
    expected_uuid = service.entity_uuid(
        models.ProviderDomain.MAIL,
        "messages",
        account_uuid,
        "folder/42",
    )
    assert entity_call[1].endswith(f"/mail/messages/{expected_uuid}")
    assert entity_call[2]["json"] == {
        "external_account_uuid": str(account_uuid),
        "provider_external_id": "folder/42",
        "subject": "Hello",
    }
    status_call = session.calls[3]
    assert status_call[0] == "POST"
    assert status_call[1].endswith(
        f"/external_accounts/{account_uuid}/actions/status/invoke"
    )
    assert status_call[2]["json"] == {"status": "confirmed"}
    upload_call = session.calls[4]
    assert upload_call[0] == "POST"
    assert upload_call[1].endswith(f"/providers/{provider_uuid}/blobs/")
    assert upload_call[2]["data"] == {
        "external_account_uuid": str(account_uuid),
        "name": "note.txt",
        "content_type": "text/plain",
        "hash": "sha256",
    }
    assert upload_call[2]["files"]["file"] == (
        "note.txt",
        b"content",
        "text/plain",
    )
    attachment_call = session.calls[5]
    assert attachment_call[0] == "PUT"
    assert attachment_call[2]["json"]["attachments"] == [attachment]
    assert attachment_call[2]["json"]["external_account_uuid"] == str(account_uuid)
    download_call = session.calls[6]
    assert download_call[0] == "GET"
    assert download_call[1].endswith(
        f"/providers/{provider_uuid}/blobs/{blob.uuid}/actions/download"
    )
    assert "params" not in download_call[2]
    poll_call = session.calls[7]
    assert poll_call[0] == "GET"
    assert poll_call[1].endswith(f"/providers/{provider_uuid}/mail/commands/")
    assert poll_call[2]["params"] == {"status": "pending", "page_limit": 100}
    assert command.external_account_uuid == account_uuid
    result_call = session.calls[8]
    assert result_call[0] == "POST"
    assert result_call[1].endswith(
        f"/mail/commands/{command.uuid}/actions/result/invoke"
    )
    assert result_call[2]["json"] == {
        "status": "delivered",
        "provider_external_id": "remote-42",
    }
    delete_call = session.calls[9]
    assert delete_call[0] == "DELETE"
    assert delete_call[2]["params"] == {"external_account_uuid": str(account_uuid)}


def test_service_client_retries_transport_errors_without_secrets():
    session = FakeSession(
        [
            requests.ConnectionError("password=very-secret"),
            FakeResponse(payload=[]),
        ]
    )
    service = client.WorkspaceServiceClient(
        "https://workspace.example.com",
        uuid.uuid4(),
        session=session,
        sleep=lambda value: None,
        retry_policy=backoff.RetryPolicy(attempts=2, jitter_ratio=0),
    )
    assert service.poll_commands(models.ProviderDomain.MAIL) == []
    assert len(session.calls) == 2
    assert redaction.safe_error("password=very-secret") == "password=***"


def test_dynamic_reconciliation_expands_on_drift_and_backs_off_when_clean():
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    scheduler = reconciliation.DynamicReconciliationScheduler(
        min_interval_seconds=10,
        max_interval_seconds=1000,
        max_depth=16,
    )
    partition = reconciliation.ReconciliationPartition(
        account_uuid=str(uuid.uuid4()),
        entity_kind="mail_message",
        partition_key="hot",
        depth=2,
        interval_seconds=100,
        next_due_at=now,
    )
    assert scheduler.select([partition], now, budget=1) == [partition]
    drifted = scheduler.complete(partition, now, mismatches=3, actual_cost=2)
    assert drifted.depth == 4
    assert drifted.interval_seconds == 50
    assert drifted.mismatch_score == 3
    clean = drifted
    for index in range(3):
        clean = scheduler.complete(
            clean,
            now + datetime.timedelta(seconds=index + 1),
            mismatches=0,
            actual_cost=1,
        )
    assert clean.depth == 2
    assert clean.interval_seconds > drifted.interval_seconds
    assert clean.mismatch_score < drifted.mismatch_score


class FakeConnection:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters=None, **kwargs):
        self.statements.append((statement, parameters, kwargs))
        return self


class RuntimeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters):
        self.statement = statement

    def fetchone(self):
        return self.connection.runtime


class RuntimeConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.runtime = None

    def cursor(self, **kwargs):
        return RuntimeCursor(self)

    def execute(self, statement, parameters=None, **kwargs):
        super().execute(statement, parameters, **kwargs)
        if "INSERT INTO provider_runtime" in statement:
            self.runtime = {
                "provider_uuid": parameters[0],
                "backend_url": parameters[1],
                "provider_kind": parameters[2],
            }
        return self


def test_provider_database_bootstrap_uses_only_provider_owned_schema():
    connection = FakeConnection()
    repository = state.PostgresStateRepository(
        "postgresql://provider-db",
        "mail",
        connect=lambda url: connection,
    )
    repository.bootstrap()
    combined = "\n".join(statement for statement, _, _ in connection.statements)
    assert "mail_folder_states" in combined
    assert "mail_message_states" in combined
    assert "m_workspace_" not in combined
    assert "m_mail_" not in combined
    assert "singleton BOOLEAN PRIMARY KEY" in combined
    assert "ADD COLUMN IF NOT EXISTS next_retry_at" in combined


def test_provider_database_rejects_second_runtime_identity():
    connection = RuntimeConnection()
    repository = state.PostgresStateRepository(
        "postgresql://provider-db",
        "mail",
        connect=lambda url: connection,
    )
    first_uuid = uuid.uuid4()
    repository.save_runtime(first_uuid, "https://workspace.example.com")
    repository.save_runtime(first_uuid, "https://workspace.example.com")
    try:
        repository.save_runtime(uuid.uuid4(), "https://workspace.example.com")
    except RuntimeError as exc:
        assert "another runtime identity" in str(exc)
    else:
        raise AssertionError("second provider identity was accepted")


class CommandStateRepository:
    def __init__(self):
        self.commands = {}
        self.history = []

    def get_command(self, command_uuid):
        return self.commands.get(command_uuid)

    def save_command(
        self,
        command_uuid,
        status,
        attempts,
        external_id=None,
        result=None,
        error=None,
        next_retry_at=None,
    ):
        row = {
            "command_uuid": command_uuid,
            "status": status,
            "attempts": attempts,
            "external_id": external_id,
            "result": result,
            "last_error": error,
            "next_retry_at": next_retry_at,
        }
        self.commands[command_uuid] = row
        self.history.append(row.copy())


class CommandService:
    base_url = "https://workspace.example.com"

    def __init__(self):
        self.results = []

    def report_command_result(self, command_uuid, domain, result):
        self.results.append((command_uuid, domain, result))


class CommandDaemon(daemon.ProviderDaemon):
    provider_kind = models.ProviderKind.MAIL
    provider_domain = models.ProviderDomain.MAIL

    def __init__(self, outcomes, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = list(outcomes)
        self.handle_calls = 0

    def sync_account(self, account):
        raise AssertionError("unexpected account synchronization")

    def handle_command(self, command, accounts):
        self.handle_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def provider_command():
    return models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.MAIL,
        uuid.uuid4(),
        "message.send",
        f"urn:mail-message:{uuid.uuid4()}",
        {"subject": "Hello"},
    )


def command_daemon(outcomes, repository, service, clock):
    return CommandDaemon(
        outcomes,
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        command_retry_policy=backoff.RetryPolicy(
            attempts=1,
            base_delay=10,
            max_delay=10,
            jitter_ratio=0,
        ),
        now=lambda: clock[0],
    )


def test_provider_daemon_retries_transient_command_after_persisted_backoff():
    now = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    clock = [now]
    repository = CommandStateRepository()
    service = CommandService()
    command = provider_command()
    delivered = models.CommandResult(
        models.DeliveryStatus.DELIVERED,
        provider_external_id="remote-42",
    )
    provider = command_daemon(
        [requests.ConnectionError("token=secret"), delivered],
        repository,
        service,
        clock,
    )

    provider.process_command(command, [])

    pending = repository.commands[command.uuid]
    assert pending["status"] == models.DeliveryStatus.PENDING.value
    assert pending["attempts"] == 1
    assert pending["next_retry_at"] == now + datetime.timedelta(seconds=10)
    assert "secret" not in pending["last_error"]
    assert service.results == []

    provider.process_command(command, [])
    assert provider.handle_calls == 1
    assert repository.commands[command.uuid] == pending

    clock[0] = pending["next_retry_at"]
    provider.process_command(command, [])

    assert provider.handle_calls == 2
    assert repository.commands[command.uuid]["status"] == (
        models.DeliveryStatus.DELIVERED.value
    )
    assert repository.commands[command.uuid]["attempts"] == 2
    assert service.results == [(command.uuid, command.domain, delivered)]


def test_provider_daemon_marks_permanent_4xx_terminal_and_replays_result():
    clock = [datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)]
    repository = CommandStateRepository()
    service = CommandService()
    command = provider_command()
    provider = command_daemon(
        [client.ServiceApiError(404, "unknown entity")],
        repository,
        service,
        clock,
    )

    provider.process_command(command, [])

    failed = repository.commands[command.uuid]
    assert failed["status"] == models.DeliveryStatus.FAILED.value
    assert failed["attempts"] == 1
    assert failed["next_retry_at"] is None
    assert service.results[0][2].status is models.DeliveryStatus.FAILED

    provider.process_command(command, [])

    assert provider.handle_calls == 1
    assert len(service.results) == 2
    assert service.results[1][2] == service.results[0][2]


def test_provider_daemon_persists_pending_before_side_effect_for_crash_replay():
    clock = [datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)]
    repository = CommandStateRepository()
    service = CommandService()
    command = provider_command()
    delivered = models.CommandResult(models.DeliveryStatus.DELIVERED)
    provider = command_daemon(
        [KeyboardInterrupt(), delivered],
        repository,
        service,
        clock,
    )

    with pytest.raises(KeyboardInterrupt):
        provider.process_command(command, [])

    pending = repository.commands[command.uuid]
    assert pending["status"] == models.DeliveryStatus.PENDING.value
    assert pending["attempts"] == 1
    assert pending["next_retry_at"] is None
    assert service.results == []

    provider.process_command(command, [])

    assert repository.commands[command.uuid]["status"] == (
        models.DeliveryStatus.DELIVERED.value
    )
    assert repository.commands[command.uuid]["attempts"] == 2
    assert service.results == [(command.uuid, command.domain, delivered)]
