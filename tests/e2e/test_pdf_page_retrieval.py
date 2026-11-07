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

import asyncio
from hashlib import sha256
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFPageInput, search_pdf_pages


class _PagePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            if isinstance(source, SourceObservation):
                projection = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                # Include two Memory entries per page to exercise page deduplication.
                entries.extend(
                    MemoryEntryInput(kind=kind, text=projection.content, sources=(source,))
                    for kind in ("source-evidence", "fact")
                )
        return tuple(entries)


def test_search_keeps_evidence_from_both_statements_in_the_requested_report(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"))
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        observations = {}
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Statement evidence", idempotency_key="reports")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for name in ("OTHER_2023_10K", "NORTHWIND_2023_10K"):
                for number, text in enumerate(
                    (
                        "Consolidated Statements of Operations\nNet sales 300\nOperating profit 120",
                        "Consolidated Statements of Cash Flows\nNet cash provided by operating activities 180\n"
                        + "Notes to the statement.\n" * 400
                        + "Closing cash 360",
                    ),
                    1,
                ):
                    page = PDFPageInput(
                        document_id=name,
                        document_sha256=sha256(name.encode()).hexdigest(),
                        page_number=number,
                        text=text,
                    )
                    observation = project_source_for_transport(registry, await registry.resolve(page))
                    await runtime.ingestion.submit(
                        SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                    )
                    observations[observation.name] = TextEvidence.model_validate(
                        observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
                    )
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in observations:
                if not (await memory.flush()).processed:
                    break
            assert not (await memory.flush()).processed
            await memory.remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="note", text="Northwind EBITDA cash flow operating income report."),)
                )
            )
            assert len((await memory.list()).entries) == 2 * len(observations) + 1

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            question = "What is Northwind's EBITDA using the income statement and the cash flow statement?"
            citations = await search_pdf_pages(memory, question, limit=2)
            locations = set()
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                (reference,) = entry.sources
                assert reference.source_type == "pdf-page"
                projection = observations[reference.source_id]
                assert entry.text == projection.content
                locations.add((projection.metadata["document_id"], projection.metadata["page_number"]))
            assert locations == {("NORTHWIND_2023_10K", 1), ("NORTHWIND_2023_10K", 2)}

            all_citations = await search_pdf_pages(memory, question, limit=10)
            assert len(all_citations) == len(observations)
            assert await search_pdf_pages(memory, question, limit=0) == ()

    asyncio.run(scenario())
