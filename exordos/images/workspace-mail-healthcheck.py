#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import configparser
import imaplib
import smtplib
import ssl
import sys


config = configparser.ConfigParser()
config.read(sys.argv[1])
mail = config["messenger_mail"]
target = f"healthcheck@{mail['technical_domain']}"
username = f"{target}*{mail['imap_master_username']}"
smtp_context = ssl.create_default_context(cafile=mail["smtp_ca_file"])
imap_context = ssl.create_default_context(cafile=mail["imap_ca_file"])

with smtplib.SMTP(mail["smtp_host"], mail.getint("smtp_port"), timeout=10) as smtp:
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
