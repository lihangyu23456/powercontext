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

"""Row-level CSV Source evidence with a stable primary-key identity."""

from __future__ import annotations

import csv
import io
import json
from typing import Final
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME: Final = "csv-row"
CSV_ROW_SOURCE_VERSION: Final = "1"
_CSV_ROW_ID_PREFIX: Final = "csv:"
_UTF8_BOM: Final = b"\xef\xbb\xbf"


class CSVDocumentInput(BaseModel):
    """One offline CSV document addressed by its primary-key column."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed CSV row that keeps its column order and raw field values."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """Captured CSV row kept as one durable piece of evidence."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Return the stable identity of one CSV row."""

    _validate_document_id(document_id)
    if not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-blank value")  # noqa: TRY003
    identity = f"{_CSV_ROW_ID_PREFIX}{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one offline CSV document into validated row-level inputs.

    The whole document is decoded, parsed and validated before any row is
    returned, so a malformed document never yields partial results.
    """

    _validate_document_id(value.document_id)
    if not value.primary_key:
        raise ValueError("primary_key must name a column")  # noqa: TRY003
    columns, records = _parse_records(_decode_csv(value.content))
    primary_key_index = _primary_key_index(columns, value.primary_key)
    return _parse_rows(value, columns, primary_key_index, records)


class CSVRowSourceAdapter:
    """Resolve and read row-level CSV evidence."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one captured CSV row as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
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


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSVRowSourceAdapter(),
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _validate_document_id(document_id: str) -> None:
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty trimmed string")  # noqa: TRY003


def _decode_csv(content: bytes) -> str:
    if content.startswith(_UTF8_BOM):
        content = content[len(_UTF8_BOM) :]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003


def _read_records(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",", quotechar='"', doublequote=True, strict=True)
    try:
        return list(reader)
    except csv.Error as error:
        raise ValueError(f"CSV content is malformed: {error}") from error  # noqa: TRY003


def _parse_records(text: str) -> tuple[tuple[str, ...], list[list[str]]]:
    records = _read_records(text)
    if not records or not records[0]:
        raise ValueError("CSV document must contain a header row")  # noqa: TRY003
    header = records[0]
    columns = tuple(header)
    seen: set[str] = set()
    for column in columns:
        if not column.strip():
            raise ValueError("CSV header fields must be non-empty and non-blank")  # noqa: TRY003
        if column in seen:
            raise ValueError(f"CSV header field {column!r} is duplicated")  # noqa: TRY003
        seen.add(column)
    return columns, records[1:]


def _primary_key_index(columns: tuple[str, ...], primary_key: str) -> int:
    try:
        return columns.index(primary_key)
    except ValueError:
        raise ValueError(f"primary_key {primary_key!r} must match a CSV header field") from None  # noqa: TRY003


def _parse_rows(
    value: CSVDocumentInput,
    columns: tuple[str, ...],
    primary_key_index: int,
    records: list[list[str]],
) -> tuple[CSVRowInput, ...]:
    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError(f"CSV record has {len(record)} fields but the header declares {len(columns)}")  # noqa: TRY003
        primary_key_value = record[primary_key_index]
        if not primary_key_value.strip():
            raise ValueError("CSV primary key value must be a non-blank value")  # noqa: TRY003
        if primary_key_value in seen:
            raise ValueError(f"CSV primary key value {primary_key_value!r} is duplicated")  # noqa: TRY003
        seen.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
