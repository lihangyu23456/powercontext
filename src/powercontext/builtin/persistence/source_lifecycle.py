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

"""Persistence for the Source lifecycle dependency graph."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.codec import dump_model, load_model, stored_bytes
from powercontext.builtin.persistence.errors import InvalidRepositoryArgumentError
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.limits import MAX_ARTIFACT_ID_LENGTH, MAX_SCOPE_ID_LENGTH, MAX_SOURCE_ID_LENGTH

SourceLifecycleKind = Literal["source", "memory", "topic"]
SourceLifecyclePolicy = Literal["all", "any"]


class StoredSourceLifecycleNode(BaseModel):
    """One registered node of a Scope's evidence dependency graph."""

    node_id: str = Field(max_length=MAX_SOURCE_ID_LENGTH)
    kind: SourceLifecycleKind
    parents: tuple[str, ...] = ()
    policy: SourceLifecyclePolicy = "all"
    reference: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = Field(default=None, max_length=MAX_ARTIFACT_ID_LENGTH)
    available: bool = True
    revision: int = Field(ge=1)
    sequence: int = Field(ge=0)
    reason: str = ""


class SourceLifecycleRepository:
    """Persist the registered nodes and their last applied availability."""

    async def load(self, connection: AsyncConnection, scope_id: str, /) -> dict[str, StoredSourceLifecycleNode]:
        """Return every registered node of one Scope."""

        _require_scope(scope_id)
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
            )
        ).mappings()
        return {str(row["node_id"]): _decode_row(row) for row in rows}

    async def unavailable_artifact_ids(self, connection: AsyncConnection, scope_id: str, /) -> frozenset[str]:
        """Return the Topic artifacts whose evidence no longer supports them."""

        _require_scope(scope_id)
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE.c.artifact_id).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.kind == "topic",
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available.is_(False),
                )
            )
        ).all()
        return frozenset(str(row[0]) for row in rows if row[0] is not None)

    async def save(
        self,
        connection: AsyncConnection,
        scope_id: str,
        nodes: Sequence[StoredSourceLifecycleNode],
        /,
    ) -> None:
        """Replace the registered nodes of one Scope with the given state."""

        _require_scope(scope_id)
        await connection.execute(
            delete(SOURCE_LIFECYCLE_NODES_TABLE).where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
        )
        for node in nodes:
            await connection.execute(
                insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                    scope_id=scope_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    parents=stored_bytes(
                        dump_model(_Parents(parents=node.parents), kind="source-lifecycle", name="parents"),
                        column="parents",
                    ),
                    policy=node.policy,
                    reference=stored_bytes(
                        dump_model(_Reference(reference=node.reference), kind="source-lifecycle", name="reference"),
                        column="reference",
                    ),
                    artifact_id=node.artifact_id,
                    available=1 if node.available else 0,
                    revision=node.revision,
                    sequence=node.sequence,
                    reason=stored_bytes(
                        dump_model(_Reason(reason=node.reason), kind="source-lifecycle", name="reason"),
                        column="reason",
                    ),
                )
            )

    async def touch(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        available: bool,
        revision: int,
        sequence: int,
        reason: str,
    ) -> None:
        """Record the last applied availability event of one Source."""

        _require_scope(scope_id)
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(
                available=1 if available else 0,
                revision=revision,
                sequence=sequence,
                reason=stored_bytes(
                    dump_model(_Reason(reason=reason), kind="source-lifecycle", name="reason"),
                    column="reason",
                ),
            )
        )


class _Parents(BaseModel):
    parents: tuple[str, ...] = ()


class _Reference(BaseModel):
    reference: dict[str, Any] = Field(default_factory=dict)


class _Reason(BaseModel):
    reason: str = ""


def _decode_row(row: Any) -> StoredSourceLifecycleNode:
    return StoredSourceLifecycleNode(
        node_id=str(row["node_id"]),
        kind=str(row["kind"]),
        parents=load_model(_Parents, row["parents"], kind="source-lifecycle", name="parents").parents,
        policy=str(row["policy"]),
        reference=load_model(_Reference, row["reference"], kind="source-lifecycle", name="reference").reference,
        artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
        available=bool(row["available"]),
        revision=int(row["revision"]),
        sequence=int(row["sequence"]),
        reason=load_model(_Reason, row["reason"], kind="source-lifecycle", name="reason").reason,
    )


def _require_scope(scope_id: str) -> None:
    if not scope_id or len(scope_id) > MAX_SCOPE_ID_LENGTH:
        raise InvalidRepositoryArgumentError("scope_id", "must be a bounded identifier")


__all__ = [
    "SourceLifecycleKind",
    "SourceLifecyclePolicy",
    "SourceLifecycleRepository",
    "StoredSourceLifecycleNode",
]
