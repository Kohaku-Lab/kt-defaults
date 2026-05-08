"""Lightweight KohakuRAG database reader — no kohakurag dependency.

Opens a KohakuRAG ``.db`` file via KohakuVault. Uses the same table
layout as KohakuRAG's ``KVaultNodeStore``:

  ``{prefix}_kv``   — node metadata (KVault, auto-packed)
  ``{prefix}_vec``  — embeddings (VectorKVault)
  ``{prefix}_bm25`` — FTS5 text index (TextVault, optional)

Supports context expansion (sentence → paragraph → section) and
tree-based deduplication (remove children when parent is present).
"""

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
from kohakuvault import KVault, TextVault, VectorKVault

from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


# ── Types matching KohakuRAG's schema ────────────────────────────────


class NodeKind(str, Enum):
    DOCUMENT = "document"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    SENTENCE = "sentence"


@dataclass
class StoredNode:
    node_id: str
    parent_id: str | None
    kind: NodeKind
    title: str
    text: str
    child_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RAGResult:
    """A retrieval result with expanded context."""

    content: str
    score: float = 0.0
    node_id: str = ""
    kind: str = ""
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Reader ───────────────────────────────────────────────────────────


class RAGReader:
    """Read-only access to a KohakuRAG database.

    Discovers the table prefix automatically, then opens KVault (metadata),
    VectorKVault (embeddings), and TextVault (BM25) tables.
    """

    def __init__(self, db_path: str | Path):
        self._path = str(Path(db_path).expanduser().resolve())
        if not Path(self._path).exists():
            raise FileNotFoundError(f"RAG database not found: {self._path}")

        self._prefix: str = ""
        self._kv: KVault | None = None
        self._vec: VectorKVault | None = None
        self._bm25: TextVault | None = None
        self._vec_dims: int = 0
        self._discover()

    def _discover(self) -> None:
        """Discover KohakuRAG tables and their prefix."""
        conn = sqlite3.connect(self._path)
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor}
        finally:
            conn.close()

        # Find the prefix by looking for *_kv tables with companion *_vec
        for t in sorted(tables):
            if t.endswith("_kv"):
                prefix = t[: -len("_kv")]
                if f"{prefix}_vec" in tables:
                    self._prefix = prefix
                    break

        if not self._prefix:
            logger.warning("Could not detect KohakuRAG table prefix", tables=tables)
            return

        # Open metadata KVault
        kv_table = f"{self._prefix}_kv"
        if kv_table in tables:
            self._kv = KVault(self._path, table=kv_table)
            self._kv.enable_auto_pack()

        # Open vector table — get dimensions from stored metadata
        vec_table = f"{self._prefix}_vec"
        if vec_table in tables and self._kv:
            try:
                meta = self._kv.get("__kohakurag_meta__", None)
                if meta and isinstance(meta, dict):
                    self._vec_dims = int(meta.get("dimensions", 0))
                if self._vec_dims > 0:
                    self._vec = VectorKVault(
                        self._path, table=vec_table, dimensions=self._vec_dims
                    )
                    self._vec.enable_auto_pack()
            except Exception as e:
                logger.debug("Failed to open vector table", error=str(e))

        # Open BM25 TextVault (optional)
        bm25_table = f"{self._prefix}_bm25"
        if bm25_table in tables:
            try:
                self._bm25 = TextVault(self._path, table=bm25_table)
                self._bm25.enable_auto_pack()
            except Exception:
                pass

        logger.info(
            "RAG database opened",
            path=self._path,
            prefix=self._prefix,
            has_kv=self._kv is not None,
            has_vec=self._vec is not None,
            has_bm25=self._bm25 is not None,
            vec_dims=self._vec_dims,
        )

    @property
    def has_fts(self) -> bool:
        return self._bm25 is not None

    @property
    def has_vectors(self) -> bool:
        return self._vec is not None

    @property
    def vector_dims(self) -> int:
        return self._vec_dims

    # ── Node access ──────────────────────────────────────────────────

    def get_node(self, node_id: str) -> StoredNode | None:
        """Fetch a single node by ID."""
        if not self._kv:
            return None
        try:
            record = self._kv[node_id]
            return _deserialize(record)
        except KeyError:
            return None

    # ── Context expansion ────────────────────────────────────────────

    def get_context(
        self,
        node_id: str,
        parent_depth: int = 1,
        child_depth: int = 0,
    ) -> list[StoredNode]:
        """Expand a node to include parents and/or children.

        Example: for a matched sentence, parent_depth=1 gives the
        paragraph, parent_depth=2 gives paragraph + section.
        """
        node = self.get_node(node_id)
        if not node:
            return []

        context: list[StoredNode] = [node]
        seen: set[str] = {node_id}

        # Walk up the tree
        current = node
        for _ in range(parent_depth):
            if not current.parent_id:
                break
            parent = self.get_node(current.parent_id)
            if not parent or parent.node_id in seen:
                break
            seen.add(parent.node_id)
            context.append(parent)
            current = parent

        # Walk down the tree
        if child_depth > 0:
            _collect_children(self, node, child_depth, context, seen)

        return context

    # ── Search ───────────────────────────────────────────────────────

    def search_fts(self, query: str, k: int = 10) -> list[RAGResult]:
        """BM25 text search via TextVault."""
        if not self._bm25:
            return []
        try:
            hits = self._bm25.search(query, k=k)
            results = []
            for row_id, score, node_id in hits:
                node = self.get_node(str(node_id)) if node_id else None
                results.append(
                    RAGResult(
                        content=node.text if node else "",
                        score=max(0.0, min(1.0, (score + 20) / 20)),
                        node_id=node.node_id if node else "",
                        kind=node.kind.value if node else "",
                        title=node.title if node else "",
                    )
                )
            return results
        except Exception as e:
            logger.debug("BM25 search failed", error=str(e))
            return []

    def search_vector(
        self, query_embedding: np.ndarray, k: int = 10
    ) -> list[RAGResult]:
        """Vector similarity search via VectorKVault."""
        if not self._vec:
            return []
        try:
            hits = self._vec.search(query_embedding.astype(np.float32), k=k)
            results = []
            for row_id, distance, node_id in hits:
                node = self.get_node(str(node_id)) if node_id else None
                results.append(
                    RAGResult(
                        content=node.text if node else "",
                        score=1.0 - float(distance),
                        node_id=node.node_id if node else "",
                        kind=node.kind.value if node else "",
                        title=node.title if node else "",
                    )
                )
            return results
        except Exception as e:
            logger.debug("Vector search failed", error=str(e))
            return []

    def search_hybrid(
        self,
        query: str,
        query_embedding: np.ndarray | None = None,
        k: int = 10,
    ) -> list[RAGResult]:
        """Hybrid search with reciprocal rank fusion."""
        fts_results = self.search_fts(query, k=k * 2)
        vec_results = (
            self.search_vector(query_embedding, k=k * 2)
            if query_embedding is not None
            else []
        )

        if not vec_results:
            return fts_results[:k]
        if not fts_results:
            return vec_results[:k]

        # RRF merge with node_id dedup
        rrf_k = 60
        scores: dict[str, float] = {}
        result_map: dict[str, RAGResult] = {}

        for rank, r in enumerate(fts_results):
            scores[r.node_id] = scores.get(r.node_id, 0) + 1.0 / (rrf_k + rank)
            result_map[r.node_id] = r

        for rank, r in enumerate(vec_results):
            scores[r.node_id] = scores.get(r.node_id, 0) + 1.0 / (rrf_k + rank)
            if r.node_id not in result_map:
                result_map[r.node_id] = r

        ranked = sorted(scores, key=lambda nid: scores[nid], reverse=True)[:k]
        return [
            RAGResult(
                content=result_map[nid].content,
                score=scores[nid],
                node_id=nid,
                kind=result_map[nid].kind,
                title=result_map[nid].title,
                metadata=result_map[nid].metadata,
            )
            for nid in ranked
        ]

    # ── Search with context expansion + dedup ────────────────────────

    def search_with_context(
        self,
        query: str,
        query_embedding: np.ndarray | None = None,
        k: int = 5,
        parent_depth: int = 1,
        child_depth: int = 0,
        dedup: str = "tree",
    ) -> list[RAGResult]:
        """Search + expand + dedup.

        For each hit, walks up to parents (sentence → paragraph → section)
        and down to children. Then deduplicates:
          "tree":    remove children when ancestor is present
          "node_id": simple ID dedup
          "none":    no dedup
        """
        raw_hits = self.search_hybrid(query, query_embedding, k=k)

        all_snippets: list[RAGResult] = []
        seen_ids: set[str] = set()

        for hit in raw_hits:
            if not hit.node_id:
                all_snippets.append(hit)
                continue
            nodes = self.get_context(hit.node_id, parent_depth, child_depth)
            for node in nodes:
                if node.node_id in seen_ids:
                    continue
                seen_ids.add(node.node_id)
                all_snippets.append(
                    RAGResult(
                        content=node.text,
                        score=hit.score,
                        node_id=node.node_id,
                        kind=node.kind.value,
                        title=node.title,
                        metadata=node.metadata,
                    )
                )

        if dedup == "tree":
            all_snippets = _dedup_tree(all_snippets)

        return all_snippets

    def close(self) -> None:
        self._kv = None
        self._vec = None
        self._bm25 = None


# ── Pure helpers ─────────────────────────────────────────────────────


def _deserialize(record: dict) -> StoredNode:
    kind_str = record.get("kind", "paragraph")
    try:
        kind = NodeKind(kind_str)
    except ValueError:
        kind = NodeKind.PARAGRAPH
    return StoredNode(
        node_id=record.get("node_id", ""),
        parent_id=record.get("parent_id"),
        kind=kind,
        title=record.get("title", ""),
        text=record.get("text", ""),
        child_ids=list(record.get("child_ids", [])),
        metadata=record.get("metadata", {}),
    )


def _collect_children(
    reader: RAGReader,
    node: StoredNode,
    depth: int,
    acc: list[StoredNode],
    seen: set[str],
) -> None:
    if depth <= 0:
        return
    for child_id in node.child_ids:
        if child_id in seen:
            continue
        child = reader.get_node(child_id)
        if not child:
            continue
        seen.add(child_id)
        acc.append(child)
        _collect_children(reader, child, depth - 1, acc, seen)


def _dedup_tree(snippets: list[RAGResult]) -> list[RAGResult]:
    """Remove snippets whose ancestor is also present.

    KohakuRAG node IDs are hierarchical: "doc:sec1:p2:s3".
    If "doc:sec1:p2" is present, "doc:sec1:p2:s3" is redundant
    because the parent's text already contains the child's text.
    """
    all_ids = {s.node_id for s in snippets}
    return [
        s
        for s in snippets
        if not any(
            other != s.node_id and s.node_id.startswith(other + ":")
            for other in all_ids
        )
    ]
