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


from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter


class _Embedding:
    profile = EmbeddingProfile(
        profile_id="test-seekdb-hybrid-filter",
        model="fixed",
        dimension=3,
        distance="l2",
        normalization="unit",
    )

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple((0.96, 0.28, 0.0) if text.startswith("Eligible") else (1.0, 0.0, 0.0) for text in texts)
        )


@pytest.mark.asyncio
async def test_hybrid_tag_filter_selects_vector_candidates_before_the_limit(tmp_path: Path) -> None:
    pytest.importorskip("pylibseekdb")
    config = BuiltinConfig(database=SeekDBConfig(path=tmp_path / "db"))
    async with open_builtin_contexts(config, embedding_model=_Embedding()) as contexts:
        scope_id = "search-filter"
        memory = (await contexts.get(scope_id)).artifacts.memory
        eligible_text = "Eligible authorization record."
        head = await memory.remember(
            memory=None,
            entries=(
                *(MemoryEntryInput(kind="fact", text=f"Closer distractor number {i}.") for i in range(48)),
                MemoryEntryInput(kind="fact", text=eligible_text),
            ),
            mode="append",
        )
        assert head is not None
        entries = await memory.entries(head)
        eligible = next(entry for entry in entries if entry.text == eligible_text)
        closer = next(entry for entry in entries if entry.text.startswith("Closer"))
        for entry, tags in ((eligible, ("blue", "reviewed")), (closer, ("blue",))):
            target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=entry.entry_id)
            existing = await contexts.records.get_tags(scope_id, target)
            await contexts.records.replace_tags(scope_id, target, tags, expected_etag=existing.etag)

        # No lexical match. The eligible vector is farther away than all
        # untagged candidates and must still make it into the ANN top-k.
        result = await memory.search(
            "unmatched", memories=(head,), mode="hybrid", limit=1, tag_filter=TagFilter(tags=("blue", "reviewed"))
        )
        assert [(hit.entry_id, hit.entry_version_id, hit.text, hit.matched_by) for hit in result.hits] == [
            (eligible.entry_id, eligible.entry_version_id, eligible_text, ("vector",))
        ]
        any_tag = await memory.search(
            "unmatched",
            memories=(head,),
            mode="hybrid",
            limit=2,
            tag_filter=TagFilter(tags=("blue", "reviewed"), match="any"),
        )
        assert [hit.entry_id for hit in any_tag.hits] == [closer.entry_id, eligible.entry_id]

        empty = await memory.search(
            "unmatched", memories=(head,), mode="hybrid", limit=1, tag_filter=TagFilter(tags=("absent",))
        )
        assert empty.hits == ()
