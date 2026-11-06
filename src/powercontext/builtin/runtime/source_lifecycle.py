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

"""Scope-local evidence availability and its native Memory projections."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from graphlib import TopologicalSorter
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import InvalidMemoryCitationError, MemoryEntryNotFoundError, MemoryService
from powercontext.builtin.artifacts.topic_memory import TopicMemory
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_GRAPHS_TABLE,
    SOURCE_LIFECYCLE_WITHDRAWN_TOPICS_TABLE,
)
from powercontext.builtin.persistence.topic_memory import TopicMemoryRepository
from powercontext.builtin.sources.journal import validate_scope_id
from powercontext.builtin.statistics import ModelUsagePurpose
from powercontext.errors import ArtifactNotFoundError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

_Reference: TypeAlias = SourceRef | MemoryCitation | ArtifactRef
_Policy: TypeAlias = Literal["all", "any"]
_PositiveInt: TypeAlias = Annotated[int, Field(strict=True, gt=0)]


class SourceLifecycleError(ValueError):
    """An invalid binding, event, or lifecycle resolution."""


class SourceLifecycleEvent(BaseModel):
    """One availability decision for a registered Source version."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str
    reason: str
    revision: _PositiveInt
    sequence: _PositiveInt
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")  # noqa: TRY003
        return value


class _Node(BaseModel):
    reference: _Reference
    parents: tuple[str, ...] = ()
    policy: _Policy = "all"
    source_revision: int | None = None
    event: SourceLifecycleEvent | None = None
    available: bool = True

    def binding(self) -> tuple[_Reference, tuple[str, ...], _Policy, int | None]:
        return self.reference, self.parents, self.policy, self.source_revision


_Graph: TypeAlias = dict[str, _Node]
_GRAPH = TypeAdapter(_Graph)


class SourceLifecycleTransaction:
    """Use one bound connection for the graph, native Memory, and Topic visibility."""

    def __init__(
        self,
        connection: AsyncConnection,
        scope_id: str,
        *,
        sources: SourceRepository,
        topics: TopicMemoryRepository,
        memory: MemoryService,
        memory_artifact_id: str,
    ) -> None:
        self.connection = connection
        self.scope_id = scope_id
        self.sources = sources
        self.topics = topics
        self.memory = memory
        self.memory_artifact_id = memory_artifact_id

    async def load(self) -> _Graph:
        table = SOURCE_LIFECYCLE_GRAPHS_TABLE
        payload = await self.connection.scalar(select(table.c.payload).where(table.c.scope_id == self.scope_id))
        return {} if payload is None else _GRAPH.validate_json(payload)

    async def validate_reference(self, reference: _Reference) -> None:
        try:
            if isinstance(reference, SourceRef):
                await self.sources.get(self.connection, self.scope_id, reference)
            elif isinstance(reference, MemoryCitation):
                if reference.memory_ref.artifact_id != self.memory_artifact_id:
                    raise SourceLifecycleError("citation does not belong to the scoped Memory")  # noqa: TRY003
                await self.memory.validate_citation(reference)
            else:
                await self.topics.get_exact(self.connection, self.scope_id, reference)
        except (
            RepositoryNotFoundError,
            ArtifactNotFoundError,
            InvalidMemoryCitationError,
            MemoryEntryNotFoundError,
        ) as error:
            raise SourceLifecycleError("registration reference is not readable in this scope") from error  # noqa: TRY003

    async def save(self, previous: _Graph, current: _Graph) -> None:
        old_memory, old_topics = _projection_states(previous.values())
        memory, topics = _projection_states(current.values())
        await self._sync_memory({key: state for key, state in memory.items() if old_memory.get(key) != state})
        await self._sync_topics(topics, old_topics)
        table = SOURCE_LIFECYCLE_GRAPHS_TABLE
        payload = _GRAPH.dump_json(current)
        if previous:
            await self.connection.execute(
                update(table).where(table.c.scope_id == self.scope_id).values(payload=payload)
            )
        else:
            await self.connection.execute(insert(table).values(scope_id=self.scope_id, payload=payload))

    async def _sync_memory(self, changed: dict[tuple[str, str], bool]) -> None:
        if not changed:
            return
        head = await self.memory.head(self.memory_artifact_id)
        desired = {
            item.entry_id: available
            for item in head.content.manifest.entries
            if (available := changed.get((item.entry_id, item.entry_version_id))) is not None
            and available != (item.state == "active")
        }
        if not desired:
            return
        entries = await self.memory.entries(head)
        deactivate = tuple(entry for entry in entries if desired.get(entry.entry_id) is False)
        reactivate = tuple(entry for entry in entries if desired.get(entry.entry_id) is True)
        if deactivate:
            head = await self.memory.forget(head, entries=deactivate, reason="source_lifecycle")
        if reactivate:
            await self.memory.reactivate(head, entries=reactivate, reason="source_lifecycle")

    async def _sync_topics(self, topics: dict[tuple[str, int], bool], previous: dict[tuple[str, int], bool]) -> None:
        table = SOURCE_LIFECYCLE_WITHDRAWN_TOPICS_TABLE
        for (artifact_id, revision), available in topics.items():
            was_available = previous.get((artifact_id, revision), True)
            if available and not was_available:
                await self.connection.execute(
                    delete(table).where(
                        table.c.scope_id == self.scope_id,
                        table.c.artifact_id == artifact_id,
                        table.c.revision == revision,
                    )
                )
            elif not available and was_available:
                await self.connection.execute(
                    insert(table).values(scope_id=self.scope_id, artifact_id=artifact_id, revision=revision)
                )


SourceLifecycleTransactions: TypeAlias = Callable[[str], AbstractAsyncContextManager[SourceLifecycleTransaction]]


class SourceLifecycleApplication:
    """Select the evidence lifecycle for one Runtime scope."""

    def __init__(self, runtime: BuiltinRuntime, transactions: SourceLifecycleTransactions | None) -> None:
        self._runtime = runtime
        self._transactions = transactions

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._transactions, validate_scope_id(scope_id))


class ScopedSourceLifecycle:
    """Register evidence and propagate availability through one immutable dependency graph."""

    def __init__(
        self, runtime: BuiltinRuntime, transactions: SourceLifecycleTransactions | None, scope_id: str
    ) -> None:
        self._runtime = runtime
        self._transactions = transactions
        self._scope_id = scope_id

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if not isinstance(source_ref, SourceRef) or type(revision) is not int or revision < 1:
            raise SourceLifecycleError("expected a SourceRef and positive lifecycle revision")  # noqa: TRY003
        await self._register(node_id, _Node(reference=source_ref, source_revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: Sequence[str], policy: _Policy
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("expected a MemoryCitation")  # noqa: TRY003
        await self._register(node_id, _derived(citation, parents, policy))

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: Sequence[str], policy: _Policy
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != TopicMemory.family:
            raise SourceLifecycleError("expected a Topic Memory ArtifactRef")  # noqa: TRY003
        await self._register(node_id, _derived(artifact_ref, parents, policy))

    async def _register(self, node_id: str, node: _Node) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node_id must contain non-whitespace text")  # noqa: TRY003
        async with self._transaction() as transaction:
            nodes = await transaction.load()
            if (existing := nodes.get(node_id)) is not None:
                if existing.binding() != node.binding():
                    raise SourceLifecycleError("node_id is already bound")  # noqa: TRY003
                return
            if any(parent not in nodes for parent in node.parents):
                raise SourceLifecycleError("all parents must be registered before the derived node")  # noqa: TRY003
            await transaction.validate_reference(node.reference)
            if node.parents:
                node.available = _supported(node, (nodes[parent].available for parent in node.parents))
            await transaction.save(nodes, {**nodes, node_id: node})

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("expected a SourceLifecycleEvent")  # noqa: TRY003
        async with self._transaction() as transaction:
            nodes = await transaction.load()
            root = nodes.get(event.node_id)
            if root is None or not isinstance(root.reference, SourceRef) or root.source_revision != event.revision:
                raise SourceLifecycleError("event does not match a registered Source revision")  # noqa: TRY003
            if root.event is not None and event.sequence <= root.event.sequence:
                if event != root.event:
                    raise SourceLifecycleError("event sequence is stale or conflicts with the recorded event")  # noqa: TRY003
                return _available_ids(nodes)

            updated = {**nodes, event.node_id: root.model_copy(update={"available": event.available, "event": event})}
            if root.available != event.available:
                order = TopologicalSorter({node_id: node.parents for node_id, node in nodes.items()}).static_order()
                for node_id in order:
                    node = updated[node_id]
                    if node.parents:
                        available = _supported(node, (updated[parent].available for parent in node.parents))
                        if node.available != available:
                            updated[node_id] = node.model_copy(update={"available": available})
            await transaction.save(nodes, updated)
            return _available_ids(updated)

    async def resolve(self, node_id: str, *, purpose: Literal["generation", "audit"]) -> _Reference:
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("unknown lifecycle resolution purpose")  # noqa: TRY003
        if not isinstance(node_id, str):
            raise SourceLifecycleError("node_id must be a string")  # noqa: TRY003
        async with self._transaction() as transaction:
            nodes = await transaction.load()
            node = nodes.get(node_id)
            if node is None or (purpose == "generation" and not node.available):
                raise SourceLifecycleError("node is unknown or unavailable for this purpose")  # noqa: TRY003
            return node.reference

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[SourceLifecycleTransaction]:
        if self._transactions is None:
            raise SourceLifecycleError("source lifecycle storage is unavailable")  # noqa: TRY003
        async with (
            self._runtime._scoped_operation(self._scope_id, embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING),
            self._runtime._locked(self._scope_id),
            self._transactions(self._scope_id) as transaction,
        ):
            yield transaction


def _derived(reference: MemoryCitation | ArtifactRef, parents: Sequence[str], policy: _Policy) -> _Node:
    if policy not in ("all", "any"):
        raise SourceLifecycleError("support policy must be all or any")  # noqa: TRY003
    if (
        not isinstance(parents, Sequence)
        or isinstance(parents, str)
        or not parents
        or any(not isinstance(parent, str) for parent in parents)
    ):
        raise SourceLifecycleError("derived nodes require a non-empty sequence of parent IDs")  # noqa: TRY003
    if len(set(parents)) != len(parents):
        raise SourceLifecycleError("parent IDs must be distinct")  # noqa: TRY003
    return _Node(reference=reference, parents=tuple(sorted(parents)), policy=policy)


def _supported(node: _Node, parents: Iterable[bool]) -> bool:
    return all(parents) if node.policy == "all" else any(parents)


def _available_ids(nodes: _Graph) -> list[str]:
    return sorted(node_id for node_id, node in nodes.items() if node.available)


def _projection_states(nodes: Iterable[_Node]) -> tuple[dict[tuple[str, str], bool], dict[tuple[str, int], bool]]:
    # Distinct node IDs may support the same exact native content independently.
    memory: dict[tuple[str, str], bool] = {}
    topics: dict[tuple[str, int], bool] = {}
    for node in nodes:
        reference = node.reference
        if isinstance(reference, MemoryCitation):
            entry = (reference.entry_id, reference.entry_version_id)
            memory[entry] = node.available or memory.get(entry, False)
        elif isinstance(reference, ArtifactRef):
            topic = (reference.artifact_id, reference.revision)
            topics[topic] = node.available or topics.get(topic, False)
    return memory, topics


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
