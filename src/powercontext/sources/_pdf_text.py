# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Extract PDF text with spacing and ligature information from the source page."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from pypdf.generic import (
    ByteStringObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    StreamObject,
    TextStringObject,
)

if TYPE_CHECKING:
    from pypdf import PageObject
    from pypdf.generic import ContentStream, PdfObject

_CMAP = NameObject("/ToUnicode")
_WHITESPACE = re.compile(r"\s+")
_HORIZONTAL_GAP = re.compile(r"[ \t]+")
_LATIN_WORDS = re.compile(r"[a-z]+")
_SHORT_WORDS = frozenset({
    "a",
    "am",
    "an",
    "as",
    "at",
    "be",
    "by",
    "do",
    "go",
    "he",
    "i",
    "if",
    "in",
    "is",
    "it",
    "me",
    "my",
    "no",
    "of",
    "on",
    "or",
    "so",
    "to",
    "up",
    "us",
    "we",
})
_Glyph = tuple[str, bytes]
_Ligatures = dict[str, dict[bytes, set[str]]]
_MarkedSpan = tuple[str | None, list[_Glyph]]


def extract_page_text(page: PageObject) -> str:
    """Use positioned text when it accounts for all the glyphs in the plain pass."""

    contents = page.get_contents()
    patches = _ligature_maps(page, contents) if contents is not None and b"/ActualText" in contents.get_data() else ()
    for font, _, updated in patches:
        font[_CMAP] = updated
    try:
        plain = page.extract_text()
        if not plain.strip():
            return plain
        try:
            layout = page.extract_text(extraction_mode="layout", layout_mode_space_vertically=False)
        except (ArithmeticError, ValueError):
            # Plain extraction remains usable when glyphs lack usable layout dimensions.
            return plain
        # Layout mode can omit rotated text and Form XObjects. Use the plain
        # pass when the two disagree on the inventory of non-whitespace glyphs.
        if _prefer_layout(plain, layout):
            return "\n".join(_HORIZONTAL_GAP.sub(" ", line.strip()) for line in layout.splitlines())
        return plain
    finally:
        for font, original, _ in patches:
            font[_CMAP] = original


def _prefer_layout(plain: str, layout: str) -> bool:
    if Counter(_WHITESPACE.sub("", layout)) != Counter(_WHITESPACE.sub("", plain)):
        return False
    # Adjacent table columns can also be joined by the layout pass. Compare
    # only changed words for signs of splitting or joining.
    plain_words = Counter(_LATIN_WORDS.findall(plain.casefold()))
    layout_words = Counter(_LATIN_WORDS.findall(layout.casefold()))
    return _word_fragments(layout_words - plain_words) <= _word_fragments(plain_words - layout_words)


def _word_fragments(words: Counter[str]) -> int:
    return sum(
        count for word, count in words.items() if (len(word) <= 2 and word not in _SHORT_WORDS) or len(word) >= 22
    )


def _ligature_maps(
    page: PageObject, contents: ContentStream
) -> tuple[tuple[DictionaryObject, PdfObject, DecodedStreamObject], ...]:
    resources = page.get("/Resources")
    if resources is None:
        return ()
    fonts = resources.get_object().get("/Font")
    if fonts is None:
        return ()
    fonts = fonts.get_object()
    if not isinstance(fonts, DictionaryObject):
        return ()

    replacements = _single_glyph_actual_text(contents)
    patches = []
    for font_name, codes in replacements.items():
        font = fonts.get(font_name)
        if font is None or not isinstance(font := font.get_object(), DictionaryObject):
            continue
        original = font.get(_CMAP)
        cmap = original.get_object() if original is not None else None
        if not isinstance(cmap, StreamObject):
            continue
        # Marked content can supply the correct text for a ligature when a
        # font's ToUnicode table maps it to NUL or a presentation-form glyph.
        code_size = 2 if font.get("/Encoding") in ("/Identity-H", "/Identity-V") else 1
        rows = [
            b"<" + code.hex().encode() + b"> <" + next(iter(values)).encode("utf-16-be").hex().encode() + b">"
            for code, values in codes.items()
            if len(code) == code_size and len(values) == 1
        ]
        if not rows:
            continue
        mapping = str(len(rows)).encode() + b" beginbfchar\n" + b"\n".join(rows) + b"\nendbfchar\n"
        updated = DecodedStreamObject()
        data = cmap.get_data()
        end = data.rfind(b"endcmap")
        updated.set_data(data + b"\n" + mapping if end < 0 else data[:end] + mapping + data[end:])
        patches.append((font, original, updated))
    return tuple(patches)


def _single_glyph_actual_text(contents: ContentStream) -> _Ligatures:
    """Read explicit ligature text from marked spans containing one shown glyph."""

    mappings: _Ligatures = defaultdict(lambda: defaultdict(set))
    font = ""
    saved_fonts: list[str] = []
    spans: list[_MarkedSpan] = []
    for operands, operator in contents.operations:
        if operator == b"Tf":
            font = str(operands[0])
        elif operator == b"q":
            saved_fonts.append(font)
        elif operator == b"Q" and saved_fonts:
            font = saved_fonts.pop()
        elif operator in {b"BMC", b"BDC"}:
            spans.append((_marked_ligature(operands), []))
        elif operator == b"EMC" and spans:
            _finish_span(spans, mappings)
        elif spans and operator in {b"Tj", b"TJ", b"'", b'"'}:
            strings = operands[-1] if operator == b"TJ" else (operands[-1],)
            spans[-1][1].extend(_shown_glyphs(font, strings))
    return mappings


def _marked_ligature(operands: list[PdfObject]) -> str | None:
    properties = operands[1] if len(operands) > 1 else None
    actual = properties.get("/ActualText") if isinstance(properties, DictionaryObject) else None
    return actual if isinstance(actual, str) and 2 <= len(actual) <= 3 and actual.isalpha() else None


def _finish_span(spans: list[_MarkedSpan], mappings: _Ligatures) -> None:
    actual, glyphs = spans.pop()
    if actual is not None and len(glyphs) == 1:
        font, code = glyphs[0]
        mappings[font][code].add(actual)
    if spans:
        spans[-1][1].extend(glyphs)


def _shown_glyphs(font: str, strings: Iterable[PdfObject]) -> Iterator[_Glyph]:
    for value in strings:
        if isinstance(value, TextStringObject):
            yield font, value.original_bytes
        elif isinstance(value, ByteStringObject):
            yield font, bytes(value)
