import dataclasses
import re
from typing import Any


SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credentials",
        "password",
        "secret",
        "secret_key",
        "token",
    }
)
SECRET_PATTERN = re.compile(
    r"(?i)(authorization|password|token|api[_-]?key|secret)\s*[:=]\s*([^\s,;]+)"
)
REDACTED = "***"


def redact(value: Any, key: str | None = None) -> Any:
    """Return a log-safe copy of a nested value."""
    if key is not None and key.lower() in SECRET_KEYS:
        return REDACTED
    if dataclasses.is_dataclass(value):
        return redact(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {
            str(item_key): redact(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", value)
    return value


def safe_error(error: BaseException | str, limit: int = 2048) -> str:
    return str(redact(str(error)))[:limit]
