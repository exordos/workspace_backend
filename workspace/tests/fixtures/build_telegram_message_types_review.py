"""Capture the current converters for an offline review; never modify expectations."""

import copy
import json
import uuid
import pathlib

from workspace.bridges.telegram import conversion
from workspace.bridges.zulip import message_conversion

DIRECTORY = pathlib.Path(__file__).parent
EXAMPLES = json.loads((DIRECTORY / "zulip_messages.json").read_text())
MESSAGES = {str(example["message"]["id"]): example["message"] for example in EXAMPLES}


def telegram_cycle(content):
    first = capture(content)
    second = capture(first["roundtrip"])
    return {
        **first,
        "second": second,
        "exact_roundtrip": first["roundtrip"] == content,
        "repeat_stable": first["roundtrip"] == second["roundtrip"],
        "scope": "text-and-entities-only; reply, files and identities require runtime",
    }


def zulip_cycle(example):
    context = {**example["conversion_context"], "messages": MESSAGES}
    payload = copy.deepcopy(example["expected_payload"])
    cycles = []
    for _ in range(2):
        content = message_conversion.workspace_to_zulip(payload, context)
        payload = message_conversion.zulip_to_workspace(
            {**example["message"], "content": content}, context
        )
        cycles.append({"content": content, "returned": payload})
    return {
        **cycles[0],
        "second": cycles[1],
        "exact_roundtrip": cycles[0]["returned"] == example["expected_payload"],
        "repeat_stable": cycles[0]["returned"] == cycles[1]["returned"],
        "scope": "converters with synthetic persisted identity, quote and file mappings",
    }


def capture(content):
    references = {}
    text, entities = conversion.to_telegram(content, references=references)
    return {
        "text": text,
        "entities": [
            dict(
                type=type(entity).__name__,
                **{key: value for key, value in entity.to_dict().items() if key != "_"},
            )
            for entity in entities
        ],
        "references": references,
        "roundtrip": conversion.to_workspace(text, entities),
    }


def category(example):
    name = example["name"]
    if "quote" in name:
        return "quotes"
    if any(value in name for value in ("mention", "notification")):
        return "mentions"
    if any(
        value in name
        for value in ("attachment", "image", "document", "video", "audio", "animated")
    ):
        return "media"
    if any(value in name for value in ("link", "url")):
        return "links"
    if any(
        value in name
        for value in ("poll", "todo", "math", "time", "emoji", "me_action")
    ):
        return "special"
    if any(
        value in name
        for value in ("direct", "edited", "reaction", "topic", "system_bot")
    ):
        return "runtime"
    return "formatting"


rows = []
for example in EXAMPLES:
    kind = category(example)
    value = telegram_cycle(example["expected_payload"]["content"])
    notes = []
    if "urn:forward:" in example["expected_payload"]["content"]:
        notes.append(
            "Текст снимка цитаты сейчас не разворачивается: в Telegram остаётся подпись автора без содержимого цитаты."
        )
    if "reply" in value["references"]:
        notes.append(
            "Конвертер передаёт одну reply-ссылку в отправку. Это ещё не проверка доставки или отображения цитаты в Telegram; несколько и вложенные цитаты должны сохраняться в тексте отдельно."
        )
    if kind == "quotes":
        proposal = "Сохранить все цитируемые тексты, авторов, уровни вложенности и комментарии в исходном порядке. Одну reply-ссылку использовать дополнительно. Для вложенных уровней предлагаются явные маркеры │ / › в тексте, без недопустимых вложенных Telegram blockquote entities. Частичные цитаты должны сохранять выделенный фрагмент."
    elif example["name"] == "table":
        proposal = "Передать таблицу читаемым моноширинным блоком с сохранением строк, столбцов и значений; согласовать этот fallback вместо таблицы Zulip."
        notes.append(
            "Сейчас таблица передаётся текстом с вертикальными разделителями, без табличного или единого моноширинного блока."
        )
    elif example["name"] in (
        "headings_and_horizontal_rule",
        "bulleted_list",
        "numbered_list",
        "nested_lists",
    ):
        proposal = "Сохранить текст и структуру: заголовки выделить, разделители и маркеры/номера/отступы списков передать явно."
        notes.append(
            "Проверить структуру: Telegram не получает исходное дерево Markdown, только текст и entities."
        )
    elif kind == "media":
        proposal = "Доставить все файлы/изображения/видео/аудио и подписи; проверить содержимое и порядок в реальном Telegram. Превью и references на странице не являются подтверждением загрузки."
    elif kind == "mentions":
        proposal = "Сохранить видимую подпись; разрешать пользователя через Telegram-аккаунт выбранного топика. Скрытые упоминания не должны превращаться в уведомляющие. Для групп и @all согласовать читаемый текстовый fallback."
    elif kind == "special":
        proposal = "Сохранить весь смысл и текст. Для отсутствующего интерактива предложить явный текстовый снимок; формулы, время, пользовательские emoji и действия не должны молча исчезать. Нативные функции требуют отдельной проверки доставки."
    elif kind == "runtime":
        proposal = "Проверить в живом потоке: адресата/тип чата, топик, редактирование, реакции и служебные поля. Статическая конвертация текста этого не доказывает."
    else:
        proposal = "Сохранить читаемый текст, форматирование и назначение ссылок; отдельно проверить обратное преобразование."
    rows.append(
        {
            "fixture_source": "zulip",
            "category": kind,
            "example": example,
            "telegram": value,
            "zulip": zulip_cycle(example),
            "notes": notes,
            "proposal": proposal,
        }
    )

native = []
for index, original in enumerate(
    json.loads((DIRECTORY / "telegram_messages.json").read_text())
):
    example = copy.deepcopy(original)
    message_id = 20000 + index
    message_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"review:{message_id}"))
    # Give native-origin cases a synthetic Zulip destination and identity context.
    # No provider API is called and these identities are never production data.
    example["message"] = {
        **EXAMPLES[0]["message"],
        "id": message_id,
        "content": example["expected_payload"]["content"],
    }
    example["conversion_context"] = {
        "server_url": "https://zulip.example.test",
        "original_url": f"https://zulip.example.test/#narrow/near/{message_id}",
        "current_message_uuid": message_uuid,
        "message_uuids": {str(message_id): message_uuid},
    }
    MESSAGES[str(message_id)] = example["message"]
    native.append(
        {
            "fixture_source": "telegram",
            "category": "native",
            "example": example,
            "original_fixture": original,
            "telegram": telegram_cycle(example["expected_payload"]["content"]),
            "zulip": zulip_cycle(example),
            "notes": [],
            "proposal": "Сохранить семантику Workspace в обоих направлениях. Различие Markdown не равно потере смысла; итог проверяется по тексту, структуре и ссылкам. Повторный цикл не должен добавлять экранирование или менять форматирование.",
        }
    )

assert len(rows) == 79 and len(native) == 17
result = {
    "status": "workspace-canonical-review-before-converter-changes",
    "zulip_cases": rows,
    "native_telegram_cases": native,
}
(DIRECTORY / "telegram_message_types_review.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n"
)
for provider in ("zulip", "telegram"):
    cases = rows + native
    print(
        provider,
        "cases",
        len(cases),
        "exact Markdown roundtrip",
        sum(row[provider]["exact_roundtrip"] for row in cases),
        "second cycle stable",
        sum(row[provider]["repeat_stable"] for row in cases),
    )
