#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
import typing

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm


STICKER_CATEGORIES = ("gif", "sticker")
STICKER_FORMATS = ("gif", "webp", "png")
STICKER_IMPORT_STATUSES = ("created", "duplicate")
DEFAULT_STICKER_CATEGORY = "sticker"

MAX_TITLE_LENGTH = 200
MAX_ALT_TEXT_LENGTH = 500
MAX_TAG_LENGTH = 64
MAX_TAG_COUNT = 64
MAX_TAGS_BYTES = 4096
MAX_EMOJI_COUNT = 5
MAX_QUERY_LENGTH = 200
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 50
MAX_UUID_FILTER_COUNT = 100
MAX_FILE_PATH_LENGTH = 260
SHA256_LENGTH = 64
STICKER_FILE_PATH_TYPE = types.String(
    min_length=1,
    max_length=MAX_FILE_PATH_LENGTH,
)
STICKER_ADMIN_MUTABLE_FIELDS = frozenset(
    {"title", "alt_text", "emoji", "tags", "category", "active", "blocked"}
)
STICKER_INTERNAL_FIELDS = frozenset(
    {
        "search_text",
        "format",
        "width",
        "height",
        "size_bytes",
        "sha256",
        "media_object_id",
    }
)
STICKER_READ_ONLY_FIELDS = STICKER_INTERNAL_FIELDS | {
    "uuid",
    "created_at",
    "updated_at",
}


class _StrictInputTypeMixin:
    """Reject JSON values whose type does not match the declared RA type."""

    def from_simple_type(self, value: typing.Any) -> typing.Any:
        if not self.validate(value):
            raise TypeError("Invalid value type")
        return super().from_simple_type(value)


class _StrictString(_StrictInputTypeMixin, types.String):
    pass


class _StrictBoolean(_StrictInputTypeMixin, types.Boolean):
    pass


class _StrictTypedList(_StrictInputTypeMixin, types.TypedList):
    pass


class _StrictEnum(_StrictInputTypeMixin, types.Enum):
    pass


class _StrictFieldsMixin:
    """A model that rejects fields not declared by its contract."""

    def __init__(self, **kwargs: typing.Any) -> None:
        model_type = typing.cast(typing.Any, type(self))
        unknown = set(kwargs).difference(model_type.properties.properties)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError("Unknown fields: %s" % names)
        super().__init__(**kwargs)


class _StrictModel(_StrictFieldsMixin, models.Model):
    pass


class StickerListQuery(_StrictModel):
    """Normalized parameters for one public sticker catalog page."""

    q = properties.property(
        _StrictString(max_length=MAX_QUERY_LENGTH),
        default="",
    )
    tag_query = properties.property(
        _StrictString(max_length=MAX_QUERY_LENGTH),
        default="",
    )
    favorite = properties.property(_StrictBoolean(), default=False)
    uuids = properties.property(
        _StrictTypedList(types.UUID()),
        default=list,
    )
    category = properties.property(
        types.AllowNone(_StrictEnum(STICKER_CATEGORIES)),
        default=None,
    )
    format = properties.property(
        types.AllowNone(_StrictEnum(STICKER_FORMATS)),
        default=None,
    )
    page_limit = properties.property(
        types.Integer(min_value=1, max_value=MAX_PAGE_LIMIT),
        default=DEFAULT_PAGE_LIMIT,
    )
    page_marker = properties.property(
        types.AllowNone(_StrictString(min_length=1)),
        default=None,
    )

    def validate(self) -> None:
        if len(self.uuids) > MAX_UUID_FILTER_COUNT:
            raise ValueError("uuid cannot contain more than 100 values")


class Sticker(
    _StrictFieldsMixin,
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    """The persisted, global sticker catalog row."""

    __tablename__ = "m_workspace_stickers"

    title = properties.property(
        _StrictString(min_length=1, max_length=MAX_TITLE_LENGTH),
        required=True,
    )
    alt_text = properties.property(
        _StrictString(max_length=MAX_ALT_TEXT_LENGTH),
        required=True,
    )
    emoji = properties.property(
        _StrictTypedList(_StrictString(min_length=1, max_length=128)),
        default=list,
    )
    tags = properties.property(
        _StrictTypedList(_StrictString(min_length=1, max_length=MAX_TAG_LENGTH)),
        default=list,
    )
    search_text = properties.property(
        types.String(),
        required=True,
        read_only=True,
    )
    category = properties.property(
        _StrictEnum(STICKER_CATEGORIES),
        default=DEFAULT_STICKER_CATEGORY,
    )
    format = properties.property(
        types.Enum(STICKER_FORMATS),
        required=True,
        read_only=True,
    )
    width = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
        read_only=True,
    )
    height = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
        read_only=True,
    )
    size_bytes = properties.property(
        types.Integer(min_value=1),
        required=True,
        read_only=True,
    )
    sha256 = properties.property(
        types.String(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH),
        required=True,
        read_only=True,
    )
    media_object_id = properties.property(
        types.String(min_length=1),
        required=True,
        read_only=True,
    )
    active = properties.property(_StrictBoolean(), default=True)
    blocked = properties.property(_StrictBoolean(), default=False)

    def validate(self) -> None:
        if self.active and self.blocked:
            raise ValueError("active and blocked cannot both be true")
        if type(self.width) is bool or type(self.height) is bool:
            raise ValueError("width and height must be positive integers")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be lowercase hexadecimal")
        _validate_tags(self.tags)
        if len(self.emoji) > MAX_EMOJI_COUNT:
            raise ValueError("emoji cannot contain more than 5 values")


class StickerFavorite(models.ModelWithTimestamp, orm.SQLStorableMixin):
    """The global user-to-sticker favorite mapping."""

    __tablename__ = "m_workspace_sticker_favorites"

    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    sticker_uuid = properties.property(types.UUID(), required=True, read_only=True)

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {
            "user_uuid": cls.properties.properties["user_uuid"],
            "sticker_uuid": cls.properties.properties["sticker_uuid"],
        }


class StickerMedia(_StrictModel):
    """The media portion of a public sticker card."""

    format = properties.property(
        types.Enum(STICKER_FORMATS), required=True, read_only=True
    )
    width = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
        read_only=True,
    )
    height = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
        read_only=True,
    )
    url = properties.property(types.String(min_length=1), required=True, read_only=True)

    def validate(self) -> None:
        if type(self.width) is bool or type(self.height) is bool:
            raise ValueError("width and height must be positive integers")


class StickerCard(_StrictModel):
    """Public catalog card. Storage identity never belongs to this model."""

    id = properties.property(types.UUID(), required=True, read_only=True)
    title = properties.property(
        types.String(max_length=MAX_TITLE_LENGTH), required=True, read_only=True
    )
    alt_text = properties.property(
        types.String(max_length=MAX_ALT_TEXT_LENGTH),
        required=True,
        read_only=True,
    )
    emoji = properties.property(
        types.TypedList(types.String(min_length=1, max_length=128)),
        default=list,
        read_only=True,
    )
    tags = properties.property(
        types.TypedList(types.String(min_length=1, max_length=MAX_TAG_LENGTH)),
        default=list,
        read_only=True,
    )
    category = properties.property(
        types.Enum(STICKER_CATEGORIES), required=True, read_only=True
    )
    media = properties.property(
        types_dynamic.KindModelType(StickerMedia),
        required=True,
        read_only=True,
    )
    is_favorite = properties.property(types.Boolean(), default=False, read_only=True)

    def as_plain_dict(self) -> dict[str, object]:
        """Keep nested media serializable without exposing storage fields."""

        media = self.media
        return {
            "id": str(self.id),
            "title": self.title,
            "alt_text": self.alt_text,
            "emoji": list(self.emoji),
            "tags": list(self.tags),
            "category": self.category,
            "media": {
                "format": media.format,
                "width": media.width,
                "height": media.height,
                "url": media.url,
            },
            "is_favorite": self.is_favorite,
        }


class StickerManifestItem(_StrictModel):
    """One strict schema-v1 manifest item; media bytes are validated elsewhere."""

    client_id = properties.property(types.UUID(), required=True)
    file = properties.property(
        STICKER_FILE_PATH_TYPE,
        required=True,
    )
    sha256 = properties.property(
        types.String(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH),
        required=True,
    )
    format = properties.property(types.Enum(STICKER_FORMATS), required=True)
    title = properties.property(
        types.String(min_length=1, max_length=MAX_TITLE_LENGTH),
        required=True,
    )
    alt_text = properties.property(
        types.String(max_length=MAX_ALT_TEXT_LENGTH),
        required=True,
    )
    emoji = properties.property(
        types.TypedList(types.String(min_length=1, max_length=128)),
        required=True,
    )
    tags = properties.property(
        types.TypedList(types.String(min_length=1, max_length=MAX_TAG_LENGTH)),
        required=True,
    )
    category = properties.property(
        types.Enum(STICKER_CATEGORIES),
        default=DEFAULT_STICKER_CATEGORY,
    )
    width = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
    )
    height = properties.property(
        types.AllowNone(types.Integer(min_value=1)),
        default=None,
    )

    def validate(self) -> None:
        if type(self.width) is bool or type(self.height) is bool:
            raise ValueError("width and height must be positive integers")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be lowercase hexadecimal")
        if self.file != "media/%s.%s" % (self.client_id, self.format):
            raise ValueError("file must match client_id and format")
        if len(self.emoji) > MAX_EMOJI_COUNT:
            raise ValueError("emoji cannot contain more than 5 values")
        _validate_tags(self.tags)

    @classmethod
    def from_simple_type(cls, value: typing.Any) -> "StickerManifestItem":
        return typing.cast(
            "StickerManifestItem",
            types_dynamic.KindModelType(cls).from_simple_type(value),
        )


class StickerManifest(_StrictModel):
    """The schema-v1 manifest root, deliberately limited to two fields."""

    schema_version = properties.property(
        types.Integer(min_value=1, max_value=1),
        required=True,
    )
    items = properties.property(types.List(), required=True)

    def __init__(self, items: typing.Any = None, **kwargs: typing.Any) -> None:
        if items is not None:
            items = [
                StickerManifestItem.from_simple_type(item)
                if isinstance(item, dict)
                else item
                for item in items
            ]
            kwargs["items"] = items
        super().__init__(**kwargs)

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("manifest schema_version must be 1")
        items = self["items"]
        if not all(isinstance(item, StickerManifestItem) for item in items):
            raise ValueError("manifest items must be StickerManifestItem values")
        client_ids = [item.client_id for item in items]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("manifest client_id values must be unique")

    def to_simple_type(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "items": [
                {
                    name: prop.get_property_type().to_simple_type(item[name])
                    for name, prop in StickerManifestItem.properties.properties.items()
                }
                for item in self["items"]
            ],
        }

    @classmethod
    def from_simple_type(cls, value: typing.Any) -> "StickerManifest":
        if not isinstance(value, dict):
            raise ValueError("manifest must be an object")
        unknown = set(value).difference(("schema_version", "items"))
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError("Unknown fields: %s" % names)
        if "schema_version" not in value or "items" not in value:
            raise ValueError("manifest requires schema_version and items")
        if type(value["schema_version"]) is not int:
            raise ValueError("manifest schema_version must be 1")
        raw_items = value["items"]
        if not isinstance(raw_items, list):
            raise ValueError("manifest items must be a list")
        return cls(
            schema_version=value["schema_version"],
            items=[StickerManifestItem.from_simple_type(item) for item in raw_items],
        )


class StickerImportItemResult(_StrictModel):
    client_id = properties.property(types.UUID(), required=True, read_only=True)
    file = properties.property(
        STICKER_FILE_PATH_TYPE,
        required=True,
        read_only=True,
    )
    status = properties.property(
        types.Enum(STICKER_IMPORT_STATUSES),
        required=True,
        read_only=True,
    )
    sticker_uuid = properties.property(types.UUID(), required=True, read_only=True)


class StickerImportResult(_StrictModel):
    created = properties.property(types.Integer(min_value=0), default=0, read_only=True)
    duplicates = properties.property(
        types.Integer(min_value=0),
        default=0,
        read_only=True,
    )
    items = properties.property(
        types.TypedList(types_dynamic.KindModelType(StickerImportItemResult)),
        default=list,
        read_only=True,
    )

    def to_simple_type(self) -> dict[str, object]:
        return {
            "created": self.created,
            "duplicates": self.duplicates,
            "items": [
                {
                    name: prop.get_property_type().to_simple_type(item[name])
                    for name, prop in StickerImportItemResult.properties.properties.items()
                }
                for item in self["items"]
            ],
        }


def _validate_tags(tags: typing.Iterable[str]) -> None:
    tags_list = list(tags)
    if len(tags_list) > MAX_TAG_COUNT:
        raise ValueError("tags cannot contain more than 64 values")
    if sum(len(tag.encode("utf-8")) for tag in tags_list) > MAX_TAGS_BYTES:
        raise ValueError("tags exceed 4096 UTF-8 bytes")


# Names used by the API work packages remain explicit aliases rather than
# duplicate models, so SQL mappings and public contracts have one owner.
WorkspaceSticker = Sticker
StickerPublicCard = StickerCard
StickerManifestV1 = StickerManifest
StickerManifestV1Item = StickerManifestItem
StickerImportItem = StickerImportItemResult
