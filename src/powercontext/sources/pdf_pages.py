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
import unicodedata
from collections import Counter
from hashlib import sha256
from io import BytesIO
from math import log1p
from typing import TYPE_CHECKING, Annotated, NamedTuple, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from powercontext.artifacts import MemoryCitation
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import ScopedMemoryApplication
    from powercontext.builtin.runtime.models import MemoryEntryRecord

PDF_PAGE_SOURCE_TYPE = "pdf-page"


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

    from powercontext.sources._pdf_text import extract_page_text

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
            texts = tuple(extract_page_text(page) for page in reader.pages)
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
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=digest,
            page_number=number,
            text=unicodedata.normalize("NFC", text),
        )
        for number, text in enumerate(texts, 1)
    )


class _PDFPageSource(Source):
    page: PDFPageInput


class _PDFPageAdapter:
    name = PDF_PAGE_SOURCE_TYPE
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
            source_type=PDF_PAGE_SOURCE_TYPE,
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
_YEAR = re.compile(r"(?:fy)?((?:19|20)\d{2})(?:q[1-4])?")
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


# Annual reports use different headings and line item names for the same disclosure.
_TOPICS = (
    _Topic(
        re.compile(
            r"\b(?:cash flows?|capex|capital expenditures?|d a|depreciation and amortization|"
            r"cash (?:provided|generated|received|used) (?:by|from|in) operat\w+|"
            r"(?:investing|investments?|purchases?) in property|property (?:plant )?and equipment)\b"
        ),
        "cash flows operating activities investing financing capital expenditures additions property plant equipment",
        re.compile(r"\b(?:statements? of (?:consolidated )?cash flows?|cash flows? statements?)\b"),
    ),
    _Topic(
        re.compile(
            r"\b(?:income statements?|p l|p and l|statements? of (?:operations|earnings|income)|"
            r"ebitda|operating (?:income|profit|margin)|net (?:income|earnings|loss|sales|revenue)|"
            r"gross (?:profit|margin)|return on (?:assets|equity)|cost of (?:sales|revenue|goods sold))\b"
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
            r"(?:current|quick) ratio|working capital|total (?:debt|equity)|return on (?:assets|equity)|"
            r"debt to equity|shareholders? equity|stockholders? equity)\b"
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
    _Topic(
        re.compile(r"\b(?:employees?|headcount|workforce|personnel|staffing|human capital)\b"),
        "employees employed employment headcount workforce personnel staff",
        re.compile(r"\b(?:employees|workforce|human capital(?: management)?)\b"),
    ),
)


def _term(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "is", "us")):
        return word[:-1]
    return word


def _report_match(label: str, question_names: set[str], years: set[str]) -> tuple[bool, bool]:
    # A label like COMPANY_2022_10K can disambiguate equivalent disclosures.
    name = []
    year_match = False
    for word in _WORDS.findall(label.casefold()):
        if (year := _YEAR.fullmatch(word)) is not None:
            year_match = year[1] in years
            break
        if word in {"10k", "10q", "annual", "report"}:
            break
        name.append(word)
    while name and name[-1] in {"inc", "co", "corp", "corporation", "plc", "ltd", "limited"}:
        name.pop()
    company = "".join(name)
    return company in question_names or company.removeprefix("the") in question_names, year_match


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


class _RankedPage(NamedTuple):
    record: MemoryEntryRecord
    report_match: tuple[bool, bool]
    score: float
    topics: tuple[float, ...]


def _rank_pdf_records(
    records: list[MemoryEntryRecord],
    terms: tuple[str, ...],
    topics: tuple[_Topic, ...],
    question_words: list[str],
    fts_ranks: dict[str, int],
) -> list[MemoryEntryRecord]:
    """Rank complete pages using lexical matches, report identity and disclosure headings."""

    if not records:
        return []
    question_names = {
        "".join(question_words[start:end])
        for start in range(len(question_words))
        for end in range(start + 1, min(start + 4, len(question_words)) + 1)
    }
    years = {match[1] for word in question_words if (match := _YEAR.fullmatch(word)) is not None}
    topic_terms = tuple(set(map(_term, topic.terms.split())) for topic in topics)
    term_set = set(terms).union(*topic_terms)
    pages = []
    frequencies: Counter[str] = Counter()
    for record in records:
        label, _, body = record.entry.text.partition("\n")
        body = body.casefold()
        words = _WORDS.findall(body)
        counts = Counter(term for word in words if (term := _term(word)) in term_set)
        frequencies.update(counts.keys())
        lines = [" ".join(_WORDS.findall(line)) for line in body.splitlines()] if topics else []
        scores = tuple(
            _heading_score(topic, lines) + 0.25 * len(counts.keys() & aliases)
            for topic, aliases in zip(topics, topic_terms, strict=True)
        )
        pages.append((record, _report_match(label, question_names, years), len(words), counts, scores))

    average_length = max(sum(length for _, _, length, _, _ in pages) / len(pages), 1)
    weights = {term: log1p((len(pages) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)) for term in terms}
    ranked = []
    for record, report_match, length, counts, scores in pages:
        norm = 0.75 + 0.45 * length / average_length
        score = sum(weight * counts[term] / (counts[term] + norm) for term, weight in weights.items())
        if (rank := fts_ranks.get(record.entry.entry_version_id)) is not None:
            score += 1 / (1 + 0.2 * rank)
        if score or any(scores):
            ranked.append(_RankedPage(record, report_match, score, scores))

    # Reserve leading results for each disclosure when a question needs several
    # statements. Otherwise terms repeated in one statement can crowd out another.
    focused = [
        sorted(
            (row for row in ranked if row.topics[index] > 0),
            key=lambda row: (row.report_match, row.topics[index], row.score),
            reverse=True,
        )[:2]
        for index in range(len(topics))
    ]
    preferred: dict[str, MemoryEntryRecord] = {}
    best_report = max((row.report_match for row in ranked), default=(False, False))
    for position in range(2):
        for rows in focused:
            if position < len(rows) and rows[position].report_match == best_report:
                record = rows[position].record
                preferred.setdefault(record.entry.entry_version_id, record)
    for row in sorted(ranked, key=lambda row: (row.report_match, row.score + sum(row.topics)), reverse=True):
        preferred.setdefault(row.record.entry.entry_version_id, row.record)
    return list(preferred.values())


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Search a scoped Memory for distinct, exactly citable PDF pages."""

    from powercontext.builtin.artifacts.search import analyze_fts_query
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    question_words = _WORDS.findall(question.casefold())
    if limit <= 0 or not question_words:
        return ()

    normalized = " ".join(question_words)
    topics = tuple(topic for topic in _TOPICS if topic.question.search(normalized))
    analyzed = analyze_fts_query(question.partition("?")[0] or question).split()
    if not analyzed:
        analyzed = analyze_fts_query(question).split()
    terms = [word for word in analyzed if word not in _QUERY_FILLER and not _YEAR.fullmatch(word)]
    query_words = tuple(dict.fromkeys(terms or analyzed))[:24]

    # FTS is useful for literal matches, but its admission and top-k apply to
    # the whole scope. Rank the PDF records from one exact Memory snapshot as
    # well, so another report or a synonym cannot exclude the evidence page.
    hits = await memory.search(SearchMemoryRequest(query=" ".join(query_words), mode="fts", limit=max(4 * limit, 64)))
    snapshot = await memory.list()
    pages = [
        record
        for record in snapshot.entries
        if len(record.entry.sources) == 1 and record.entry.sources[0].source_type == PDF_PAGE_SOURCE_TYPE
    ]
    fts_ranks = (
        {hit.entry_version_id: rank for rank, hit in enumerate(hits.hits)}
        if hits.memory_ref == snapshot.memory_ref
        else {}
    )
    rank_terms = tuple(dict.fromkeys(map(_term, query_words)))
    citations: dict[str, MemoryCitation] = {}
    for record in _rank_pdf_records(pages, rank_terms, topics, question_words, fts_ranks):
        source_id = record.entry.sources[0].source_id
        citations.setdefault(source_id, record.citation)
        if len(citations) == limit:
            break
    return tuple(citations.values())


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
