# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use official service CLIs through the connection tools.

The CLI keeps its own credentials. Commands use argument lists, an explicit
folder, closed stdin and a timeout. They do not run through a shell.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from connection_catalog import CLI_PROVIDERS

OUTPUT_LIMIT = 12000
PROBE_TIMEOUT = 15
COMMAND_TIMEOUT = 120
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CAPABILITIES = {
    "google": "Drive files, Gmail, Calendar, Sheets, Docs and Workspace APIs",
    "microsoft": "Microsoft Graph mail, files, calendars and users",
    "atlassian": "Jira projects, issues, boards and work items",
    "huggingface": "model repositories, datasets, downloads and uploads",
    "kaggle": "datasets, competitions, kernels and models",
    "github": "repositories, pull requests, issues and Actions",
    "gitlab": "repositories, merge requests, issues and pipelines",
    "sentry": "issues, errors, events, releases and logs",
}


def _binary(command):
    found = shutil.which(command)
    if found:
        return found
    # Add common user install folders to a desktop launcher's smaller PATH.
    home = Path.home()
    directories = [Path(sys.executable).parent, home / ".local/bin", home / ".npm-global/bin", home / ".cargo/bin", home / "go/bin"]
    if os.name == "nt":
        for variable in ("APPDATA", "LOCALAPPDATA"):
            if os.environ.get(variable):
                base = Path(os.environ[variable])
                directories.extend([base / "npm", base / "Microsoft/WinGet/Links"])
    else:
        versions = Path(os.environ.get("NVM_DIR") or home / ".nvm") / "versions/node"
        if versions.is_dir():
            nodes = [path for path in versions.iterdir() if re.fullmatch(r"v\d+\.\d+\.\d+", path.name)]
            directories.extend(path / "bin" for path in sorted(nodes, key=lambda p: tuple(map(int, p.name[1:].split("."))), reverse=True))
    suffixes = ("", ".exe", ".cmd") if os.name == "nt" else ("",)
    for directory in directories:
        for suffix in suffixes:
            path = directory / (command + suffix)
            if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
                return str(path)
    return None


def launcher(provider, windows=None):
    spec = CLI_PROVIDERS[provider]
    binary = _binary(spec["command"])
    if not binary:
        raise ValueError(f"{spec['command']} is not installed or is not on Olith's PATH. {spec['install']} Then run: {spec['login']}")
    executable = Path(binary).absolute()
    windows = os.name == "nt" if windows is None else windows
    if windows and executable.suffix.lower() in {".cmd", ".bat"}:
        # Resolve npm's Windows shim to its package and run Node directly.
        # Keep shell metacharacters as literal arguments.
        package = executable.parent / "node_modules" / spec.get("package", "")
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        bins = metadata.get("bin", {})
        script = bins if isinstance(bins, str) else bins.get(spec["command"])
        if not script:
            raise ValueError("The installed CLI's launcher is incomplete. Reinstall it using the provider instructions.")
        target = (package / script).resolve()
        if not target.is_relative_to(package.resolve()) or not target.is_file():
            raise ValueError("The installed CLI has an invalid executable path. Reinstall it.")
        if target.suffix.lower() in {".js", ".cjs", ".mjs"}:
            node = shutil.which("node")
            if not node:
                raise ValueError("Install Node.js to use this CLI.")
            return [node, str(target)]
        return [str(target)]
    return [str(executable)]


def _clean(text):
    text = ANSI.sub("", text)
    # Environment-supplied CLI credentials may appear in provider error output.
    for key, value in os.environ.items():
        if value and len(value) >= 6 and re.search(r"TOKEN|API_KEY|PASSWORD|CLIENT_SECRET", key, re.I):
            text = text.replace(value, "[redacted]")
    return text[:OUTPUT_LIMIT]


async def execute(provider, args, cwd, timeout=COMMAND_TIMEOUT):
    command = launcher(provider)
    env = {**os.environ, "CI": "1", "NO_COLOR": "1", "DO_NOT_TRACK": "1", "VERCEL_TELEMETRY_DISABLED": "1"}
    env["PATH"] = str(Path(command[0]).parent) + os.pathsep + env.get("PATH", "")
    process = await asyncio.create_subprocess_exec(*command, *args, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    return {"exit_code": process.returncode, "stdout": _clean(stdout.decode("utf-8", errors="replace")),
            "stderr": _clean(stderr.decode("utf-8", errors="replace"))}


async def verify(provider):
    spec = CLI_PROVIDERS[provider]
    with tempfile.TemporaryDirectory(prefix="olith-cli-check-") as cwd:
        try:
            result = await execute(provider, spec["probe"], cwd, timeout=PROBE_TIMEOUT)
        except TimeoutError as exc:
            raise ValueError(f"{spec['label']} CLI login check timed out after {PROBE_TIMEOUT} seconds. Check your network, run {spec['login']}, then retry.") from exc
    output = result["stdout"].strip()
    denied = re.search(r"not (?:logged|signed) in|not authenticated|unauthenticated|authentication required|invalid (?:token|credentials)|no (?:existing )?(?:token|credentials)|error:", output, re.I)
    try:
        data = json.loads(output)
    except ValueError:
        data = None
    valid = result["exit_code"] == 0 and bool(output) and not denied
    if provider not in {"cloudflare", "huggingface", "github", "kaggle"}:
        valid = valid and isinstance(data, (dict, list))
    if isinstance(data, dict) and (data.get("error") or data.get("authenticated") is False):
        valid = False
    if isinstance(data, dict):
        valid = valid and any(data.get(key) for key in ("id", "username", "name", "email", "user", "account", "data"))
    if isinstance(data, list) and provider not in {"supabase", "atlassian"}:
        valid = False
    if provider == "google":
        valid = result["exit_code"] == 0 and isinstance(data, dict) and isinstance(data.get("files"), list) and not data.get("error")
    if provider == "atlassian" and isinstance(data, dict) and isinstance(data.get("values"), list):
        valid = result["exit_code"] == 0 and not data.get("error")
    if provider == "cloudflare":
        valid = valid and bool(re.search(r"you are logged in", output, re.I))
    if provider == "kaggle":
        valid = valid and output.startswith("ref,")
    if provider in {"github", "huggingface"} and data is None:
        valid = valid and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", output) or
                               provider == "huggingface" and re.search(r"(?im)^user(?:name)?:\s*[A-Za-z0-9][A-Za-z0-9_.-]*\s*$", output))
    if not valid:
        # Do not return probe output: some CLI versions include masked tokens.
        raise ValueError(f"{spec['label']} CLI is installed, but its login could not be verified. Run {spec['login']} in your terminal, then verify again.")


class Session:
    """Minimal session interface shared with the MCP connection dispatcher."""
    def __init__(self, provider):
        if provider not in CLI_PROVIDERS:
            raise ValueError("Choose a supported CLI provider")
        self.provider = provider

    async def list_tools(self, cursor=None):
        from mcp.types import ListToolsResult, Tool, ToolAnnotations
        label = CLI_PROVIDERS[self.provider]["label"]
        hint = (" Before project operations, run project inspect --non-interactive from the intended directory, verify the resolved owner and project, and stop on link_required or a mismatch. Do not auto-link." if self.provider == "vercel" else " Verify the intended account, project and team before changes.")
        return ListToolsResult(tools=[
            Tool(name=self.provider + "_help", description=f"Read installed {label} CLI command help. Use before choosing command flags.",
                 inputSchema={"type": "object", "properties": {}, "additionalProperties": False}, annotations=ToolAnnotations(readOnlyHint=True)),
            Tool(name=self.provider + "_command", description=f"Use {label} directly through its official CLI: {CAPABILITIES.get(self.provider, 'projects, deployments, services and logs')}. Pass arguments separately and an absolute project directory. Uses the CLI's existing login; never request credentials. Olith handles command approvals according to the user's Approval mode setting." + hint,
                 inputSchema={"type": "object", "properties": {
                     "args": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "minItems": 1, "maxItems": 100},
                     "cwd": {"type": "string", "description": "Absolute path to the intended local project directory."},
                     "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": COMMAND_TIMEOUT,
                                         "description": "Deadline for this command; allow more time for deployments and uploads."}},
                     "required": ["args", "cwd"], "additionalProperties": False}, annotations=ToolAnnotations(readOnlyHint=False)),
        ])

    async def call_tool(self, name, arguments):
        from mcp.types import CallToolResult, TextContent
        help_result = False
        if name == self.provider + "_help":
            with tempfile.TemporaryDirectory(prefix="olith-cli-help-") as cwd:
                result = await execute(self.provider, ["--help"], cwd, timeout=15)
            help_result = result["exit_code"] in {0, 2} and bool(result["stdout"] or result["stderr"])
        elif name == self.provider + "_command":
            args = arguments["args"]
            blocked = {"--token", "--api-key", "--access-token", "--password", "--client-secret", "--show-token", "--global-config", "--cwd", "--workdir", "--debug"}
            verbs = {"auth", "login", "logout", "config", "token", "tokens"}
            if (not args or args[0].startswith("-") or any(arg.lower() in verbs for arg in args) or
                    any("\x00" in arg or arg.split("=", 1)[0].lower() in blocked for arg in args)):
                raise ValueError("Use a service command with the existing login. Authenticate or manage credentials yourself in a terminal; pass the working directory through cwd.")
            cwd = Path(arguments["cwd"]).expanduser()
            if not cwd.is_absolute() or not cwd.is_dir():
                raise ValueError("Choose an existing absolute project directory")
            timeout = arguments.get("timeout_seconds", COMMAND_TIMEOUT)
            try:
                result = await execute(self.provider, args, cwd.resolve(), timeout=timeout)
            except TimeoutError as exc:
                raise ValueError(f"CLI command timed out after {timeout} seconds. A remote operation may still be running; inspect its status before retrying.") from exc
        else:
            raise ValueError("Unknown CLI tool")
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))],
                              structuredContent=result, isError=result["exit_code"] != 0 and not help_result)
