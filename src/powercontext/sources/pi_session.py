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

"""Capture immutable evidence from one branch of an offline Pi v3 session."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Never, cast

from pydantic import BaseModel, JsonValue

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_SOURCE_TYPE = "pi-event"
_Record = dict[str, JsonValue]


class PiSessionInput(BaseModel):
    """Complete JSONL session and the last entry to include in its history."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event and its complete original record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: _Record


class PiEventSource(Source):
    """Captured Pi event addressed by its session and entry identities."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: _Record


def _required_string(record: _Record, field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Pi field {field!r} must be a non-empty string")  # noqa: TRY003
    return value


def _reject_constant(value: str) -> Never:
    raise ValueError(f"Invalid Pi JSON number {value!r}")  # noqa: TRY003


def _parse_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_constant(value)
    return number


def _records(content: bytes) -> Iterator[_Record]:
    for line in io.StringIO(content.decode("utf-8-sig"), newline="\n"):
        if not line.strip():
            continue
        record = json.loads(line, parse_constant=_reject_constant, parse_float=_parse_float)
        if not isinstance(record, dict):
            raise ValueError("Each Pi record must be a JSON object")  # noqa: TRY003, TRY004
        yield record


def _read_session(content: bytes) -> tuple[str, dict[str, _Record]]:
    records = _records(content)
    header = next(records, {})
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("Expected a Pi v3 session header")  # noqa: TRY003
    session_id = _required_string(header, "id")
    nodes: dict[str, _Record] = {}
    for record in records:
        event_id = _required_string(record, "id")
        if _required_string(record, "type") == "session":
            raise ValueError("A Pi session header must be the first record")  # noqa: TRY003
        if event_id in nodes:
            raise ValueError(f"Duplicate Pi event id {event_id!r}")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError(f"Missing parentId for Pi event {event_id!r}")  # noqa: TRY003
        parent = record["parentId"]
        if parent is not None and (not isinstance(parent, str) or parent not in nodes):
            raise ValueError(f"Pi event {event_id!r} must refer to an earlier parent")  # noqa: TRY003
        nodes[event_id] = record
    return session_id, nodes


def _compact_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _visible_text(content: JsonValue, *, tool_calls: bool = False) -> str:
    if not isinstance(content, list):
        return _text(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(text := block.get("text"), str):
            parts.append(text)
        elif tool_calls and block.get("type") == "toolCall":
            name = _required_string(block, "name")
            call_id = _required_string(block, "id")
            parts.append(f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments', {}))}")
    return "\n".join(parts)


def _message_body(message: _Record, role: str) -> str:
    if role == "bashExecution":
        return f"$ {_required_string(message, 'command')}\n{_text(message.get('output'))}"
    text = _visible_text(message.get("content"), tool_calls=role != "toolResult")
    if role == "toolResult":
        tool = _required_string(message, "toolName")
        call_id = _required_string(message, "toolCallId")
        return f"[tool_result {tool} {call_id}] {text}"
    return text


def _event_body(record: _Record, event_type: str) -> tuple[str, str]:
    if event_type == "message" and isinstance(message := record.get("message"), dict):
        role = _text(message.get("role"))
        return role, _message_body(message, role)
    if event_type in {"compaction", "branch_summary"}:
        return event_type, _text(record.get("summary"))
    if event_type == "custom_message":
        return "custom", _visible_text(record.get("content"))
    return "", ""


def _selected_events(session_id: str, nodes: dict[str, _Record], leaf_id: str | None) -> tuple[PiEventInput, ...]:
    if leaf_id is None:
        leaf_id = next(reversed(nodes), None)
    elif leaf_id not in nodes:
        raise ValueError(f"Unknown Pi leaf id {leaf_id!r}")  # noqa: TRY003

    events: list[PiEventInput] = []
    while leaf_id is not None:
        record = nodes[leaf_id]
        parent_id = cast(str | None, record["parentId"])
        event_type = cast(str, record["type"])
        role, body = _event_body(record, event_type)
        if body.strip():
            if not role:
                raise ValueError(f"Missing role for visible Pi event {leaf_id!r}")  # noqa: TRY003
            events.append(
                PiEventInput(
                    session_id=session_id,
                    event_id=leaf_id,
                    parent_id=parent_id,
                    event_type=event_type,
                    role=role,
                    content=f"{role}: {body}",
                    record=record,
                )
            )
        leaf_id = parent_id
    return tuple(reversed(events))


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the selected branch's visible events in ancestor order."""
    session_id, nodes = _read_session(value.content)
    return _selected_events(session_id, nodes, value.leaf_id)


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(_compact_json(value).encode("utf-8")).hexdigest()


def _source_id(event: PiEventInput) -> str:
    # The array framing keeps the components distinct even when IDs contain delimiters.
    return f"pi:{_digest([event.session_id, event.event_id])}"


class _PiTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=_SOURCE_TYPE,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class _PiEventDefinition:
    name = _SOURCE_TYPE
    version = "1"
    input_class = PiEventInput
    source_class = PiEventSource
    projections: tuple[SourceProjection[PiEventSource], ...] = (_PiTextEvidenceProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(name=_source_id(value), **value.model_dump())

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = _PiEventDefinition()


class _PiCheckpoint(BaseModel):
    session_id: str
    imported: dict[str, str]


class PiSessionConnector:
    """Import a selected branch and keep progress in its lifecycle binding."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({_SOURCE_TYPE})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, nodes = _read_session(await asyncio.to_thread(self._path.read_bytes))
        events = _selected_events(session_id, nodes, self._leaf_id)
        checkpoint = (
            _PiCheckpoint(session_id=session_id, imported={})
            if session.checkpoint is None
            else _PiCheckpoint.model_validate(session.checkpoint)
        )
        if checkpoint.session_id != session_id:
            raise ValueError("Pi session identity changed for this connector binding")  # noqa: TRY003

        # Check the entire imported history, including events on other branches.
        for event_id, digest in checkpoint.imported.items():
            if event_id not in nodes or _digest(nodes[event_id]) != digest:
                raise ValueError(f"Previously imported Pi event {event_id!r} changed or disappeared")  # noqa: TRY003

        pending = [
            (event, _source_id(event), _digest(event.record))
            for event in events
            if event.event_id not in checkpoint.imported
        ]
        for event, source_id, digest in pending:
            outcome = await session.submit(source_id, _SOURCE_TYPE, event)
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
            checkpoint.imported[event.event_id] = digest
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint.model_dump(mode="json"))
