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

"""Cross-call request coalescing and a bounded vector cache for embeddings."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Final

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage

DEFAULT_MAX_BATCH_SIZE: Final = 64
DEFAULT_COALESCE_MS: Final = 5.0
DEFAULT_CACHE_SIZE: Final = 1024


@dataclass(frozen=True, slots=True)
class _Resolved:
    """One resolved text with the batch that produced it."""

    vector: tuple[float, ...]
    batch_id: int
    usage: InferenceUsage


class CoalescingEmbeddingModel:
    """Share in-flight embedding work and cache successful vectors across calls.

    Concurrent callers that need the same text wait for one provider request
    instead of issuing their own. Successful vectors stay in a bounded LRU cache
    so later calls reuse them without spending another request.
    """

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        """Bind one provider model to the coalescing and cache policy."""

        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self.profile = model.profile
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_Resolved]] = {}
        self._queue: list[str] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._drain: asyncio.Task[None] | None = None
        self._batches: set[asyncio.Task[None]] = set()
        self._batch_id = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input text, keeping the caller's order."""

        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        vectors: dict[str, tuple[float, ...]] = {}
        missing: list[str] = []
        for text in dict.fromkeys(requested):
            cached = self._cached(text)
            if cached is None:
                missing.append(text)
            else:
                vectors[text] = cached

        usage = InferenceUsage(requests=0)
        if missing:
            futures = await self._register(tuple(missing))
            resolved = [await asyncio.shield(futures[text]) for text in missing]
            batches: dict[int, InferenceUsage] = {}
            for text, item in zip(missing, resolved, strict=True):
                vectors[text] = item.vector
                batches.setdefault(item.batch_id, item.usage)
            usage = InferenceUsage(
                requests=len(batches),
                input_tokens=sum(item.input_tokens or 0 for item in batches.values()),
                output_tokens=sum(item.output_tokens or 0 for item in batches.values()),
            )
        return EmbeddingResult(vectors=tuple(vectors[text] for text in requested), usage=usage)

    def _cached(self, text: str) -> tuple[float, ...] | None:
        if self._cache_size < 1:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        # A cache hit is a recent use, so the entry survives one more eviction.
        self._cache.move_to_end(text)
        return vector

    async def _register(self, texts: tuple[str, ...]) -> dict[str, asyncio.Future[_Resolved]]:
        """Join the in-flight work for every text this call still needs."""

        async with self._lock:
            loop = asyncio.get_running_loop()
            futures: dict[str, asyncio.Future[_Resolved]] = {}
            added = False
            for text in texts:
                future = self._inflight.get(text)
                if future is None:
                    future = loop.create_future()
                    self._inflight[text] = future
                    self._queue.append(text)
                    added = True
                futures[text] = future
            if added:
                self._wake.set()
                self._ensure_drain()
        return futures

    def _ensure_drain(self) -> None:
        if self._drain is None or self._drain.done():
            self._drain = asyncio.create_task(self._drain_queue())

    async def _drain_queue(self) -> None:
        """Turn queued texts into bounded provider batches."""

        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._coalesce_seconds:
                # Let the calls that started together join this window.
                await asyncio.sleep(self._coalesce_seconds)
            while True:
                async with self._lock:
                    if not self._queue:
                        self._drain = None
                        return
                    batch = tuple(self._queue[: self._max_batch_size])
                    del self._queue[: len(batch)]
                    self._batch_id += 1
                    batch_id = self._batch_id
                task = asyncio.create_task(self._run_batch(batch, batch_id))
                self._batches.add(task)
                task.add_done_callback(self._batches.discard)

    async def _run_batch(self, texts: tuple[str, ...], batch_id: int) -> None:
        try:
            result = await self._model.embed(texts)
        except BaseException as error:
            await self._settle(texts, error)
            return
        if len(result.vectors) != len(texts):
            await self._settle(texts, ValueError("embedding provider returned a different number of vectors"))
            return
        async with self._lock:
            for text, vector in zip(texts, result.vectors, strict=True):
                if self._cache_size > 0:
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                future = self._inflight.pop(text, None)
                if future is not None and not future.done():
                    future.set_result(_Resolved(vector=vector, batch_id=batch_id, usage=result.usage))

    async def _settle(self, texts: tuple[str, ...], error: BaseException) -> None:
        """Fail every waiter of one batch without touching the other batches."""

        async with self._lock:
            for text in texts:
                future = self._inflight.pop(text, None)
                if future is None or future.done():
                    continue
                if isinstance(error, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(error)


__all__ = [
    "DEFAULT_CACHE_SIZE",
    "DEFAULT_COALESCE_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescingEmbeddingModel",
]
