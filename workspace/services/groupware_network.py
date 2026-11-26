# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import dataclasses
import ipaddress
import socket
import ssl
import typing
import urllib.parse

import requests.adapters


TIMEOUT = 15
TLSMode = typing.Literal["implicit", "starttls"]
_TRANSLATED_IPV6 = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
    ipaddress.IPv6Network("2002::/16"),
)


class EndpointError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    tls: TLSMode = "implicit"

    def __post_init__(self) -> None:
        if (
            not self.host
            or self.host != self.host.strip()
            or any(char.isspace() or char in "/@%?#\\" for char in self.host)
            or not 1 <= self.port <= 65535
            or self.tls not in ("implicit", "starttls")
        ):
            raise EndpointError("Invalid groupware server address")

    def public_addresses(self) -> tuple[str, ...]:
        addresses = tuple(
            dict.fromkeys(
                str(result[4][0])
                for result in socket.getaddrinfo(
                    self.host, self.port, type=socket.SOCK_STREAM
                )
            )
        )
        if not addresses or not all(_is_public(address) for address in addresses):
            raise EndpointError("Groupware server must resolve to public IP addresses")
        return addresses

    def connect(self, timeout: float | None, context: ssl.SSLContext) -> socket.socket:
        addresses = self.public_addresses()
        error: OSError | None = None
        for address in addresses:
            try:
                connection = socket.create_connection((address, self.port), timeout)
            except OSError as exc:
                error = exc
                continue
            if self.tls == "starttls":
                return connection
            try:
                return context.wrap_socket(connection, server_hostname=self.host)
            except BaseException:
                connection.close()
                raise
        raise OSError("Unable to connect to groupware server") from error


def _is_public(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.teredo is not None or any(
            address in network for network in _TRANSLATED_IPV6
        ):
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


class HTTPSAdapter(requests.adapters.HTTPAdapter):
    """Connect to a validated address with the original HTTP host and TLS identity."""

    def get_connection_with_tls_context(
        self,
        request: requests.PreparedRequest,
        verify: bool | str | None,
        proxies: typing.Mapping[str, str] | None = None,
        cert: str | tuple[str, str] | None = None,
    ) -> typing.Any:
        if not verify or request.url is None or proxies:
            raise EndpointError("Groupware HTTPS requires verified TLS")
        url = urllib.parse.urlsplit(request.url)
        if url.scheme != "https" or not url.hostname:
            raise EndpointError("Invalid groupware HTTPS address")
        endpoint = Endpoint(url.hostname, url.port or 443)
        addresses = endpoint.public_addresses()
        host, tls = super().build_connection_pool_key_attributes(request, verify, cert)
        host["host"] = min(addresses, key=lambda address: ":" in address)
        request.headers["Host"] = url.netloc
        return self.poolmanager.connection_from_host(
            **host,
            pool_kwargs={
                **tls,
                "assert_hostname": endpoint.host,
                "server_hostname": endpoint.host,
            },
        )
