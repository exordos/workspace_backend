# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

_CAPABILITIES = {
    "zulip": ("messenger",),
    "mail": ("mail",),
    "calendar": ("calendar",),
    "google": ("mail", "calendar"),
    "microsoft": ("mail", "calendar"),
}


def for_provider(provider: str) -> list[str]:
    return list(_CAPABILITIES.get(provider, ()))


def providers_with(capability: str) -> list[str]:
    return [
        provider for provider, values in _CAPABILITIES.items() if capability in values
    ]
