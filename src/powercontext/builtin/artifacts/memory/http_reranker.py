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

"""HTTP scoring adapter for listwise Memory reranking."""

from __future__ import annotations

import math
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_MEMORY_RERANKER_POLICY_ID: Final = "http-cross-encoder-v1"
HTTP_MEMORY_RERANKER_BATCH_SIZE: Final = 32


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _HTTPBatchCandidate(_StrictModel):
    """One candidate offered to the scoring service under its global rank."""

    rank: int = Field(ge=1)
    text: str


class _HTTPBatchRequest(_StrictModel):
    """One scoring request body."""

    query: str
    candidates: tuple[_HTTPBatchCandidate, ...]


class _HTTPScore(_StrictModel):
    """One score reported for a candidate rank."""

    rank: int = Field(ge=1)
    score: float


class _HTTPBatchResponse(_StrictModel):
    """One scoring response body."""

    scores: tuple[_HTTPScore, ...]


class HTTPMemoryReranker:
    """Score Memory candidates with one offline HTTP scoring service.

    The service may only score a bounded number of candidates per request, so
    the coarse pool is split into batches. Every candidate keeps its rank in
    the complete pool, and the final selection is decided once over all
    returned scores.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = HTTP_MEMORY_RERANKER_BATCH_SIZE,
        policy_id: str = HTTP_MEMORY_RERANKER_POLICY_ID,
    ) -> None:
        """Bind one scoring endpoint and the batch size it accepts."""

        if batch_size < 1:
            raise ValueError("batch_size must be a positive number of candidates")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Select the original ranks that the service scored highest."""

        if not candidates:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if limit < 1 or limit > len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003

        scores: dict[int, float] = {}
        requests = 0
        for batch in _batches(candidates, self._batch_size):
            payload = _HTTPBatchRequest(
                query=query,
                candidates=tuple(_HTTPBatchCandidate(rank=rank, text=candidate.text) for rank, candidate in batch),
            )
            response = await self._client.post(self._endpoint, json=payload.model_dump(mode="json"))
            response.raise_for_status()
            requests += 1
            scores.update(_validated_scores(response, {rank for rank, _ in batch}))
        return MemoryRerankDecision(
            selected_ranks=_selected_ranks(scores, limit),
            usage=InferenceUsage(requests=requests),
        )


def _batches(
    candidates: tuple[MemoryHit, ...],
    batch_size: int,
) -> list[tuple[tuple[int, MemoryHit], ...]]:
    """Split the coarse pool into batches that keep their global ranks."""

    batches: list[tuple[tuple[int, MemoryHit], ...]] = []
    current: list[tuple[int, MemoryHit]] = []
    for rank, candidate in enumerate(candidates, start=1):
        current.append((rank, candidate))
        if len(current) == batch_size:
            batches.append(tuple(current))
            current = []
    if current:
        batches.append(tuple(current))
    return batches


def _validated_scores(response: httpx.Response, expected_ranks: set[int]) -> dict[int, float]:
    """Return the scores of one batch after checking the response contract."""

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("rerank service returned a body that is not JSON") from error  # noqa:  TRY003 Avoid specifying long messages outside the exception class
    try:
        parsed = _HTTPBatchResponse.model_validate(payload)
    except ValidationError as error:
        raise ValueError("rerank service returned an invalid response body") from error  # noqa:  TRY003 Avoid specifying long messages outside the exception class

    scores: dict[int, float] = {}
    for item in parsed.scores:
        if item.rank in scores:
            raise ValueError(f"rerank service returned rank {item.rank} more than once")  # noqa: TRY003
        if not math.isfinite(item.score):
            raise ValueError(f"rerank service returned a non-finite score for rank {item.rank}")  # noqa: TRY003
        scores[item.rank] = item.score
    if set(scores) != expected_ranks:
        raise ValueError("rerank service did not score every requested rank exactly once")  # noqa: TRY003
    return scores


def _selected_ranks(scores: dict[int, float], limit: int) -> tuple[int, ...]:
    """Order the scored ranks by descending score and then by ascending rank."""

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return tuple(rank for rank, _ in ordered[:limit])


__all__ = [
    "HTTP_MEMORY_RERANKER_BATCH_SIZE",
    "HTTP_MEMORY_RERANKER_POLICY_ID",
    "HTTPMemoryReranker",
]
