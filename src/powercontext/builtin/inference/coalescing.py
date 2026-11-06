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

"""Share pending embedding work and cache vectors within one event loop."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel, embed_query


@dataclass(eq=False, frozen=True, slots=True)
class _BatchUsage:
    # Distinct provider calls can return identical usage values.
    usage: InferenceUsage


class _EmbeddedText(NamedTuple):
    vector: EmbeddingVector
    batch: _BatchUsage


class CoalescingEmbeddingModel:
    """Coalesce concurrent embedding requests and retain successful vectors in an LRU.

    Share this instance between callers on one event loop. Models with a distinct
    query method have separate query and document entries in the same bounded cache.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if not math.isfinite(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be finite and non-negative")  # noqa: TRY003
        if not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003

        self.profile = model.profile
        self._model = model
        self._separate_queries = isinstance(model, QueryEmbeddingModel)
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[tuple[bool, str], EmbeddingVector] = OrderedDict()
        self._pending: dict[tuple[bool, str], asyncio.Future[_EmbeddedText]] = {}
        self._queued: dict[bool, list[str]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return document vectors in the original order, including duplicates."""
        return await self._embed(texts, query=False)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Preserve the provider's query mode, sharing document work for symmetric models."""
        return await self._embed(texts, query=self._separate_queries)

    async def _embed(self, texts: tuple[str, ...], *, query: bool) -> EmbeddingResult:
        vectors: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_EmbeddedText]] = {}
        for text in texts:
            key = (query, text)
            if (vector := self._cache.get(key)) is not None:
                self._cache.move_to_end(key)
                vectors[text] = vector
            elif text not in waiting:
                waiting[text] = self._get_or_queue(key)

        batches: set[_BatchUsage] = set()
        if waiting:
            # The caller owns these shields. The underlying futures and provider
            # tasks can outlive any of their individual waiters.
            embedded = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
            for text, item in zip(waiting, embedded, strict=True):
                vectors[text] = item.vector
                batches.add(item.batch)

        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(batch.usage.requests for batch in batches),
                input_tokens=_sum_reported(batch.usage.input_tokens for batch in batches),
                output_tokens=_sum_reported(batch.usage.output_tokens for batch in batches),
            ),
        )

    def _get_or_queue(self, key: tuple[bool, str]) -> asyncio.Future[_EmbeddedText]:
        if (future := self._pending.get(key)) is not None:
            return future

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[key] = future
        query, text = key
        queue = self._queued.setdefault(query, [])
        if not queue:
            if self._delay:
                loop.call_later(self._delay, self._dispatch, query)
            else:
                loop.call_soon(self._dispatch, query)
        queue.append(text)
        return future

    def _dispatch(self, query: bool) -> None:
        queued = self._queued.pop(query)
        for start in range(0, len(queued), self._max_batch_size):
            texts = tuple(queued[start : start + self._max_batch_size])
            task = asyncio.create_task(self._run_batch(texts, query=query))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, texts: tuple[str, ...], *, query: bool) -> None:
        futures = tuple(self._pending[(query, text)] for text in texts)
        try:
            result = await embed_query(self._model, texts) if query else await self._model.embed(texts)
            # Check the entire response before any vector becomes cacheable.
            rows = tuple(zip(texts, futures, result.vectors, strict=True))
            batch = _BatchUsage(result.usage.model_copy())
        except asyncio.CancelledError:
            for future in futures:
                future.cancel()
            raise
        except Exception as error:
            for future in futures:
                future.set_exception(error)
                # A waiter may have cancelled before the provider failed.
                future.exception()
        else:
            for text, future, vector in rows:
                if self._cache_size:
                    self._cache[(query, text)] = vector
                    if len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                future.set_result(_EmbeddedText(vector, batch))
        finally:
            for text in texts:
                del self._pending[(query, text)]


def _sum_reported(counts: Iterable[int | None]) -> int | None:
    reported = [count for count in counts if count is not None]
    return sum(reported) if reported else None
