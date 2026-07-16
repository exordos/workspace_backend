#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import configparser
import imaplib
import smtplib
import sys


config = configparser.ConfigParser()
config.read(sys.argv[1])
mail = config["messenger_mail"]
target = f"healthcheck@{mail['technical_domain']}"
username = f"{target}*{mail['imap_master_username']}"

with smtplib.SMTP(mail["smtp_host"], mail.getint("smtp_port"), timeout=10) as smtp:
    if mail.get("smtp_username") is not None:
        smtp.login(mail["smtp_username"], mail["smtp_password"])
    smtp.noop()
with imaplib.IMAP4(mail["imap_host"], mail.getint("imap_port"), timeout=10) as imap:
    imap.login(username, mail["imap_master_password"])
    status, _ = imap.select("INBOX")
    if status != "OK":
        raise RuntimeError("Workspace Messenger IMAP INBOX is unavailable")
