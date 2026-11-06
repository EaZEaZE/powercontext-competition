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

"""Stateless wire encoding and prefix reuse for prepared Context."""

from __future__ import annotations

import json
import re
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Self

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """Invalid Context Pack, delta, or UTF-8 chunk boundary."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """An immutable full pack whose digest identifies the exact ordered chunks."""

    items: tuple[bytes, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_items(self.items)
        object.__setattr__(self, "digest", f"sha256:{sha256(_json_bytes(self._body())).hexdigest()}")

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> Self:
        return cls(items)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> Self:
        """Split text on UTF-8 boundaries, preferring line ends within the byte cap."""
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if not isinstance(content, str):
            raise ContextPackError("content must be text")  # noqa: TRY003
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContextPackError("content is not valid UTF-8 text") from error  # noqa: TRY003

        items: list[bytes] = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk_bytes cannot fit a UTF-8 character")  # noqa: TRY003
            # A line end can preserve a reusable prefix when the next line changes.
            line_end = data.rfind(b"\n", start, end)
            if line_end >= start:
                end = line_end + 1
            items.append(data[start:end])
            start = end
        return cls.build(tuple(items))

    def _body(self) -> dict[str, object]:
        return {"schema": _PACK_SCHEMA, "items": _encode_items(self.items)}

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        body = {**self._body(), "digest": self.digest, "byte_count": 0}
        # The length includes the digits of byte_count itself.
        while True:
            encoded = _json_bytes(body)
            if len(encoded) == body["byte_count"]:
                return encoded
            body["byte_count"] = len(encoded)

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        payload = _read_message(data, _PACK_SCHEMA, {"items", "digest", "byte_count"})
        pack = cls.build(_decode_items(payload["items"]))
        if pack.digest != payload["digest"]:
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count != len(data) or byte_count != pack.byte_count:
            raise ContextPackError("pack byte_count does not match its wire length")  # noqa: TRY003
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Reuse a prefix of a known base and carry the remaining target chunks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> Self:
        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        _validate_items(self.appended)
        return _json_bytes({
            "schema": _DELTA_SCHEMA,
            "base_digest": _parse_digest(self.base_digest),
            "target_digest": _parse_digest(self.target_digest),
            "prefix_items": _parse_prefix(self.prefix_items),
            "appended": _encode_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        payload = _read_message(data, _DELTA_SCHEMA, {"base_digest", "target_digest", "prefix_items", "appended"})
        return cls(
            base_digest=_parse_digest(payload["base_digest"]),
            target_digest=_parse_digest(payload["target_digest"]),
            prefix_items=_parse_prefix(payload["prefix_items"]),
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild and verify a target using only the base and the received delta."""
    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the supplied pack")  # noqa: TRY003
    prefix = _parse_prefix(delta.prefix_items)
    if prefix > len(base.items):
        raise ContextPackError("delta prefix exceeds the number of base items")  # noqa: TRY003
    _validate_items(delta.appended)
    target = ContextPack.build(base.items[:prefix] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match the reconstructed pack")  # noqa: TRY003
    return target


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_message(data: bytes, schema: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise ContextPackError("wire message must be UTF-8 bytes")  # noqa: TRY003
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as error:
        raise ContextPackError("invalid Context Pack JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or payload.keys() != fields | {"schema"} or payload["schema"] != schema:
        raise ContextPackError(f"invalid fields or schema for {schema}")  # noqa: TRY003
    return payload


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ContextPackError("duplicate JSON field")  # noqa: TRY003
    return fields


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("pack items must be a tuple of bytes")  # noqa: TRY003


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("wire items must be an array of Base64 strings")  # noqa: TRY003
    items: list[bytes] = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError("wire items must be Base64 strings")  # noqa: TRY003
        try:
            raw = b64decode(item, validate=True)
        except ValueError as error:
            raise ContextPackError("invalid Base64 item") from error  # noqa: TRY003
        if b64encode(raw).decode("ascii") != item:
            raise ContextPackError("non-canonical Base64 item")  # noqa: TRY003
        items.append(raw)
    return tuple(items)


def _parse_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ContextPackError("expected a lowercase sha256 digest")  # noqa: TRY003
    return value


def _parse_prefix(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ContextPackError("delta prefix_items must be a nonnegative integer")  # noqa: TRY003
    return value


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
