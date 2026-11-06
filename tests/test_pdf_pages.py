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

from io import BytesIO
from typing import cast

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document


def test_pdf_with_truncated_end_marker_is_rejected() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    buffer = BytesIO()
    writer.write(buffer)

    raw = buffer.getvalue().rstrip().removesuffix(b"F")
    with pytest.raises(ValueError, match="end-of-file"):
        parse_pdf_document(PDFDocumentInput(document_id="truncated", content=raw))


@pytest.mark.parametrize("encrypted", [False, True])
def test_pdf_with_underreported_page_count_is_rejected(encrypted: bool) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.add_blank_page(width=100, height=200)
    page_tree = cast(DictionaryObject, writer.root_object["/Pages"])
    page_tree[NameObject("/Count")] = NumberObject(1)
    if encrypted:
        writer.encrypt(user_password="", algorithm="AES-128")
    buffer = BytesIO()
    writer.write(buffer)

    with pytest.raises(ValueError, match="every page"):
        parse_pdf_document(PDFDocumentInput(document_id="inconsistent", content=buffer.getvalue()))
