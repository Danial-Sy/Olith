# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep Olith's record of file changes and checks.

Separate existing tests, tests changed during the task, and build/run checks
so the final reply shows what was actually checked.
"""
from __future__ import annotations

import difflib
import glob
import os
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Iterator
from itertools import takewhile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import symbols
from task_changes import FileSnapshot

# Commands that only read, or only write files, are not checks.
_NOT_CHECKS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "find", "grep", "rg", "which", "pwd",
    "date", "tree", "du", "df", "basename", "dirname", "realpath", "sort", "uniq", "cut",
    "sed", "awk", "diff", "cmp", "md5sum", "sha256sum", "jq", "git", "cd", "echo", "printf",
    "mkdir", "cp", "mv", "rm", "touch", "tee", "chmod", "ln", "true", "false", "sleep",
    "pkg-config", "ldconfig", "dpkg", "apt", "apt-cache", "export", "set", "test", "[", "[[",
    "for", "case", "done", "fi", "esac", "}", "install", "dd", "pushd", "popd",
})

# Quoted text and comments are matched first, so a commit message body is never read as commands.
_SEPARATOR = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.|(?<![^\s;&|])#[^\n]*|(&&|\|\||;|\||\n)""", re.S)

# Only non-word characters are escapes, so a Windows path like C:\tmp\a.txt keeps its backslashes.
_SHELL_TOKEN = re.compile(
    r"""(?P<compare>(?<![^\s;&|()$])\(\((?:[^()]|\([^()]*\))*\)\)|"""
    r"""(?<![^\s;&|()])\[\[\s(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^'"\\])*?\s\]\](?![^\s;&|()]))|"""
    r"""'(?P<single>[^']*)'|"(?P<double>(?:\\.|[^"\\])*)"|\\(?P<escape>[^\w./:-])|(?<![^\s;&|()])#[^\n]*|"""
    r"""(?<![^\s;&|()])\d+(?=[<>])|[<>]&[\d-]+|(?P<write>>>?[|&]?)|(?P<read><+&?)|(?P<separator>[;&|()`\n])|"""
    r"""[^\S\n]+|(?P<plain>[^\s;&|<>()`'"\\]+|['"\\])""",
    re.S,
)
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)")
# Per tool: flags that hold the script, and flags whose value could be read as more flags.
_INPLACE = {"sed": ("ef", ""), "perl": ("eE", "MmIF")}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{"})
# Commands that run the command after them: their flags that take a value, and their leading operands.
_WRAPPERS = {
    "sudo": ("ugprtCDhRTU", 0), "doas": ("uC", 0), "env": ("uCS", 0), "nice": ("n", 0), "ionice": ("cnp", 0),
    "timeout": ("ks", 1), "stdbuf": ("ioe", 0), "time": ("fo", 0), "nohup": ("", 0), "command": ("", 0),
    "exec": ("a", 0), "busybox": ("", 0), "xargs": ("aEILnPsd", 0), "parallel": ("jaSNnI", 0),
}
# These hand the wrapped command file names that the command line does not show.
_FANOUT = frozenset({"xargs", "parallel"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FILE_TOOLS = frozenset({*_WRAPPERS, *_INPLACE, "tee", "cp", "mv", "rm", "find", "bash", "sh",
                         "touch", "ln", "install", "dd", "git"})
# A name the shell fills in when it runs, so the file cannot be known from the command.
_EXPANSION = re.compile(r"[$*?`]|\[.*\]|\{.*\}")
_INLINE_CODE = re.compile(r"-\w*[ceE]|--(?:eval|exec|execute|command)")

_TEST_FILE = re.compile(r"(?i)(?:^|/)(?:tests?/|test_|test\.)|_test\.|\.test\.|\.spec\.|_spec\.")
_TEST_RUNNER = re.compile(
    r"\b(?:pytest|unittest|jest|vitest|mocha|ctest|phpunit|rspec|node\s+--test|"
    r"(?:go|cargo|npm|pnpm|yarn|bun|dotnet|mvn|gradle|make)\s+(?:run\s+)?(?:test|check))\b"
)

CLAIM = re.compile(
    r"(?i)\b(verified|all (?:tests|checks) pass\w*|tests? pass\w*|check(?:s)? pass\w*|passes|"
    r"now works|works now|confirmed|builds? (?:clean(?:ly)?|successfully)|"
    r"compiles? (?:clean(?:ly)?|successfully))\b"
)


def _split_commands(command: str) -> list[str]:
    parts, start = [], 0
    for match in _SEPARATOR.finditer(command):
        if match.group(1):
            parts.append(command[start:match.start()])
            start = match.end()
    parts.append(command[start:])
    return parts


def _segments(command: str) -> list[list[str]]:
    out = []
    for part in _split_commands(command):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split()
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies, which are file contents, not commands. The rest of the opening line still runs."""
    return re.sub(r"<<-?\s*['\"]?(\w+)['\"]?([^\n]*)\n.*?\n\s*\1\s*(?:\n|$)", r"\2\n", command, flags=re.S)


def _unwrap(words: list[str], fanned: bool = False) -> tuple[str | None, list[str], bool]:
    """The tool a command really runs, its arguments, and whether it gets file names it does not show."""
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word in _KEYWORDS or _ASSIGNMENT.match(word):
            continue
        name = Path(word.lstrip("\\")).name
        if name not in _WRAPPERS:
            return name, words[index:], fanned
        fanned = fanned or name in _FANOUT
        value_flags, operands = _WRAPPERS[name]
        while index < len(words):
            word = words[index]
            if word == "--":
                index += 1
                break
            if word.startswith("--"):
                index += 1
                # A long flag may take the next word. Only a tool we read is sure to be the command instead.
                if ("=" not in word and index < len(words) and not words[index].startswith("-")
                        and Path(words[index]).name not in _FILE_TOOLS):
                    index += 1
            elif word.startswith("-") and len(word) > 1:
                index += 1
                for position, flag in enumerate(word[1:], 2):
                    if flag in value_flags:
                        index += position == len(word)
                        break
            elif operands:
                operands -= 1
                index += 1
            else:
                break
    return None, [], fanned


def is_check(command: str) -> bool:
    for _, words, _, _ in _parse(_strip_heredocs(command)):
        name, args, _ = _unwrap(words)
        if name in {"bash", "sh"} and len(args) > 1 and args[0] == "-c":
            if is_check(args[1]):
                return True
            continue
        # Perl can run a program, but with -i it only rewrites files.
        if name == "perl" and _inplace_files(name, args) is not None:
            continue
        if name and name not in _NOT_CHECKS and not name.startswith(("[[", "((")):
            return True
    return False


def _identity(command: str) -> tuple:
    """The same check retried with other flags replaces its earlier result."""
    parts = []
    for words in _segments(_strip_heredocs(command)):
        name, args, _ = _unwrap(words)
        if name and name not in _NOT_CHECKS:
            parts.append(name)
            parts.extend(sorted(w for w in args if not w.startswith("-") and re.search(r"\.\w+$", w)))
    return tuple(parts) or (command,)


class _Step(NamedTuple):
    """One command in the order it runs, with the separator before it."""
    separator: str
    words: list[str]
    targets: list[str]
    scripts: list[str]


def _parse(command: str) -> list[_Step]:
    """Commands as lists of words, each with its redirect targets and "$(...)" scripts."""
    steps = [_Step("", [], [], [])]
    into, word = steps[-1].words, None
    for match in _SHELL_TOKEN.finditer(command):
        kind = match.lastgroup
        if kind in {"compare", "single", "double", "escape", "plain"}:
            text = match.group(kind)
            if kind == "escape" and text == "\n":
                continue
            if kind in {"compare", "double"}:
                steps[-1].scripts.extend(_SUBSTITUTION.findall(text))
            if kind == "double":
                text = re.sub(r'\\(["\\$`])', r"\1", text)
            word = (word or "") + text
            continue
        if word is not None:
            into.append(word)
            into, word = steps[-1].words, None
        if kind == "write":
            into = steps[-1].targets
        elif kind == "read":
            into = []
        elif kind == "separator":
            steps.append(_Step(match.group(kind), [], [], []))
            into = steps[-1].words
    if word is not None:
        into.append(word)
    return steps


def _inplace_files(tool: str, args: list[str]) -> list[str] | None:
    """Files an in-place sed or perl rewrites, None if it only reads. Without -e, the first plain word is the script."""
    script_flags, value_flags = _INPLACE[tool]
    inplace = scripted = False
    files = []
    words = iter(args)
    for word in words:
        if word.startswith("--"):
            inplace = inplace or word.startswith("--in-place")
            if word.startswith(("--expression", "--file")):
                scripted = True
                if "=" not in word:
                    next(words, None)
        elif word.startswith("-") and len(word) > 1:
            for index, flag in enumerate(word[1:], 2):
                if flag == "i" or flag in value_flags:
                    inplace = inplace or flag == "i"
                    break
                if flag in script_flags:
                    scripted = True
                    if index == len(word):
                        next(words, None)
                    break
        elif word:
            files.append(word)
    if not inplace:
        return None
    # find -exec passes its own placeholders, which name no file.
    return [f for f in (files if scripted else files[1:]) if f not in {"{}", "+", ";"}]


def _copy_paths(args: list[str], is_dir: Callable[[str], bool], value_flags: str = "tS") -> tuple[list[str], list[str]]:
    """The paths cp, mv, ln and install write, and the sources they take them from."""
    directory, no_directory, operands = None, False, []
    words = iter(args)
    for word in words:
        if word == "--":
            operands.extend(words)
        elif word.startswith("--target-directory"):
            directory = word.partition("=")[2] or next(words, None)
        elif word == "--no-target-directory":
            no_directory = True
        elif word in {"--suffix", "--mode", "--owner", "--group"}:
            next(words, None)
        elif word.startswith("--"):
            continue
        elif word.startswith("-") and len(word) > 1:
            for position, flag in enumerate(word[1:], 2):
                if flag in value_flags:
                    value = word[position:] or next(words, None)
                    directory = value if flag == "t" else directory
                    break
                no_directory = no_directory or flag == "T"
        else:
            operands.append(word)
    if directory is None:
        if len(operands) < 2:
            return [], []
        directory, operands = operands[-1], operands[:-1]
        into = len(operands) > 1 or directory.endswith("/") or directory in {".", ".."} or is_dir(directory)
        if no_directory or not into:
            return [directory], operands
    return [str(PurePosixPath(directory, PurePosixPath(source).name)) for source in operands], operands


def _located(effects: Iterable[tuple[str, str]], cwd: str | None) -> Iterator[tuple[str, str]]:
    """Effects read from a directory, seen from the project root. None is a directory the command does not show."""
    for action, path in effects:
        if not path or cwd == "" or Path(path).is_absolute():
            yield action, path
        elif cwd is None:
            yield "unknown", ""
        else:
            yield action, posixpath.normpath(posixpath.join(cwd, path))


def _next_dir(cwd: str | None, words: list[str]) -> str | None:
    name, args, _ = _unwrap(words)
    if name == "popd":
        return None
    if name not in {"cd", "pushd"}:
        return cwd
    target = next((a for a in args if a == "-" or not a.startswith("-")), "-")
    if target == "-" or target.startswith("~") or _EXPANSION.search(target) or (cwd is None and not Path(target).is_absolute()):
        return None
    return posixpath.join(cwd or "", target)


def _git_effects(args: list[str]) -> Iterator[tuple[str, str]]:
    """Only what changes the working tree. Files that come back from git are the project's, so they are not "write"."""
    while args and args[0].startswith("-"):
        if args[0] == "-C" and len(args) > 1:
            yield from _located(_git_effects(args[2:]), args[1])
            return
        args = args[2 if args[0] in {"-c", "--git-dir", "--work-tree", "--namespace"} else 1:]
    if not args:
        return
    command, flags = args[0], [a for a in args[1:] if a.startswith("-") and a not in {"-", "--"}]
    operands = [a for a in args[1:] if a == "-" or not a.startswith("-")]
    bundled = "".join(f[1:] for f in flags if not f.startswith("--"))
    if command == "rm" and not ({"--cached", "--dry-run"} & set(flags) or "n" in bundled):
        # With no operands the names come from xargs or find.
        for path in operands or [""]:
            yield "remove", path
    elif command == "restore" and ("--worktree" in flags or "W" in bundled or not ("--staged" in flags or "S" in bundled)):
        yield "restore", ""
    elif command in {"checkout", "switch"}:
        # A new branch at HEAD changes no file, but one from a start point or any other branch can change them all.
        names = any(f in {"--orphan", "--create", "--force-create"} or re.fullmatch(r"-\w*[bBcC]", f) for f in flags)
        if len(operands) > names or "--" in args or "-f" in flags or "--force" in flags:
            yield "restore", ""


def _command_effects(words: list[str], is_dir: Callable[[str], bool], fanned: bool) -> Iterator[tuple[str, str]]:
    name, args, fanned = _unwrap(words, fanned)
    written, removed, touched = [], [], []
    if name == "find":
        rest = iter(args)
        for word in rest:
            if word == "-delete":
                yield "unknown", ""
            elif word in _FIND_EXEC:
                yield from _command_effects(list(takewhile(lambda w: w not in {";", "+"}, rest)), is_dir, True)
        return
    if name in {"bash", "sh"}:
        flags = list(takewhile(lambda a: a.startswith("-"), args))
        if flags and len(args) > len(flags) and re.fullmatch(r"-\w*c", flags[-1]):
            yield from _effects(args[len(flags)], is_dir, fanned)
        return
    if name == "tee":
        written = [a for a in args if not a.startswith("-")]
    elif name in _INPLACE:
        written = _inplace_files(name, args)
        if written is None:
            return
    elif name in {"cp", "mv"}:
        written, sources = _copy_paths(args, is_dir)
        removed = sources if name == "mv" else []
    elif name == "rm":
        removed = [a for a in args if not a.startswith("-")]
    elif name == "ln":
        # With one operand, ln makes the link right here.
        written = _copy_paths(args, is_dir)[0] or _copy_paths([*args, "."], is_dir)[0]
    elif name == "install":
        if not any(a == "--directory" or re.match(r"-[bcCpsvDTZ]*d", a) for a in args):
            written = _copy_paths(args, is_dir, "tSmog")[0]
    elif name == "dd":
        written = [a[3:] for a in args if a.startswith("of=")]
    elif name == "touch":
        if not any(a == "--no-create" or re.fullmatch(r"-[am]*c[am]*", a) for a in args):
            rest = iter(args)
            for word in rest:
                if word in {"-d", "-t", "-r", "--date", "--reference", "--time"}:
                    next(rest, None)
                elif not word.startswith("-"):
                    touched.append(word)
    elif name == "git":
        for action, path in _git_effects(args):
            yield ("unknown", "") if fanned and action == "remove" else (action, path)
        return
    else:
        return
    if fanned:
        yield "unknown", ""
    for path in written:
        yield "write", path
    for path in removed:
        yield "remove", path
    for path in touched:
        yield "create", path


def _effects(command: str, is_dir: Callable[[str], bool], fanned: bool = False) -> Iterator[tuple[str, str]]:
    cwd, outer, in_backticks = "", [], False

    def here(path: str) -> bool:
        return cwd is not None and is_dir(posixpath.join(cwd, path))

    for step in _parse(_strip_heredocs(command)):
        # A subshell's cd ends with it.
        if step.separator == "(" or (step.separator == "`" and not in_backticks):
            outer.append(cwd)
        elif step.separator in {")", "`"} and outer:
            cwd = outer.pop()
        in_backticks ^= step.separator == "`"
        yield from _located((("write", target) for target in step.targets), cwd)
        # Quotes also hide commands that still run: bash -c scripts and "$(...)".
        for script in step.scripts:
            yield from _located(_effects(script, here, fanned), cwd)
        yield from _located(_command_effects(step.words, here, fanned), cwd)
        cwd = _next_dir(cwd, step.words)


def file_effects(command: str, is_dir: Callable[[str], bool] = lambda _: False) -> Iterator[tuple[str, str]]:
    """List file effects: write, remove, create, restore or unknown.

    Create counts only for new files. Unknown includes the pattern when available.
    """
    for action, path in _effects(command, is_dir):
        if _EXPANSION.search(path):
            yield "unknown", path
        elif action in {"unknown", "restore"} or (path and path != "/dev/null"):
            yield action, path


def written_paths(command: str, is_dir: Callable[[str], bool] = lambda _: False) -> set[str]:
    return {path for action, path in file_effects(command, is_dir) if path and action != "unknown"}


def _check_words(command: str, cwd: str | None = "") -> list[str]:
    """Words of the commands that are checks. A commit or a cat says nothing about what ran."""
    out = []
    outer = []
    for separator, words, _, scripts in _parse(_strip_heredocs(command)):
        if separator == "(":
            outer.append(cwd)
        elif separator == ")" and outer:
            cwd = outer.pop()
        out.extend(word for script in scripts for word in _check_words(script, cwd))
        name = _unwrap(words)[0]
        next_cwd = _next_dir(cwd, words)
        if not name or name in _NOT_CHECKS:
            cwd = next_cwd
            continue
        for index, word in enumerate(words):
            if index and _INLINE_CODE.fullmatch(words[index - 1]):
                out.extend(_check_words(word, cwd))
            else:
                if cwd and index and not word.startswith("-") and re.search(r"[./]", word):
                    out.append(posixpath.normpath(posixpath.join(cwd, word)))
                else:
                    out.append(word)
        cwd = next_cwd
    return out


class Ledger:
    def __init__(self, root: Path | None, *, observe_shell: bool = True):
        self.root = Path(root).resolve() if root else None
        self.observe_shell = observe_shell and self.root is not None
        self._before = None
        self._snapshot = None
        self.snapshot_complete = True
        self.edits = 0
        self.created: set[str] = set()
        self.edited: set[str] = set()
        # Created and edited hold bare names, which cannot tell src/a.js from a.js.
        self.made: set[Path] = set()
        self.tests_unsure = False
        self.checks: list[dict] = []

    def _resolved(self, target: str) -> Path:
        # realpath, because Path.resolve raises on a symlink loop.
        return Path(os.path.realpath((self.root or Path()) / target))

    def _existed(self, target: str, test: Callable[[Path], bool] = Path.exists) -> bool:
        if not self.root:
            return False
        try:
            path = self._resolved(target)
            return path.is_relative_to(self.root) and test(path)
        except OSError:
            return False

    def _may_reach_tests(self, pattern: str) -> bool:
        """A plain glob can be read off the disk before it runs. Anything else could be any file."""
        if not self.root or not pattern or re.search(r"[$`{]", pattern) or _TEST_FILE.search(pattern):
            return True
        return any(_TEST_FILE.search(found + "/") for found in glob.glob(pattern, root_dir=self.root))

    def _tests_touched(self) -> bool:
        return self.tests_unsure or any(_TEST_FILE.search(name) and Path(name).suffix != ".log"
                                       for name in self.created | self.edited)

    def before_shell(self, command: str) -> None:
        """Called before a command runs, while new files can still be told from old ones."""
        if self.observe_shell:
            self._before = FileSnapshot(self.root, self._snapshot)
            return
        for action, target in file_effects(command, lambda path: self._existed(path, Path.is_dir)):
            # The files are not known, but anything checked earlier may have changed.
            if action in {"unknown", "restore"}:
                self.edits += 1
                self.tests_unsure = self.tests_unsure or (action == "unknown" and self._may_reach_tests(target))
                continue
            name, resolved = Path(target).name, self._resolved(target)
            existed, scratch = self._existed(target), resolved in self.made
            if action == "remove":
                # Clearing away a file this run made does not change what was checked.
                if existed and not scratch:
                    self.edits += 1
                    self.edited.add(name)
            elif not (action == "create" and existed):
                if not self.root or resolved.is_relative_to(self.root):
                    self.edits += 1
                if existed and not scratch:
                    self.edited.add(name)
                else:
                    self.created.add(name)
                    self.made.add(resolved)

    def after_write(self, name: str, path: str, result: dict) -> None:
        if not result.get("ok", True) or result.get("error") or result.get("changed") is False:
            return
        self.edits += 1
        if name == "write_file" and "replaced" not in result:
            self.created.add(path if self.observe_shell else Path(path).name)
            self.made.add(self._resolved(path))
        else:
            self.edited.add(path if self.observe_shell else Path(path).name)

    def _classify(self, command: str) -> str:
        """test: an existing test, untouched. own: written or edited this run. run: only built or ran."""
        tokens = []
        for word in _check_words(command):
            # Text with spaces is prose, unless a file has exactly that name.
            if not re.search(r"\s", word):
                tokens.extend(t for t in word.split("=") if t)
            elif self._existed(word):
                tokens.append(word)
        def named(name):
            if self.observe_shell:
                target = self._resolved(name)
                return any(self._resolved(t) == target or t.startswith(("./", "/"))
                           and self._resolved(t) == target.with_suffix("") for t in tokens)
            return any(Path(t).name == name or Path(t).name == Path(name).stem and t.startswith(("./", "/")) for t in tokens)
        if any(named(name) and (not self.observe_shell or _TEST_FILE.search(name)) for name in self.created):
            return "own"
        tests = [t for t in tokens if _TEST_FILE.search(t)]
        if any(named(name) for name in self.edited if _TEST_FILE.search(name)):
            return "own"
        existing = any(self._existed(t) for t in tests)
        if existing and (self.tests_unsure or self.observe_shell and self._tests_touched()):
            return "own"
        if _TEST_RUNNER.search(" ".join(tokens)):
            return "own" if self._tests_touched() else "test"
        return "test" if existing else "run"

    def after_shell(self, command: str, exit_code, *, record_check: bool = True) -> None:
        complete = True
        if self.observe_shell and self._before is not None:
            after = FileSnapshot(self.root, self._before)
            before, self._before, self._snapshot = self._before, None, after
            complete = not (before.partial or after.partial)
            self.snapshot_complete = complete
            changed = False
            for path in before.files.keys() | after.files.keys():
                if before.files.get(path) == after.files.get(path):
                    continue
                resolved = self.root / path
                if path not in after.files and resolved in self.made:
                    continue
                changed = True
                if path not in before.files:
                    self.created.add(path)
                    self.made.add(resolved)
                else:
                    self.edited.add(path)
            if changed or not complete:
                self.edits += 1
            if not complete:
                self.tests_unsure = True
        if complete and record_check and exit_code is not None and is_check(command):
            self.checks.append({"command": command, "exit_code": exit_code, "after_edit": self.edits,
                                "kind": self._classify(command), "key": _identity(command)})

    def recent(self) -> list[dict]:
        """The latest result of each distinct check since the last edit."""
        latest: dict[tuple, dict] = {}
        for check in self.checks:
            if check["after_edit"] == self.edits:
                latest[check["key"]] = check
        return list(latest.values())

    def status(self, verification: dict | None = None) -> tuple[str, list[dict]]:
        checks = self.recent()
        for entry in (verification or {}).get("commands") or []:
            if ("result" in entry and entry.get("observed", True)
                    and entry.get("after_edit", self.edits) == self.edits):
                kind = "run"
                if entry.get("kind") == "test":
                    kind = "own" if self._tests_touched() else "test"
                checks.append({"command": entry["command"], "exit_code": entry["result"].get("exit_code"), "kind": kind, "auto": True})
        if any(c["exit_code"] != 0 for c in checks):
            return "failing", checks
        for kind, state in (("test", "existing"), ("own", "own"), ("run", "ran")):
            if any(c["kind"] == kind for c in checks):
                return state, checks
        return ("unchanged" if not self.edits else "none"), checks

    def claim_problem(self, content: str, verification: dict | None = None) -> str | None:
        """A debrief claiming verification that the record does not support."""
        if not CLAIM.search(content or ""):
            return None
        state, checks = self.status(verification)
        if state == "none":
            return ("Your debrief claims the work is verified, but nothing has run since your last edit. "
                    "Run a real check now, or rewrite the debrief to say plainly what is untested.")
        if state == "failing":
            failed = [c["command"][:120] for c in checks if c["exit_code"] != 0]
            return ("Your debrief claims the work is verified, but this check still fails: "
                    + "; ".join(failed) + ". Fix it, or say plainly in the debrief that it fails.")
        return None


_HEADLINES = {
    "existing": "Checked with tests that already existed in the project.",
    "own": ("Only checked with tests Olith wrote during this run. They show the code does what Olith "
            "intended, not that it matches what you asked for."),
    "ran": "Built or ran it, but no existing test checked the behavior.",
    "none": "Nothing ran after the last change, so none of this is verified.",
    "failing": "A check still fails after the last change.",
    "unchanged": "No files were changed.",
}


def changed_symbols(review: dict) -> dict[str, list[str]]:
    """Definitions touched by each changed file, from the run's before/after diff."""
    out: dict[str, list[str]] = {}
    for change in (review or {}).get("files") or []:
        if change.get("status") != "modified":
            out[change["file"]] = []
            continue
        names: list[str] = []
        after = change.get("after_text")
        before = change.get("before_text")
        if after is None or before is None:
            out[change["file"]] = []
            continue
        entries = symbols.outline(after, change["file"]) or []
        # Ignore whitespace: format-on-save reindenting is not a change worth reporting.
        old = [" ".join(line.split()) for line in before.splitlines()]
        new = [" ".join(line.split()) for line in after.splitlines()]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            lo, hi = j1 + 1, max(j1 + 1, j2)
            hits = [e for e in entries if e["line"] <= hi and e.get("end_line", e["line"]) >= lo]
            # The innermost definition says the most: Render.drawText over Render.
            hits.sort(key=lambda e: e.get("end_line", e["line"]) - e["line"])
            name = hits[0]["name"] if hits else "top-level code"
            if name not in names:
                names.append(name)
        out[change["file"]] = names
    return out


def unmentioned(content: str, changed: dict[str, list[str]], limit: int = 10) -> list[str]:
    names = [n for items in changed.values() for n in items if n != "top-level code"]
    if not names or len(names) > limit:
        return []
    text = content or ""
    return [n for n in names if not re.search(r"(?<![\w])" + re.escape(n.split(".")[-1]) + r"(?![\w])", text)]


def block(state: str, checks: list[dict], changed: dict[str, list[str]] | None, diagnostics: int = 0) -> str:
    lines = ["**Olith's record of this run**", _HEADLINES[state]]
    for check in checks[-6:]:
        mark = "✓" if check["exit_code"] == 0 else "✗"
        command = " ".join(check["command"].split())
        if len(command) > 90:
            command = command[:87] + "..."
        source = {"test": "existing test", "own": "written this run", "run": "build or run"}[check["kind"]]
        if check.get("auto"):
            source = "automatic, " + source
        lines.append(f"- {mark} `{command}` ({source})")
    if diagnostics:
        lines.append(f"- {diagnostics} language-server error(s) remain")
    if changed:
        parts = [f"{file} ({', '.join(names)})" if names else file for file, names in changed.items()]
        lines.append("Changed: " + "; ".join(parts))
    return "\n".join(lines)
