# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Save the task's starting files, including uncommitted work.

Undo only selected edit blocks and reject files changed since review.
Leave the Git index alone.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path

from config import DATA
from repo_index import MAX_FILES, ProjectIndex, _inventory, _safe_candidate

CHANGE_DIR = DATA / "changes"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class FileSnapshot:
    """Content fingerprints for shell evidence, without indexing or storing source."""
    def __init__(self, root: Path, previous: FileSnapshot | None = None):
        self.files = {}
        self.cache = {}
        self.partial = not root.is_dir()
        total = 0

        def unreadable(error):
            self.partial = True

        try:
            for index, path in enumerate(_inventory(root, source_only=False, onerror=unreadable)):
                if index >= MAX_FILES:
                    self.partial = True
                    break
                rel = path.relative_to(root).as_posix()
                try:
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_mode)
                    cached = previous.cache.get(rel) if previous else None
                    if cached and cached[0] == stamp:
                        fingerprint = cached[1]
                    else:
                        if total + info.st_size > MAX_SNAPSHOT_BYTES:
                            self.partial = True
                            continue
                        hasher = hashlib.sha256()
                        with path.open("rb") as stream:
                            while chunk := stream.read(128 * 1024):
                                total += len(chunk)
                                if total > MAX_SNAPSHOT_BYTES:
                                    self.partial = True
                                    break
                                hasher.update(chunk)
                        if total > MAX_SNAPSHOT_BYTES:
                            continue
                        after = path.stat()
                        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino, after.st_mode) != stamp:
                            self.partial = True
                            continue
                        fingerprint = (hasher.hexdigest(), stat.S_IMODE(info.st_mode))
                    self.files[rel] = fingerprint
                    self.cache[rel] = (stamp, fingerprint)
                except FileNotFoundError:
                    # Git still lists deleted tracked files; absence belongs in the diff.
                    continue
                except OSError:
                    self.partial = True
        except (OSError, ValueError, RuntimeError):
            self.partial = True


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


class ChangeSet:
    def __init__(self, root: Path, ident: str | None = None):
        self.root = Path(root).resolve()
        self.ident = ident or uuid.uuid4().hex
        if len(self.ident) != 32 or any(c not in "0123456789abcdef" for c in self.ident):
            raise ValueError("Invalid change set")
        self.path = CHANGE_DIR / (self.ident + ".sqlite3")

    @contextmanager
    def db(self):
        CHANGE_DIR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.executescript("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,before BLOB,after BLOB); CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);")
        if "mode" not in {row[1] for row in db.execute("PRAGMA table_info(files)")}:
            db.execute("ALTER TABLE files ADD COLUMN mode INTEGER")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _current(self):
        index = ProjectIndex(self.root)
        index.refresh()
        with index.db() as db:
            paths = [r[0] for r in db.execute("SELECT path FROM files ORDER BY path")]
        result, total = {}, 0
        for rel in paths:
            target = self.root / rel
            if not _safe_candidate(target, self.root):
                continue
            try:
                raw = target.read_bytes()
            except OSError:
                continue
            total += len(raw)
            if total > MAX_SNAPSHOT_BYTES:
                break
            result[rel] = raw
        return result, total > MAX_SNAPSHOT_BYTES, paths

    def start(self):
        current, partial, paths = self._current()
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('root',?)", (str(self.root),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('partial',?)", (json.dumps(partial),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('initial_paths',?)", (json.dumps(paths),))
            for path, raw in current.items():
                db.execute("INSERT INTO files(path,before,after,mode) VALUES (?,?,?,?)", (path, zlib.compress(raw), zlib.compress(raw), (self.root / path).stat().st_mode))
        return self

    def finish(self, final=False):
        current, partial, _ = self._current()
        with self.db() as db:
            initial = {r["path"]: r for r in db.execute("SELECT * FROM files")}
            original_paths = set(json.loads(db.execute("SELECT value FROM meta WHERE key='initial_paths'").fetchone()[0]))
            for path in sorted(set(initial) | set(current)):
                if path not in initial and path in original_paths:
                    # A file omitted by the original size cap is not newly created.
                    continue
                before = zlib.decompress(initial[path]["before"]) if path in initial and initial[path]["before"] is not None else None
                # A bounded snapshot must not mislabel unvisited source as deleted.
                if path not in current and (self.root / path).exists():
                    continue
                after = current.get(path)
                if before == after:
                    continue
                db.execute("INSERT INTO files(path,before,after) VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET after=excluded.after",
                           (path, zlib.compress(before) if before is not None else None,
                            zlib.compress(after) if after is not None else None))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('finished','true')")
            if final:
                db.execute("DELETE FROM files WHERE before=after")
            if partial:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('partial','true')")
        return self.review()

    def review(self):
        if not self.path.is_file():
            raise ValueError("Unknown change set")
        changes = []
        with self.db() as db:
            root = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            if not root or root[0] != str(self.root):
                raise ValueError("Change set belongs to another project")
            partial = db.execute("SELECT value FROM meta WHERE key='partial'").fetchone()
            for row in db.execute("SELECT * FROM files ORDER BY path"):
                before = zlib.decompress(row["before"]) if row["before"] is not None else None
                after = zlib.decompress(row["after"]) if row["after"] is not None else None
                if before == after:
                    continue
                left = (before or b"").decode("utf-8").splitlines(keepends=True)
                right = (after or b"").decode("utf-8").splitlines(keepends=True)
                hunks = []
                for n, (tag, i, j, a, b) in enumerate(difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes()):
                    if tag != "equal":
                        hunks.append({"id": str(n), "before_start": i, "before_end": j,
                                      "after_start": a, "after_end": b,
                                      "diff": "".join(["-" + line for line in left[i:j]] + ["+" + line for line in right[a:b]])[:16000]})
                if not hunks:
                    hunks = [{"id": "0", "before_start": 0, "before_end": 0, "after_start": 0, "after_end": 0, "diff": "Empty file created or deleted"}]
                changes.append({"file": row["path"], "before_hash": digest(before), "after_hash": digest(after),
                                "status": "created" if before is None else "deleted" if after is None else "modified",
                                "hunks": hunks})
        return {"id": self.ident, "files": changes, "partial": bool(partial and partial[0] == "true")}

    def contents(self):
        """Before and after text of each changed file, for summarizing what a run touched."""
        out = []
        with self.db() as db:
            for row in db.execute("SELECT * FROM files ORDER BY path"):
                before = zlib.decompress(row["before"]) if row["before"] is not None else None
                after = zlib.decompress(row["after"]) if row["after"] is not None else None
                if before == after:
                    continue
                status = "created" if before is None else "deleted" if after is None else "modified"
                decode = lambda raw: raw.decode("utf-8", "replace") if raw is not None else None
                out.append({"file": row["path"], "status": status, "before_text": decode(before), "after_text": decode(after)})
        return {"files": out}

    def undo(self, selections: list[dict]):
        review = self.review()
        known = {f["file"]: f for f in review["files"]}
        plans = []
        seen = set()
        with self.db() as db:
            for selected in selections:
                rel = selected.get("file")
                if rel not in known or rel in seen:
                    raise ValueError("Select each changed file at most once")
                seen.add(rel)
                target = self.root / rel
                if not _safe_candidate(target, self.root):
                    raise ValueError("Undo target is outside the project or a symlink")
                raw = target.read_bytes() if target.exists() else None
                if digest(raw) != known[rel]["after_hash"]:
                    raise ValueError(f"{rel} changed after review. Refresh or review it manually.")
                row = db.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
                before = zlib.decompress(row["before"]) if row["before"] is not None else None
                left = (before or b"").decode("utf-8").splitlines(keepends=True)
                right = (raw or b"").decode("utf-8").splitlines(keepends=True)
                wanted = {str(h) for h in selected.get("hunks", [])}
                available = {h["id"] for h in known[rel]["hunks"]}
                if not wanted or not wanted <= available:
                    raise ValueError("Select existing edit blocks to undo")
                for h in reversed(known[rel]["hunks"]):
                    if h["id"] in wanted:
                        right[h["after_start"]:h["after_end"]] = left[h["before_start"]:h["before_end"]]
                updated = "".join(right).encode("utf-8")
                if before is None and wanted == available:
                    updated = None
                plans.append((rel, target, updated, row["mode"]))
            # Validate every target before making the first change.
            for rel, target, updated, mode in plans:
                if updated is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd, temp = tempfile.mkstemp(prefix=".olith-undo-", dir=target.parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(updated)
                        if target.exists():
                            os.chmod(temp, target.stat().st_mode)
                        elif mode is not None:
                            os.chmod(temp, mode)
                        os.replace(temp, target)
                    finally:
                        if Path(temp).exists():
                            Path(temp).unlink()
                db.execute("UPDATE files SET after=? WHERE path=?", (zlib.compress(updated) if updated is not None else None, rel))
        return self.review()
