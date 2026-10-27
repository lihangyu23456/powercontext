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

"""Page-level PDF Source evidence with document-scoped retrieval."""

from __future__ import annotations

import hashlib
import io
import unicodedata
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import BaseModel

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, MemoryEntryRecord, MemorySearchPage, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from pypdf import PdfReader

PDF_PAGE_SOURCE_NAME: Final = "pdf-page"
PDF_PAGE_SOURCE_VERSION: Final = "1"


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a PDF document."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Captured PDF page kept as one durable piece of evidence."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


class _ScopedMemory(Protocol):
    """The scoped Memory application that owns retrieved page evidence."""

    async def search(self, request: SearchMemoryRequest, /) -> MemorySearchPage: ...

    async def get(self, request: GetMemoryEntryRequest, /) -> MemoryEntryRecord: ...


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable identity of one physical PDF page."""

    identity = f"pdf:{_encode(document_id)}:{document_sha256}:{page_number}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    return f"pdf:{digest}:{document_sha256}:{page_number}"


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF document.

    The whole document is decoded, parsed and validated before any page is
    returned, so a damaged document never yields partial pages.
    """

    document_id = value.document_id
    if not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-empty trimmed string")  # noqa: TRY003
    if not value.content:
        raise ValueError("PDF content must not be empty")  # noqa: TRY003

    reader = _open_reader(value.content)
    document_sha256 = hashlib.sha256(value.content).hexdigest()
    try:
        total = len(reader.pages)
    except Exception as error:
        raise ValueError("PDF pages could not be read") from error

    pages: list[PDFPageInput] = []
    for index in range(total):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception as error:
            raise ValueError(f"PDF page {index + 1} could not be parsed") from error
        pages.append(
            PDFPageInput(
                document_id=document_id,
                document_sha256=document_sha256,
                page_number=index + 1,
                text=unicodedata.normalize("NFC", text),
            )
        )
    return tuple(pages)


async def search_pdf_pages(
    memory: _ScopedMemory,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return exact Memory citations for the pages that best answer one question.

    Every citation is read back through the public Memory API so the result
    only contains distinct physical pages that the Memory really holds.
    """

    if limit < 1:
        return ()
    page = await memory.search(
        SearchMemoryRequest(query=question, mode="fts", limit=max(limit * 4, 40))
    )
    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for hit in page.hits:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        record = await memory.get(GetMemoryEntryRequest(citation=citation))
        identities = tuple(sorted({reference.source_id for reference in record.entry.sources}))
        if not identities or identities[0] in seen:
            continue
        seen.add(identities[0])
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


class PDFPageSourceAdapter:
    """Resolve and read captured PDF pages."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one captured PDF page as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


def _open_reader(content: bytes) -> PdfReader:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as error:
        raise ValueError("PDF content could not be parsed") from error
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception as error:
            raise ValueError("PDF content could not be decrypted") from error
        if not decrypted:
            raise ValueError("PDF content requires a password")  # noqa: TRY003
    return reader


def _encode(document_id: str) -> str:
    from urllib.parse import quote

    return quote(document_id, safe="")


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
