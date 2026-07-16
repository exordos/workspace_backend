#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import argparse
import configparser
import hashlib
import hmac
import os
import pathlib
import secrets
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request


HMAC_CONTEXT = b"workspace-mail-ca-v1\0"
MAX_CA_SIZE = 1024 * 1024


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            "Redirects are disabled",
            headers,
            fp,
        )


def _mail_trust_settings(config_file):
    config = configparser.ConfigParser()
    config.read(config_file)
    pki = config["mail_pki"]
    return (
        pki["hostname"],
        pathlib.Path(pki["ca_file"]),
        pki["bootstrap_secret"].encode(),
    )


def _download_ca(host, secret, port):
    nonce = secrets.token_hex(32)
    query = urllib.parse.urlencode(
        {
            "nonce": nonce,
            "hostname": host,
        }
    )
    request = urllib.request.Request(f"http://{host}:{port}/ca.crt?{query}")
    opener = urllib.request.build_opener(NoRedirectHandler())
    with opener.open(request, timeout=10) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is None or int(content_length) > MAX_CA_SIZE:
            raise ValueError("Invalid Workspace mail CA response size")
        content = response.read(MAX_CA_SIZE + 1)
        signature = response.headers.get("X-Workspace-CA-HMAC-SHA256")
    if len(content) > MAX_CA_SIZE or signature is None:
        raise ValueError("Invalid Workspace mail CA response")
    expected = hmac.new(
        secret,
        (
            HMAC_CONTEXT
            + nonce.encode()
            + b"\0"
            + host.encode()
            + b"\0"
            + content
        ),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Workspace mail CA response authentication failed")
    ssl.create_default_context(cadata=content.decode("ascii"))
    return content


def _atomic_install(path, content):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_file")
    parser.add_argument("--port", type=int, default=21085)
    args = parser.parse_args()

    settings = _mail_trust_settings(args.config_file)
    host, ca_file, secret = settings
    _atomic_install(ca_file, _download_ca(host, secret, args.port))


if __name__ == "__main__":
    main()
