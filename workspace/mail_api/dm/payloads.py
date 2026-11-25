# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic


class TextPart(types_dynamic.AbstractKindModel):
    KIND = "text"

    content = properties.property(types.String(), required=True)


class HtmlPart(types_dynamic.AbstractKindModel):
    KIND = "html"

    content = properties.property(types.String(), required=True)


PART_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(TextPart),
    types_dynamic.KindModelType(HtmlPart),
)


class MultipartPayload(types_dynamic.AbstractKindModel):
    KIND = "multipart"

    parts = properties.property(types.TypedList(PART_TYPE), required=True)


PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(MultipartPayload),
)
