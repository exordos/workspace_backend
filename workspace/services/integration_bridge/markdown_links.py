#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain a
#    copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
import urllib.parse
import uuid as sys_uuid


MARKDOWN_LINK_RE = re.compile(r"(?P<bang>!?)\[(?P<name>[^\]]*)\]\((?P<url>[^)\s]+)\)")
ZULIP_USER_MENTION_RE = re.compile(
    r"(?P<prefix>@_?)\*\*(?P<name>[^*|]+?)(?:\|(?P<user_id>\d+))?\*\*"
)
ZULIP_STREAM_TOPIC_LINK_RE = re.compile(
    r"#\*\*(?P<stream>[^*>]+?)(?:>(?P<topic>[^*]+?))?\*\*"
)
ZULIP_HASH_HEX_RE = re.compile(r"\.([0-9A-Fa-f]{2})")


def build_markdown_link(bang, name, url):
    return f"{bang}[{name}]({url})"


def build_workspace_urn(urn_type, value):
    return f"urn:{urn_type}:{value}"


def build_workspace_url_urn(url):
    return build_workspace_urn(urn_type="url", value=url)


def parse_urn(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "urn":
        return None
    urn_type, separator, value = parsed.path.partition(":")
    if not separator or not urn_type or not value:
        return None
    return {
        "type": urn_type,
        "value": value,
        "fragment": parsed.fragment,
        "query": urllib.parse.parse_qs(parsed.query),
        "query_string": parsed.query,
    }


def parse_uuid_urn(url, urn_types):
    parsed = parse_urn(url)
    if parsed is None or parsed["type"] not in urn_types:
        return None
    try:
        parsed["uuid"] = sys_uuid.UUID(parsed["value"])
    except ValueError:
        return None
    return parsed


def encode_zulip_hash_component(value):
    quoted = urllib.parse.quote(str(value), safe="")
    return quoted.replace("%", ".")


def decode_zulip_hash_component(value):
    quoted = ZULIP_HASH_HEX_RE.sub(r"%\1", value)
    return urllib.parse.unquote(quoted)


def extract_zulip_narrow_url(url, server_url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme:
        server = urllib.parse.urlparse(server_url)
        if parsed.netloc != server.netloc:
            return None

    narrow = parsed.fragment or parsed.path
    narrow = narrow.strip("/")
    if narrow.startswith("narrow/"):
        narrow = narrow[len("narrow/") :]
    if not narrow:
        return None

    parts = narrow.split("/")
    result = {}
    for index, part in enumerate(parts):
        if part in ("stream", "channel") and index + 1 < len(parts):
            stream_part = parts[index + 1].split("-", 1)[0]
            if stream_part.isdigit():
                result["stream_id"] = int(stream_part)
        if part == "topic" and index + 1 < len(parts):
            result["topic_name"] = decode_zulip_hash_component(
                parts[index + 1],
            )
        if part == "near" and index + 1 < len(parts):
            message_part = parts[index + 1]
            if message_part.isdigit():
                result["message_id"] = int(message_part)
    if result:
        return result
    return None


def build_zulip_stream_narrow_url(server_url, stream_id, stream_name):
    stream = "%s-%s" % (
        stream_id,
        encode_zulip_hash_component(stream_name),
    )
    return "%s/#narrow/stream/%s" % (server_url.rstrip("/"), stream)


def build_zulip_topic_narrow_url(
    server_url,
    stream_id,
    stream_name,
    topic_name,
):
    return "%s/topic/%s" % (
        build_zulip_stream_narrow_url(
            server_url=server_url,
            stream_id=stream_id,
            stream_name=stream_name,
        ),
        encode_zulip_hash_component(topic_name),
    )


def build_zulip_message_narrow_url(
    server_url,
    stream_id,
    stream_name,
    topic_name,
    message_id,
):
    return "%s/near/%s" % (
        build_zulip_topic_narrow_url(
            server_url=server_url,
            stream_id=stream_id,
            stream_name=stream_name,
            topic_name=topic_name,
        ),
        message_id,
    )
