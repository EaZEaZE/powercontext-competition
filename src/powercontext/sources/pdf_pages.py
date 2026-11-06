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

"""Capture physical PDF pages with the pdf extra and search with the builtin extra."""

from __future__ import annotations

import re
from collections import Counter
from hashlib import sha256
from io import BytesIO
from itertools import pairwise
from math import log1p
from typing import TYPE_CHECKING, Annotated, NamedTuple, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from powercontext.artifacts import MemoryCitation
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryHit
    from powercontext.builtin.runtime.application import ScopedMemoryApplication

_SOURCE_TYPE = "pdf-page"


def _nonblank_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document_id must not be blank")  # noqa: TRY003
    return value


_DocumentId = Annotated[str, AfterValidator(_nonblank_id)]


class PDFDocumentInput(BaseModel):
    """Original PDF bytes and the caller's stable document identity."""

    document_id: _DocumentId
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of an exact PDF revision."""

    model_config = ConfigDict(frozen=True)

    document_id: _DocumentId
    document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page_number: Annotated[int, Field(ge=1)]
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract all pages in physical order, or raise ValueError for an unreadable PDF."""

    from pypdf import PasswordType, PdfReader
    from pypdf.errors import PdfReadError
    from pypdf.generic import DictionaryObject

    content = value.content
    # pypdf can recover a truncated EOF marker even in strict mode.
    if not content.rstrip(b"\x00\t\n\f\r ").endswith(b"%%EOF"):
        raise ValueError("PDF is missing its end-of-file marker")  # noqa: TRY003
    try:
        with BytesIO(content) as stream, PdfReader(stream, strict=True) as reader:
            if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                raise PdfReadError("PDF requires a password")  # noqa: TRY003, TRY301
            page_tree = cast(DictionaryObject, reader.root_object["/Pages"])
            count = page_tree["/Count"]
            if not isinstance(count, int) or count != len(reader.pages):
                raise PdfReadError("PDF page tree has an inconsistent count")  # noqa: TRY003, TRY301
            texts = tuple(page.extract_text() for page in reader.pages)
            if reader.is_encrypted:
                # For encrypted files len(reader.pages) uses /Count directly.
                try:
                    reader.get_page(count)
                except IndexError:
                    pass
                else:
                    raise PdfReadError("PDF page tree has an inconsistent count")  # noqa: TRY003
    except Exception as error:
        # Malformed objects and streams can also raise built-in exceptions in pypdf.
        raise ValueError("could not read every page of the PDF") from error  # noqa: TRY003

    digest = sha256(content).hexdigest()
    return tuple(
        PDFPageInput(document_id=value.document_id, document_sha256=digest, page_number=number, text=text)
        for number, text in enumerate(texts, 1)
    )


class _PDFPageSource(Source):
    page: PDFPageInput


class _PDFPageAdapter:
    name = _SOURCE_TYPE
    input_class = PDFPageInput
    source_class = _PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> _PDFPageSource:
        # The digest and page number have fixed syntax, so the suffix is unambiguous
        # even when the document ID contains a separator.
        identity = f"{value.document_id}\0{value.document_sha256}\0{value.page_number}"
        return _PDFPageSource(
            name=sha256(identity.encode()).hexdigest(), materialization=SourceMaterialization.CAPTURED, page=value
        )

    async def read(self, source: _PDFPageSource, /) -> PDFPageInput:
        return source.page


class _PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = _PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: _PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type=_SOURCE_TYPE,
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(_PDFPageAdapter(), projections=(_PDFPageProjection(),))

_WORDS = re.compile(r"[^\W_]+")
_YEAR = re.compile(r"(?:fy)?(?:19|20)\d{2}")
_QUERY_FILLER = frozenset([
    "about",
    "according",
    "answer",
    "based",
    "calculate",
    "define",
    "details",
    "following",
    "give",
    "many",
    "much",
    "only",
    "reported",
    "respond",
    "shown",
    "using",
    "use",
    "year",
    "fy",
    "fiscal",
    "end",
    "ended",
    "statement",
    "statements",
    "financial",
    "company",
    "filing",
    "annual",
    "report",
    "form",
    "any",
    "materially",
    "directly",
    "explicitly",
    "outlined",
    "primarily",
])


class _Topic(NamedTuple):
    question: re.Pattern[str]
    terms: str
    evidence: re.Pattern[str]


# Annual reports use different headings for the same disclosure. Separate FTS
# queries keep a question about several statements from losing one at retrieval.
_TOPICS = (
    _Topic(
        re.compile(r"\b(?:cash flows?|capex|capital expenditures?|d a|depreciation and amortization)\b"),
        "cash flows operating activities investing financing capital expenditures additions property plant equipment",
        re.compile(r"\b(?:statements? of (?:consolidated )?cash flows?|cash flows? statements?)\b"),
    ),
    _Topic(
        re.compile(
            r"\b(?:income statements?|p l|p and l|statements? of (?:operations|earnings|income)|"
            r"ebitda|operating (?:income|profit|margin)|net (?:income|earnings|loss)|gross (?:profit|margin))\b"
        ),
        "consolidated statements income operations earnings profit net sales revenues cost",
        re.compile(
            r"\b(?:statements? of (?:consolidated )?(?:income|operations|earnings|profit and loss)|"
            r"income statements?|statements? of (?:comprehensive )?income and comprehensive income)\b"
        ),
    ),
    _Topic(
        re.compile(
            r"\b(?:balance sheets?|financial position|(?:total|current) (?:assets|liabilities)|"
            r"current ratio|working capital|total (?:debt|equity))\b"
        ),
        "balance sheets financial position assets liabilities equity current",
        re.compile(r"\b(?:balance sheets?|statements? of (?:consolidated )?financial position)\b"),
    ),
    _Topic(
        re.compile(r"\b(?:geograph\w*|countries|regions?|where .* operat\w+)\b"),
        "geographic geographical regions countries markets divisions business segments operations international",
        re.compile(r"\b(?:geograph\w*|our divisions|business segments?|international operations)\b"),
    ),
    _Topic(
        re.compile(r"\b(?:legal|lawsuits?|litigation)\b"),
        "legal proceedings litigation lawsuits claims commitments contingencies",
        re.compile(r"\b(?:legal proceedings|legal matters|litigation|(?:commitments and|loss) contingencies)\b"),
    ),
)


def _term(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "is", "us")):
        return word[:-1]
    return word


def _document_matches(label: str, question: str, question_names: set[str]) -> bool:
    # A label like COMPANY_2022_10K can disambiguate equivalent disclosures.
    name = []
    for word in _WORDS.findall(label.casefold()):
        if _YEAR.fullmatch(word) or word in {"10k", "10q", "annual", "report"}:
            break
        name.append(word)
    return bool(name) and (f" {' '.join(name)} " in question or "".join(name) in question_names)


def _heading_score(topic: _Topic, lines: list[str]) -> int:
    # An isolated heading is stronger evidence than a table-of-contents entry or
    # a mention in a paragraph. Some reports place the heading below other items.
    mentioned = False
    for line in lines:
        match = topic.evidence.search(line)
        if match is not None:
            if match.start() < 35 and len(line) - match.end() < 25:
                return 4
            mentioned = True
    return int(mentioned)


class _RankedHit(NamedTuple):
    hit: MemoryHit
    document_match: bool
    score: float
    headings: tuple[int, ...]


def _rank_pdf_hits(
    hits: list[MemoryHit], terms: tuple[str, ...], topics: tuple[_Topic, ...], question_words: list[str]
) -> list[MemoryHit]:
    """Score rare terms, statement headings and report identity over the FTS pool."""

    if not hits:
        return []
    question = f" {' '.join(question_words)} "
    question_names = set(question_words)
    question_names.update(left + right for left, right in pairwise(question_words))
    pages = []
    frequencies: Counter[str] = Counter()
    term_set = set(terms)
    for hit in hits:
        label, _, body = hit.text.partition("\n")
        body = body.casefold()
        words = _WORDS.findall(body)
        counts = Counter(term for word in words if (term := _term(word)) in term_set)
        frequencies.update(counts.keys())
        lines = [" ".join(_WORDS.findall(line)) for line in body.splitlines()] if topics else []
        headings = tuple(_heading_score(topic, lines) for topic in topics)
        pages.append((hit, _document_matches(label, question, question_names), len(words), counts, headings))

    average_length = max(sum(length for _, _, length, _, _ in pages) / len(pages), 1)
    weights = {term: log1p((len(pages) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)) for term in terms}
    ranked = []
    for hit, company_match, length, counts, headings in pages:
        norm = 0.6 + 0.6 * length / average_length
        score = sum(weight * counts[term] / (counts[term] + norm) for term, weight in weights.items())
        ranked.append(_RankedHit(hit, company_match, score, headings))

    # Reserve leading results for each requested disclosure, even when a different
    # statement repeats more of the question's terms.
    prefer_named_report = any(row.document_match for row in ranked)
    focused_lists = []
    for index in range(len(topics)):
        focused = sorted(
            (row for row in ranked if row.headings[index] and row.document_match == prefer_named_report),
            key=lambda row: (row.headings[index], row.score),
            reverse=True,
        )
        focused_lists.append(focused[:2])

    preferred: dict[str, MemoryHit] = {}
    for rank in range(2):
        for focused in focused_lists:
            if rank < len(focused):
                hit = focused[rank].hit
                preferred.setdefault(hit.entry_version_id, hit)
    for row in sorted(ranked, key=lambda row: (row.document_match, row.score + sum(row.headings)), reverse=True):
        preferred.setdefault(row.hit.entry_version_id, row.hit)
    return list(preferred.values())


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Search a scoped Memory for distinct, exactly citable PDF pages."""

    from powercontext.builtin.artifacts.search import analyze_fts_query
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    question_words = _WORDS.findall(question.casefold())
    if limit <= 0 or not question_words:
        return ()

    normalized = " ".join(question_words)
    topics = tuple(topic for topic in _TOPICS if topic.question.search(normalized))
    analyzed = analyze_fts_query(question.partition("?")[0] or question).split()
    if not analyzed:
        analyzed = analyze_fts_query(question).split()
    terms = [word for word in analyzed if word not in _QUERY_FILLER and not _YEAR.fullmatch(word)]
    # Long calculation and response instructions should not dominate FTS admission.
    query_words = tuple(dict.fromkeys(terms or analyzed))[:24]
    queries = (" ".join(query_words), *(topic.terms for topic in topics))
    hits: dict[str, MemoryHit] = {}
    for query in queries:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(4 * limit, 64)))
        for hit in result.hits:
            hits.setdefault(hit.entry_version_id, hit)

    citations: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    rank_terms = tuple(dict.fromkeys(map(_term, query_words)))
    for hit in _rank_pdf_hits(list(hits.values()), rank_terms, topics, question_words):
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        record = await memory.get(GetMemoryEntryRequest(citation=citation))
        sources = record.entry.sources
        if len(sources) != 1 or sources[0].source_type != _SOURCE_TYPE or sources[0].source_id in seen_pages:
            continue
        seen_pages.add(sources[0].source_id)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
