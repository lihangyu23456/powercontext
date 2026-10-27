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
import hashlib
import io

import pytest

from powercontext.sources import SourceDefinitionRegistry, TextEvidence
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    pdf_page_source_id,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

pypdf = pytest.importorskip("pypdf")


def _blank_pdf(page_count: int, *, encrypted: bool = False) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=200)
    if encrypted:
        writer.encrypt(user_password="", owner_password="owner")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_parse_returns_every_physical_page_in_order() -> None:
    content = _blank_pdf(3)
    pages = parse_pdf_document(PDFDocumentInput(document_id="doc", content=content))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert all(page.document_id == "doc" for page in pages)
    assert all(page.document_sha256 == hashlib.sha256(content).hexdigest() for page in pages)
    assert all(page.text == "" for page in pages)


def test_empty_password_permission_marker_stays_readable() -> None:
    pages = parse_pdf_document(
        PDFDocumentInput(document_id="permissions", content=_blank_pdf(1, encrypted=True))
    )
    assert [page.page_number for page in pages] == [1]


def test_source_identity_follows_the_document_revision() -> None:
    first = pdf_page_source_id("doc", "a" * 64, 3)
    second = pdf_page_source_id("doc", "b" * 64, 3)
    assert first != second
    assert first == pdf_page_source_id("doc", "a" * 64, 3)
    assert len(pdf_page_source_id("d" * 400, "a" * 64, 3)) <= 256


def test_projection_exposes_the_page_as_document_scoped_evidence() -> None:
    page = PDFPageInput(document_id="doc", document_sha256="a" * 64, page_number=2, text="Net revenue was 1.")
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(page))
    assert source.name == source.source_id == pdf_page_source_id("doc", "a" * 64, 2)
    assert asyncio.run(registry.read(source)) == page

    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "pdf-page"
    assert evidence.source_id == source.source_id
    assert evidence.content == "doc\nNet revenue was 1."
    assert evidence.metadata == {
        "document_id": "doc",
        "document_sha256": "a" * 64,
        "page_number": 2,
    }


@pytest.mark.parametrize("raw", [b"", b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_corrupt_documents_fail_atomically(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="invalid", content=raw))


def test_blank_document_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=" ", content=b"%PDF-1.7\n"))
