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

"""Observable embedding sharing and provider compatibility."""

from __future__ import annotations

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage, embed_query
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel

_PROFILE = EmbeddingProfile(profile_id="test-embeddings", model="local", dimension=1)


def test_late_caller_shares_in_flight_batch_without_a_cache() -> None:
    class Provider:
        profile = _PROFILE

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[tuple[str, ...]] = []

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(texts)
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(
                vectors=tuple((float(len(text)),) for text in texts),
                usage=InferenceUsage(requests=3, input_tokens=11, output_tokens=0),
            )

    async def scenario() -> None:
        provider = Provider()
        model = CoalescingEmbeddingModel(provider, max_batch_size=2, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("alpha", "beta")))
        await provider.started.wait()

        late_started = asyncio.Event()

        async def join_late() -> EmbeddingResult:
            late_started.set()
            return await model.embed(("beta", "another", "alpha", "beta"))

        late = asyncio.create_task(join_late())
        await late_started.wait()
        provider.release.set()
        first_result, late_result = await asyncio.gather(first, late)
        assert first_result.vectors == ((5.0,), (4.0,))
        assert first_result.usage == InferenceUsage(requests=3, input_tokens=11, output_tokens=0)
        assert late_result.vectors == ((4.0,), (7.0,), (5.0,), (4.0,))
        assert late_result.usage == InferenceUsage(requests=6, input_tokens=22, output_tokens=0)
        assert provider.calls == [("alpha", "beta"), ("another",)]

        assert (await model.embed(("alpha",))).usage.requests == 3
        assert provider.calls[-1] == ("alpha",)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_failed_batch_does_not_discard_successes_from_the_same_call() -> None:
    class Provider:
        profile = _PROFILE

        def __init__(self) -> None:
            self.failure_started = asyncio.Event()
            self.release_failure = asyncio.Event()
            self.fail = True

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            if texts == ("retry",) and self.fail:
                self.failure_started.set()
                await self.release_failure.wait()
                raise RuntimeError("provider failure")  # noqa: TRY003
            return EmbeddingResult(vectors=((1.0,),), usage=InferenceUsage(requests=1))

    async def scenario() -> None:
        provider = Provider()
        model = CoalescingEmbeddingModel(provider, max_batch_size=1, coalesce_ms=0)
        mixed = asyncio.create_task(model.embed(("retry", "keep")))
        await provider.failure_started.wait()
        assert (await model.embed(("keep",))).vectors == ((1.0,),)
        provider.release_failure.set()
        with pytest.raises(RuntimeError, match="provider failure"):
            await mixed

        provider.fail = False
        retried = await model.embed(("keep", "retry"))
        assert retried.vectors == ((1.0,), (1.0,))
        assert retried.usage.requests == 1

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_query_and_document_vectors_use_their_own_provider_mode() -> None:
    class Provider:
        profile = _PROFILE

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            return EmbeddingResult(vectors=((1.0,),) * len(texts), usage=InferenceUsage(requests=1))

        async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            return EmbeddingResult(vectors=((-1.0,),) * len(texts), usage=InferenceUsage(requests=1))

    async def scenario() -> None:
        model = CoalescingEmbeddingModel(Provider(), coalesce_ms=0)
        document, query = await asyncio.gather(model.embed(("same",)), embed_query(model, ("same",)))
        assert document.vectors == ((1.0,),)
        assert query.vectors == ((-1.0,),)
        assert document.usage.requests == query.usage.requests == 1

        document, query = await asyncio.gather(model.embed(("same",)), embed_query(model, ("same",)))
        assert document.vectors == ((1.0,),)
        assert query.vectors == ((-1.0,),)
        assert document.usage.requests == query.usage.requests == 0

    asyncio.run(scenario())
