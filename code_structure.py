# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Parse source with bundled grammars and cache the useful code locations.

Identifier matches and imports help navigation but are not resolved references.
Each worker has its own parser.
"""
from __future__ import annotations

import ast
import importlib
import threading
from pathlib import Path

LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
}
_local = threading.local()
_CLASSES = {"class_definition", "class_declaration", "interface_declaration", "struct_item",
            "enum_item", "trait_item", "struct_specifier", "class_specifier"}
_FUNCTIONS = {"function_definition", "function_declaration", "generator_function_declaration",
              "method_definition", "method_declaration", "function_item", "constructor_declaration"}


def parser_for(path: str):
    lang = LANGUAGES.get(Path(path).suffix.lower())
    if not lang:
        return None
    parsers = getattr(_local, "parsers", None)
    if parsers is None:
        parsers = _local.parsers = {}
    if lang not in parsers:
        try:
            from tree_sitter import Language, Parser
            module = importlib.import_module("tree_sitter_" + ("typescript" if lang == "tsx" else lang))
            factory = getattr(module, "language_" + lang) if lang in {"typescript", "tsx"} else module.language
            parsers[lang] = Parser(Language(factory()))
        except (ImportError, AttributeError, TypeError, ValueError):
            parsers[lang] = None
    return parsers[lang]


def tree_outline(text: str, path: str) -> list[dict] | None:
    parser = parser_for(path)
    if parser is None:
        return None
    source = text.encode("utf-8")
    root = parser.parse(source).root_node
    lines = text.splitlines()
    entries = []

    def value(node):
        return source[node.start_byte:node.end_byte].decode("utf-8") if node else ""

    # Iterative traversal also handles deeply nested generated source.
    stack = [(root, "", False)]
    while stack:
        node, prefix, in_function = stack.pop()
        kind = "class" if node.type in _CLASSES else "function" if node.type in _FUNCTIONS else None
        name_node = node.child_by_field_name("name")
        if node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val and val.type in {"arrow_function", "function_expression", "generator_function"}:
                kind = "function"
        if node.type == "type_spec" and any(c.type in {"struct_type", "interface_type"} for c in node.named_children):
            kind = "class"
        if node.type == "function_definition" and name_node is None:
            decl = node.child_by_field_name("declarator")
            while decl and decl.type not in {"identifier", "field_identifier", "qualified_identifier"}:
                decl = decl.child_by_field_name("declarator")
            name_node = decl
        if kind and name_node and not in_function:
            name = prefix + value(name_node)
            start_node = node
            if node.parent and node.parent.type in {"decorated_definition", "export_statement"}:
                start_node = node.parent
            line = start_node.start_point.row + 1
            end = node.end_point.row + (1 if node.end_point.column else 0)
            body = node.child_by_field_name("body")
            sig_end = body.start_byte if body else node.end_byte
            signature = " ".join(source[node.start_byte:sig_end].decode("utf-8").split())[:300]
            entries.append({"name": name, "kind": kind, "line": line, "end_line": max(line, end),
                            "signature": signature or lines[line - 1][:300], "exact": not node.has_error})
            if kind == "class":
                prefix = name + "."
            else:
                in_function = True
        for child in reversed(node.named_children):
            stack.append((child, prefix, in_function))
    return entries


def facts(text: str, path: str) -> dict:
    """Extract bounded identifier occurrences and import targets."""
    parser = parser_for(path)
    refs: dict[str, list[int]] = {}
    imports: list[str] = []
    backend = "text"
    errors = False
    if parser:
        source = text.encode("utf-8")
        root = parser.parse(source).root_node
        backend, errors = "tree-sitter", root.has_error
        stack = [root]
        while stack and len(refs) < 20000:
            node = stack.pop()
            if node.type in {"identifier", "property_identifier", "field_identifier", "type_identifier"}:
                name = source[node.start_byte:node.end_byte].decode("utf-8")
                rows = refs.setdefault(name, [])
                line = node.start_point.row + 1
                if len(rows) < 100 and (not rows or rows[-1] != line):
                    rows.append(line)
            if node.type in {"import_statement", "import_declaration", "import_from_statement",
                             "use_declaration", "preproc_include"}:
                fragment = source[node.start_byte:node.end_byte].decode("utf-8")
                import re
                imports.extend(re.findall(r'[\x22\x27]([^\x22\x27]+)[\x22\x27]', fragment))
            stack.extend(reversed(node.named_children))
    # Python's AST supplies import semantics, including relative import levels.
    if Path(path).suffix.lower() in {".py", ".pyi"}:
        try:
            tree = ast.parse(text)
            if not parser:
                backend = "python-ast"
                for node in ast.walk(tree):
                    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
                    if name:
                        refs.setdefault(name, []).append(node.lineno)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = "." * node.level + (node.module or "")
                    imports.append(module)
                    imports.extend(module + ("." if node.module else "") + a.name for a in node.names)
        except (SyntaxError, ValueError, RecursionError):
            errors = True
    return {"refs": {k: sorted(set(v))[:100] for k, v in refs.items()},
            "imports": sorted(set(imports)), "backend": backend, "parse_errors": errors}
