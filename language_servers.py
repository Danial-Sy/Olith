# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Start local language servers as needed and reuse them per project and language.

Accept diagnostics and locations inside the project. Do not let servers apply
edits or execute commands.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from config import DATA, ROOT
from code_structure import LANGUAGES

_clients = {}
_lock = threading.RLock()
_failures = {}
MAX_IDLE_CLIENTS = 4


def _language(path):
    language = LANGUAGES.get(Path(path).suffix.lower())
    return "typescript" if language in {"javascript", "typescript", "tsx"} else language


def server_command(root, language):
    names = {"python": [("pyright-langserver", "--stdio"), ("pylsp",), ("jedi-language-server",)],
             "typescript": [("typescript-language-server", "--stdio")],
             "go": [("gopls",)], "rust": [("rust-analyzer",)],
             "c": [("clangd",)], "cpp": [("clangd",)], "java": [("jdtls",)]}.get(language, [])
    folders = [Path(root) / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
               ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
               Path(root) / "node_modules/.bin", DATA / "tools/language-servers/node_modules/.bin"]
    for name, *args in names:
        for folder in folders:
            for suffix in ((".exe", ".cmd", "") if os.name == "nt" else ("",)):
                candidate = folder / (name + suffix)
                if candidate.is_file():
                    return [str(candidate), *args]
        candidate = shutil.which(name)
        if candidate:
            return [candidate, *args]
    return None


def local_path(uri, root):
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("Language server returned a nonlocal location")
    raw = unquote(parsed.path)
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    path = Path(raw).resolve()
    path.relative_to(root)
    return path


class Client:
    def __init__(self, root, language, command):
        self.root, self.language = Path(root).resolve(), language
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.pending = {}
        self.diagnostics = {}
        self.documents = {}
        self.seq = 0
        self.last_used = time.monotonic()
        self.proc = subprocess.Popen(command, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, daemon=True).start()
        try:
            result = self.request("initialize", {"processId": os.getpid(), "rootUri": self.root.as_uri(),
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                "initializationOptions": {"diagnostics": {"enable": True}},
                "capabilities": {"workspace": {"configuration": True}, "textDocument": {"publishDiagnostics": {"versionSupport": True}}}})
            self.capabilities = result.get("capabilities", {})
            self.send("initialized", {})
        except Exception:
            self.close()
            raise

    def _write(self, message):
        raw = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        with self.lock:
            self.proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
            self.proc.stdin.flush()

    def send(self, method, params):
        self._write({"method": method, "params": params})

    def request(self, method, params, timeout=8):
        with self.lock:
            self.seq += 1
            ident = self.seq
            response = queue.Queue()
            self.pending[ident] = response
            self._write({"id": ident, "method": method, "params": params})
        try:
            message = response.get(timeout=timeout)
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message.get("result")
        finally:
            self.pending.pop(ident, None)

    def _read(self):
        try:
            while True:
                headers = {}
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        raise EOFError()
                    if line in {b"\r\n", b"\n"}:
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", 0))
                if not 0 < length <= 8_000_000:
                    raise ValueError("Invalid LSP frame size")
                message = json.loads(self.proc.stdout.read(length))
                if "method" in message and "id" in message:
                    method = message["method"]
                    result = [None] * len(message.get("params", {}).get("items", [])) if method == "workspace/configuration" else {"applied": False, "failureReason": "Olith applies edits through its file tools"} if method == "workspace/applyEdit" else None
                    self._write({"id": message["id"], "result": result})
                elif "id" in message:
                    pending = self.pending.get(message["id"])
                    if pending:
                        pending.put(message)
                elif message.get("method") == "textDocument/publishDiagnostics":
                    info = message.get("params", {})
                    try:
                        path = local_path(info["uri"], self.root)
                    except (KeyError, ValueError):
                        continue
                    self.diagnostics[str(path)] = {"version": info.get("version"), "items": info.get("diagnostics", [])}
        except Exception:
            for pending in list(self.pending.values()):
                pending.put({"error": "Language server disconnected"})

    def sync(self, path):
        path = Path(path).resolve()
        path.relative_to(self.root)
        text = path.read_text(encoding="utf-8")
        old = self.documents.get(str(path))
        if old and old[1] == text:
            return old[0]
        version = old[0] + 1 if old else 1
        self.diagnostics.pop(str(path), None)
        if old:
            self.send("textDocument/didChange", {"textDocument": {"uri": path.as_uri(), "version": version}, "contentChanges": [{"text": text}]})
        else:
            language = "typescriptreact" if path.suffix == ".tsx" else "javascript" if path.suffix in {".js", ".jsx", ".mjs", ".cjs"} else self.language
            self.send("textDocument/didOpen", {"textDocument": {"uri": path.as_uri(), "languageId": language, "version": version, "text": text}})
        self.documents[str(path)] = (version, text)
        self.send("textDocument/didSave", {"textDocument": {"uri": path.as_uri()}})
        return version

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


def client_for(root, path):
    language = _language(path)
    if not language:
        return None
    key = (str(Path(root).resolve()), language)
    with _lock:
        client = _clients.get(key)
        if client and client.proc.poll() is None:
            client.last_used = time.monotonic()
            return client
        if time.monotonic() - _failures.get(key, 0) < 30:
            return None
        command = server_command(root, language)
        if not command:
            return None
        try:
            client = Client(root, language, command)
            _clients[key] = client
            for old_key, old in sorted(list(_clients.items()), key=lambda item: item[1].last_used):
                if len(_clients) <= MAX_IDLE_CLIENTS:
                    break
                if old is not client and old.operation_lock.acquire(blocking=False):
                    try:
                        old.close()
                        _clients.pop(old_key, None)
                    finally:
                        old.operation_lock.release()
            return client
        except Exception:
            _failures[key] = time.monotonic()
            return None


def inspect_file(root, path, action="diagnostics", line=1, character=0, new_name=""):
    client = client_for(root, path)
    if client is None:
        return {"available": False, "reason": "No working language server for this language; syntax index remains available."}
    path = Path(path).resolve()
    # Serialize document updates without blocking server-initiated RPC replies.
    with client.operation_lock:
        version = client.sync(path)
        if str(path) not in client.diagnostics:
            # Initial analysis prevents navigation stopping at an import alias.
            client.request("textDocument/documentSymbol", {"textDocument": {"uri": path.as_uri()}})
            deadline = time.monotonic() + 1.5
            while str(path) not in client.diagnostics and time.monotonic() < deadline:
                time.sleep(0.02)
        if action == "diagnostics":
            result = client.diagnostics.get(str(path))
            current = result and (result["version"] is None or result["version"] == version)
            return {"available": True, "server": client.language, "complete": bool(current),
                    "diagnostics": result["items"][:40] if current else [], "version": version}
        method = {"definition": "textDocument/definition", "references": "textDocument/references", "hover": "textDocument/hover", "rename": "textDocument/rename"}.get(action)
        if not method:
            raise ValueError("Unknown language-server action")
        params = {"textDocument": {"uri": path.as_uri()}, "position": {"line": max(0, line - 1), "character": max(0, character)}}
        if action == "references":
            params["context"] = {"includeDeclaration": True}
        if action == "rename":
            if not new_name.strip() or len(new_name) > 128:
                raise ValueError("Rename needs a new_name of 1–128 characters")
            if not client.capabilities.get("renameProvider"):
                return {"available": False, "reason": "This language server does not provide rename edits."}
            params["newName"] = new_name
        result = client.request(method, params)
        if action == "rename":
            proposed = []
            skipped = []
            edits = list((result or {}).get("changes", {}).items())
            for change in (result or {}).get("documentChanges", []):
                if "textDocument" in change:
                    edits.append((change["textDocument"]["uri"], change.get("edits", [])))
                else:
                    skipped.append("A file operation requires separate review")
            for uri, replacements in edits:
                try:
                    target = local_path(uri, client.root)
                    raw = target.read_bytes()
                    source = raw.decode("utf-8").splitlines(keepends=True)
                    converted = []
                    for edit in replacements:
                        start, end = edit["range"]["start"], edit["range"]["end"]
                        first, last = start["line"], end["line"]
                        if first == last:
                            old = source[first].encode("utf-16-le")[start["character"] * 2:end["character"] * 2].decode("utf-16-le")
                        else:
                            old = source[first].encode("utf-16-le")[start["character"] * 2:].decode("utf-16-le") + "".join(source[first + 1:last])
                            if last < len(source):
                                old += source[last].encode("utf-16-le")[:end["character"] * 2].decode("utf-16-le")
                        converted.append({"start_line": first + 1, "end_line": last + 1,
                                          "start_character": start["character"], "end_character": end["character"],
                                          "old_text": old, "new_text": edit["newText"]})
                    proposed.append({"file": target.relative_to(client.root).as_posix(),
                                     "revision": hashlib.sha256(raw).hexdigest(), "edits": converted})
                except (OSError, ValueError, KeyError, IndexError):
                    skipped.append("A rename target is unavailable or outside the active project")
            return {"available": True, "action": "rename", "preview_only": True, "files": proposed,
                    "skipped": skipped, "hint": "Read each current source file and apply these proposed binding-aware changes with edit_file. No edits have been applied; preserve unrelated code."}
        if action == "hover":
            return {"available": True, "hover": result}
        if action == "definition":
            # Follow an import binding once to find its exported definition.
            locations = result if isinstance(result, list) else [result] if result else []
            if len(locations) == 1:
                location = locations[0]
                uri = location.get("uri") or location.get("targetUri")
                span = location.get("range") or location.get("targetSelectionRange")
                if uri == path.as_uri() and span:
                    row = span["start"]["line"]
                    lines = client.documents[str(path)][1].splitlines()
                    if row < len(lines) and lines[row].lstrip().startswith(("import ", "from ")):
                        followed = client.request(method, {"textDocument": {"uri": uri}, "position": span["start"]})
                        if followed:
                            result = followed
        hits = []
        for location in result if isinstance(result, list) else [result] if result else []:
            try:
                target = local_path(location.get("uri") or location["targetUri"], client.root)
                span = location.get("range") or location["targetSelectionRange"]
                hits.append({"file": target.relative_to(client.root).as_posix(), "line": span["start"]["line"] + 1,
                             "character": span["start"]["character"], "end_line": span["end"]["line"] + 1})
            except (KeyError, ValueError):
                continue
        return {"available": True, "action": action, "hits": hits[:100], "binding_aware": True}


def close_all():
    with _lock:
        for client in list(_clients.values()):
            client.close()
        _clients.clear()


atexit.register(close_all)
