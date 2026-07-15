import dataclasses
import enum
import uuid as uuid_module
from typing import Any


class ProviderKind(str, enum.Enum):
    ZULIP = "zulip"
    MAIL = "mail"
    CALENDAR = "calendar"


class ProviderDomain(str, enum.Enum):
    MESSENGER = "messenger"
    MAIL = "mail"
    CALENDAR = "calendar"


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class ProviderRegistration:
    provider_uuid: uuid_module.UUID
    name: str
    kinds: tuple[ProviderKind, ...]
    version: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "supported_kinds": [kind.value for kind in self.kinds],
            "version": self.version,
        }


@dataclasses.dataclass(frozen=True)
class ExternalAccount:
    uuid: uuid_module.UUID
    kind: ProviderKind
    settings: dict[str, Any]
    updated_at: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ExternalAccount":
        return cls(
            uuid=uuid_module.UUID(str(payload["uuid"])),
            kind=ProviderKind(payload["kind"]),
            settings=payload["settings"],
            updated_at=payload.get("updated_at"),
        )


@dataclasses.dataclass(frozen=True)
class ProviderCommand:
    uuid: uuid_module.UUID
    domain: ProviderDomain
    external_account_uuid: uuid_module.UUID
    operation: str
    entity_urn: str
    payload: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        domain: ProviderDomain,
    ) -> "ProviderCommand":
        return cls(
            uuid=uuid_module.UUID(str(payload["uuid"])),
            domain=domain,
            external_account_uuid=uuid_module.UUID(
                str(payload["external_account_uuid"])
            ),
            operation=payload["operation"],
            entity_urn=payload["entity_urn"],
            payload=payload.get("payload", {}),
        )


@dataclasses.dataclass(frozen=True)
class CommandResult:
    status: DeliveryStatus
    provider_external_id: str | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status.value}
        if self.provider_external_id is not None:
            payload["provider_external_id"] = self.provider_external_id
        if self.error is not None:
            payload["safe_error"] = self.error
        return payload


@dataclasses.dataclass(frozen=True)
class EntityReference:
    urn: str
    uuid: uuid_module.UUID | None
    payload: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EntityReference":
        value = payload.get("uuid")
        return cls(
            urn=payload["urn"],
            uuid=uuid_module.UUID(str(value)) if value is not None else None,
            payload=payload,
        )
