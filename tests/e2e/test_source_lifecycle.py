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

"""Lifecycle rollback, retry, and native retrieval eligibility."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput, MemoryService
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    BuiltinRuntime,
    CaptureSource,
    GetTopicMemoryRequest,
    RememberMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


async def _register_chain(runtime: BuiltinRuntime, key: str) -> tuple[str, SourceRef, MemoryCitation, ArtifactRef]:
    assert runtime.scopes is not None
    scope = await runtime.scopes.create(ScopeDraft(title=key, summary="Lifecycle test", idempotency_key=key))
    scope_id = scope.scope_id
    lifecycle = runtime.lifecycle.for_scope(scope_id)
    source = await runtime.sources.for_scope(scope_id).capture(
        CaptureSource(source_id="origin", content="Origin", metadata={})
    )
    await lifecycle.register_source("source", source.source_ref, revision=1)
    created = await runtime.memory.for_scope(scope_id).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="Evidence for a harbor survey."),))
    )
    assert created.entry is not None
    citation = created.entry.citation
    await lifecycle.register_memory("memory", citation, parents=("source",), policy="all")
    topic = await runtime.records.for_scope(scope_id).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": "Harbor", "summary": "Harbor survey", "detail": "Coordinates of the harbor."}),
    )
    ref = ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision)
    await lifecycle.register_topic("topic", ref, parents=("memory",), policy="all")
    return scope_id, source.source_ref, citation, ref


@pytest.mark.parametrize("restore", [False, True])
def test_source_lifecycle_retries_native_write_after_rollback_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool
) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}"))
        withdrawal = SourceLifecycleEvent(node_id="source", revision=1, sequence=1, available=False, reason="withdraw")
        event = (
            SourceLifecycleEvent(node_id="source", revision=1, sequence=2, available=True, reason="restore")
            if restore
            else withdrawal
        )
        async with open_builtin_runtime(config) as runtime:
            scope_id, source, citation, topic = await _register_chain(runtime, "first")
            other_scope, _, _, other_topic = await _register_chain(runtime, "second")
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            memory = runtime.memory.for_scope(scope_id)
            if restore:
                await lifecycle.apply(withdrawal)
            before = (await memory.list(include_inactive=True)).memory_ref
            original = MemoryService.reactivate if restore else MemoryService.forget

            async def fail_after_write(self, *args, **kwargs):
                await original(self, *args, **kwargs)
                raise RuntimeError("interrupted after native write")  # noqa: TRY003

            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, original.__name__, fail_after_write)
                with pytest.raises(RuntimeError, match="interrupted after native write"):
                    await lifecycle.apply(event)
            assert (await memory.list(include_inactive=True)).memory_ref == before
            assert await lifecycle.resolve("source", purpose="audit") == source
            found = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query="harbor"))
            assert bool(found.hits) is not restore

        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            memory = runtime.memory.for_scope(scope_id)
            assert await lifecycle.apply(event) == (["memory", "source", "topic"] if restore else [])
            page = await memory.list(include_inactive=True)
            assert page.memory_ref is not None and before is not None
            assert page.memory_ref.revision == before.revision + 1
            assert page.entries[0].state == ("active" if restore else "inactive")
            assert page.entries[0].entry.entry_version_id == citation.entry_version_id
            await lifecycle.apply(event)
            assert (await memory.list(include_inactive=True)).memory_ref == page.memory_ref
            assert await lifecycle.resolve("memory", purpose="audit") == citation
            assert await lifecycle.resolve("topic", purpose="audit") == topic
            assert await runtime.lifecycle.for_scope(other_scope).resolve("topic", purpose="generation") == other_topic

    asyncio.run(scenario())


class _EmbeddingModel:
    profile = EmbeddingProfile(profile_id="topic-lifecycle", model="test", dimension=3)

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple((1.0, 0.0, 0.0) for _ in texts))


def test_topic_lifecycle_filters_vector_only_hits_and_preserves_exact_read() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(
            BuiltinConfig(database=SQLiteConfig()), embedding_model=_EmbeddingModel()
        ) as runtime:
            scope_id, _, _, reference = await _register_chain(runtime, "vector")
            topic = runtime.topic_memory.for_scope(scope_id)
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            request = SearchTopicMemoryRequest(query="unrelated vocabulary", limit=1)
            initial = await topic.search(request)
            assert initial.mode == "hybrid"
            assert initial.hits[0].artifact_ref == reference
            assert set(initial.hits[0].matched_by) == {"topic_vector", "detail_vector"}

            event = SourceLifecycleEvent(node_id="source", revision=1, sequence=1, available=False, reason="withdraw")
            await lifecycle.apply(event)
            assert (await topic.search(request)).hits == ()
            assert (await topic.get(GetTopicMemoryRequest(artifact=reference))).topic.as_ref() == reference
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("topic", purpose="generation")

            await lifecycle.apply(
                SourceLifecycleEvent(node_id="source", revision=1, sequence=2, available=True, reason="restore")
            )
            assert (await topic.search(request)).hits[0].artifact_ref == reference

    asyncio.run(scenario())
