#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import argparse
import configparser
import imaplib
import smtplib
import ssl
import subprocess


parser = argparse.ArgumentParser()
parser.add_argument("config")
parser.add_argument("--smtp-inactive", action="store_true")
args = parser.parse_args()
config = configparser.ConfigParser()
config.read(args.config)
mail = config["messenger_mail"]
target = f"healthcheck@{mail['technical_domain']}"
username = f"{target}*{mail['imap_master_username']}"
smtp_context = ssl.create_default_context(cafile=mail["smtp_ca_file"])
imap_context = ssl.create_default_context(cafile=mail["imap_ca_file"])

if args.smtp_inactive:
    exim_status = subprocess.run(
        ("systemctl", "is-active", "--quiet", "exim4.service"),
        check=False,
    )
    if exim_status.returncode != 3:
        raise RuntimeError("Workspace Messenger SMTP listener is not inactive")
else:
    with smtplib.SMTP(
        mail["smtp_host"], mail.getint("smtp_port"), timeout=10
    ) as smtp:
        smtp.starttls(context=smtp_context)
        if mail.get("smtp_username") is not None:
            smtp.login(mail["smtp_username"], mail["smtp_password"])
        smtp.noop()
with imaplib.IMAP4(mail["imap_host"], mail.getint("imap_port"), timeout=10) as imap:
    imap.starttls(ssl_context=imap_context)
    imap.login(username, mail["imap_master_password"])
    status, _ = imap.noop()
    if status != "OK":
        raise RuntimeError("Workspace Messenger IMAP NOOP failed")
