#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -o pipefail

WORKSPACE_CONFIG=${1:-/etc/workspace/mail.conf}
TLS_STORE=${2:?Persistent TLS store path is required}
TLS_LIVE_DIR=${3:-/etc/workspace/tls}
TLS_GROUP=${WORKSPACE_MAIL_TLS_GROUP:-Debian-exim}
TLS_GENERATION="$TLS_STORE/v1"
TLS_CURRENT="$TLS_GENERATION/current"
LEAF_RENEW_SECONDS=${WORKSPACE_MAIL_LEAF_RENEW_SECONDS:-2592000}
LEAF_RETENTION_MINUTES=${WORKSPACE_MAIL_LEAF_RETENTION_MINUTES:-10080}

validate_store_path() {
    python3 - "$TLS_STORE" <<'PY'
import os
import pathlib
import stat
import sys

store = pathlib.Path(sys.argv[1])
if not store.is_absolute():
    raise SystemExit("Persistent TLS store must be an absolute path")
parent = store.parent
parent_stat = os.lstat(parent)
if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
    raise SystemExit("Persistent TLS store parent must be a real directory")
expected_uid = os.getuid() if os.environ.get("WORKSPACE_MAIL_TLS_SKIP_CHOWN") == "1" else 0
if parent_stat.st_uid != expected_uid:
    raise SystemExit("Persistent TLS store parent must be root-owned")
if parent_stat.st_mode & 0o022:
    raise SystemExit("Persistent TLS store parent must not be group/world writable")

try:
    store_stat = os.lstat(store)
except FileNotFoundError:
    store.mkdir(mode=0o700)
    store_stat = os.lstat(store)
if stat.S_ISLNK(store_stat.st_mode) or not stat.S_ISDIR(store_stat.st_mode):
    raise SystemExit("Persistent TLS store must be a real directory")
if store_stat.st_dev != parent_stat.st_dev:
    raise SystemExit("Persistent TLS store must remain on its parent filesystem")
PY
}

readarray -t REALM_VALUES < <(
    python3 - "$WORKSPACE_CONFIG" <<'PY'
import configparser
import re
import sys

config = configparser.ConfigParser()
config.read(sys.argv[1])
pki = config["mail_pki"]
hostname = pki["hostname"]
if len(hostname) > 253 or not re.fullmatch(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    hostname,
):
    raise SystemExit("Workspace mail hostname must be an RFC 1123 DNS name")
if not pki["realm_id"] or not pki["bootstrap_secret"]:
    raise SystemExit("Workspace mail PKI realm and bootstrap secret are required")
print(hostname)
print(pki["realm_id"])
PY
)

if [[ ${#REALM_VALUES[@]} -ne 2 ]]; then
    echo "Workspace mail TLS realm is unavailable" >&2
    exit 1
fi
MAIL_HOST=${REALM_VALUES[0]}
REALM_ID=${REALM_VALUES[1]}

write_realm_metadata() {
    local output=$1
    python3 - "$WORKSPACE_CONFIG" "$MAIL_HOST" "$output" <<'PY'
import configparser
import json
import pathlib
import sys

hostname = sys.argv[2]
config = configparser.ConfigParser()
config.read(sys.argv[1])
pki = config["mail_pki"]
pathlib.Path(sys.argv[3]).write_text(
    json.dumps(
        {
            "schema_version": 1,
            "hostname": hostname,
            "realm_id": pki["realm_id"],
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
}

validate_realm_metadata() {
    python3 - "$WORKSPACE_CONFIG" "$MAIL_HOST" "$TLS_GENERATION/realm.json" <<'PY'
import configparser
import json
import pathlib
import sys

hostname = sys.argv[2]
config = configparser.ConfigParser()
config.read(sys.argv[1])
pki = config["mail_pki"]
metadata = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
if metadata != {
    "schema_version": 1,
    "hostname": hostname,
    "realm_id": pki["realm_id"],
}:
    raise SystemExit("Persistent Workspace mail TLS realm does not match")
PY
}

generate_leaf() {
    local target=$1
    install -d -m 0700 "$target"
    openssl genpkey \
        -algorithm RSA \
        -pkeyopt rsa_keygen_bits:3072 \
        -out "$target/leaf.key"
    openssl req \
        -new \
        -sha256 \
        -key "$target/leaf.key" \
        -out "$target/leaf.csr" \
        -subj "/CN=$MAIL_HOST"
    cat >"$target/leaf.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$MAIL_HOST
authorityKeyIdentifier=keyid,issuer
subjectKeyIdentifier=hash
EOF
    openssl x509 \
        -req \
        -sha256 \
        -days 825 \
        -in "$target/leaf.csr" \
        -CA "$TLS_GENERATION/ca.crt" \
        -CAkey "$TLS_GENERATION/ca.key" \
        -CAserial "$TLS_GENERATION/ca.srl" \
        -extfile "$target/leaf.ext" \
        -out "$target/leaf.crt"
    cat \
        "$target/leaf.key" \
        "$target/leaf.crt" \
        "$TLS_GENERATION/ca.crt" \
        >"$target/workspace-mail.pem"
    rm -f "$target/leaf.csr" "$target/leaf.ext"
    chmod 0600 "$target/leaf.key"
    chmod 0644 "$target/leaf.crt"
    chmod 0640 "$target/workspace-mail.pem"
    if [[ ${WORKSPACE_MAIL_TLS_SKIP_CHOWN:-0} != 1 ]]; then
        chown root:"$TLS_GROUP" "$target"
        chmod 0710 "$target"
        chown root:root "$target/leaf.key" "$target/leaf.crt"
        chown root:"$TLS_GROUP" "$target/workspace-mail.pem"
    fi
}

validate_current_leaf() {
    test -L "$TLS_CURRENT"
    CURRENT_TARGET=$(readlink "$TLS_CURRENT")
    [[ "$CURRENT_TARGET" =~ ^leaves/leaf-[A-Za-z0-9._-]+$ ]]
    test -s "$TLS_CURRENT/leaf.key"
    test -s "$TLS_CURRENT/leaf.crt"
    test -s "$TLS_CURRENT/workspace-mail.pem"
    openssl verify \
        -no_check_time \
        -CAfile "$TLS_GENERATION/ca.crt" \
        "$TLS_CURRENT/leaf.crt" >/dev/null
    openssl x509 \
        -in "$TLS_CURRENT/leaf.crt" \
        -noout \
        -checkhost "$MAIL_HOST" >/dev/null
    cmp \
        <(openssl pkey -in "$TLS_CURRENT/leaf.key" -pubout 2>/dev/null) \
        <(openssl x509 -in "$TLS_CURRENT/leaf.crt" -pubkey -noout)
    cmp \
        <(
            cat \
                "$TLS_CURRENT/leaf.key" \
                "$TLS_CURRENT/leaf.crt" \
                "$TLS_GENERATION/ca.crt"
        ) \
        "$TLS_CURRENT/workspace-mail.pem"
}

require_state() {
    local kind=$1
    local path=$2
    local mode=$3
    local uid=$4
    local gid=$5

    [[ ! -L "$path" ]]
    if [[ "$kind" == directory ]]; then
        [[ -d "$path" ]]
    else
        [[ -f "$path" ]]
    fi
    [[ $(stat -c %a "$path") == "$mode" ]]
    [[ $(stat -c %u "$path") == "$uid" ]]
    [[ $(stat -c %g "$path") == "$gid" ]]
}

validate_structure() {
    local current_target
    local current_directory

    require_state directory "$TLS_STORE" \
        "$(stat -c %a "$TLS_STORE")" \
        "$(stat -c %u "$TLS_STORE")" \
        "$(stat -c %g "$TLS_STORE")"
    require_state directory "$TLS_GENERATION" \
        "$(stat -c %a "$TLS_GENERATION")" \
        "$(stat -c %u "$TLS_GENERATION")" \
        "$(stat -c %g "$TLS_GENERATION")"
    require_state directory "$TLS_GENERATION/leaves" \
        "$(stat -c %a "$TLS_GENERATION/leaves")" \
        "$(stat -c %u "$TLS_GENERATION/leaves")" \
        "$(stat -c %g "$TLS_GENERATION/leaves")"
    for path in \
        "$TLS_GENERATION/ca.key" \
        "$TLS_GENERATION/ca.crt" \
        "$TLS_GENERATION/ca.srl" \
        "$TLS_GENERATION/realm.json"; do
        [[ -f "$path" && ! -L "$path" ]]
    done

    [[ -L "$TLS_CURRENT" ]]
    current_target=$(readlink "$TLS_CURRENT")
    [[ "$current_target" =~ ^leaves/leaf-[A-Za-z0-9._-]+$ ]]
    [[ ! -L "$TLS_GENERATION/$current_target" ]]
    current_directory=$(readlink -f "$TLS_CURRENT")
    [[ "$current_directory" == "$TLS_GENERATION"/leaves/leaf-* ]]
    [[ -d "$current_directory" && ! -L "$current_directory" ]]
    for path in \
        "$TLS_CURRENT/leaf.key" \
        "$TLS_CURRENT/leaf.crt" \
        "$TLS_CURRENT/workspace-mail.pem"; do
        [[ -f "$path" && ! -L "$path" ]]
    done
    for leaf_directory in "$TLS_GENERATION"/leaves/leaf-*; do
        [[ -d "$leaf_directory" && ! -L "$leaf_directory" ]]
        for path in \
            "$leaf_directory/leaf.key" \
            "$leaf_directory/leaf.crt" \
            "$leaf_directory/workspace-mail.pem"; do
            [[ -f "$path" && ! -L "$path" ]]
        done
    done
}

normalize_permissions() {
    if [[ ${WORKSPACE_MAIL_TLS_SKIP_CHOWN:-0} == 1 ]]; then
        chmod 0700 \
            "$TLS_STORE" \
            "$TLS_GENERATION" \
            "$TLS_GENERATION/leaves"
    else
        chown root:"$TLS_GROUP" \
            "$TLS_STORE" \
            "$TLS_GENERATION" \
            "$TLS_GENERATION/leaves"
        chmod 0710 \
            "$TLS_STORE" \
            "$TLS_GENERATION" \
            "$TLS_GENERATION/leaves"
        chown root:root \
            "$TLS_GENERATION/ca.key" \
            "$TLS_GENERATION/ca.crt" \
            "$TLS_GENERATION/ca.srl" \
            "$TLS_GENERATION/realm.json"
    fi
    chmod 0600 "$TLS_GENERATION/ca.key"
    chmod 0644 \
        "$TLS_GENERATION/ca.crt" \
        "$TLS_GENERATION/ca.srl" \
        "$TLS_GENERATION/realm.json"
    for leaf_directory in "$TLS_GENERATION"/leaves/leaf-*; do
        if [[ ${WORKSPACE_MAIL_TLS_SKIP_CHOWN:-0} == 1 ]]; then
            chmod 0700 "$leaf_directory"
        else
            chown root:"$TLS_GROUP" "$leaf_directory"
            chmod 0710 "$leaf_directory"
            chown root:root \
                "$leaf_directory/leaf.key" \
                "$leaf_directory/leaf.crt"
            chown root:"$TLS_GROUP" "$leaf_directory/workspace-mail.pem"
        fi
        chmod 0600 "$leaf_directory/leaf.key"
        chmod 0644 "$leaf_directory/leaf.crt"
        chmod 0640 "$leaf_directory/workspace-mail.pem"
    done
}

validate_permissions() {
    local owner_uid
    local root_gid
    local group_gid
    local directory_mode
    if [[ ${WORKSPACE_MAIL_TLS_SKIP_CHOWN:-0} == 1 ]]; then
        owner_uid=$(id -u)
        root_gid=$(id -g)
        group_gid=$(id -g)
        directory_mode=700
    else
        owner_uid=$(id -u root)
        root_gid=$(getent group root | cut -d: -f3)
        group_gid=$(getent group "$TLS_GROUP" | cut -d: -f3)
        directory_mode=710
    fi

    require_state directory "$TLS_STORE" "$directory_mode" "$owner_uid" "$group_gid"
    require_state directory "$TLS_GENERATION" "$directory_mode" "$owner_uid" "$group_gid"
    require_state directory "$TLS_GENERATION/leaves" "$directory_mode" "$owner_uid" "$group_gid"
    require_state file "$TLS_GENERATION/ca.key" 600 "$owner_uid" "$root_gid"
    require_state file "$TLS_GENERATION/ca.crt" 644 "$owner_uid" "$root_gid"
    require_state file "$TLS_GENERATION/ca.srl" 644 "$owner_uid" "$root_gid"
    require_state file "$TLS_GENERATION/realm.json" 644 "$owner_uid" "$root_gid"
    require_state directory "$(readlink -f "$TLS_CURRENT")" "$directory_mode" "$owner_uid" "$group_gid"
    require_state file "$TLS_CURRENT/leaf.key" 600 "$owner_uid" "$root_gid"
    require_state file "$TLS_CURRENT/leaf.crt" 644 "$owner_uid" "$root_gid"
    require_state file "$TLS_CURRENT/workspace-mail.pem" 640 "$owner_uid" "$group_gid"
    for leaf_directory in "$TLS_GENERATION"/leaves/leaf-*; do
        require_state directory "$leaf_directory" "$directory_mode" "$owner_uid" "$group_gid"
        require_state file "$leaf_directory/leaf.key" 600 "$owner_uid" "$root_gid"
        require_state file "$leaf_directory/leaf.crt" 644 "$owner_uid" "$root_gid"
        require_state file "$leaf_directory/workspace-mail.pem" 640 "$owner_uid" "$group_gid"
    done
}

validate_store_path
if [[ ${WORKSPACE_MAIL_TLS_SKIP_CHOWN:-0} != 1 ]]; then
    chown root:"$TLS_GROUP" "$TLS_STORE"
    chmod 0710 "$TLS_STORE"
fi
if [[ ! -e "$TLS_GENERATION" ]]; then
    TLS_TMP="$TLS_STORE/.v1.$$"
    trap 'rm -rf "$TLS_TMP"' EXIT
    install -d -m 0700 "$TLS_TMP" "$TLS_TMP/leaves"

    openssl genpkey \
        -algorithm RSA \
        -pkeyopt rsa_keygen_bits:3072 \
        -out "$TLS_TMP/ca.key"
    openssl req \
        -x509 \
        -new \
        -sha256 \
        -days 3650 \
        -key "$TLS_TMP/ca.key" \
        -out "$TLS_TMP/ca.crt" \
        -subj "/CN=Workspace Mail Internal CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash"
    openssl rand -hex 16 >"$TLS_TMP/ca.srl"
    write_realm_metadata "$TLS_TMP/realm.json"

    TLS_GENERATION="$TLS_TMP"
    TLS_CURRENT="$TLS_TMP/current"
    generate_leaf "$TLS_TMP/leaves/leaf-initial"
    ln -s "leaves/leaf-initial" "$TLS_CURRENT"
    TLS_GENERATION="$TLS_STORE/v1"
    TLS_CURRENT="$TLS_GENERATION/current"

    chmod 0600 "$TLS_TMP/ca.key"
    chmod 0644 "$TLS_TMP/ca.crt" "$TLS_TMP/ca.srl" "$TLS_TMP/realm.json"
    if [[ ${WORKSPACE_MAIL_TLS_SKIP_CHOWN:-0} != 1 ]]; then
        chown root:"$TLS_GROUP" "$TLS_TMP" "$TLS_TMP/leaves"
        chmod 0710 "$TLS_TMP" "$TLS_TMP/leaves"
        chown root:root \
            "$TLS_TMP/ca.key" \
            "$TLS_TMP/ca.crt" \
            "$TLS_TMP/ca.srl" \
            "$TLS_TMP/realm.json"
    fi
    mv "$TLS_TMP" "$TLS_GENERATION"
    trap - EXIT
fi

validate_structure
normalize_permissions
validate_permissions
validate_realm_metadata
openssl verify \
    -CAfile "$TLS_GENERATION/ca.crt" \
    "$TLS_GENERATION/ca.crt" >/dev/null
cmp \
    <(openssl pkey -in "$TLS_GENERATION/ca.key" -pubout 2>/dev/null) \
    <(openssl x509 -in "$TLS_GENERATION/ca.crt" -pubkey -noout)
CA_TEXT=$(openssl x509 -in "$TLS_GENERATION/ca.crt" -noout -text)
grep -F "X509v3 Basic Constraints: critical" <<<"$CA_TEXT" >/dev/null
grep -F "CA:TRUE" <<<"$CA_TEXT" >/dev/null
grep -F "X509v3 Key Usage: critical" <<<"$CA_TEXT" >/dev/null
grep -F "Certificate Sign, CRL Sign" <<<"$CA_TEXT" >/dev/null
validate_current_leaf

if ! openssl x509 \
    -in "$TLS_CURRENT/leaf.crt" \
    -noout \
    -checkend "$LEAF_RENEW_SECONDS" >/dev/null; then
    LEAF_NAME="leaf-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    LEAF_TMP="$TLS_GENERATION/leaves/.$LEAF_NAME"
    trap 'rm -rf "$LEAF_TMP"' EXIT
    generate_leaf "$LEAF_TMP"
    mv "$LEAF_TMP" "$TLS_GENERATION/leaves/$LEAF_NAME"
    ln -s "leaves/$LEAF_NAME" "$TLS_GENERATION/.current.$$"
    mv -Tf "$TLS_GENERATION/.current.$$" "$TLS_CURRENT"
    trap - EXIT
    validate_structure
    normalize_permissions
    validate_permissions
    validate_current_leaf
fi

install -d -m 0755 "$TLS_LIVE_DIR"
ln -s "$TLS_CURRENT/workspace-mail.pem" \
    "$TLS_LIVE_DIR/.workspace-mail.pem.$$"
mv -Tf \
    "$TLS_LIVE_DIR/.workspace-mail.pem.$$" \
    "$TLS_LIVE_DIR/workspace-mail.pem"
install -m 0644 \
    "$TLS_GENERATION/ca.crt" \
    "$TLS_LIVE_DIR/.workspace-mail-ca.crt.$$"
mv -f \
    "$TLS_LIVE_DIR/.workspace-mail-ca.crt.$$" \
    "$TLS_LIVE_DIR/workspace-mail-ca.crt"
install -m 0644 \
    "$TLS_GENERATION/realm.json" \
    "$TLS_LIVE_DIR/.workspace-mail-realm.json.$$"
mv -f \
    "$TLS_LIVE_DIR/.workspace-mail-realm.json.$$" \
    "$TLS_LIVE_DIR/workspace-mail-realm.json"

CURRENT_DIRECTORY=$(readlink -f "$TLS_CURRENT")
for leaf_directory in "$TLS_GENERATION"/leaves/leaf-*; do
    if [[ "$leaf_directory" == "$CURRENT_DIRECTORY" ]]; then
        continue
    fi
    if find "$leaf_directory" -maxdepth 0 -mmin "+$LEAF_RETENTION_MINUTES" \
        -print -quit | grep -q .; then
        rm -rf -- "$leaf_directory"
    fi
done
