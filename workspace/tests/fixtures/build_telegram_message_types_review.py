"""Capture the current converters for an offline review; never modify expectations."""

import json
import pathlib

from telethon.tl import types

from workspace.bridges.telegram import conversion

DIRECTORY = pathlib.Path(__file__).parent


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
for example in json.loads((DIRECTORY / "zulip_messages.json").read_text()):
    kind = category(example)
    value = capture(example["expected_payload"]["content"])
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
            "source": "zulip",
            "category": kind,
            "example": example,
            "telegram": value,
            "notes": notes,
            "proposal": proposal,
        }
    )

native = []
for example in json.loads((DIRECTORY / "telegram_messages.json").read_text()):
    if example.get("direction") == "outgoing":
        value = capture(example["expected_payload"]["content"])
    else:
        entities = [
            getattr(types, entity["type"])(
                **{key: value for key, value in entity.items() if key != "type"}
            )
            for entity in example["message"]["entities"]
        ]
        value = {
            "text": example["message"]["content"],
            "entities": example["message"]["entities"],
            "references": {},
            "roundtrip": conversion.to_workspace(
                example["message"]["content"], entities
            ),
        }
    native.append({"source": "telegram", "example": example, "telegram": value})

assert len(rows) == 79 and len(native) == 17
result = {
    "status": "review-before-converter-changes",
    "zulip_cases": rows,
    "native_telegram_cases": native,
}
(DIRECTORY / "telegram_message_types_review.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n"
)
print(
    "Captured 79 Zulip cases and 17 native Telegram cases from the unchanged converter."
)
