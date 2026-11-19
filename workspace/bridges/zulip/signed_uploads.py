# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Operator trust policy for Zulip signed attachment downloads."""

import configparser
import ipaddress
import json
import socket

import aiohttp
import yarl
from aiohttp import abc as http_abc


Origin = tuple[str, str, int]
Networks = tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
Origins = dict[Origin, Networks]


class AddressDenied(OSError):
    """The resolved storage address is outside the configured trust boundary."""

    def __init__(self) -> None:
        super().__init__("Signed upload address is not allowed")


def origin(url: yarl.URL) -> Origin:
    if (
        url.scheme not in ("http", "https")
        or not url.raw_host
        or not url.port
        or url.raw_user is not None
        or url.raw_password is not None
        or url.raw_fragment
    ):
        raise ValueError("Invalid signed upload URL")
    return url.scheme, url.raw_host.lower(), url.port


def configured_origins(config: configparser.ConfigParser) -> Origins:
    configured = json.loads(
        config.get("zulip_signed_uploads", "allowed_origins", fallback="{}")
    )
    if not isinstance(configured, dict):
        raise ValueError("Signed upload allowed_origins must be a JSON object")
    origins: Origins = {}
    for value, addresses in configured.items():
        url = yarl.URL(value)
        key = origin(url)
        if (
            url.raw_path != "/"
            or url.raw_query_string
            or key in origins
            or not isinstance(addresses, list)
            or any(not isinstance(address, str) for address in addresses)
            or (url.scheme == "http" and not addresses)
        ):
            raise ValueError("Invalid signed upload origin or address list")
        origins[key] = tuple(ipaddress.ip_network(address) for address in addresses)
    return origins


class StorageResolver(aiohttp.ThreadedResolver):
    def __init__(self, port: int, networks: Networks) -> None:
        super().__init__()
        self.port = port
        self.networks = networks

    def allows(self, host: str, port: int) -> bool:
        address = ipaddress.ip_address(host.partition("%")[0])
        if (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            address = address.ipv4_mapped
        return (
            port == self.port
            and not address.is_unspecified
            and not address.is_link_local
            and not address.is_multicast
            and (not address.is_reserved or address.is_loopback)
            and not (isinstance(address, ipaddress.IPv6Address) and address.sixtofour)
            and (
                any(address in network for network in self.networks)
                if self.networks
                else address.is_global
            )
        )

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[http_abc.ResolveResult]:
        addresses = await super().resolve(host, port, family)
        allowed = [
            address
            for address in addresses
            if self.allows(address["host"], address["port"])
        ]
        if not allowed:
            raise AddressDenied()
        return allowed
