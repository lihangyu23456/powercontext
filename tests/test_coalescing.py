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

import pytest
from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class _Embedding:
    profile = EmbeddingProfile(profile_id="test", model="local", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return EmbeddingResult(vectors=tuple((float(len(text)),) for text in texts), usage=InferenceUsage(requests=1))


def test_concurrent_calls_share_texts_and_keep_order() -> None:
    async def scenario() -> None:
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=20, cache_size=3)
        first, second = await asyncio.gather(
            model.embed(("alpha", "beta", "alpha")), model.embed(("beta", "gamma"))
        )
        assert first.vectors == ((5.0,), (4.0,), (5.0,))
        assert second.vectors == ((4.0,), (5.0,))
        assert sorted(text for call in base.calls for text in call) == ["alpha", "beta", "gamma"]
        assert all(len(call) <= 2 for call in base.calls)
        await model.embed(("alpha",))
        assert sum(call.count("alpha") for call in base.calls) == 1

    asyncio.run(scenario())


def test_lru_eviction_refreshes_recent_use() -> None:
    async def scenario() -> None:
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=1)
        await model.embed(("one",))
        await model.embed(("two",))
        await model.embed(("one",))
        assert base.calls == [("one",), ("two",), ("one",)]

    asyncio.run(scenario())


def test_empty_input_never_reaches_the_provider() -> None:
    async def scenario() -> None:
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)
        empty = await model.embed(())
        assert empty.vectors == ()
        assert empty.usage.requests == 0
        assert base.calls == []

    asyncio.run(scenario())


def test_failure_is_not_cached_and_can_be_retried() -> None:
    class _Flaky(_Embedding):
        async def embed(self, texts):
            self.calls.append(tuple(texts))
            if len(self.calls) == 1:
                raise RuntimeError("temporary")
            return EmbeddingResult(vectors=tuple((1.0,) for _ in texts))

    async def scenario() -> None:
        model = CoalescingEmbeddingModel(_Flaky(), coalesce_ms=0)
        with pytest.raises(RuntimeError):
            await model.embed(("retry",))
        assert (await model.embed(("retry",))).vectors == ((1.0,),)

    asyncio.run(scenario())


def test_cancelling_one_waiter_keeps_the_shared_request_alive() -> None:
    class _Blocked(_Embedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts):
            self.calls.append(tuple(texts))
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(vectors=((1.0,),))

    async def scenario() -> None:
        base = _Blocked()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        cancelled = asyncio.create_task(model.embed(("shared",)))
        survivor = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        base.release.set()
        assert (await survivor).vectors == ((1.0,),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_provider_usage_is_preserved_and_cache_hits_cost_nothing() -> None:
    class _Metered(_Embedding):
        async def embed(self, texts):
            self.calls.append(tuple(texts))
            return EmbeddingResult(
                vectors=tuple((1.0,) for _ in texts),
                usage=InferenceUsage(requests=1, input_tokens=7, output_tokens=0),
            )

    async def scenario() -> None:
        model = CoalescingEmbeddingModel(_Metered(), coalesce_ms=0)
        result = await model.embed(("first", "first"))
        assert result.usage.input_tokens == 7
        assert result.usage.output_tokens == 0
        cached = await model.embed(("first",))
        assert cached.usage.requests == 0
        assert cached.usage.input_tokens in (None, 0)

    asyncio.run(scenario())


def test_invalid_vector_count_fails_with_value_error() -> None:
    class _Invalid(_Embedding):
        async def embed(self, texts):
            return EmbeddingResult(vectors=())

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await CoalescingEmbeddingModel(_Invalid(), coalesce_ms=0).embed(("broken",))

    asyncio.run(scenario())
