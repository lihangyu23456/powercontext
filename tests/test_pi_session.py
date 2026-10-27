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

from __future__ import annotations

import asyncio
import json

import pytest

from powercontext.sources import SourceDefinitionRegistry, TextEvidence
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionInput,
    parse_pi_session,
    pi_event_source_id,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

_HEADER = {"type": "session", "version": 3, "id": "session-1"}


def archive(*records: dict) -> bytes:
    return "\n".join(json.dumps(record) for record in (_HEADER, *records)).encode()


def test_parse_returns_visible_events_from_root_to_leaf() -> None:
    raw = archive(
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "alpha"}},
        {"type": "model_change", "id": "control", "parentId": "a"},
        {"type": "message", "id": "b", "parentId": "control", "message": {"role": "assistant", "content": "beta"}},
    )
    events = parse_pi_session(PiSessionInput(content=raw))
    assert [event.event_id for event in events] == ["a", "b"]
    assert [event.content for event in events] == ["user: alpha", "assistant: beta"]
    assert events[1].parent_id == "control"


def test_parse_renders_tool_calls_results_and_summaries() -> None:
    raw = archive(
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "toolCall", "name": "read", "id": "call-1", "arguments": {"path": "a.txt"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call-1",
                "content": [{"type": "text", "text": "file contents"}],
            },
        },
        {"type": "compaction", "id": "c", "parentId": "b", "summary": "retained summary"},
        {"type": "branch_summary", "id": "d", "parentId": "c", "summary": "branch summary"},
    )
    events = parse_pi_session(PiSessionInput(content=raw))
    assert [event.content for event in events] == [
        'assistant: [tool_call read call-1] {"path":"a.txt"}',
        "toolResult: [tool_result read call-1] file contents",
        "compaction: retained summary",
        "branch_summary: branch summary",
    ]
    assert [event.role for event in events] == ["assistant", "toolResult", "compaction", "branch_summary"]


def test_parse_accepts_bom_and_rejects_malformed_archives() -> None:
    raw = b"\xef\xbb\xbf" + archive(
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "hi"}}
    )
    assert parse_pi_session(PiSessionInput(content=raw))[0].content == "user: hi"

    for invalid in (b"", b"{}\n", b"[]\n", b"null\n", b"\xff", b'{"type":"session","version":2,"id":"x"}\n'):
        with pytest.raises(ValueError):
            parse_pi_session(PiSessionInput(content=invalid))


def test_parse_rejects_broken_graphs_atomically() -> None:
    missing_parent = archive({"type": "model_change", "id": "a", "parentId": "missing"})
    self_cycle = archive({"type": "model_change", "id": "a", "parentId": "a"})
    duplicated = archive(
        {"type": "model_change", "id": "a", "parentId": None},
        {"type": "model_change", "id": "a", "parentId": None},
    )
    for raw in (missing_parent, self_cycle, duplicated):
        with pytest.raises(ValueError):
            parse_pi_session(PiSessionInput(content=raw))
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive(), leaf_id="absent"))


def test_source_identity_and_projection_keep_the_full_event() -> None:
    raw = archive(
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "alpha"}}
    )
    event = parse_pi_session(PiSessionInput(content=raw))[0]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(event))
    assert source.name == source.source_id == "pi:session-1:a"
    assert asyncio.run(registry.read(source)) == event

    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "pi-event"
    assert evidence.source_id == "pi:session-1:a"
    assert evidence.content == "user: alpha"
    assert evidence.metadata == {
        "session_id": "session-1",
        "event_id": "a",
        "parent_id": None,
        "event_type": "message",
        "role": "user",
    }
