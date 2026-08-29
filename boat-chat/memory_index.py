#!/usr/bin/env python3
"""Local hybrid memory index for Boat Chat.

The index always supports SQLite FTS5 keyword retrieval. If sqlite-vec is
installed, it also stores deterministic hashed embeddings for vector KNN.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_DB_PATH = ROOT / "memory" / "boat_memory.sqlite"
VECTOR_DIM = 384
CHUNK_TARGET_CHARS = 1400
CHUNK_OVERLAP_LINES = 3

DEFAULT_DOC_PATHS = [
    REPO_ROOT / "BOAT_AGENT.md",
    REPO_ROOT / "STACK_OVERVIEW.md",
    REPO_ROOT / "RESTORE.md",
    REPO_ROOT / "boat-chat" / "boat_facts.json",
    REPO_ROOT / "boat-chat" / "BOAT_CHAT_AGENT.md",
    REPO_ROOT / "boat-chat" / "README.md",
    REPO_ROOT / "boat-chat" / "POWER_MONITORING.md",
    REPO_ROOT / "boat-chat" / "knowledge" / "telemetry.md",
    REPO_ROOT / "boat-chat" / "knowledge" / "answering.md",
]
_SEMANTIC_MODEL: dict[str, Any] = {"name": None, "model": None, "failed": False}


def db_path() -> Path:
    return Path(os.environ.get("BOAT_CHAT_MEMORY_DB", str(DEFAULT_DB_PATH)))


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma synchronous=normal")
    return conn


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore

        sqlite_vec.load(conn)
        return True
    except Exception:
        return False


def init_schema(conn: sqlite3.Connection) -> bool:
    has_vec = load_sqlite_vec(conn)
    conn.executescript(
        """
        create table if not exists chunks (
            id integer primary key,
            source text not null,
            path text not null,
            line integer not null,
            title text not null,
            text text not null,
            chunk_hash text not null unique,
            updated_at integer not null
        );
        create virtual table if not exists chunks_fts using fts5(
            title,
            path,
            text,
            tokenize='porter unicode61'
        );
        create table if not exists index_meta (
            key text primary key,
            value text not null
        );
        """
    )
    if has_vec:
        conn.execute(f"create virtual table if not exists chunk_vec using vec0(embedding float[{VECTOR_DIM}])")
    conn.commit()
    return has_vec


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_./:-]{1,}", text)]


def embedding_backend() -> str:
    model = os.environ.get("BOAT_CHAT_EMBEDDING_MODEL")
    return f"sentence-transformers:{model}" if model and not _SEMANTIC_MODEL["failed"] else "hashed-token"


def semantic_embed_text(text: str, dim: int) -> list[float] | None:
    model_name = os.environ.get("BOAT_CHAT_EMBEDDING_MODEL", "").strip()
    if not model_name or _SEMANTIC_MODEL["failed"]:
        return None
    try:
        if _SEMANTIC_MODEL["model"] is None or _SEMANTIC_MODEL["name"] != model_name:
            from sentence_transformers import SentenceTransformer  # type: ignore
            _SEMANTIC_MODEL.update(name=model_name, model=SentenceTransformer(model_name), failed=False)
        values = [float(value) for value in _SEMANTIC_MODEL["model"].encode(text, normalize_embeddings=True).tolist()]
        values = (values + [0.0] * dim)[:dim]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values
    except Exception:
        _SEMANTIC_MODEL["failed"] = True
        return None


def embed_text(text: str, dim: int = VECTOR_DIM) -> list[float]:
    semantic = semantic_embed_text(text, dim)
    if semantic is not None:
        return semantic
    vector = [0.0] * dim
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % dim
        sign = 1.0 if raw & (1 << 63) else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def serialize_vector(vector: list[float]) -> bytes:
    try:
        import sqlite_vec  # type: ignore

        return sqlite_vec.serialize_float32(vector)
    except Exception:
        return struct.pack(f"{len(vector)}f", *vector)


def chunk_markdown(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return []

    chunks: list[dict[str, Any]] = []
    current: list[tuple[int, str]] = []
    current_title = path.name
    last_heading = path.name

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = "\n".join(line for _, line in current).strip()
        if text:
            chunks.append(
                {
                    "source": "markdown",
                    "path": str(path),
                    "line": current[0][0],
                    "title": current_title or last_heading,
                    "text": text,
                }
            )
        current = current[-CHUNK_OVERLAP_LINES:] if CHUNK_OVERLAP_LINES else []

    for line_no, line in enumerate(lines, start=1):
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            last_heading = heading.group(2).strip()
            if current and sum(len(item[1]) for item in current) >= 300:
                flush()
            current_title = last_heading
        current.append((line_no, line))
        if sum(len(item[1]) + 1 for item in current) >= CHUNK_TARGET_CHARS:
            flush()

    if current:
        text = "\n".join(line for _, line in current).strip()
        if text and (not chunks or chunks[-1]["text"] != text):
            chunks.append(
                {
                    "source": "markdown",
                    "path": str(path),
                    "line": current[0][0],
                    "title": current_title or last_heading,
                    "text": text,
                }
            )
    return chunks


def chunk_json(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except Exception:
        return []
    text = json.dumps(data, indent=2, sort_keys=True)
    return [
        {
            "source": "json",
            "path": str(path),
            "line": 1,
            "title": path.name,
            "text": text,
        }
    ]


def chunk_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        return chunk_json(path)
    return chunk_markdown(path)


def chunk_hash(chunk: dict[str, Any]) -> str:
    payload = json.dumps(
        {"path": chunk["path"], "line": chunk["line"], "title": chunk["title"], "text": chunk["text"]},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rebuild(paths: list[Path] | None = None, path: Path | None = None) -> dict[str, Any]:
    conn = connect(path)
    has_vec = init_schema(conn)
    docs = paths or DEFAULT_DOC_PATHS
    chunks: list[dict[str, Any]] = []
    for doc_path in docs:
        if doc_path.exists() and doc_path.is_file():
            chunks.extend(chunk_file(doc_path))

    now = int(time.time())
    with conn:
        conn.execute("delete from chunks")
        conn.execute("delete from chunks_fts")
        if has_vec:
            conn.execute("delete from chunk_vec")
        for chunk in chunks:
            digest = chunk_hash(chunk)
            cursor = conn.execute(
                """
                insert into chunks (source, path, line, title, text, chunk_hash, updated_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk["source"], chunk["path"], chunk["line"], chunk["title"], chunk["text"], digest, now),
            )
            rowid = cursor.lastrowid
            conn.execute(
                "insert into chunks_fts (rowid, title, path, text) values (?, ?, ?, ?)",
                (rowid, chunk["title"], chunk["path"], chunk["text"]),
            )
            if has_vec:
                conn.execute(
                    "insert into chunk_vec (rowid, embedding) values (?, ?)",
                    (rowid, serialize_vector(embed_text(chunk["title"] + "\n" + chunk["text"]))),
                )
        conn.execute(
            "insert or replace into index_meta (key, value) values (?, ?)",
            ("last_rebuild", json.dumps({"time": now, "chunks": len(chunks), "sqlite_vec": has_vec, "embedding_backend": embedding_backend()})),
        )
    conn.close()
    return {"chunks": len(chunks), "sqlite_vec": has_vec, "embedding_backend": embedding_backend(), "path": str(path or db_path())}


def fts_query(query: str) -> str:
    tokens = tokenize(query)
    if not tokens:
        return ""
    # Tokens may contain FTS5 syntax characters (dots in SignalK paths, colons,
    # slashes), so each term must be quoted to stay a string literal.
    return " OR ".join('"' + token.replace('"', '') + '"' for token in tokens[:12])


def search(query: str, limit: int = 8, path: Path | None = None) -> list[dict[str, Any]]:
    target = path or db_path()
    if not target.exists():
        return []
    conn = connect(target)
    has_vec = init_schema(conn)
    scores: dict[int, dict[str, Any]] = {}

    fts = fts_query(query)
    if fts:
        try:
            rows = conn.execute(
                """
                select c.id, c.path, c.line, c.title, c.text, bm25(chunks_fts) as distance
                from chunks_fts
                join chunks c on c.id = chunks_fts.rowid
                where chunks_fts match ?
                order by bm25(chunks_fts)
                limit ?
                """,
                (fts, max(limit * 2, 12)),
            ).fetchall()
            for rank, row in enumerate(rows):
                scores[row["id"]] = {
                    "path": row["path"],
                    "line": row["line"],
                    "title": row["title"],
                    "excerpt": row["text"],
                    "score": round(1.0 / (rank + 1), 4),
                    "retrieval": "fts",
                }
        except sqlite3.Error:
            pass

    if has_vec:
        try:
            rows = conn.execute(
                """
                select c.id, c.path, c.line, c.title, c.text, v.distance
                from chunk_vec v
                join chunks c on c.id = v.rowid
                where v.embedding match ? and k = ?
                order by v.distance
                """,
                (serialize_vector(embed_text(query)), max(limit * 2, 12)),
            ).fetchall()
            for rank, row in enumerate(rows):
                current = scores.get(row["id"])
                vec_score = 0.2 / (rank + 1)
                if current:
                    current["score"] = round(float(current["score"]) + vec_score, 4)
                    current["retrieval"] = "hybrid"
                else:
                    scores[row["id"]] = {
                        "path": row["path"],
                        "line": row["line"],
                        "title": row["title"],
                        "excerpt": row["text"],
                        "score": round(vec_score, 4),
                        "distance": round(float(row["distance"]), 6),
                        "retrieval": "vector",
                    }
        except sqlite3.Error:
            pass

    conn.close()
    results = sorted(scores.values(), key=lambda item: float(item["score"]), reverse=True)
    return results[:limit]


def status(path: Path | None = None) -> dict[str, Any]:
    target = path or db_path()
    if not target.exists():
        return {"path": str(target), "exists": False, "chunks": 0, "sqlite_vec": False}
    conn = connect(target)
    has_vec = init_schema(conn)
    chunks = conn.execute("select count(*) from chunks").fetchone()[0]
    meta_row = conn.execute("select value from index_meta where key = 'last_rebuild'").fetchone()
    conn.close()
    return {
        "path": str(target),
        "exists": True,
        "chunks": chunks,
        "sqlite_vec": has_vec,
        "last_rebuild": json.loads(meta_row["value"]) if meta_row else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and query the Boat Chat memory index.")
    parser.add_argument("command", choices=["rebuild", "search", "status"])
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    if args.command == "rebuild":
        print(json.dumps(rebuild(), indent=2, sort_keys=True))
    elif args.command == "search":
        print(json.dumps(search(args.query, args.limit), indent=2, sort_keys=True))
    else:
        print(json.dumps(status(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
