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

"""Capture offline CSV records as stable, independently retrievable Sources."""

from __future__ import annotations

import csv
import io
import json
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_SOURCE_TYPE = "csv-row"


class CSVDocumentInput(BaseModel):
    """Complete offline CSV bytes and the name of their primary key column."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """A CSV record with all its fields in their original order."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """Captured record identified by its document and primary key value."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def _validate_document_id(document_id: str) -> None:
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty and have no surrounding whitespace")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Encode both identity components without changing their original values."""
    _validate_document_id(document_id)
    if not primary_key_value.strip():
        raise ValueError("primary key value must not be blank")  # noqa: TRY003
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def _primary_key_index(columns: tuple[str, ...], primary_key: str) -> int:
    if not columns or any(not column.strip() for column in columns):
        raise ValueError("CSV header must contain non-blank column names")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV column names must be unique")  # noqa: TRY003
    if primary_key not in columns:
        raise ValueError("primary_key must exactly match a CSV column")  # noqa: TRY003
    return columns.index(primary_key)


def _key_value(columns: tuple[str, ...], values: tuple[str, ...], key_index: int) -> str:
    if len(values) != len(columns):
        raise ValueError("CSV record width does not match the header")  # noqa: TRY003
    return values[key_index]


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Return all records after validating the complete document."""
    _validate_document_id(value.document_id)
    reader = csv.reader(io.StringIO(value.content.decode("utf-8-sig"), newline=""), strict=True)
    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    try:
        columns = tuple(next(reader, ()))
        key_index = _primary_key_index(columns, value.primary_key)
        for record in reader:
            if not record:
                continue
            values = tuple(record)
            key = _key_value(columns, values, key_index)
            csv_row_source_id(value.document_id, key)
            if key in seen:
                raise ValueError("CSV primary key values must be unique within a document")  # noqa: TRY003
            seen.add(key)
            rows.append(
                CSVRowInput(
                    document_id=value.document_id,
                    primary_key=value.primary_key,
                    primary_key_value=key,
                    columns=columns,
                    values=values,
                )
            )
    except csv.Error as error:
        raise ValueError(f"Invalid CSV near line {reader.line_num}: {error}") from error  # noqa: TRY003
    return tuple(rows)


class _CSVTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=_SOURCE_TYPE,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


class _CSVRowDefinition:
    name = _SOURCE_TYPE
    version = "1"
    input_class = CSVRowInput
    source_class = CSVRowSource
    projections: tuple[SourceProjection[CSVRowSource], ...] = (_CSVTextEvidenceProjection(),)

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        key_index = _primary_key_index(value.columns, value.primary_key)
        key = _key_value(value.columns, value.values, key_index)
        if value.primary_key_value != key:
            raise ValueError("primary_key_value must match its CSV field")  # noqa: TRY003
        source_id = csv_row_source_id(value.document_id, key)
        return CSVRowSource(name=source_id, source_id=source_id, **value.model_dump())

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = _CSVRowDefinition()
