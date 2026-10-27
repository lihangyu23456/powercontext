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

import asyncio
import json

import httpx
import pytest
from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
        entry_id=f"e{rank}",
        entry_version_id=f"v{rank}",
        text=f"candidate {rank}",
        score=1.0 / rank,
        matched_by=("fts",),
    )


def test_batches_keep_global_ranks_and_select_by_score() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        scores = [{"rank": item["rank"], "score": {1: 0.2, 2: 0.9, 3: 0.9}[item["rank"]]} for item in body["candidates"]]
        return httpx.Response(200, json={"scores": scores})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=2)
            decision = await reranker.rerank("query", tuple(_hit(index) for index in range(1, 4)), 2)
        assert decision.selected_ranks == (2, 3)
        assert decision.usage.requests == 2

    asyncio.run(scenario())
    assert sorted(item["rank"] for body in bodies for item in body["candidates"]) == [1, 2, 3]


def test_out_of_order_scores_are_accepted() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"scores": [{"rank": 2, "score": 0.1}, {"rank": 1, "score": 0.8}]})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=2)
            decision = await reranker.rerank("q", (_hit(1), _hit(2)), 1)
        assert decision.selected_ranks == (1,)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": [{"rank": 2, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5}, {"rank": 1, "score": 0.4}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": float("inf")}]},
        {"scores": [{"rank": 1, "score": 0.5, "extra": True}]},
        {"scores": [], "extra": True},
    ],
)
def test_invalid_responses_fail(payload: dict) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://reranker.invalid/score", client=client).rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())


def test_bounds_and_transport_errors_are_reported() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (), 1)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (_hit(1),), 2)
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=0)
            with pytest.raises(httpx.ConnectError):
                await reranker.rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())
