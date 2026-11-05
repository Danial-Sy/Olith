# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Cache project symbols, text search, vectors and dependency maps in SQLite.

Refresh the file list before queries and check source hashes before returning
locations. Models and parsers are installed separately.
"""
from __future__ import annotations

import hashlib
import builtins
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import threading
import time
import heapq
from importlib.metadata import PackageNotFoundError, version
from collections import defaultdict
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import code_structure
import symbols
from config import DATA

INDEX_DIR = DATA / "indexes"
SCHEMA = "1"
MAX_FILES = 20000
MAX_BYTES = 1_000_000
CHUNK_CHARS = 2200
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "build", "dist", ".next",
             ".cache", "site-packages", "blobs", "snapshots", "checkpoints", ".ollama", "wandb"}
TEXT_SUFFIXES = set(code_structure.LANGUAGES) | {
    ".md", ".txt", ".rst", ".html", ".css", ".scss", ".json", ".toml", ".yaml", ".yml",
    ".sh", ".bash", ".ps1", ".sql", ".vue", ".svelte", ".rb", ".php", ".cs", ".kt", ".swift", ".cfg", ".ini", ".mod",
}


def terms(text: str) -> list[str]:
    # Split camelCase as well as snake_case for natural-language/code matching.
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return list(dict.fromkeys(re.findall(r"[a-zA-Z][a-zA-Z0-9]{1,63}", text.lower())))[:40]


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_candidate(path: Path, root: Path, source_only: bool = True, checked_dirs: dict | None = None) -> bool:
    try:
        rel = path.relative_to(root)
        if ".." in rel.parts:
            return False
        if checked_dirs is None:
            path.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    if any(p in SKIP_DIRS or p.startswith("models--") for p in rel.parts):
        return False
    for i in range(1, len(rel.parts) + 1):
        part = root.joinpath(*rel.parts[:i])
        if checked_dirs is not None and i < len(rel.parts):
            if part not in checked_dirs:
                checked_dirs[part] = part.is_symlink()
            linked = checked_dirs[part]
        else:
            linked = part.is_symlink()
        if linked:
            return False
    name = path.name.lower()
    if source_only and (name == ".env" or name.startswith(".env.") or name in {"credentials.json", "secrets.json"}):
        return False
    return not source_only or path.suffix.lower() in TEXT_SUFFIXES or name in {"dockerfile", "makefile"}


def _inventory(root: Path, source_only: bool = True, onerror=None):
    """Honor Git ignores, including nested rules; use pathspec outside Git."""
    import pathspec
    # Shell snapshots only need containment checks once per directory in each capture.
    checked_dirs = {} if not source_only else None
    olith_ignore = root / ".olithignore"
    custom = pathspec.GitIgnoreSpec.from_lines(
        olith_ignore.read_text(encoding="utf-8").splitlines() if olith_ignore.is_file() and not olith_ignore.is_symlink() else [])
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                               "--exclude-standard", "-z", "--", "."],
                              capture_output=True, timeout=10, check=True)
        for raw in sorted(set(proc.stdout.split(b"\0"))):
            if raw:
                path = root / os.fsdecode(raw)
                rel = path.relative_to(root).as_posix()
                if not custom.match_file(rel) and _safe_candidate(path, root, source_only, checked_dirs):
                    yield path
        return
    except (OSError, subprocess.SubprocessError):
        pass
    rules = {}
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        here = Path(folder)
        inherited = list(rules.get(here.parent, []))
        ignore_file = here / ".gitignore"
        if ignore_file.is_file() and not ignore_file.is_symlink():
            inherited.append((here, pathspec.GitIgnoreSpec.from_lines(ignore_file.read_text(encoding="utf-8").splitlines())))
        rules[here] = inherited

        def ignored(path, directory=False):
            rel = path.relative_to(root).as_posix() + ("/" if directory else "")
            if custom.match_file(rel):
                return True
            result = False
            for base, spec in inherited:
                match = spec.check_file(path.relative_to(base).as_posix() + ("/" if directory else ""))
                if match.include is not None:
                    result = match.include
            return result

        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (here / d).is_symlink()
                         and not ignored(here / d, True))
        for name in sorted(files):
            path = here / name
            if not ignored(path) and _safe_candidate(path, root, source_only, checked_dirs):
                yield path


def _chunks(text: str, entries: list[dict]):
    lines = text.splitlines()
    boundaries = {e["line"] for e in entries}
    pos = 0
    while pos < len(lines):
        start, length = pos, 0
        while pos < len(lines) and pos - start < 60:
            size = len(lines[pos]) + 1
            if pos > start and (length + size > CHUNK_CHARS or pos + 1 in boundaries):
                break
            length += size
            pos += 1
        enclosing = [e for e in entries if e["line"] <= start + 1 <= e["end_line"]]
        name = min(enclosing, key=lambda e: e["end_line"] - e["line"])["name"] if enclosing else ""
        body = "\n".join(lines[start:pos])
        yield start + 1, pos, name, body[:CHUNK_CHARS], len(body) > CHUNK_CHARS


def pagerank(graph: dict[str, set[str]], seeds: dict[str, float] | None = None) -> dict[str, float]:
    """Personalized PageRank; dangling nodes redistribute using the task prior."""
    if not graph:
        return {}
    n = len(graph)
    prior = {p: 1.0 + (seeds or {}).get(p, 0.0) for p in graph}
    total = sum(prior.values())
    prior = {p: v / total for p, v in prior.items()}
    scores = dict(prior)
    for _ in range(40):
        dangling = sum(scores[p] for p, targets in graph.items() if not targets)
        nxt = {p: (0.15 + 0.85 * dangling) * prior[p] for p in graph}
        for p, targets in graph.items():
            if targets:
                share = 0.85 * scores[p] / len(targets)
                for target in targets:
                    nxt[target] += share
        if sum(abs(nxt[p] - scores[p]) for p in graph) < 1e-8:
            return nxt
        scores = nxt
    return scores


class ProjectIndex:
    def __init__(self, root: Path, cache_dir: Path | None = None):
        self.root = Path(root).resolve()
        self.key = _hash(os.fsencode(self.root))[:24]
        self.cache_dir = Path(cache_dir) if cache_dir is not None else INDEX_DIR
        self.path = self.cache_dir / (self.key + ".sqlite3")
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.path), threading.RLock())

    @contextmanager
    def db(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, stamp TEXT, hash TEXT, facts TEXT, symbols TEXT);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, path TEXT REFERENCES files(path) ON DELETE CASCADE,
                line INTEGER, end_line INTEGER, name TEXT, body TEXT, clipped INTEGER, hash TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(path, name, body);
            CREATE TABLE IF NOT EXISTS vectors (
                chunk INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                model TEXT, dimension INTEGER, vector BLOB);
            CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
        """)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def refresh(self, force: bool = False, progress=None) -> dict:
        if not self.root.is_dir():
            raise ValueError("Project folder is unavailable")
        with self.lock, self.db() as db:
            parser_versions = []
            for package in ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript", "tree-sitter-typescript",
                            "tree-sitter-go", "tree-sitter-rust", "tree-sitter-java", "tree-sitter-c", "tree-sitter-cpp"):
                try:
                    parser_versions.append(package + ":" + version(package))
                except PackageNotFoundError:
                    parser_versions.append(package + ":missing")
            schema = SCHEMA + ":" + ",".join(parser_versions)
            stored_version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            force = force or not stored_version or stored_version[0] != schema
            previous = {r["path"]: dict(r) for r in db.execute("SELECT * FROM files")}
            seen, changed, skipped, count = set(), 0, 0, 0
            truncated = False
            for path in _inventory(self.root):
                if count >= MAX_FILES:
                    truncated = True
                    break
                count += 1
                rel = path.relative_to(self.root).as_posix()
                try:
                    stat = path.stat()
                    if stat.st_size > MAX_BYTES:
                        skipped += 1
                        continue
                    stamp = f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}"
                    old = previous.get(rel)
                    if old and old["stamp"] == stamp and not force:
                        seen.add(rel)
                        continue
                    raw = path.read_bytes()
                    if b"\0" in raw or len(raw) > MAX_BYTES:
                        skipped += 1
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeError):
                    skipped += 1
                    continue
                seen.add(rel)
                if progress and count % 50 == 0:
                    progress({"files": len(seen), "changed": changed})
                digest = _hash(raw)
                if old and old["hash"] == digest and not force:
                    db.execute("UPDATE files SET stamp=? WHERE path=?", (stamp, rel))
                    continue
                self._delete_file(db, rel)
                entries = symbols.outline(text, rel) or []
                info = code_structure.facts(text, rel)
                db.execute("INSERT INTO files VALUES (?,?,?,?,?)",
                           (rel, stamp, digest, json.dumps(info), json.dumps(entries)))
                for start, end, name, body, clipped in _chunks(text, entries):
                    chunk_hash = _hash((rel + "\n" + name + "\n" + body).encode())
                    cur = db.execute("INSERT INTO chunks(path,line,end_line,name,body,clipped,hash) VALUES (?,?,?,?,?,?,?)",
                                     (rel, start, end, name, body, int(clipped), chunk_hash))
                    db.execute("INSERT INTO search(rowid,path,name,body) VALUES (?,?,?,?)",
                               (cur.lastrowid, rel, " ".join(terms(name)) + " " + name,
                                " ".join(terms(body)) + "\n" + body))
                changed += 1
                if progress:
                    progress({"files": len(seen), "changed": changed})
            removed = set(previous) - seen
            # Keep only this manifest, including when coverage is partial. An
            # older cached file may now be ignored and must not leak into hits.
            for rel in removed:
                self._delete_file(db, rel)
            stats = {"files": db.execute("SELECT count(*) FROM files").fetchone()[0],
                     "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                     "changed": changed, "removed": len(removed), "skipped": skipped,
                     "truncated": truncated, "updated": time.time()}
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema',?)", (schema,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('status',?)", (json.dumps(stats),))
            return stats

    @staticmethod
    def _delete_file(db, rel):
        db.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (rel,))
        db.execute("DELETE FROM files WHERE path=?", (rel,))

    def status(self, model: str = "") -> dict:
        if not self.path.exists():
            return {"files": 0, "chunks": 0, "vectors": 0, "updated": None}
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='status'").fetchone()
            result = json.loads(row[0]) if row else {}
            result["vectors"] = db.execute("SELECT count(*) FROM vectors WHERE model LIKE ?", (model + "@%",)).fetchone()[0] if model else 0
            result["parsers"] = sorted({json.loads(r[0])["backend"] for r in db.execute("SELECT facts FROM files")})
            return result

    def _records(self, db):
        return {r["path"]: {"hash": r["hash"], "symbols": json.loads(r["symbols"]),
                            **json.loads(r["facts"])} for r in db.execute("SELECT * FROM files")}

    def _graph(self, records: dict):
        graph = {p: set() for p in records}
        owners = defaultdict(set)
        modules = defaultdict(set)
        def family(path):
            language = code_structure.LANGUAGES.get(PurePosixPath(path).suffix)
            if language in {"javascript", "typescript", "tsx"}:
                return "javascript"
            if language in {"c", "cpp"}:
                return "c"
            return language
        for path, info in records.items():
            for entry in info["symbols"]:
                owners[entry["name"].split(".")[-1]].add(path)
            stem = str(PurePosixPath(path).with_suffix(""))
            modules[stem].add(path)
            modules[stem.replace("/", ".")].add(path)
            if stem.endswith("/__init__"):
                modules[stem[:-9].replace("/", ".")].add(path)
        for path, info in records.items():
            for imp in info["imports"]:
                candidates = {imp, imp.replace("::", "/")}
                if imp.startswith(".") and path.endswith((".py", ".pyi")):
                    level = len(imp) - len(imp.lstrip("."))
                    parts = list(PurePosixPath(path).parent.parts)
                    parts = parts[:max(0, len(parts) - level + 1)]
                    candidates.add(".".join(parts + [imp.lstrip(".")]).strip("."))
                elif imp.startswith(".") or "/" in imp:
                    relative = os.path.normpath(str(PurePosixPath(path).parent / imp)).replace("\\", "/")
                    candidates.update({relative, str(PurePosixPath(relative).with_suffix("")), relative + "/index"})
                for candidate in candidates:
                    graph[path].update(modules.get(candidate, ()))
            # Symbol edges are heuristic. Ambiguous/common names add no edge.
            for name in info["refs"]:
                if len(name) < 4 or (family(path) == "python" and name in vars(builtins)):
                    continue
                targets = owners.get(name, ())
                if len(targets) == 1:
                    graph[path].update(target for target in targets if family(path) and family(target) == family(path))
            graph[path].discard(path)
        return graph

    def repo_map(self, query: str = "", token_budget: int = 1400, *, refresh: bool = True) -> str:
        from ollama_client import estimate_tokens
        if refresh:
            self.refresh()
        token_budget = max(0, min(int(token_budget), 4096))
        if token_budget < 128:
            return ""
        with self.db() as db:
            records = self._records(db)
        if not records:
            return ""
        wanted = set(terms(query))
        seeds = {p: 8.0 * len(wanted & set(terms(p + " " + " ".join(e["name"] for e in info["symbols"]))))
                 for p, info in records.items()}
        graph = self._graph(records)
        ranks = pagerank(graph, seeds)
        # Direct task matches survive even when an obscure module has no inbound edges.
        ordered = sorted(records, key=lambda p: (-(ranks[p] + seeds[p] / max(1, sum(seeds.values()))), p))
        header = ("# Repository map\nNavigation hints from cached source; read current code before editing. "
                  "Dependencies and identifier references are heuristic, not a complete call graph.\n")
        result = header
        for path in ordered:
            info = records[path]
            entries = sorted(info["symbols"], key=lambda e: (-len(wanted & set(terms(e["name"]))), e["line"]))
            block = path + ":\n"
            for entry in entries[:8]:
                block += f"  {entry['line']}: {entry['signature'][:160]}\n"
            if graph[path]:
                block += "  uses: " + ", ".join(sorted(graph[path])[:4]) + "\n"
            if estimate_tokens(result + block) <= token_budget:
                if self._live(path, info["hash"]) is not None:
                    result += block
            elif entries:
                short = f"{path}: {entries[0]['line']}: {entries[0]['signature'][:120]}\n"
                if estimate_tokens(result + short) <= token_budget:
                    if self._live(path, info["hash"]) is not None:
                        result += short
            if estimate_tokens(result) > token_budget - 48:
                break
        return result if result != header else ""

    def _live(self, path: str, digest: str) -> list[str] | None:
        candidate = self.root / path
        if not _safe_candidate(candidate, self.root):
            return None
        try:
            raw = candidate.read_bytes()
            return raw.decode("utf-8").splitlines() if _hash(raw) == digest else None
        except (OSError, UnicodeError):
            return None

    @staticmethod
    def _matches(path, subpath, glob):
        prefix = subpath.strip("/")
        return (not prefix or prefix == "." or path == prefix or path.startswith(prefix + "/")) and (
            not glob or glob in {"**/*", "**"} or fnmatch(path, glob) or
            (glob.startswith("**/") and fnmatch(path, glob[3:])))

    def search(self, query: str, mode: str = "hybrid", limit: int = 8, *,
               subpath: str = "", glob: str = "", embedder=None) -> dict:
        if mode not in {"hybrid", "symbols", "references"}:
            raise ValueError("Unknown indexed search mode")
        if not query.strip() or len(query) > 2000:
            raise ValueError("Search query must contain 1–2000 characters")
        limit = max(1, min(int(limit), 20))
        stats = self.refresh()
        with self.db() as db:
            records = self._records(db)
            if mode in {"symbols", "references"}:
                hits = []
                for path, info in sorted(records.items()):
                    if not self._matches(path, subpath, glob):
                        continue
                    lines = self._live(path, info["hash"])
                    if lines is None:
                        continue
                    if mode == "symbols":
                        for entry in info["symbols"]:
                            if query.lower() in entry["name"].lower():
                                hits.append({"file": path, **entry})
                    else:
                        for line in info["refs"].get(query.split(".")[-1], []):
                            hits.append({"file": path, "line": line, "text": lines[line - 1][:240]})
                return {"mode": mode, "hits": hits[:limit], "truncated": len(hits) > limit,
                        "hint": "Identifier occurrences include declarations; bindings are not compiler-resolved." if mode == "references" else "Read symbols with read_file(path, symbol)."}
            tokens = terms(query)
            lexical = []
            if tokens:
                expression = " OR ".join('"' + term + '"' for term in tokens)
                for row in db.execute("SELECT rowid, path, bm25(search,2,5,1) AS rank FROM search WHERE search MATCH ? ORDER BY rank", (expression,)):
                    if self._matches(row["path"], subpath, glob):
                        lexical.append(row)
                    if len(lexical) >= 300:
                        break
            scores = defaultdict(float)
            for rank, row in enumerate(lexical):
                scores[row["rowid"]] += 1.0 / (40 + rank)
            semantic = {"available": False, "reason": "Semantic search is disabled; using text and symbols."}
            if embedder:
                try:
                    model = embedder.identity()
                    vector_count = db.execute("SELECT count(*) FROM vectors WHERE model=?", (model,)).fetchone()[0]
                    if vector_count:
                        vec = embedder.embed([query], query=True)[0]
                        def similarities():
                            for row in db.execute("SELECT v.*, c.path FROM vectors v JOIN chunks c ON c.id=v.chunk WHERE model=?", (model,)):
                                if row["dimension"] == len(vec) and self._matches(row["path"], subpath, glob):
                                    stored = struct.unpack(f"<{len(vec)}f", row["vector"])
                                    yield sum(a * b for a, b in zip(vec, stored)), row["chunk"]
                        ranked = heapq.nlargest(100, similarities())
                        for rank, (_, chunk_id) in enumerate(ranked):
                            scores[chunk_id] += 1.0 / (40 + rank)
                        semantic = {"available": bool(ranked), "indexed_chunks": vector_count, "total_chunks": stats["chunks"]}
                        if not ranked:
                            semantic["reason"] = "No compatible embeddings for this scope; rebuild the semantic index."
                    else:
                        semantic["reason"] = "Build the semantic index from the project's Index panel."
                except Exception as exc:
                    semantic["reason"] = f"Embeddings unavailable; using text and symbols. {exc}"
            # Promote exact identifiers; preserve rare exact matches in large repos.
            exact = db.execute("SELECT id, path FROM chunks WHERE lower(name)=? OR lower(name) LIKE ?",
                               (query.lower(), "%." + query.lower())).fetchall()
            for row in exact:
                if self._matches(row["path"], subpath, glob):
                    scores[row[0]] += 0.06
            hits, verified, remaining = [], {}, 6000
            for chunk_id in sorted(scores, key=lambda cid: (-scores[cid], cid)):
                row = db.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
                if not row or not self._matches(row["path"], subpath, glob):
                    continue
                path = row["path"]
                if path not in verified:
                    verified[path] = self._live(path, records[path]["hash"])
                if verified[path] is None:
                    continue
                excerpt = row["body"][:remaining]
                hits.append({"file": path, "line": row["line"], "end_line": row["end_line"],
                             "symbol": row["name"], "text": excerpt, "clipped": bool(row["clipped"]) or len(excerpt) < len(row["body"]),
                             "score": round(scores[chunk_id], 5)})
                remaining -= len(excerpt)
                if len(hits) >= limit or remaining < 200:
                    break
            return {"mode": "hybrid", "hits": hits, "semantic": semantic, "index": stats}

    def embedding_batch(self, embedder, batch_size: int = 8) -> int:
        """Embed one batch; commit only chunks that still have the same content."""
        model = embedder.identity()
        with self.db() as db:
            rows = list(db.execute("SELECT c.* FROM chunks c LEFT JOIN vectors v ON v.chunk=c.id AND v.model=? WHERE v.chunk IS NULL LIMIT ?",
                                   (model, batch_size)))
        if not rows:
            return 0
        vectors = embedder.embed([r["path"] + "\n" + r["name"] + "\n" + r["body"] for r in rows])
        with self.lock, self.db() as db:
            for row, vector in zip(rows, vectors):
                current = db.execute("SELECT hash FROM chunks WHERE id=?", (row["id"],)).fetchone()
                if current and current[0] == row["hash"]:
                    db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                               (row["id"], model, len(vector), struct.pack(f"<{len(vector)}f", *vector)))
        return len(rows)


class OllamaEmbedder:
    def __init__(self, settings: dict):
        from config import OLLAMA_HOST
        self.host = OLLAMA_HOST
        self.model = str(settings.get("embedding_model") or "nomic-embed-text").strip()
        self.device = settings.get("embedding_device", "cpu")
        self._identity = None

    def identity(self, refresh: bool = False) -> str:
        import httpx
        if self._identity is None or refresh:
            response = httpx.get(self.host + "/api/tags", timeout=5)
            response.raise_for_status()
            target = self.model if ":" in self.model else self.model + ":latest"
            match = next((m for m in response.json().get("models", []) if (m.get("name") or m.get("model")) == target), None)
            if not match or not match.get("digest"):
                raise ValueError(f"Install the embedding model first: ollama pull {self.model}")
            self._identity = self.model + "@" + match["digest"] + ":v1"
        return self._identity

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        import httpx
        expected_identity = self.identity()
        # Nomic's training contract distinguishes query and document prefixes.
        if self.model.split(":")[0].split("/")[-1].startswith("nomic-embed-text"):
            texts = [("search_query: " if query else "search_document: ") + t for t in texts]
        response = httpx.post(self.host + "/api/embed", json={
            "model": self.model, "input": texts, "truncate": False,
            "keep_alive": 0, "options": {"num_gpu": 0} if self.device == "cpu" else {},
        }, timeout=45)
        response.raise_for_status()
        if self.identity(refresh=True) != expected_identity:
            raise ValueError("The embedding model changed during this request; rebuild its index.")
        vectors = response.json().get("embeddings", [])
        if len(vectors) != len(texts):
            raise ValueError("Embedding server returned the wrong number of vectors")
        normalized = []
        for vector in vectors:
            if not isinstance(vector, list) or not 1 <= len(vector) <= 8192:
                raise ValueError("Embedding server returned an invalid dimension")
            values = [float(v) for v in vector]
            norm = math.sqrt(sum(v * v for v in values))
            if not math.isfinite(norm) or norm == 0:
                raise ValueError("Embedding server returned an invalid vector")
            normalized.append([v / norm for v in values])
        if len({len(v) for v in normalized}) != 1:
            raise ValueError("Embedding dimensions differ within a batch")
        return normalized
