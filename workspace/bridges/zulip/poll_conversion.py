# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""The common interactive subset of Workspace and Zulip poll widgets."""

import dataclasses
import json
import re
import urllib.parse
import uuid

from workspace.messenger_api.dm import polls

OBSERVE_SQL = """
    INSERT INTO zulip_poll_observations(project_id,message_uuid,provider_uuid,submessage_id)
    SELECT project_id,uuid,%s,%s FROM messenger_messages WHERE project_id=%s AND uuid=%s
    ON CONFLICT(project_id,message_uuid,provider_uuid) DO UPDATE
      SET submessage_id=GREATEST(zulip_poll_observations.submessage_id,EXCLUDED.submessage_id)
"""
SELECT_OBSERVATION_SQL = """
    SELECT submessage_id FROM zulip_poll_observations
    WHERE project_id=%s AND message_uuid=%s AND provider_uuid=%s
"""


def message_url(
    origin: str, project: object, stream: object, topic: object, message: object
) -> str:
    endpoint = urllib.parse.urlsplit(origin)
    if (
        endpoint.scheme not in ("http", "https")
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in ("", "/")
    ):
        raise ValueError("Configure workspace_url as the public Workspace origin")
    port = endpoint.port
    if port == (443 if endpoint.scheme == "https" else 80):
        port = None
    org = endpoint.hostname.lower() + (f"-{port}" if port else "")
    org = re.sub(r"-+", "-", re.sub(r"[^a-z0-9.-]", "-", org)).strip("-")
    return (
        origin.rstrip("/")
        + f"/org/{org}/project/{uuid.UUID(str(project))}"
        + f"/stream/{uuid.UUID(str(stream))}/topic/{uuid.UUID(str(topic))}?msg={uuid.UUID(str(message))}"
    )


def compatible(payload: dict) -> bool:
    if (
        payload["kind"] != "poll"
        or len(payload["questions"]) != 1
        or not payload["multiple_answers"]
        or payload["is_anonymous"]
        or not payload["allow_revote"]
        or payload["results_visibility"] != "always"
        or not payload.get("allow_adding_options", False)
        or payload.get("closes_at") is not None
        or payload.get("closed_at") is not None
    ):
        return False
    question = payload["questions"][0]
    labels = [o["text"] for o in question["options"]]
    return (
        not question.get("correct_option_uuids")
        and len(set(labels)) == len(labels)
        and all(
            text == text.strip() and not any(c in text for c in ("\n", "\r", "@*"))
            for text in [question["text"], *labels]
        )
    )


def command(payload: dict) -> str:
    if not compatible(payload):
        raise ValueError("Poll features are not compatible with Zulip")
    question = payload["questions"][0]
    return (
        "/poll "
        + question["text"]
        + "\n"
        + "\n".join(
            ("- " if o["text"].startswith(("-", "*")) else "") + o["text"]
            for o in question["options"]
        )
    )


@dataclasses.dataclass
class Poll:
    question: str
    options: dict[str, str]
    votes: dict[str, set[int]]
    revision: int = 0
    vote_events: list[tuple[int, int, str, int]] = dataclasses.field(
        default_factory=list
    )


def read(message: dict) -> Poll | None:
    result = None
    for submessage in message.get("submessages", []):
        if submessage.get("msg_type") != "widget":
            continue
        try:
            data = json.loads(submessage["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        sender = submessage["sender_id"]
        if result is None and data.get("widget_type") == "poll":
            extra = data.get("extra_data", {})
            if not isinstance(extra, dict):
                continue
            question, labels = extra.get("question", ""), extra.get("options", [])
            if (
                not isinstance(question, str)
                or not isinstance(labels, list)
                or not all(isinstance(v, str) for v in labels)
            ):
                continue
            result = Poll(question, {}, {})
            for index, label in enumerate(labels):
                if label not in result.options.values():
                    key = f"canned,{index}"
                    result.options[key], result.votes[key] = label, set()
        elif result is not None and data.get("type") == "question":
            if sender == message["sender_id"] and isinstance(data.get("question"), str):
                result.question = data["question"]
        elif result is not None and data.get("type") == "new_option":
            new_index, new_label = data.get("idx"), data.get("option")
            if (
                type(new_index) is int
                and 0 <= new_index <= 1000
                and isinstance(new_label, str)
                and new_label not in result.options.values()
                and f"{sender},{new_index}" not in result.options
            ):
                key = f"{sender},{new_index}"
                result.options[key], result.votes[key] = new_label, set()
        elif result is not None and data.get("type") == "vote":
            vote_key, vote = data.get("key"), data.get("vote")
            if (
                isinstance(vote_key, str)
                and vote_key in result.options
                and type(vote) is int
            ):
                if vote in (1, -1):
                    result.vote_events.append(
                        (submessage.get("id", 0), sender, vote_key, vote)
                    )
                if vote == 1:
                    result.votes[vote_key].add(sender)
                elif vote == -1:
                    result.votes[vote_key].discard(sender)
        if result is not None:
            result.revision = max(result.revision, submessage.get("id", 0))
    return result


def payload(
    observed: Poll, namespace: object, remote_id: int, existing: dict | None = None
) -> dict | None:
    namespace = uuid.UUID(str(namespace))
    question_id = str(uuid.uuid5(namespace, f"poll:{remote_id}:question"))
    known = {}
    if existing is not None and existing["kind"] == "poll":
        question = existing["questions"][0]
        question_id = question["uuid"]
        known = {o["text"]: o["uuid"] for o in question["options"]}
    raw: dict = {
        "kind": "poll",
        "questions": [
            {
                "uuid": question_id,
                "text": observed.question,
                "options": [
                    {
                        "uuid": known.get(text)
                        or str(uuid.uuid5(namespace, f"poll:{remote_id}:option:{key}")),
                        "text": text,
                    }
                    for key, text in observed.options.items()
                ],
            }
        ],
        "multiple_answers": True,
        "is_anonymous": False,
        "allow_revote": True,
        "results_visibility": "always",
        "allow_adding_options": True,
        "closes_at": None,
        "closed_at": None,
    }
    # Provider widgets allow empty/long/unbounded lists. Preserve those as
    # snapshots rather than manufacturing an invalid native poll.
    question = raw["questions"][0]
    if not (
        2 <= len(question["options"]) <= polls.MAX_POLL_OPTIONS_PER_QUESTION
        and 0 < len(question["text"]) <= 1000
        and all(
            0 < len(o["text"]) <= 200 and o["text"].strip() for o in question["options"]
        )
    ):
        return None
    if not compatible(raw):
        return None
    return polls.POLL_TYPE.to_simple_type(polls.POLL_TYPE.from_simple_type(raw))
