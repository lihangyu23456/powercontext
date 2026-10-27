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
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def test_parse_keeps_column_order_and_raw_values() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="doc",
            primary_key="id",
            content=b"id,name,note\n01,Alpha,  spaced  \n",
        )
    )
    assert rows == (
        CSVRowInput(
            document_id="doc",
            primary_key="id",
            primary_key_value="01",
            columns=("id", "name", "note"),
            values=("01", "Alpha", "  spaced  "),
        ),
    )


def test_parse_accepts_bom_and_multiline_quoted_fields() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="doc",
            primary_key=" id ",
            content=b'\xef\xbb\xbf" id ",name,note\r\n001,"a, b","line1\r\nline2 ""q"""\r\n',
        )
    )
    assert rows[0].columns == (" id ", "name", "note")
    assert rows[0].values == ("001", "a, b", 'line1\r\nline2 "q"')


def test_parse_ignores_empty_records_and_accepts_header_only_documents() -> None:
    assert parse_csv_document(
        CSVDocumentInput(document_id="doc", primary_key="id", content=b"id,name\n\n")
    ) == ()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"id,name\n1,one\n1,duplicate\n",
        b"id,name\n,empty\n",
        b"id,name\n1\n",
        b"id,id\n1,1\n",
        b",name\n1,one\n",
        b"id,name\n1,\"unclosed\n",
    ],
)
def test_parse_rejects_invalid_documents_without_partial_rows(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))


def test_source_identity_is_stable_and_delimiter_safe() -> None:
    assert csv_row_source_id("doc:one", "key/one") == "csv:doc%3Aone:key%2Fone"
    assert csv_row_source_id("doc:one", "key/one") != csv_row_source_id("doc:two", "key/one")
    with pytest.raises(ValueError):
        csv_row_source_id(" doc ", "key")
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "x" * 260)


def test_projection_exposes_the_row_as_ordered_json_evidence() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(document_id="doc", primary_key="id", content=b"id,name\n01,Alpha\n")
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(rows[0]))
    assert isinstance(source, CSVRowSource)
    assert source.source_id == source.name == "csv:doc:01"
    assert asyncio.run(registry.read(source)) == rows[0]

    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "csv-row"
    assert evidence.source_id == "csv:doc:01"
    assert evidence.content == json.dumps({"id": "01", "name": "Alpha"}, ensure_ascii=False, separators=(",", ":"))
    assert evidence.metadata == {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "01",
        "columns": ["id", "name"],
        "row": {"id": "01", "name": "Alpha"},
    }
