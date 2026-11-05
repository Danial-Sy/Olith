# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desktop GitHub controls and the authenticated VS Code bridge."""
from pathlib import Path
import json
import shutil
import subprocess
import time
import httpx

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pydantic import ValidationError
from fastapi.exceptions import RequestValidationError

import agent
import editor_bridge as bridge
import github_tools as git
import storage as st

router = APIRouter()
_project_links: dict[str, tuple] = {}


def _project_link(root: str) -> dict:
    cached = _project_links.get(root)
    if cached and time.monotonic() - cached[0] < 15:
        return cached[1]
    path = Path(root).resolve()
    result = {"git": False, "repo": None, "branch": None}
    try:
        top = git._run(path, ["rev-parse", "--show-toplevel"], check=False, timeout=5)
        if top["ok"] and Path(top["output"]).resolve() == path:
            result["git"] = True
            result["branch"] = git._run(path, ["branch", "--show-current"], check=False, timeout=5)["output"] or "Detached HEAD"
            try:
                result["repo"] = git.repository(path)
            except ValueError:
                pass
    except (ValueError, OSError, subprocess.TimeoutExpired):
        result["unavailable"] = True
    _project_links[root] = (time.monotonic(), result)
    return result


@router.get("/api/project-links")
async def project_links():
    return {project["id"]: await _work(_project_link, project["root"]) for project in st.list_projects()}


@router.get("/api/ide/status")
async def editor_status():
    metadata = json.loads((Path(__file__).parent / "extensions/olith-vscode/package.json").read_text())
    return {"sessions": bridge.status(), "extension_version": metadata["version"], "can_install": bool(shutil.which("code"))}


def _install_editor_extension():
    binary = shutil.which("code")
    if not binary:
        raise ValueError("VS Code's installer command is unavailable. Download the extension and use Extensions: Install from VSIX instead.")
    from scripts.package_vscode import package
    artifact = package()
    try:
        result = subprocess.run([binary, "--install-extension", str(artifact), "--force"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired as error:
        raise ValueError("VS Code installation timed out. Check the installed extension version before retrying.") from error
    if result.returncode:
        raise ValueError("VS Code could not install the extension. Use the download and Install from VSIX instead.")
    metadata = json.loads((Path(__file__).parent / "extensions/olith-vscode/package.json").read_text())
    return {"ok": True, "version": metadata["version"], "note": "Installed. In VS Code, run Developer: Reload Window to activate the model picker."}


@router.post("/api/ide/install-extension")
async def editor_install_extension():
    return await _work(_install_editor_extension)


@router.get("/api/github/status")
async def github_status():
    if not await _work(git.token):
        return {"connected": False, "status": "not_connected", "note": "Connect your GitHub account to browse and clone repositories."}
    try:
        return {**await _work(git.account), "status": "connected"}
    except HTTPException as error:
        return {"connected": False, "status": "needs_attention", "note": str(error.detail)}


class Operation(BaseModel):
    action: str = "status"
    options: dict = Field(default_factory=dict)
    confirmed: bool = False


def _options(body: Operation) -> dict:
    allowed = {"remote", "branch", "paths", "message", "staged", "repo", "name", "description", "private", "auto_init", "number", "head", "base", "title", "body", "draft", "state", "sha", "merge_method", "event"}
    if set(body.options) - allowed:
        raise HTTPException(400, "Unsupported repository options")
    return body.options


class Credential(BaseModel):
    token: str = Field(max_length=1000)


class Clone(BaseModel):
    repo: str
    destination: str


class Pair(BaseModel):
    code: str = Field(max_length=100)
    root: str = Field(max_length=4096)


class PairCodeStatus(BaseModel):
    code: str = Field(min_length=8, max_length=8)


class Ack(BaseModel):
    ok: bool
    error: str = Field(default="", max_length=1000)


class EditorModel(BaseModel):
    model_key: str = Field(min_length=1, max_length=500)


def _editor_models():
    import server
    settings, registry = server._registry()
    eligible = server.reg.models_for("agentic", settings, registry)
    return registry, eligible, server.reg.active("agentic", settings, registry)


def _require_editor_model(model_key: str):
    _, eligible, _ = _editor_models()
    if model_key not in eligible:
        raise HTTPException(400, "This model is not available for Agentic. Refresh the model list or assign it to Agentic in Olith's Models panel.")


def _editor(sid: str, request: Request):
    credential = request.headers.get("authorization", "")
    if not credential.startswith("Bearer "):
        raise HTTPException(401, "Editor bearer credential required")
    try:
        return bridge.session(sid, credential[7:])
    except ValueError as error:
        raise HTTPException(401, str(error)) from error


def _chat(sid: str, cid: str, request: Request):
    value = _editor(sid, request)
    chat = st.get_chat(cid)
    project = st.get_project((chat or {}).get("project_id"))
    if not chat or chat.get("editor_session_id") != sid or not project or Path(project["root"]).resolve() != value["root"]:
        raise HTTPException(403, "This chat does not belong to the paired workspace")
    return chat


async def _work(function, *args, mutation=False, **kwargs):
    import server
    if mutation and server._gen_lock.locked():
        raise HTTPException(409, "Stop the current task before changing the repository")
    try:
        if mutation:
            async with server._gen_lock:
                return await server._index_work(function, *args, **kwargs)
        return await server._index_work(function, *args, **kwargs)
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise HTTPException(504, "Git operation timed out. Refresh repository status before retrying; it may have completed.") from error
    except httpx.HTTPError as error:
        raise HTTPException(502, "Could not reach GitHub. Check the connection and retry.") from error


@router.get("/api/github/account")
async def github_account():
    return await _work(git.account)


@router.post("/api/github/connect")
async def github_connect(body: Credential):
    return await _work(git.connect, body.token)


@router.delete("/api/github/connect")
async def github_disconnect():
    import connections
    return connections.forget("github-api")


@router.get("/api/github/repos")
async def github_repos(page: int = Query(default=1, ge=1, le=10000)):
    return await _work(git.execute, None, "repos", page=page)


@router.post("/api/github/repos")
async def github_create_repo(body: Operation):
    if body.action != "create_repo" or not body.confirmed:
        raise HTTPException(400, "Confirm creation of the repository")
    return await _work(git.execute, None, body.action, mutation=True, **_options(body))


@router.post("/api/github/clone")
async def github_clone(body: Clone):
    result = await _work(git.clone, body.repo, body.destination, mutation=True)
    if result["ok"]:
        result["project"] = st.new_project(body.repo.split("/")[-1], result["root"])
    return result


@router.post("/api/projects/{pid}/git")
async def project_git(pid: str, body: Operation):
    project = st.get_project(pid)
    if not project:
        raise HTTPException(404, "Unknown project")
    mutation = body.action not in git.READ_ACTIONS
    if mutation and not body.confirmed:
        raise HTTPException(400, "Confirm the repository operation")
    root = agent.resolve_path(project["root"], Path(project["root"]))
    result = await _work(git.execute, root, body.action, mutation=mutation, **_options(body))
    _project_links.pop(project["root"], None)
    return result


@router.post("/api/ide/pair-code")
async def editor_pair_code():
    return bridge.pair_code()


@router.post("/api/ide/pair-code/status")
async def editor_pair_status(body: PairCodeStatus):
    return bridge.pair_status(body.code)


@router.get("/api/ide/extension")
async def editor_extension():
    path = Path(__file__).parent / "dist" / "olith-vscode.vsix"
    # An app update must not serve an old ignored dist artifact.
    from scripts.package_vscode import package
    package(path)
    metadata = json.loads((Path(__file__).parent / "extensions/olith-vscode/package.json").read_text())
    return FileResponse(path, filename=f"olith-vscode-{metadata['version']}.vsix", media_type="application/octet-stream")


@router.post("/api/ide/connect")
async def editor_connect(body: Pair):
    try:
        value = bridge.connect(body.code, Path(body.root))
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error
    project = next((p for p in st.list_projects() if Path(p["root"]).resolve() == Path(value["root"])), None)
    value["project"] = project or st.new_project(Path(value["root"]).name, value["root"])
    return value


class ForgetEditor(BaseModel):
    root: str = Field(min_length=1, max_length=4096)


@router.delete("/api/ide/pairings")
async def editor_forget_pairing(body: ForgetEditor):
    await _work(bridge.forget_root, Path(body.root), mutation=True)
    return {"ok": True}


@router.delete("/api/ide/{sid}")
async def editor_disconnect(sid: str, request: Request):
    _editor(sid, request)
    bridge.disconnect(sid)
    return {"ok": True}


@router.get("/api/ide/{sid}/edits")
async def editor_edits(sid: str, request: Request):
    _editor(sid, request)
    return {"edits": bridge.pending(sid, request.headers.get("x-olith-extension-version"))}


@router.get("/api/ide/{sid}/models")
async def editor_models(sid: str, request: Request):
    _editor(sid, request)
    registry, eligible, active = _editor_models()
    models = [{"id": key, "label": registry[key].get("label") or key} for key in eligible]
    return {"models": sorted(models, key=lambda model: model["label"].lower()), "active": active}


@router.post("/api/ide/{sid}/edits/{eid}")
async def editor_ack(sid: str, eid: str, body: Ack, request: Request):
    _editor(sid, request)
    try:
        bridge.acknowledge(sid, eid, body.ok, body.error)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True}


@router.post("/api/ide/{sid}/edits/{eid}/claim")
async def editor_claim(sid: str, eid: str, request: Request):
    _editor(sid, request)
    try:
        return bridge.claim(sid, eid)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/ide/{sid}/chats")
async def editor_new_chat(sid: str, request: Request):
    value = _editor(sid, request)
    project = next((p for p in st.list_projects() if Path(p["root"]).resolve() == value["root"]), None)
    if not project:
        raise HTTPException(404, "Project was removed; pair again")
    chat = st.new_chat("agentic", "VS Code", project["id"])
    chat["editor_session_id"] = sid
    st.save_chat(chat)
    return chat


@router.get("/api/ide/{sid}/chats/{cid}")
async def editor_chat(sid: str, cid: str, request: Request):
    return _chat(sid, cid, request)


@router.post("/api/ide/{sid}/chats/{cid}/{action}")
async def editor_chat_action(sid: str, cid: str, action: str, request: Request):
    import server
    chat = _chat(sid, cid, request)
    if action == "model":
        try:
            body = EditorModel.model_validate_json(await request.body())
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        if server._gen_lock.locked():
            raise HTTPException(409, "Stop the current task before changing models")
        async with server._gen_lock:
            _require_editor_model(body.model_key)
            chat = _chat(sid, cid, request)
            chat["model_id"] = body.model_key
            chat["agent_model"] = body.model_key
            st.save_chat(chat)
        return {"model_key": body.model_key}
    if action == "send":
        try:
            body = server.SendMessage.model_validate(await request.json())
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        body.mode = "agentic"
        body.project_id = chat["project_id"]
        for requested in (body.model_key, body.agent_model):
            if requested is not None:
                _require_editor_model(requested)
        if body.model_key or body.agent_model:
            body.model_key = body.model_key or body.agent_model
            body.agent_model = body.model_key
        return await server.api_send(cid, body)
    if action == "stop":
        # Wake an edit worker before the generation's cancellation waits for it.
        bridge.cancel_pending(sid)
        return await server.stop_generation(cid)
    if action == "answer":
        return await server.answer_question(cid, server.AnswerIn.model_validate(await request.json()))
    raise HTTPException(404, "Unsupported editor action")


@router.get("/api/ide/{sid}/chats/{cid}/changes")
async def editor_changes(sid: str, cid: str, request: Request):
    import server
    _chat(sid, cid, request)
    return await server.api_task_changes(cid)
