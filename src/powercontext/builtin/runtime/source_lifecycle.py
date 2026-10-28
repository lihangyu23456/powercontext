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

"""Source lifecycle dependencies and the availability they propagate."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.source_lifecycle import (
    SourceLifecycleRepository,
    StoredSourceLifecycleNode,
)
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.sources import SourceRef

SourceLifecyclePurpose = Literal["generation", "audit"]
_SOURCE_KIND: Final = "source"
_MEMORY_KIND: Final = "memory"
_TOPIC_KIND: Final = "topic"


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle registration or event breaks the contract."""


class SourceLifecycleEvent(BaseModel):
    """One availability change for a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int = Field(gt=0, strict=True)
    sequence: int = Field(gt=0, strict=True)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


class ScopedSourceLifecycleService:
    """Register evidence dependencies and propagate Source withdrawals."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        *,
        revision: int = 1,
    ) -> None:
        """Register one captured Source as the root of a dependency branch."""

        _require_node_id(node_id)
        if revision < 1:
            raise SourceLifecycleError("revision must be positive")  # noqa: TRY003
        reference = SourceRef.model_validate(source_ref)
        await self._runtime.records.for_scope(self.scope_id).get_source(
            reference.source_type, reference.source_id
        )
        await self._register(
            StoredSourceLifecycleNode(
                node_id=node_id,
                kind=_SOURCE_KIND,
                parents=(),
                policy="all",
                reference=reference.model_dump(mode="json"),
                artifact_id=None,
                available=True,
                revision=revision,
                sequence=0,
                reason="",
            )
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        """Register one Memory entry and the Sources that support it."""

        _require_node_id(node_id)
        reference = MemoryCitation.model_validate(citation)
        await self._runtime.memory.for_scope(self.scope_id).get(
            GetMemoryEntryRequest(citation=reference)
        )
        await self._register(
            StoredSourceLifecycleNode(
                node_id=node_id,
                kind=_MEMORY_KIND,
                parents=tuple(parents),
                policy=_require_policy(policy),
                reference=reference.model_dump(mode="json"),
                artifact_id=None,
                available=True,
                revision=1,
                sequence=0,
                reason="",
            )
        )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        """Register one Topic artifact and the entries that support it."""

        _require_node_id(node_id)
        artifact = ArtifactRef.model_validate(reference)
        await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
            artifact.family, artifact.artifact_id, artifact.revision
        )
        await self._register(
            StoredSourceLifecycleNode(
                node_id=node_id,
                kind=_TOPIC_KIND,
                parents=tuple(parents),
                policy=_require_policy(policy),
                reference=artifact.model_dump(mode="json"),
                artifact_id=artifact.artifact_id,
                available=True,
                revision=1,
                sequence=0,
                reason="",
            )
        )

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Apply one availability event and return the available node ids."""

        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._runtime.database.transaction() as connection:
                repository = SourceLifecycleRepository()
                nodes = await repository.load(connection, self.scope_id)
                node = nodes.get(event.node_id)
                if node is None or node.kind != _SOURCE_KIND:
                    raise SourceLifecycleError("event targets an unknown Source")  # noqa: TRY003
                if node.revision != event.revision:
                    raise SourceLifecycleError("event revision does not match the Source")  # noqa: TRY003
                if event.sequence < node.sequence:
                    raise SourceLifecycleError("event sequence moved backwards")  # noqa: TRY003
                if event.sequence == node.sequence:
                    if node.available != event.available:
                        raise SourceLifecycleError("event sequence was reused")  # noqa: TRY003
                    return _available(nodes)
                updated = dict(nodes)
                updated[event.node_id] = node.model_copy(
                    update={
                        "available": event.available,
                        "sequence": event.sequence,
                        "reason": event.reason,
                    }
                )
                _propagate(updated)
                changed = _changed_nodes(nodes, updated)
            # The native state is written before the lifecycle state, so a failed
            # native write leaves the event unapplied and the same event can be
            # retried to completion.
            await self._apply_native(changed)
            async with self._runtime.database.transaction() as connection:
                await SourceLifecycleRepository().save(connection, self.scope_id, tuple(updated.values()))
            return _available(updated)

    async def resolve(
        self,
        node_id: str,
        *,
        purpose: str = "generation",
    ) -> MemoryCitation | ArtifactRef | SourceRef:
        """Return the original reference of one node for the requested purpose."""

        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown purpose")  # noqa: TRY003
        async with self._runtime.database.transaction() as connection:
            nodes = await SourceLifecycleRepository().load(connection, self.scope_id)
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown node")  # noqa: TRY003
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("node is not available for generation")  # noqa: TRY003
        if node.kind == _MEMORY_KIND:
            return MemoryCitation.model_validate(node.reference)
        if node.kind == _TOPIC_KIND:
            return ArtifactRef.model_validate(node.reference)
        return SourceRef.model_validate(node.reference)

    async def _register(self, node: StoredSourceLifecycleNode) -> None:
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._runtime.database.transaction() as connection:
                repository = SourceLifecycleRepository()
                nodes = await repository.load(connection, self.scope_id)
                existing = nodes.get(node.node_id)
                if existing is not None:
                    if existing.kind != node.kind or existing.reference != node.reference:
                        raise SourceLifecycleError("node id is already bound to other evidence")  # noqa: TRY003
                    if existing.parents != node.parents or existing.policy != node.policy:
                        raise SourceLifecycleError("node id is already bound to other evidence")  # noqa: TRY003
                    return
                _validate_parents(nodes, node)
                updated = dict(nodes)
                updated[node.node_id] = node
                _propagate(updated)
                changed = _changed_nodes(nodes, updated)
            # The native state is written before the lifecycle state, so a failed
            # native write leaves the registration unapplied and the same
            # registration can be retried to completion.
            await self._apply_native(changed)
            async with self._runtime.database.transaction() as connection:
                await SourceLifecycleRepository().save(connection, self.scope_id, tuple(updated.values()))

    async def _apply_native(self, changed: dict[str, StoredSourceLifecycleNode]) -> None:
        """Deactivate or restore the Memory entries whose support changed."""

        application = self._runtime.memory.for_scope(self.scope_id)
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            for node in changed.values():
                if node.kind != _MEMORY_KIND:
                    continue
                citation = MemoryCitation.model_validate(node.reference)
                record = await application.get(GetMemoryEntryRequest(citation=citation))
                head = await service.head(record.memory_ref.artifact_id)
                # The citation keeps the Memory Revision it was registered against,
                # so the current state is read from the current head.
                current = next(
                    (entry for entry in head.content.manifest.entries if entry.entry_id == record.entry.entry_id),
                    None,
                )
                state = None if current is None else current.state
                if node.available and state == "active":
                    continue
                if not node.available and state == "inactive":
                    continue
                if node.available:
                    await service.reactivate(head, entries=(record.entry,))
                else:
                    await service.forget(head, entries=(record.entry,))


class SourceLifecycleApplication:
    """Select the lifecycle service for one Scope."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        """Return the lifecycle service of one Scope."""

        return ScopedSourceLifecycleService(self._runtime, scope_id)


def _available(nodes: dict[str, StoredSourceLifecycleNode]) -> list[str]:
    return sorted(node_id for node_id, node in nodes.items() if node.available)


def _changed_nodes(
    before: dict[str, StoredSourceLifecycleNode],
    after: dict[str, StoredSourceLifecycleNode],
) -> dict[str, StoredSourceLifecycleNode]:
    """Return the nodes whose availability differs between two graph states."""

    return {
        node_id: node
        for node_id, node in after.items()
        if node_id not in before or before[node_id].available != node.available
    }


def _propagate(nodes: dict[str, StoredSourceLifecycleNode]) -> None:
    """Recompute every derived node's availability until it stops changing."""

    for _ in range(len(nodes) + 1):
        updated = False
        for node_id, node in nodes.items():
            if node.kind == _SOURCE_KIND:
                continue
            support = [nodes[parent].available for parent in node.parents if parent in nodes]
            if not support:
                continue
            available = all(support) if node.policy == "all" else any(support)
            if available != node.available:
                nodes[node_id] = node.model_copy(update={"available": available})
                updated = True
        if not updated:
            return


def _validate_parents(nodes: dict[str, StoredSourceLifecycleNode], node: StoredSourceLifecycleNode) -> None:
    if node.kind == _SOURCE_KIND:
        return
    if not node.parents:
        raise SourceLifecycleError("a derived node needs at least one parent")  # noqa: TRY003
    if len(set(node.parents)) != len(node.parents):
        raise SourceLifecycleError("a derived node must not repeat a parent")  # noqa: TRY003
    for parent in node.parents:
        if parent == node.node_id:
            raise SourceLifecycleError("a derived node must not depend on itself")  # noqa: TRY003
        if parent not in nodes:
            raise SourceLifecycleError("a derived node depends on an unknown parent")  # noqa: TRY003
    if _reaches(nodes, node.parents, node.node_id):
        raise SourceLifecycleError("a derived node must not close a cycle")  # noqa: TRY003


def _reaches(nodes: dict[str, StoredSourceLifecycleNode], parents: Sequence[str], target: str) -> bool:
    pending = list(parents)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        parent = nodes.get(current)
        if parent is not None:
            pending.extend(parent.parents)
    return False


def _require_node_id(node_id: str) -> None:
    if not node_id or not node_id.strip():
        raise SourceLifecycleError("node id must not be blank")  # noqa: TRY003


def _require_policy(policy: str) -> Literal["all", "any"]:
    if policy not in {"all", "any"}:
        raise SourceLifecycleError("policy must be all or any")  # noqa: TRY003
    return policy  # type: ignore[return-value]


__all__ = [
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]
