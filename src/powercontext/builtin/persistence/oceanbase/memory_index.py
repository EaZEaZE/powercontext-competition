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

"""OceanBase Memory search indexes using FULLTEXT and vector indexes."""

from __future__ import annotations

from typing import Any

from pyobvector import VECTOR, VectorIndex
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    Table,
    UniqueConstraint,
    bindparam,
    column,
    delete,
    func,
    insert,
    literal,
    null,
    select,
    text,
    union_all,
)
from sqlalchemy.dialects.mysql import match
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import TextClause
from typing_extensions import override

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import embedding_content_hash, validate_embedding
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.tables import (
    ARTIFACT_HEADS_TABLE,
    MAX_MEMORY_ENTRY_ID_LENGTH,
    MAX_MEMORY_HASH_LENGTH,
    MEMORY_ENTRY_HEADS_TABLE,
    MEMORY_ENTRY_VERSIONS_TABLE,
    identity_string,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql, tag_predicate
from powercontext.limits import MAX_ARTIFACT_ID_LENGTH, MAX_SCOPE_ID_LENGTH

_OCEANBASE_FTS_INDEX_NAME = "ix_pc_memory_entry_heads_fts"
_OCEANBASE_CREATE_FTS_SQL = f"""
CREATE FULLTEXT INDEX {_OCEANBASE_FTS_INDEX_NAME}
ON pc_memory_entry_heads (searchable_text) WITH PARSER SPACE
"""
_OCEANBASE_FTS_INDEX_EXISTS_SQL = text(
    """
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'pc_memory_entry_heads'
      AND index_name = :index_name
    """
)
_OCEANBASE_VECTOR_TABLE_NAME = "pc_memory_vector_entries"
_OCEANBASE_VECTOR_INDEX_NAME = "ix_pc_memory_vector_entries_embedding"
_OCEANBASE_VECTOR_TYPE_SQL = text(
    """
    SELECT data_type
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = :table_name
      AND column_name = 'embedding'
    """
)
_OCEANBASE_VECTOR_SEARCH_SQL = """
SELECT
    m.memory_artifact_id,
    m.head_revision,
    m.entry_id,
    m.entry_version_id,
    {body_column}l2_distance(m.embedding, :query_vector) AS distance
FROM pc_memory_vector_entries AS m
{body_join}WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
{tag_filter}ORDER BY l2_distance(m.embedding, :query_vector){approximate}
LIMIT :candidate_limit
"""
_VECTOR_BODY_JOIN = """
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
"""

# seekdb can push membership in a statement-level JSON value into the vector
# scan. A correlated tag subquery runs after ANN has truncated the neighbors.
_SEEKDB_VECTOR_TAG_FILTER = """
  AND JSON_CONTAINS_PATH(
      (SELECT JSON_OBJECTAGG(eligible.vector_id, 1)
       FROM (
           SELECT vectors.vector_id
           FROM pc_artifact_tags AS tags
           JOIN pc_memory_vector_entries AS vectors
             ON vectors.scope_id = tags.scope_id
            AND vectors.memory_artifact_id = tags.artifact_id
            AND vectors.entry_id = tags.target_id
           WHERE tags.scope_id = :scope_id
             AND tags.artifact_id IN :memory_artifact_ids
             AND tags.family = 'memory'
             AND tags.target_type = 'memory_entry'
             AND tags.tag_key_hash IN :tag_hashes
             AND tags.tag_key IN :tag_keys
           GROUP BY vectors.vector_id
           HAVING COUNT(*) >= :tag_minimum
       ) AS eligible),
      'one', CONCAT('$."', m.vector_id, '"'))
"""


class OceanBaseMemoryFTSIndex:
    """OceanBase FULLTEXT strategy over the relational active-head projection."""

    capabilities = MemoryCapabilities(fts=True, tag_filter=True)
    tables: tuple[Table, ...] = ()

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "mysql":
            raise CapabilityNotSupportedError("oceanbase-fts")
        count = await connection.scalar(
            _OCEANBASE_FTS_INDEX_EXISTS_SQL,
            {"index_name": _OCEANBASE_FTS_INDEX_NAME},
        )
        if count == 0:
            await connection.exec_driver_sql(_OCEANBASE_CREATE_FTS_SQL)
        probe = match(MEMORY_ENTRY_HEADS_TABLE.c.searchable_text, against="powercontext")
        await connection.execute(select(MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id).where(probe).limit(1))

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, projections

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, entry_ids

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, projections

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode not in {"fts", "hybrid"}:
            return MemorySearchChannels()
        rows = (await connection.execute(_fts_candidates(scope_id, request))).mappings()
        return MemorySearchChannels(fts=memory_channel_hits(rows, request.memories))

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        del connection, scope_id, memories, profile
        return False

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        del connection, scope_id
        return projections


class OceanBaseMemoryVectorIndex:
    """OceanBase HNSW strategy over rebuildable active-head embeddings."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        if profile.dimension < 1 or profile.distance != "l2" or profile.normalization != "unit":
            raise CapabilityNotSupportedError(
                "vector",
                "OceanBase requires a positive unit-normalized L2 embedding profile",
            )
        self.profile = profile
        self.capabilities = MemoryCapabilities(
            tag_filter=True,
            fts=False,
            vector=True,
            embedding_profile=profile,
        )
        metadata = MetaData()
        self.table = Table(
            _OCEANBASE_VECTOR_TABLE_NAME,
            metadata,
            Column("vector_id", BigInteger, primary_key=True, autoincrement=True),
            Column("scope_id", identity_string(MAX_SCOPE_ID_LENGTH), nullable=False),
            Column("memory_artifact_id", identity_string(MAX_ARTIFACT_ID_LENGTH), nullable=False),
            Column("head_revision", BigInteger, nullable=False),
            Column("entry_id", identity_string(MAX_MEMORY_ENTRY_ID_LENGTH), nullable=False),
            Column("entry_version_id", identity_string(MAX_MEMORY_ENTRY_ID_LENGTH), nullable=False),
            Column("entry_content_hash", identity_string(MAX_MEMORY_HASH_LENGTH), nullable=False),
            Column("embedding_content_hash", identity_string(MAX_MEMORY_HASH_LENGTH), nullable=False),
            Column("embedding", VECTOR(profile.dimension), nullable=False),
            UniqueConstraint(
                "scope_id",
                "memory_artifact_id",
                "entry_id",
                name="uq_pc_memory_vector_entries_head",
            ),
        )
        self.tables: tuple[Table, ...] = (self.table,)
        self._vector_index = VectorIndex(
            _OCEANBASE_VECTOR_INDEX_NAME,
            self.table.c.embedding,
            params="distance=l2,type=hnsw",
        )
        self._search_sql = _vector_candidates(profile, approximate=True)
        self._tagged_search_sql = _vector_candidates(profile, tag_filter_sql=memory_tag_sql("m"), approximate=False)

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "mysql":
            raise CapabilityNotSupportedError("oceanbase-vector")
        column_type = await connection.scalar(
            _OCEANBASE_VECTOR_TYPE_SQL,
            {"table_name": _OCEANBASE_VECTOR_TABLE_NAME},
        )
        expected = f"VECTOR({self.profile.dimension})"
        if str(column_type).upper() != expected:
            raise CapabilityNotSupportedError(
                "vector",
                f"OceanBase projection uses {column_type!r}; expected {expected}",
            )
        await connection.run_sync(lambda sync_connection: self._vector_index.create(sync_connection, checkfirst=True))

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await connection.execute(
            delete(self.table).where(
                self.table.c.scope_id == scope_id,
                self.table.c.memory_artifact_id == memory_ref.artifact_id,
            )
        )
        await self.upsert(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        if not entry_ids:
            return
        await connection.execute(
            delete(self.table).where(
                self.table.c.scope_id == scope_id,
                self.table.c.memory_artifact_id == memory_ref.artifact_id,
                self.table.c.entry_id.in_(entry_ids),
            )
        )

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        values = []
        for projection in projections:
            if projection.embedding is None or projection.embedding_content_hash is None:
                continue
            entry = projection.entry_version
            values.append({
                "scope_id": scope_id,
                "memory_artifact_id": memory_ref.artifact_id,
                "head_revision": memory_ref.revision,
                "entry_id": entry.entry_id,
                "entry_version_id": entry.entry_version_id,
                "entry_content_hash": entry.entry_content_hash,
                "embedding_content_hash": projection.embedding_content_hash,
                "embedding": validate_embedding(
                    projection.embedding,
                    dimension=self.profile.dimension,
                ),
            })
        if values:
            await connection.execute(insert(self.table), values)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode not in {"vector", "hybrid"}:
            return MemorySearchChannels()
        parameters = self._search_parameters(scope_id, request)
        if not await self.vector_complete(connection, scope_id, request.memories, self.profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            await connection.execute(
                self._search_sql if request.tag_filter is None else self._tagged_search_sql,
                parameters,
            )
        ).mappings()
        return MemorySearchChannels(vector=memory_channel_hits(rows, request.memories))

    def _search_parameters(self, scope_id: str, request: MemorySearchRequest) -> dict[str, Any]:
        if request.embedding_profile != self.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        return {
            "scope_id": scope_id,
            "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
            "query_vector": validate_embedding(request.query_vector, dimension=self.profile.dimension),
            "candidate_limit": request.candidate_limit,
            **memory_tag_parameters(request.tag_filter),
        }

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        if profile != self.profile:
            return False
        for memory in memories:
            current = await connection.scalar(
                select(ARTIFACT_HEADS_TABLE.c.revision).where(
                    ARTIFACT_HEADS_TABLE.c.scope_id == scope_id,
                    ARTIFACT_HEADS_TABLE.c.family == "memory",
                    ARTIFACT_HEADS_TABLE.c.artifact_id == memory.artifact_id,
                )
            )
            if current is None or int(current) != memory.revision:
                # The caller validated this head before the search. If it moved while
                # this query ran, report a stale head so the runtime can retry with it.
                raise CapabilityNotSupportedError("head")
            heads = (
                await connection.execute(
                    select(
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_content_hash,
                    ).where(
                        MEMORY_ENTRY_HEADS_TABLE.c.scope_id == scope_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id == memory.artifact_id,
                    )
                )
            ).all()
            metadata = (
                await connection.execute(
                    select(
                        self.table.c.entry_id,
                        self.table.c.entry_version_id,
                        self.table.c.entry_content_hash,
                        self.table.c.embedding_content_hash,
                    ).where(
                        self.table.c.scope_id == scope_id,
                        self.table.c.memory_artifact_id == memory.artifact_id,
                    )
                )
            ).all()
            expected = {(str(row[0]), str(row[1]), str(row[2])) for row in heads}
            actual = {(str(row[0]), str(row[1]), str(row[2])) for row in metadata}
            if actual != expected:
                return False
            if any(str(row[3]) != _embedding_hash(self.profile, str(row[2])) for row in metadata):
                return False
        return True

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        hydrated: list[MemoryProjection] = []
        for projection in projections:
            entry = projection.entry_version
            row = (
                await connection.execute(
                    select(
                        self.table.c.entry_content_hash,
                        self.table.c.embedding_content_hash,
                        self.table.c.embedding,
                    ).where(
                        self.table.c.scope_id == scope_id,
                        self.table.c.memory_artifact_id == entry.memory_artifact_id,
                        self.table.c.entry_id == entry.entry_id,
                        self.table.c.entry_version_id == entry.entry_version_id,
                    )
                )
            ).one_or_none()
            if (
                row is None
                or str(row[0]) != entry.entry_content_hash
                or str(row[1]) != _embedding_hash(self.profile, entry.entry_content_hash)
            ):
                hydrated.append(projection)
                continue
            embedding = validate_embedding(
                tuple(float(value) for value in row[2]),
                dimension=self.profile.dimension,
            )
            hydrated.append(
                MemoryProjection(
                    entry_version=entry,
                    searchable_text=projection.searchable_text,
                    embedding=embedding,
                    embedding_content_hash=str(row[1]),
                )
            )
        return tuple(hydrated)


_CANDIDATE_COLUMNS = ("memory_artifact_id", "head_revision", "entry_id", "entry_version_id")


class SeekDBMemoryIndex(CompositeMemoryIndex):
    """Keep seekdb's lexical and semantic top-k selections in one statement."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        self._vector = OceanBaseMemoryVectorIndex(profile)
        super().__init__(OceanBaseMemoryFTSIndex(), self._vector)
        self._untagged_candidates = self._vector._search_sql.columns(
            *(column(name) for name in _CANDIDATE_COLUMNS), column("text"), column("distance")
        ).subquery("semantic")
        self._tagged_candidates = (
            _vector_candidates(profile, tag_filter_sql=_SEEKDB_VECTOR_TAG_FILTER, approximate=True, include_body=False)
            .columns(*(column(name) for name in _CANDIDATE_COLUMNS), column("distance"))
            .subquery("semantic")
        )

    @override
    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode != "hybrid":
            return await super().search(connection, scope_id, request)

        # Mode selection has already checked embedding completeness. The
        # repository validates the requested heads around this statement.
        parameters = self._vector._search_parameters(scope_id, request)
        lexical = _fts_candidates(scope_id, request, include_body=False).subquery("lexical")
        semantic = self._untagged_candidates if request.tag_filter is None else self._tagged_candidates

        # Each channel selects its own top-k before UNION ALL. Restore both
        # rankings explicitly because the union does not preserve input order.
        candidates = union_all(
            select(
                literal("fts").label("channel"),
                lexical.c.relevance,
                *(lexical.c[name] for name in _CANDIDATE_COLUMNS),
                null().label("distance"),
                null().label("semantic_rank"),
            ),
            select(
                literal("vector"),
                null(),
                *(semantic.c[name] for name in _CANDIDATE_COLUMNS),
                semantic.c.distance,
                # The native top-k subquery already emits rows in distance
                # order. Capture its tie order before the union and body join.
                func.row_number().over(),
            ),
        ).subquery()
        # Fetch exact bodies only for candidates from the two limited scans.
        versions = MEMORY_ENTRY_VERSIONS_TABLE
        statement = (
            select(candidates, versions.c.text)
            .join(
                versions,
                (versions.c.scope_id == scope_id)
                & (versions.c.memory_artifact_id == candidates.c.memory_artifact_id)
                & (versions.c.entry_version_id == candidates.c.entry_version_id),
            )
            .order_by(
                candidates.c.channel,
                candidates.c.relevance.desc(),
                candidates.c.distance,
                candidates.c.semantic_rank,
                candidates.c.memory_artifact_id,
                candidates.c.entry_id,
                candidates.c.entry_version_id,
            )
        )
        rows = (await connection.execute(statement, parameters)).mappings().all()
        return MemorySearchChannels(
            fts=memory_channel_hits((row for row in rows if row["channel"] == "fts"), request.memories),
            vector=memory_channel_hits((row for row in rows if row["channel"] == "vector"), request.memories),
        )


def oceanbase_memory_index(profile: EmbeddingProfile | None, *, native_hybrid: bool) -> CompositeMemoryIndex:
    """Build the Memory indexes for an OceanBase-compatible deployment."""
    if profile is None:
        return CompositeMemoryIndex(OceanBaseMemoryFTSIndex())
    if native_hybrid:
        return SeekDBMemoryIndex(profile)
    return CompositeMemoryIndex(OceanBaseMemoryFTSIndex(), OceanBaseMemoryVectorIndex(profile))


def _fts_candidates(scope_id: str, request: MemorySearchRequest, *, include_body: bool = True) -> Select[Any]:
    heads = MEMORY_ENTRY_HEADS_TABLE
    score = match(heads.c.searchable_text, against=request.analyzed_query)
    statement = (
        select(
            heads.c.memory_artifact_id,
            heads.c.head_revision,
            heads.c.entry_id,
            heads.c.entry_version_id,
        )
        .where(
            heads.c.scope_id == scope_id,
            heads.c.memory_artifact_id.in_(tuple(memory.artifact_id for memory in request.memories)),
            score,
        )
        .order_by(score.desc(), heads.c.memory_artifact_id, heads.c.entry_id, heads.c.entry_version_id)
        .limit(request.candidate_limit)
    )
    if request.tag_filter is not None:
        statement = statement.where(
            tag_predicate(
                scope_id, "memory", heads.c.memory_artifact_id, "memory_entry", heads.c.entry_id, request.tag_filter
            )
        )
    if not include_body:
        # A selective scope can otherwise make seekdb choose a scalar index
        # and evaluate MATCH as a filter instead of using text retrieval.
        return statement.add_columns(score.label("relevance")).with_hint(
            heads, f"FORCE INDEX ({_OCEANBASE_FTS_INDEX_NAME})", dialect_name="mysql"
        )
    versions = MEMORY_ENTRY_VERSIONS_TABLE
    return statement.add_columns(versions.c.text).join(
        versions,
        (versions.c.scope_id == heads.c.scope_id)
        & (versions.c.memory_artifact_id == heads.c.memory_artifact_id)
        & (versions.c.entry_version_id == heads.c.entry_version_id),
    )


def _vector_candidates(
    profile: EmbeddingProfile, *, approximate: bool, tag_filter_sql: str = "", include_body: bool = True
) -> TextClause:
    statement = text(
        _OCEANBASE_VECTOR_SEARCH_SQL.format(
            body_column="v.text,\n    " if include_body else "",
            body_join=_VECTOR_BODY_JOIN if include_body else "",
            tag_filter=tag_filter_sql,
            approximate=" APPROXIMATE" if approximate else "",
        )
    ).bindparams(
        bindparam("memory_artifact_ids", expanding=True),
        bindparam("query_vector", type_=VECTOR(profile.dimension)),
    )
    if tag_filter_sql:
        statement = statement.bindparams(
            bindparam("tag_keys", expanding=True),
            bindparam("tag_hashes", expanding=True),
        )
    return statement


def _embedding_hash(profile: EmbeddingProfile, entry_hash: str) -> str:
    return embedding_content_hash(
        profile_id=profile.profile_id,
        model=profile.model,
        dimension=profile.dimension,
        distance=profile.distance,
        normalization=profile.normalization,
        entry_content_hash=entry_hash,
    )
