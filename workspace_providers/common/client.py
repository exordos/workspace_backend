import pathlib
import time
import uuid
from collections.abc import Iterable
from typing import Any

import requests

from workspace_providers.common import backoff
from workspace_providers.common import models
from workspace_providers.common import redaction


DEFAULT_API_PREFIX = "/api/workspace-service/v1"
DOMAIN_RESOURCES = {
    models.ProviderDomain.MESSENGER: frozenset(
        {"users", "streams", "topics", "messages", "reactions", "files"}
    ),
    models.ProviderDomain.MAIL: frozenset({"folders", "messages", "attachments"}),
    models.ProviderDomain.CALENDAR: frozenset({"calendars", "events"}),
}


class ServiceApiError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class WorkspaceServiceClient:
    """Synchronous client for the trusted Workspace service API.

    Authentication is deliberately absent in v1.  Every route contains the
    configured provider UUID and the server enforces ownership by that UUID.
    """

    def __init__(
        self,
        base_url: str,
        provider_uuid: uuid.UUID,
        api_prefix: str = DEFAULT_API_PREFIX,
        session: requests.Session | None = None,
        retry_policy: backoff.RetryPolicy | None = None,
        timeout: float = 30.0,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.provider_uuid = provider_uuid
        self.api_prefix = "/" + api_prefix.strip("/")
        self.session = session or requests.Session()
        self.retry_policy = retry_policy or backoff.RetryPolicy()
        self.timeout = timeout
        self.sleep = sleep

    @property
    def provider_path(self) -> str:
        return f"{self.api_prefix}/providers/{self.provider_uuid}"

    def _url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_error = None
        for attempt in range(1, self.retry_policy.attempts + 1):
            try:
                response = self.session.request(method, self._url(path), **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.retry_policy.attempts:
                    raise ServiceApiError(0, redaction.safe_error(exc)) from exc
            else:
                if response.status_code not in self.retry_policy.retry_statuses:
                    if response.status_code >= 400:
                        message = response.text or response.reason
                        raise ServiceApiError(
                            response.status_code,
                            redaction.safe_error(message),
                        )
                    return response
                last_error = ServiceApiError(
                    response.status_code,
                    redaction.safe_error(response.text or response.reason),
                )
                if attempt == self.retry_policy.attempts:
                    raise last_error
            self.sleep(self.retry_policy.delay(attempt))
        raise ServiceApiError(0, redaction.safe_error(last_error or "request failed"))

    @staticmethod
    def _json(response: requests.Response) -> Any:
        if not response.content:
            return None
        return response.json()

    def register(self, registration: models.ProviderRegistration) -> dict[str, Any]:
        if registration.provider_uuid != self.provider_uuid:
            raise ValueError("registration provider UUID does not match client UUID")
        response = self._request(
            "PUT",
            self.provider_path,
            json=registration.to_payload(),
        )
        return self._json(response)

    def list_external_accounts(
        self,
        kind: models.ProviderKind,
        updated_after: str | None = None,
        limit: int = 200,
    ) -> list[models.ExternalAccount]:
        params: dict[str, Any] = {
            "account_type": kind.value,
            "page_limit": limit,
        }
        if updated_after is not None:
            params["updated_at>"] = updated_after
        response = self._request(
            "GET",
            f"{self.provider_path}/external_accounts/",
            params=params,
        )
        payload = self._json(response) or []
        return [models.ExternalAccount.from_payload(item) for item in payload]

    def report_external_account_status(
        self,
        account_uuid: uuid.UUID,
        status: str,
        safe_error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status}
        if safe_error is not None:
            payload["safe_error"] = redaction.safe_error(safe_error)
        response = self._request(
            "POST",
            f"{self.provider_path}/external_accounts/{account_uuid}/actions/status/invoke",
            json=payload,
        )
        return self._json(response)

    def upload_blob(
        self,
        account_uuid: uuid.UUID,
        name: str,
        content_type: str,
        data: bytes,
        content_hash: str | None = None,
    ) -> models.EntityReference:
        metadata = {
            "external_account_uuid": str(account_uuid),
            "name": name,
            "content_type": content_type,
        }
        if content_hash is not None:
            metadata["hash"] = content_hash
        response = self._request(
            "POST",
            f"{self.provider_path}/blobs/",
            data=metadata,
            files={"file": (pathlib.PurePath(name).name, data, content_type)},
        )
        return models.EntityReference.from_payload(self._json(response))

    def download_blob(self, urn: str) -> bytes:
        prefix = "urn:file:"
        if not urn.startswith(prefix):
            raise ValueError(f"unsupported blob URN: {urn}")
        blob_uuid = uuid.UUID(urn.removeprefix(prefix))
        response = self._request(
            "GET",
            f"{self.provider_path}/blobs/{blob_uuid}/actions/download",
        )
        return response.content

    def _validate_resource(
        self,
        domain: models.ProviderDomain,
        resource: str,
    ) -> None:
        if resource not in DOMAIN_RESOURCES[domain]:
            raise ValueError(f"unsupported {domain.value} resource: {resource}")

    def upsert_entity(
        self,
        domain: models.ProviderDomain,
        resource: str,
        provider_external_id: str,
        account_uuid: uuid.UUID,
        payload: dict[str, Any],
        *,
        entity_uuid: uuid.UUID | None = None,
    ) -> models.EntityReference:
        self._validate_resource(domain, resource)
        if entity_uuid is None:
            entity_uuid = self.entity_uuid(
                domain,
                resource,
                account_uuid,
                provider_external_id,
            )
        response = self._request(
            "PUT",
            f"{self.provider_path}/{domain.value}/{resource}/{entity_uuid}",
            json={
                "external_account_uuid": str(account_uuid),
                "provider_external_id": provider_external_id,
                **payload,
            },
        )
        return models.EntityReference.from_payload(self._json(response))

    def entity_uuid(
        self,
        domain: models.ProviderDomain,
        resource: str,
        account_uuid: uuid.UUID,
        provider_external_id: str,
    ) -> uuid.UUID:
        self._validate_resource(domain, resource)
        name = ":".join(
            (str(account_uuid), domain.value, resource, provider_external_id)
        )
        return uuid.uuid5(self.provider_uuid, name)

    def delete_entity(
        self,
        domain: models.ProviderDomain,
        resource: str,
        provider_external_id: str,
        account_uuid: uuid.UUID,
        *,
        entity_uuid: uuid.UUID | None = None,
    ) -> None:
        self._validate_resource(domain, resource)
        if entity_uuid is None:
            entity_uuid = self.entity_uuid(
                domain,
                resource,
                account_uuid,
                provider_external_id,
            )
        self._request(
            "DELETE",
            f"{self.provider_path}/{domain.value}/{resource}/{entity_uuid}",
            params={"external_account_uuid": str(account_uuid)},
        )

    def list_entities(
        self,
        domain: models.ProviderDomain,
        resource: str,
        account_uuid: uuid.UUID,
        filters: dict[str, Any] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self._validate_resource(domain, resource)
        params = dict(filters or {})
        params.update({"external_account_uuid": str(account_uuid), "page_limit": limit})
        response = self._request(
            "GET",
            f"{self.provider_path}/{domain.value}/{resource}/",
            params=params,
        )
        return self._json(response) or []

    def poll_commands(
        self,
        domain: models.ProviderDomain,
        limit: int = 100,
    ) -> list[models.ProviderCommand]:
        response = self._request(
            "GET",
            f"{self.provider_path}/{domain.value}/commands/",
            params={"status": models.DeliveryStatus.PENDING.value, "page_limit": limit},
        )
        payload = self._json(response) or []
        return [models.ProviderCommand.from_payload(item, domain) for item in payload]

    def report_command_result(
        self,
        command_uuid: uuid.UUID,
        domain: models.ProviderDomain,
        result: models.CommandResult,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"{self.provider_path}/{domain.value}/commands/{command_uuid}/actions/result/invoke",
            json=result.to_payload(),
        )
        return self._json(response)

    def upsert_messenger(
        self,
        resource: str,
        provider_external_id: str,
        account_uuid: uuid.UUID,
        payload: dict[str, Any],
        *,
        entity_uuid: uuid.UUID | None = None,
    ) -> models.EntityReference:
        return self.upsert_entity(
            models.ProviderDomain.MESSENGER,
            resource,
            provider_external_id,
            account_uuid,
            payload,
            entity_uuid=entity_uuid,
        )

    def sync_messenger_message_flags(
        self,
        message_uuid: uuid.UUID,
        account_uuid: uuid.UUID,
        *,
        read: bool,
        starred: bool,
    ) -> None:
        self._request(
            "POST",
            f"{self.provider_path}/messenger/messages/{message_uuid}"
            "/actions/flags/invoke",
            json={
                "external_account_uuid": str(account_uuid),
                "read": read,
                "starred": starred,
            },
        )

    def upsert_mail(
        self,
        resource: str,
        provider_external_id: str,
        account_uuid: uuid.UUID,
        payload: dict[str, Any],
        *,
        entity_uuid: uuid.UUID | None = None,
    ) -> models.EntityReference:
        return self.upsert_entity(
            models.ProviderDomain.MAIL,
            resource,
            provider_external_id,
            account_uuid,
            payload,
            entity_uuid=entity_uuid,
        )

    def upsert_calendar(
        self,
        resource: str,
        provider_external_id: str,
        account_uuid: uuid.UUID,
        payload: dict[str, Any],
        *,
        entity_uuid: uuid.UUID | None = None,
    ) -> models.EntityReference:
        return self.upsert_entity(
            models.ProviderDomain.CALENDAR,
            resource,
            provider_external_id,
            account_uuid,
            payload,
            entity_uuid=entity_uuid,
        )

    def delete_missing_entities(
        self,
        domain: models.ProviderDomain,
        resource: str,
        account_uuid: uuid.UUID,
        remote_keys: Iterable[str],
    ) -> list[str]:
        remote = set(remote_keys)
        deleted = []
        for entity in self.list_entities(domain, resource, account_uuid):
            key = entity["provider_external_id"]
            if key in remote:
                continue
            self.delete_entity(domain, resource, key, account_uuid)
            deleted.append(key)
        return deleted
