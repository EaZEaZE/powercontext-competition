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


"""Rank and pack literal excerpts from already retrieved Memory entries."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import TYPE_CHECKING

from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.builtin.runtime.prepared_text import render_context_envelope

if TYPE_CHECKING:
    from powercontext.builtin.runtime.prepared_context import MemoryEntryAddress

_SENTENCE_BREAK = re.compile(r"""(?<=[.!?])\s+|(?<=[.!?]["'”’])\s+|(?<=[。！？])|[\r\n]+""")  # noqa: RUF001
_WORD_BREAK = re.compile(r"\s+")
_SUFFIX = re.compile(r"(?:ing|ed|ion|ive|(?<=[sxz])es|(?<!s)s)$")
_MERGE_GAP_CHARS = 80
_LINE_SEPARATORS = {0x85: r"\u0085", 0x2028: r"\u2028", 0x2029: r"\u2029"}


@dataclass(frozen=True)
class PreparedMemoryFragment:
    """A literal excerpt at half-open Python character offsets in an exact Memory entry."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Document:
    address: MemoryEntryAddress
    text: str
    citation: dict[str, object]

    def item(self, start: int, end: int) -> dict[str, object]:
        return {"citation": self.citation, "content": self.text[start:end], "start": start, "end": end}


@dataclass(frozen=True)
class _Span:
    document: int
    start: int
    end: int
    size: int
    terms: frozenset[str]
    score: float = 0.0


def select_memory_evidence(
    query: str,
    entries: Sequence[tuple[MemoryEntryAddress, str]],
    max_bytes: int,
    limit: int,
) -> tuple[str | None, tuple[PreparedMemoryFragment, ...], int]:
    """Return the rendered evidence, the corresponding fragments, and budget drops."""

    query_terms = frozenset(map(_term_key, analyze_fts_query(query).split()))
    if not query_terms or not entries:
        return None, (), 0

    documents = [
        _Document(
            address,
            text,
            {
                "scope_id": address.memory.scope_id,
                "memory_ref": address.memory.artifact.model_dump(mode="json"),
                "entry_id": address.entry_id,
                "entry_version_id": address.entry_version_id,
            },
        )
        for address, text in entries
    ]
    budget = max_bytes - len(render_context_envelope(()).encode("utf-8"))
    groups = [tuple(_document_spans(index, document, query_terms, budget)) for index, document in enumerate(documents)]
    ranked = _rank_spans(groups)
    selected: list[_Span] = []
    used = 0
    drops = 0
    for span in ranked:
        neighbors = [
            index
            for index, previous in enumerate(selected)
            if previous.document == span.document
            and previous.end + _MERGE_GAP_CHARS >= span.start
            and span.end + _MERGE_GAP_CHARS >= previous.start
        ]
        if neighbors:
            start = min(span.start, *(selected[index].start for index in neighbors))
            end = max(span.end, *(selected[index].end for index in neighbors))
            size = _json_bytes(documents[span.document].item(start, end))
            merged_used = used - sum(selected[index].size + 1 for index in neighbors) + size + 1
            if merged_used <= budget + 1:
                selected[neighbors[0]] = replace(span, start=start, end=end, size=size)
                for index in reversed(neighbors[1:]):
                    del selected[index]
                used = merged_used
                continue
        if len(selected) >= limit:
            continue
        if used + span.size > budget:
            drops += 1
            continue
        selected.append(span)
        # Charge a separator on every item and credit one back for the entire envelope.
        used += span.size + 1

    if not selected:
        return None, (), drops
    items = [documents[span.document].item(span.start, span.end) for span in selected]
    fragments = tuple(
        PreparedMemoryFragment(
            address=documents[span.document].address,
            start=span.start,
            end=span.end,
            text=documents[span.document].text[span.start : span.end],
        )
        for span in selected
    )
    # Keep the payload on one line even when source text contains Unicode line separators.
    return render_context_envelope(items).translate(_LINE_SEPARATORS), fragments, drops


def _document_spans(index: int, document: _Document, query_terms: frozenset[str], budget: int) -> Iterator[_Span]:
    text = document.text
    if _json_bytes(document.item(0, 0)) >= budget:
        return

    start = 0
    seen: set[str] = set()
    breaks = ((match.start(), match.end()) for match in _SENTENCE_BREAK.finditer(text))
    for end, next_start in chain(breaks, ((len(text), len(text)),)):
        while start < end and text[end - 1].isspace():
            end -= 1
        while start < end:
            while start < end and text[start].isspace():
                start += 1
            if start == end:
                break
            stop = _fitting_end(document, start, end, budget)
            if stop == start:
                # A single character can cost more than the available JSON string budget.
                start += 1
                continue
            body = text[start:stop]
            if body not in seen:
                seen.add(body)
                terms = query_terms.intersection(map(_term_key, analyze_text(body).split()))
                yield _Span(index, start, stop, _json_bytes(document.item(start, stop)), terms)
            start = stop
        start = next_start


def _fitting_end(document: _Document, start: int, end: int, budget: int) -> int:
    # Each character costs at least one byte. Limit serialization of oversized sentences
    # to the prefix that might fit, and keep normal sentences intact whenever possible.
    upper = min(end, start + budget)
    if upper == end and _json_bytes(document.item(start, end)) <= budget:
        return end
    lower, upper = start, upper + 1
    while lower + 1 < upper:
        middle = (lower + upper) // 2
        if _json_bytes(document.item(start, middle)) <= budget:
            lower = middle
        else:
            upper = middle
    # Keep words whole when an oversized sentence has a nearby word boundary.
    if lower == start or document.text[lower].isspace():
        return lower
    midpoint = (start + lower) // 2
    breaks = _WORD_BREAK.finditer(document.text, midpoint, lower)
    return max((match.start() for match in breaks if match.start() > start), default=lower)


def _rank_spans(groups: Sequence[Sequence[_Span]]) -> list[_Span]:
    document_terms = [frozenset().union(*(span.terms for span in group)) for group in groups]
    by_document = Counter(term for terms in document_terms for term in terms)
    by_sentence = Counter(term for group in groups for span in group for term in span.terms)
    sentence_count = sum(map(len, groups))
    weights = {
        term: 1
        + math.log((1 + len(groups)) / (1 + count))
        + 0.35 * math.log((1 + sentence_count) / (1 + by_sentence[term]))
        for term, count in by_document.items()
    }
    max_coverage = max((_coverage(terms, weights) for terms in document_terms), default=0.0)
    if not max_coverage:
        return []
    scored: list[_Span] = []
    for group, terms in zip(groups, document_terms, strict=True):
        prior = 0.4 + 0.6 * _coverage(terms, weights) / max_coverage
        local = [_coverage(span.terms, weights) / max(1.0, (span.end - span.start) / 180) ** 0.25 for span in group]
        for index, span in enumerate(group):
            neighbors = (
                local[other]
                for other in (index - 1, index + 1)
                if 0 <= other < len(group)
                and max(group[other].start - span.end, span.start - group[other].end) <= _MERGE_GAP_CHARS
            )
            score = prior * (local[index] + 0.25 * max(neighbors, default=0.0))
            if score:
                scored.append(replace(span, score=score))
    return sorted(scored, key=lambda span: (-span.score, span.document, span.start))


def _coverage(terms: frozenset[str], weights: dict[str, float]) -> float:
    return math.fsum(weights[term] for term in terms)


def _term_key(term: str) -> str:
    """Match common English inflections while retaining Analyzer terms for other scripts."""

    if len(term) <= 4 or not term.isascii() or not term.isalpha():
        return term
    if term.endswith("ies"):
        return term[:-3] + "y"
    stem = _SUFFIX.sub("", term)
    return stem.removesuffix("e") if len(stem) > 4 else stem


def _json_bytes(item: dict[str, object]) -> int:
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).translate(_LINE_SEPARATORS)
    return len(encoded.encode("utf-8"))
