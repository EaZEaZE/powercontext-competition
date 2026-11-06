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

"""HTTP scoring for Memory search candidates."""

from __future__ import annotations

from math import isfinite

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score batches via a caller-owned client and select ranks from the full pool."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
    ) -> MemoryRerankDecision:
        """Return original ranks in descending score order, breaking ties by rank."""
        if type(limit) is not int:
            raise ValueError("rerank limit must be an integer")  # noqa: TRY003
        _validate_bounds(len(candidates), limit)

        scores: dict[int, int | float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            stop = min(start + self._batch_size, len(candidates))
            ranks = range(start + 1, stop + 1)
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in ranks],
                },
                follow_redirects=False,
            )
            response.raise_for_status()
            scores.update(_parse_batch_scores(response, ranks))
            requests += 1

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(selected_ranks=tuple(ordered[:limit]), usage=InferenceUsage(requests=requests))


def _parse_batch_scores(response: httpx.Response, ranks: range) -> dict[int, int | float]:
    try:
        payload = response.json(object_pairs_hook=_unique_object)
    except RecursionError as error:
        raise ValueError("rerank response is nested too deeply") from error  # noqa: TRY003
    if not isinstance(payload, dict) or payload.keys() != {"scores"} or not isinstance(payload["scores"], list):
        raise ValueError("rerank response must contain only a scores array")  # noqa: TRY003

    scores: dict[int, int | float] = {}
    for row in payload["scores"]:
        if not isinstance(row, dict) or row.keys() != {"rank", "score"}:
            raise ValueError("rerank score record must contain exactly rank and score")  # noqa: TRY003
        rank, score = row["rank"], row["score"]
        if type(rank) is not int or rank not in ranks or rank in scores:
            raise ValueError("rerank score rank must be an integer unique to the requested batch")  # noqa: TRY003
        if type(score) is not int and (type(score) is not float or not isfinite(score)):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        scores[rank] = score
    if len(scores) != len(ranks):
        raise ValueError("rerank response must score every rank in the requested batch")  # noqa: TRY003
    return scores


def _unique_object(fields: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(fields)
    if len(obj) != len(fields):
        raise ValueError("rerank response contains a duplicate JSON field")  # noqa: TRY003
    return obj


__all__ = ["HTTPMemoryReranker"]
