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

"""Pi v3 session history evidence with branch-aware incremental capture."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    TextEvidence,
)
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME: Final = "pi-event"
PI_SESSION_CONNECTOR_NAME: Final = "pi-session-history"
PI_SESSION_VERSION: Final = "1"
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_CHECKPOINT_VERSION: Final = 1


class PiSessionInput(BaseModel):
    """One offline Pi session archive and the branch it should reveal."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch of a Pi session."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """Captured Pi session event kept as one durable piece of evidence."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PiArchive:
    """Parsed session header and the full node graph of one archive."""

    session_id: str
    nodes: dict[str, dict[str, Any]]
    order: tuple[str, ...]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable identity of one Pi session event."""

    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the visible events of the selected branch, ordered from root to leaf.

    The whole archive is decoded, parsed and validated before any event is
    produced, so a malformed session never yields partial results.
    """

    archive = _parse_archive(value.content)
    return _branch_events(archive, value.leaf_id)


class PiEventSourceAdapter:
    """Resolve and read captured Pi session events."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

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


class PiEventTextEvidenceProjection:
    """Expose one captured Pi session event as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PiEventSourceAdapter(),
    version=PI_SESSION_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Capture the visible history of one Pi session branch, incrementally."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        """Import the events of the selected branch that were never captured yet."""

        archive = _parse_archive(self._path.read_bytes())
        events = _branch_events(archive, self._leaf_id)
        session_id, captured = _captured_events(session.checkpoint)
        if session_id is not None and session_id != archive.session_id:
            raise ValueError("Pi session connector is bound to a different session")  # noqa: TRY003
        _verify_captured_records(archive, captured)

        imported = tuple(event for event in events if event.event_id not in captured)
        for event in imported:
            await session.submit(
                pi_event_source_id(event.session_id, event.event_id),
                PI_EVENT_SOURCE_NAME,
                event,
            )

        merged = dict(captured)
        for event in imported:
            merged[event.event_id] = {
                "identity": pi_event_source_id(event.session_id, event.event_id),
                "digest": _record_digest(event.record),
            }
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={
                "version": _CHECKPOINT_VERSION,
                "session_id": archive.session_id,
                "events": merged,
            },
        )


def _parse_archive(content: bytes) -> _PiArchive:
    text = _decode(content)
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session line {number} is not valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError(f"Pi session line {number} must be a JSON object")  # noqa: TRY003
        records.append(record)

    if not records:
        raise ValueError("Pi session must contain a session header")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session":
        raise ValueError("Pi session must start with a session record")  # noqa: TRY003
    if header.get("version") != 3:
        raise ValueError("Pi session version must be 3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header must carry a non-empty id")  # noqa: TRY003

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Pi session records must carry a non-empty id")  # noqa: TRY003
        if event_id in nodes:
            raise ValueError(f"Pi session event {event_id!r} is duplicated")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError("Pi session parentId must be a string or null")  # noqa: TRY003
        if parent_id and parent_id not in nodes:
            raise ValueError(f"Pi session parent {parent_id!r} is missing")  # noqa: TRY003
        nodes[event_id] = record
        order.append(event_id)
    return _PiArchive(session_id=session_id, nodes=nodes, order=tuple(order))


def _branch_events(archive: _PiArchive, leaf_id: str | None) -> tuple[PiEventInput, ...]:
    events: list[PiEventInput] = []
    for event_id in _resolve_chain(archive, leaf_id):
        record = archive.nodes[event_id]
        role = _visible_role(record)
        if role is None:
            continue
        body = _visible_body(record, role)
        if body is None:
            continue
        events.append(
            PiEventInput(
                session_id=archive.session_id,
                event_id=event_id,
                parent_id=record.get("parentId") or None,
                event_type=str(record.get("type")),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


def _resolve_chain(archive: _PiArchive, leaf_id: str | None) -> tuple[str, ...]:
    if leaf_id is None:
        if not archive.order:
            raise ValueError("Pi session does not contain any event")  # noqa: TRY003
        leaf = archive.order[-1]
    else:
        leaf = leaf_id
    if leaf not in archive.nodes:
        raise ValueError(f"Pi session leaf {leaf!r} is missing")  # noqa: TRY003

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = leaf
    while current is not None:
        if current in seen:
            raise ValueError("Pi session history contains a cycle")  # noqa: TRY003
        seen.add(current)
        chain.append(current)
        current = archive.nodes[current].get("parentId") or None
    chain.reverse()
    return tuple(chain)


def _visible_role(record: dict[str, Any]) -> str | None:
    kind = record.get("type")
    if kind in {"compaction", "branch_summary"}:
        return str(kind)
    if kind == "custom_message":
        return "custom"
    if kind != "message":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if not isinstance(role, str) or not role:
        return None
    return role


def _visible_body(record: dict[str, Any], role: str) -> str | None:
    kind = record.get("type")
    if kind in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        return summary if isinstance(summary, str) and summary else None
    message = record.get("message")
    if kind == "custom_message":
        return _visible_text(record.get("content"))
    if not isinstance(message, dict):
        return None
    if role == "toolResult":
        return _tool_result_body(message)
    if role == "bashExecution":
        return _bash_execution_body(message)
    return _visible_text(message.get("content"))


def _tool_result_body(message: dict[str, Any]) -> str | None:
    tool_name = message.get("toolName")
    tool_call_id = message.get("toolCallId")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    return f"[tool_result {tool_name} {tool_call_id}] {_visible_text(message.get('content')) or ''}"


def _bash_execution_body(message: dict[str, Any]) -> str | None:
    command = message.get("command")
    if not isinstance(command, str) or not command:
        return None
    output = message.get("output")
    return f"$ {command}\n{output if isinstance(output, str) else ''}"


def _visible_text(content: object) -> str | None:
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif kind == "toolCall":
            rendered = _tool_call_body(block)
            if rendered is not None:
                parts.append(rendered)
    if not parts:
        return None
    return "\n".join(parts)


def _tool_call_body(block: dict[str, Any]) -> str | None:
    name = block.get("name")
    call_id = block.get("id")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(call_id, str) or not call_id:
        return None
    arguments = block.get("arguments")
    rendered = json.dumps(
        arguments if arguments is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"[tool_call {name} {call_id}] {rendered}"


def _decode(content: bytes) -> str:
    if content.startswith(_UTF8_BOM):
        content = content[len(_UTF8_BOM) :]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error


def _captured_events(checkpoint: object) -> tuple[str | None, dict[str, dict[str, str]]]:
    if checkpoint is None:
        return None, {}
    if not isinstance(checkpoint, dict):
        raise ValueError("Pi session checkpoint must be a JSON object")  # noqa: TRY003
    events = checkpoint.get("events", {})
    if not isinstance(events, dict):
        raise ValueError("Pi session checkpoint events must be a JSON object")  # noqa: TRY003
    session_id = checkpoint.get("session_id")
    return (session_id if isinstance(session_id, str) else None), events


def _verify_captured_records(archive: _PiArchive, captured: dict[str, dict[str, str]]) -> None:
    for event_id, entry in captured.items():
        record = archive.nodes.get(event_id)
        if record is None:
            continue
        expected = entry.get("digest") if isinstance(entry, dict) else None
        if expected != _record_digest(record):
            raise ValueError(f"Pi session event {event_id!r} was rewritten after capture")  # noqa: TRY003


def _record_digest(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
