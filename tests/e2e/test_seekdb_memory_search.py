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

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter


class _Embedding:
    profile = EmbeddingProfile(
        profile_id="tagged-hybrid-v1",
        model="test",
        dimension=3,
        distance="l2",
        normalization="unit",
    )

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple((0.8, 0.6, 0.0) if text == "Relevant semantic record." else (1.0, 0.0, 0.0) for text in texts)
        )


@pytest.mark.asyncio
async def test_seekdb_hybrid_vector_limit_applies_after_tag_filtering() -> None:
    pytest.importorskip("pylibseekdb")
    with tempfile.TemporaryDirectory(prefix="pc-memory-") as directory:
        async with open_builtin_contexts(
            BuiltinConfig(database=SeekDBConfig(path=Path(directory) / "db")), embedding_model=_Embedding()
        ) as contexts:
            scope = "project"
            memory = (await contexts.get(scope)).artifacts.memory
            head = await memory.remember(
                memory=None,
                entries=(
                    *(MemoryEntryInput(kind="fact", text=f"Alpha decoy number {i}.") for i in range(40)),
                    MemoryEntryInput(kind="fact", text="Relevant semantic record."),
                ),
                mode="append",
            )
            assert head is not None
            entry = next(entry for entry in await memory.entries(head) if entry.text == "Relevant semantic record.")
            target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=entry.entry_id)
            empty = await contexts.records.get_tags(scope, target)
            await contexts.records.replace_tags(scope, target, ("selected", "urgent"), expected_etag=empty.etag)

            statements = []

            def record(_connection, _cursor, statement, parameters, _context, _executemany) -> None:
                if "AGAINST" in statement.upper() or "l2_distance" in statement.lower():
                    statements.append((statement, parameters))

            event.listen(Engine, "before_cursor_execute", record)
            try:
                result = await memory.search(
                    "alpha",
                    memories=(head,),
                    mode="hybrid",
                    limit=1,
                    tag_filter=TagFilter(tags=("selected", "urgent"), match="all"),
                )
            finally:
                event.remove(Engine, "before_cursor_execute", record)

            assert len(result.hits) == 1
            hit = result.hits[0]
            assert hit.entry_id == entry.entry_id
            assert hit.entry_version_id == entry.entry_version_id
            assert hit.text == entry.text
            assert hit.matched_by == ("vector",)
            assert len(statements) == 1
            statement, parameters = statements[0]
            async with contexts.database.transaction() as connection:
                plan = "\n".join(
                    str(row[0]) for row in (await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)).all()
                )
            assert "TEXT RETRIEVAL SCAN" in plan
            assert "VECTOR INDEX" in plan
