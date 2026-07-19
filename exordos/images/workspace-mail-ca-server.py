#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import argparse
import configparser
import hashlib
import hmac
import http.server
import json
import pathlib
import urllib.parse


HMAC_CONTEXT = b"workspace-mail-ca-v1\0"
DEFAULT_CA_FILE = pathlib.Path("/etc/workspace/tls/workspace-mail-ca.crt")
DEFAULT_REALM_FILE = pathlib.Path("/etc/workspace/tls/workspace-mail-realm.json")
MAX_REQUEST_TARGET = 512


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "WorkspaceMailCA/1"

    def do_GET(self):
        if len(self.path) > MAX_REQUEST_TARGET:
            self.send_error(414)
            return
        try:
            target = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(
                target.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            self.send_error(400)
            return
        if (
            target.path != "/ca.crt"
            or set(query) != {"nonce", "hostname"}
            or len(query["nonce"]) != 1
            or len(query["hostname"]) != 1
        ):
            self.send_error(404)
            return
        nonce = query["nonce"][0]
        requested_hostname = query["hostname"][0]
        if (
            len(nonce) != 64
            or any(character not in "0123456789abcdef" for character in nonce)
        ):
            self.send_error(400)
            return

        config = configparser.ConfigParser()
        config.read(self.server.config_file)
        try:
            pki = config["mail_pki"]
            secret = pki["bootstrap_secret"].encode()
            hostname = pki["hostname"]
            realm_id = pki["realm_id"]
            metadata = json.loads(
                self.server.realm_file.read_text(encoding="utf-8")
            )
            content = self.server.ca_file.read_bytes()
        except (KeyError, OSError, ValueError):
            self.send_error(503)
            return
        if requested_hostname != hostname or metadata != {
            "schema_version": 1,
            "hostname": requested_hostname,
            "realm_id": realm_id,
        }:
            self.send_error(409)
            return

        signature = hmac.new(
            secret,
            (
                HMAC_CONTEXT
                + nonce.encode()
                + b"\0"
                + requested_hostname.encode()
                + b"\0"
                + content
            ),
            hashlib.sha256,
        ).hexdigest()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Workspace-CA-HMAC-SHA256", signature)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        return


class Server(http.server.HTTPServer):
    request_queue_size = 8

    def __init__(self, address, config_file, ca_file, realm_file):
        super().__init__(address, Handler)
        self.config_file = config_file
        self.ca_file = ca_file
        self.realm_file = realm_file

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-file",
        default="/etc/workspace/mail-pki.conf",
    )
    parser.add_argument(
        "--ca-file",
        type=pathlib.Path,
        default=DEFAULT_CA_FILE,
    )
    parser.add_argument(
        "--realm-file",
        type=pathlib.Path,
        default=DEFAULT_REALM_FILE,
    )
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=21085)
    args = parser.parse_args()

    with Server(
        (args.bind, args.port),
        args.config_file,
        args.ca_file,
        args.realm_file,
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
