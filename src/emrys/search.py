"""Semantic search over emrys knowledge entries.

Uses sentence-transformers for embedding and cosine similarity.
Falls back to FTS5 keyword search if vectors aren't available.

Install: pip install emrys[vectors]
"""

import logging
import struct
from pathlib import Path

from emrys import db

log = logging.getLogger("emrys")

# Default model — small, fast, runs on CPU, good quality
DEFAULT_MODEL = "all-MiniLM-L6-v2"
_model = None


def _get_model():
    """Lazy-load the sentence-transformers model."""
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise ImportError(
            "Semantic search requires sentence-transformers.\n"
            "Install with: pip install emrys[vectors]"
        )
    log.info("Loading embedding model '%s'...", DEFAULT_MODEL)
    _model = SentenceTransformer(DEFAULT_MODEL)
    return _model


def _embed(text: str) -> bytes:
    """Embed a single text string, return as blob."""
    model = _get_model()
    vec = model.encode(text, normalize_embeddings=True)
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes):
    """Unpack a blob back to a list of floats."""
    n = len(blob) // 4  # 4 bytes per float32
    return struct.unpack(f"{n}f", blob)


def _cosine_similarity(a, b) -> float:
    """Cosine similarity between two vectors (already normalized)."""
    return sum(x * y for x, y in zip(a, b))


def embed_entry(conn, knowledge_id: int, title: str, content: str, tags: str = ""):
    """Embed a single knowledge entry and store in knowledge_vectors."""
    text = f"{title}\n{content}\n{tags}".strip()
    embedding = _embed(text)
    conn.execute(
        """INSERT OR REPLACE INTO knowledge_vectors
           (knowledge_id, embedding, model) VALUES (?, ?, ?)""",
        (knowledge_id, embedding, DEFAULT_MODEL),
    )
    conn.commit()


def embed_all(conn, *, force: bool = False):
    """Embed all knowledge entries that don't have vectors yet.

    Args:
        conn: Database connection.
        force: If True, re-embed even if vectors exist.
    """
    if force:
        rows = conn.execute(
            "SELECT id, title, content, tags FROM knowledge"
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT k.id, k.title, k.content, k.tags
               FROM knowledge k
               LEFT JOIN knowledge_vectors v ON k.id = v.knowledge_id
               WHERE v.id IS NULL"""
        ).fetchall()

    if not rows:
        log.info("All entries already embedded.")
        return 0

    log.info("Embedding %d entries...", len(rows))
    model = _get_model()

    # Batch embed for efficiency
    texts = [f"{r['title']}\n{r['content']}\n{r['tags']}".strip() for r in rows]
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)

    for row, vec in zip(rows, vectors):
        blob = struct.pack(f"{len(vec)}f", *vec)
        conn.execute(
            """INSERT OR REPLACE INTO knowledge_vectors
               (knowledge_id, embedding, model) VALUES (?, ?, ?)""",
            (row["id"], blob, DEFAULT_MODEL),
        )
    conn.commit()
    log.info("Embedded %d entries.", len(rows))
    return len(rows)


def search(
    query: str,
    *,
    limit: int = 10,
    agent: str | None = None,
    topic: str | None = None,
    threshold: float = 0.3,
) -> list[dict]:
    """Semantic search over knowledge entries.

    Args:
        query: Natural language search query.
        limit: Max results to return.
        agent: Filter by agent name (optional).
        topic: Filter by topic (optional).
        threshold: Minimum similarity score (0-1).

    Returns:
        List of dicts with keys: id, title, content, agent, topic, tags, score.
    """
    conn = db.get_db()

    # Check if we have any vectors
    vec_count = conn.execute(
        "SELECT COUNT(*) FROM knowledge_vectors"
    ).fetchone()[0]

    if vec_count == 0:
        log.info("No vectors found. Embedding all entries first...")
        embed_all(conn)
        vec_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_vectors"
        ).fetchone()[0]
        if vec_count == 0:
            log.warning("No knowledge entries to search.")
            return []

    # Embed the query
    query_vec = _embed(query)
    query_floats = _blob_to_vec(query_vec)

    # Fetch all vectors (for small-medium DBs this is fine; for large ones
    # we'd want an ANN index, but emrys is personal-scale)
    where_clauses = []
    params = []
    if agent:
        where_clauses.append("k.agent = ?")
        params.append(agent)
    if topic:
        where_clauses.append("k.topic = ?")
        params.append(topic)

    where_sql = ""
    if where_clauses:
        where_sql = "AND " + " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT k.id, k.title, k.content, k.agent, k.topic, k.tags,
                   k.created_at, v.embedding
            FROM knowledge k
            JOIN knowledge_vectors v ON k.id = v.knowledge_id
            WHERE 1=1 {where_sql}""",
        params,
    ).fetchall()

    # Score each result
    results = []
    for row in rows:
        row_vec = _blob_to_vec(row["embedding"])
        score = _cosine_similarity(query_floats, row_vec)
        if score >= threshold:
            results.append({
                "id": row["id"],
                "title": row["title"],
                "content": row["content"][:200] + ("..." if len(row["content"]) > 200 else ""),
                "agent": row["agent"],
                "topic": row["topic"],
                "tags": row["tags"],
                "created_at": row["created_at"],
                "score": round(score, 4),
            })

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def search_fts(query: str, *, limit: int = 10) -> list[dict]:
    """Fallback keyword search using FTS5. No vectors needed."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT k.id, k.title, k.content, k.agent, k.topic, k.tags, k.created_at
           FROM knowledge_fts f
           JOIN knowledge k ON k.id = f.rowid
           WHERE knowledge_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (query, limit),
    ).fetchall()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "content": r["content"][:200] + ("..." if len(r["content"]) > 200 else ""),
            "agent": r["agent"],
            "topic": r["topic"],
            "tags": r["tags"],
            "created_at": r["created_at"],
            "score": None,  # FTS doesn't give cosine scores
        }
        for r in rows
    ]
