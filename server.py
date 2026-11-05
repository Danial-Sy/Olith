# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Olith: local Claude-style desktop companion."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import itertools
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

import agent
import attachment_router as ar
import evidence
import ollama_client as oc
import search_router as sr
import search_web
import storage as st
import config
import models as reg
import library as lib
from config import COMPACT_PROMPT, ROOT, TITLE_PROMPT, UPLOADS, VERSION
from config import effort_for as cfg_effort_for
import browser
import computer
import computer_overlay
import connections
import memory_router
import repo_index
import project_commands
import task_changes
import language_servers
import github_tools
import editor_bridge
import mcp_connections
from mcp_routes import router as mcp_router

app = FastAPI(title="Olith", version=VERSION)
app.include_router(mcp_router)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
STATIC = ROOT / "static"
UPLOADS.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS)), name="uploads")
_gen_lock = asyncio.Lock()
# A generation that hangs or dies badly used to hold _gen_lock forever, leaving
# every later message with a silent empty stream and a spinner that never stops.
GEN_LOCK_TIMEOUT = 20.0
# chat id -> {"future": Future, "event": dict}. The event is kept so the UI can
# re-read a prompt it never rendered instead of the run stalling until timeout.
_pending_questions: dict[str, dict] = {}


def _park_prompt(key: str, event: dict) -> asyncio.Future:
    """Register an open prompt and return the future its answer resolves."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_questions[key] = {"future": fut, "event": event}
    return fut


def _approval_key(chat: dict) -> str:
    """Use the parent chat ID for prompts raised by a sub-task so the UI can answer them."""
    return str(chat.get("approval_key") or chat.get("id") or "")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Disable static-file caching so desktop restarts load the updated UI."""
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.middleware("http")
async def local_origin_only(request: Request, call_next):
    """Reject browser writes originating outside Olith's local UI."""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://127.0.0.1:7878",
            "http://localhost:7878",
        }:
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    return await call_next(request)


class ChatCreate(BaseModel):
    mode: str = "chat"  # chat | agentic (legacy: code | computer)
    title: str | None = None
    project_id: str | None = None
    # Records the picker's choice at creation, so a new chat cannot reset it.
    agent_model: str | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    reasoning: bool | None = None
    computer_use: bool | None = None
    mode: str | None = None
    project_id: str | None = None
    pinned: bool | None = None
    # Lets the model picker retarget an open chat instead of forcing a new one.
    agent_model: str | None = None


class AttachmentIn(BaseModel):
    name: str
    kind: str = "file"  # file | folder | image | screenshot
    path: str | None = None
    text: str | None = None
    image_b64: str | None = None


class SendMessage(BaseModel):
    content: str
    reasoning: bool | None = None
    model_key: str | None = None
    # The model id that runs the whole job when this is an agentic turn.
    agent_model: str | None = None
    # Which tab sent this. One model can serve both roles, so its id alone
    # cannot say whether the turn is agentic.
    mode: str | None = None
    effort: str | None = None
    computer_use: bool = False
    think: bool | None = None
    project_id: str | None = None
    web_search: bool = False
    attachments: list[AttachmentIn] = Field(default_factory=list)


class MemoryNote(BaseModel):
    text: str
    tags: list[str] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    name: str
    root: str = ""
    create_folder: bool = False
    description: str = ""


class SettingsPatch(BaseModel):
    data: dict


_warm_event = asyncio.Event()
_warm_state: dict = {
    "status": "idle",
    "error": None,
    "model": None,
    "label": None,
    "progress": 0,
    "total": 0,
}
_warm_task: asyncio.Task | None = None
_active_gens: dict[str, asyncio.Task] = {}
_index_tasks: dict[str, asyncio.Task] = {}
_index_states: dict[str, dict] = {}
def _registry() -> tuple[dict, dict]:
    settings = st.load_settings()
    return settings, reg.installed(settings)


def _meta(model_id: str, role: str, settings: dict | None = None) -> dict:
    return reg.resolve(model_id, role, settings if settings is not None else st.load_settings())


async def _warm_one(model_id: str, role: str = "chat", *, keep: bool = True) -> None:
    meta = _meta(model_id, role)
    _warm_state["label"] = meta.get("label") or model_id
    _warm_state["model"] = meta.get("id")
    await oc.force_load_model(model_id, meta=meta, num_ctx=2048,
                              keep_alive=(meta.get("keep_alive") or "30m") if keep else "0")


async def _warm_all_models() -> None:
    """Warm the default chat model into VRAM and verify it is resident before opening the UI."""
    if _warm_event.is_set() and _warm_state.get("status") == "ready":
        # Re-verify: a stale "ready" from a previous process must not skip the load
        mid = reg.active("chat") or ""
        if mid and await oc.is_model_resident(mid):
            return
        _warm_event.clear()
    _warm_state["status"] = "warming"
    _warm_state["error"] = None
    _warm_state["total"] = 1
    _warm_state["progress"] = 1
    try:
        mid = reg.active("chat")
        if not mid:
            raise RuntimeError("No chat model is installed")
        await _warm_one(mid, "chat", keep=True)
        # Poll until Ollama reports the model resident (or timeout)
        for _ in range(40):
            if await oc.is_model_resident(mid):
                break
            await asyncio.sleep(0.25)
        if not await oc.is_model_resident(mid):
            # One more force load attempt
            await _warm_one(mid, "chat", keep=True)
        if await oc.is_model_resident(mid):
            _warm_state["status"] = "ready"
            _warm_state["label"] = _meta(mid, "chat").get("label")
            _warm_state["model"] = mid
        else:
            _warm_state["status"] = "error"
            _warm_state["error"] = f"{mid} did not stay loaded in VRAM"
    except Exception as e:
        _warm_state["status"] = "error"
        _warm_state["error"] = str(e)
    finally:
        _warm_event.set()


def _ensure_warm_task() -> None:
    global _warm_task
    if _warm_event.is_set() and _warm_state.get("status") == "ready":
        return
    if _warm_task is None or _warm_task.done():
        if _warm_event.is_set() and _warm_state.get("status") != "ready":
            _warm_event.clear()
        _warm_task = asyncio.create_task(_warm_all_models())


@app.on_event("startup")
async def startup() -> None:
    oc.ensure_ollama_sync()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    st.load_settings()
    if not st.list_projects():
        try:
            from config import AI_ROOT
            st.new_project("AI Workspace", str(AI_ROOT), "Forge / models / tools")
        except Exception:
            pass
    try:
        await reg.refresh_registry()
    except Exception:
        pass
    _ensure_warm_task()


async def _stop_warm_and_unload() -> list[str]:
    global _warm_task
    task = _warm_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return await oc.unload_all_models()


@app.on_event("shutdown")
async def shutdown() -> None:
    await asyncio.to_thread(language_servers.close_all)
    try:
        computer_overlay.set_overlay(False)
    except Exception:
        pass
    try:
        await _stop_warm_and_unload()
    except Exception:
        pass


@app.post("/api/shutdown")
async def api_shutdown():
    """Explicit VRAM unload, called by the desktop shell before the process dies."""
    try:
        computer_overlay.set_overlay(False)
    except Exception:
        pass
    freed = await _stop_warm_and_unload()
    return {"ok": True, "unloaded": freed}


@app.get("/api/warmup")
async def warmup():
    """Block until startup warm finishes. Splash screen awaits this."""
    _ensure_warm_task()
    try:
        await asyncio.wait_for(_warm_event.wait(), timeout=900.0)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "status": "timeout",
            "error": "Warmup timed out",
            "model": _warm_state.get("model"),
            "label": _warm_state.get("label"),
            "progress": _warm_state.get("progress"),
            "total": _warm_state.get("total"),
        }
    return {
        "ok": _warm_state.get("status") == "ready",
        "status": _warm_state.get("status"),
        "error": _warm_state.get("error"),
        "model": _warm_state.get("model"),
        "label": _warm_state.get("label"),
        "progress": _warm_state.get("progress"),
        "total": _warm_state.get("total"),
    }


@app.get("/api/warmup/status")
async def warmup_status():
    # What Ollama actually holds. The UI cannot infer this after a reload.
    resident: list[str] = []
    try:
        running = await oc.list_running_models()
        names = {(m.get("name") or m.get("model") or "") for m in running}
        for mid in reg.installed():
            if any(n == mid or n.startswith(mid) for n in names):
                resident.append(mid)
    except Exception:
        resident = []
    return {
        "resident": resident,
        "ok": _warm_state.get("status") == "ready",
        "status": _warm_state.get("status"),
        "error": _warm_state.get("error"),
        "model": _warm_state.get("model"),
        "label": _warm_state.get("label"),
        "progress": _warm_state.get("progress"),
        "total": _warm_state.get("total"),
        "ready": _warm_event.is_set() and _warm_state.get("status") == "ready",
    }


@app.post("/api/warmup/model/{key:path}")
async def warmup_model(key: str):
    """Load one model and leave it ready. Accept IDs containing slashes, including Hugging Face tags."""
    settings, registry = _registry()
    model_id = key
    role = "agentic" if key in reg.models_for("agentic", settings, registry) else "chat"
    if not model_id or model_id not in registry:
        raise HTTPException(404, "Unknown model")
    try:
        await _warm_one(model_id, role, keep=True)
        meta = _meta(model_id, role, settings)
        return {"ok": True, "key": key, "model": model_id, "label": meta.get("label")}
    except Exception as e:
        raise HTTPException(500, str(e))


class AnswerIn(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    cancelled: bool = False
    # Shell approvals only: "command" or "binary" persists the approval so the
    # same prompt is not shown again.
    remember: str | None = None


@app.post("/api/chats/{cid}/answer")
async def answer_question(cid: str, body: AnswerIn):
    """Resolve an open ask_user prompt so the parked agent loop can continue."""
    entry = _pending_questions.get(cid)
    fut = (entry or {}).get("future")
    if not fut or fut.done():
        return {"ok": False, "reason": "no open question"}
    fut.set_result({
        "answers": body.answers,
        "cancelled": body.cancelled,
        "remember": body.remember,
    })
    return {"ok": True}


@app.get("/api/chats/{cid}/pending")
async def pending_question(cid: str):
    """Return the chat's open question or approval so polling can recover a missed stream event."""
    entry = _pending_questions.get(cid)
    fut = (entry or {}).get("future")
    if not entry or not fut or fut.done():
        return {"pending": None}
    return {"pending": entry.get("event")}


@app.get("/api/canon")
async def api_get_canon(project_id: str | None = None):
    return {
        "global": st.load_canon(None).get("entries") or [],
        "project": (st.load_canon(project_id).get("entries") or []) if project_id else [],
    }


@app.delete("/api/canon/{entry_id}")
async def api_delete_canon(entry_id: str, project_id: str | None = None):
    return st.delete_canon_entry(project_id, entry_id)


@app.post("/api/chats/{cid}/stop")
async def stop_generation(cid: str):
    task = _active_gens.get(cid)
    if task and not task.done():
        task.cancel()
        return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    try:
        installed = await oc.list_models()
        names = {m["name"] for m in installed}
    except Exception as e:
        return {"ok": False, "error": str(e), "models": []}
    settings, registry = _registry()
    models = []
    for model_id, profile in registry.items():
        models.append({
            **profile,
            "key": model_id,
            "installed": any(model_id == n or n.startswith(model_id) for n in names),
        })
    chat_id = reg.active("chat", settings, registry)
    return {
        "ok": True,
        "models": models,
        "settings": settings,
        "effort_levels": reg.effort_levels(registry.get(chat_id) or {}),
    }


class ModelRolesIn(BaseModel):
    model: str
    roles: list[str] = Field(default_factory=list)


class ModelTagIn(BaseModel):
    model: str


def _installed_view(settings: dict, registry: dict, budget: int) -> list[dict]:
    assigned = reg.role_map(settings, registry)
    active = {role: reg.active(role, settings, registry) for role in config.ROLES}
    utility = reg.utility_models()
    out = []
    for model_id, profile in registry.items():
        out.append({
            **profile,
            "roles": assigned.get(model_id) or [],
            "can": {role: reg.can_serve(profile, role) for role in config.ROLES},
            # Trained to point at things on a screen, not just describe them.
            # The model menu says so, since it decides whether clicks land.
            "grounding": config.is_grounding_model(model_id),
            "active": {role: active.get(role) == model_id for role in config.ROLES},
            "utility": model_id in utility,
            "fit": reg.fit_report(profile, budget),
            "effort_levels": reg.effort_levels(profile),
            "thinking": reg.thinking_summary(profile),
        })
    out.sort(key=lambda m: m["label"].lower())
    return out


@app.get("/api/models")
async def api_models():
    """What is installed and what this machine can hold. Finding a model to add
    is the library screen's job, so nothing here describes one that is not."""
    settings, registry = _registry()
    hw = reg.hardware()
    budget = int(hw.get("usable_bytes") or 0)
    can_drive, why = computer.available()
    return {
        "hardware": hw,
        # Whether this desktop can be driven at all. It rides here because the
        # model menu is where computer use is turned on.
        "desktop": {"ok": can_drive, "reason": why},
        "installed": _installed_view(settings, registry, budget),
        "roles": {
            **{role: reg.models_for(role, settings, registry) for role in config.ROLES},
            # Not a role of its own: the set any chat may run computer use on.
            "vision": reg.vision_models(settings, registry),
        },
        "active": {
            **{role: reg.active(role, settings, registry) for role in config.ROLES},
            "vision": reg.vision_model(settings, registry),
        },
    }


@app.post("/api/models/roles")
async def api_model_roles(body: ModelRolesIn):
    """Turn the Chat and Agentic toggles on or off for one model."""
    settings, registry = _registry()
    if body.model not in registry:
        raise HTTPException(404, "That model is not installed.")
    wanted = [r for r in ("chat", "agentic") if r in body.roles]
    profile = registry[body.model]
    for role in wanted:
        if not reg.can_serve(profile, role):
            raise HTTPException(
                400, f"{profile.get('label') or body.model} cannot serve {role}: "
                     "Ollama reports it has no tool support.")
    stored = dict(settings.get("model_roles") or {})
    current = reg.role_map(settings, registry)
    # Olith with no chat model has nothing to talk to.
    if "chat" not in wanted:
        others = [m for m, roles in current.items() if "chat" in roles and m != body.model]
        if not others:
            raise HTTPException(409, "This is the only model assigned to Chat.")
    stored[body.model] = wanted
    saved = st.save_settings({"model_roles": stored})
    registry = reg.installed(saved)
    return {"ok": True, "model": body.model, "roles": wanted,
            "roles_by_model": reg.role_map(saved, registry)}


@app.post("/api/models/active")
async def api_model_active(body: ModelRolesIn):
    """Pick which assigned model a role runs on."""
    settings, registry = _registry()
    role = next((r for r in body.roles if r in config.ROLES), "chat")
    if body.model not in reg.models_for(role, settings, registry):
        raise HTTPException(400, f"That model is not assigned to {role}.")
    active = dict(settings.get("model_active") or {})
    active[role] = body.model
    st.save_settings({"model_active": active})
    return {"ok": True, "role": role, "model": body.model}


@app.get("/api/library")
async def api_library(source: str = "ollama", q: str = "", sort: str = "popular",
                      cursor: str = "", band: str = "", fits: int = 0):
    """Return a library page as items and a next cursor. Hide source-specific paging from the UI."""
    if source not in lib.SOURCES:
        raise HTTPException(400, f"Unknown library {source!r}.")
    settings, registry = _registry()
    hw = reg.hardware()
    try:
        page = await lib.browse(
            source, q, sort, cursor or None,
            band=band, fits_only=bool(fits),
            registry=registry, budget=int(hw.get("usable_bytes") or 0),
        )
    except Exception as e:
        raise HTTPException(502, f"{source} did not answer: {e}")
    return {**page, "sorts": list(lib.SORTS), "bands": list(lib.PARAM_BANDS)}


@app.get("/api/library/variants")
async def api_library_variants(source: str = "ollama", id: str = ""):
    """Every installable version of one entry, with real sizes and what fits."""
    if source not in lib.SOURCES:
        raise HTTPException(400, f"Unknown library {source!r}.")
    if not id.strip():
        raise HTTPException(400, "Which model?")
    hw = reg.hardware()
    _, registry = _registry()
    try:
        found = await lib.variants(source, id.strip(), budget=int(hw.get("usable_bytes") or 0))
    except Exception as e:
        raise HTTPException(502, f"{source} did not answer: {e}")
    for row in found["variants"]:
        row["installed"] = row["tag"] in registry
    return found


@app.post("/api/models/verify")
async def api_model_verify(body: ModelTagIn):
    """Does this tag exist, and how big is it, before committing to a download."""
    tag = reg.normalize_tag(body.model)
    settings, registry = _registry()
    if tag in registry or body.model in registry:
        profile = registry.get(tag) or registry[body.model]
        return {"ok": True, "installed": True, "model": profile["id"],
                "bytes": profile.get("weights_bytes") or 0}
    size = await reg.fetch_manifest_size(tag)
    if not size:
        if lib.is_hf_tag(tag):
            raise HTTPException(404, f"Hugging Face has no GGUF called {body.model}. "
                                     "The part after the colon is a quantization, "
                                     "such as Q4_K_M, not a version.")
        raise HTTPException(404, f"Ollama has no model called {body.model}.")
    hw = reg.hardware()
    return {
        "ok": True,
        "installed": False,
        "model": tag,
        "bytes": size,
        "free_disk_bytes": hw["free_disk_bytes"],
        "enough_disk": size < hw["free_disk_bytes"],
        "fits_memory": (size + reg.RUNTIME_OVERHEAD) <= int(hw.get("usable_bytes") or 0),
        "entry": reg.catalog_entry(tag),
    }


@app.post("/api/models/pull")
async def api_model_pull(body: ModelTagIn):
    """Stream an Ollama pull. Errors arrive inside a 200 stream, not as a status."""
    tag = reg.normalize_tag(body.model)

    async def event_stream():
        seen_error = None
        progress = reg.PullProgress()
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{oc.OLLAMA_HOST}/api/pull",
                                         json={"model": tag, "stream": True}) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                        yield _sse({"type": "error", "message": detail.strip()})
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            seen_error = chunk["error"]
                            yield _sse({"type": "error", "message": seen_error})
                            return
                        yield _sse(progress.update(chunk))
        except Exception as e:
            yield _sse({"type": "error", "message": str(e)})
            return
        if seen_error:
            return
        # Profile it before saying it is ready, so the UI can show capabilities
        # and the pickers can offer it immediately.
        try:
            await reg.refresh_registry()
        except Exception:
            pass
        settings, registry = _registry()
        profile = registry.get(tag) or {}
        yield _sse({
            "type": "done",
            "model": tag,
            "profile": profile,
            "roles": (reg.role_map(settings, registry) or {}).get(tag) or [],
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/models/delete")
async def api_model_delete(body: ModelTagIn):
    """Remove a model from Ollama. Refused while it is the only chat model."""
    settings, registry = _registry()
    tag = body.model if body.model in registry else reg.normalize_tag(body.model)
    if tag not in registry:
        raise HTTPException(404, "That model is not installed.")
    assigned = reg.role_map(settings, registry)
    if "chat" in (assigned.get(tag) or []):
        others = [m for m, roles in assigned.items() if "chat" in roles and m != tag]
        if not others:
            raise HTTPException(409, "This is the only model assigned to Chat.")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.request("DELETE", f"{oc.OLLAMA_HOST}/api/delete",
                                        json={"model": tag})
    except Exception as e:
        raise HTTPException(502, f"Ollama did not respond: {e}")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:300])
    # Drop its assignments and any pick that pointed at it.
    roles = {k: v for k, v in (settings.get("model_roles") or {}).items() if k != tag}
    active = {k: v for k, v in (settings.get("model_active") or {}).items() if v != tag}
    ctx = {k: v for k, v in (settings.get("model_context") or {}).items() if k != tag}
    st.save_settings({"model_roles": roles, "model_active": active, "model_context": ctx})
    await reg.refresh_registry()
    return {"ok": True, "removed": tag}


@app.get("/api/settings")
async def get_settings():
    return st.load_settings()


@app.patch("/api/settings")
async def patch_settings(body: SettingsPatch):
    return st.save_settings(body.data)


@app.get("/api/chats")
async def api_list_chats(mode: str | None = None):
    return st.list_chats(mode)


@app.post("/api/chats")
async def api_new_chat(body: ChatCreate):
    mode = body.mode
    if mode in ("code", "computer", "agentic"):
        mode = "agentic"
    titles = {"agentic": "New agentic session"}
    title = body.title or titles.get(mode, "New chat")
    chat = st.new_chat(mode=mode, title=title, project_id=body.project_id)
    if mode == "agentic":
        chat["model_id"] = _agent_model_choice(body.agent_model)
        chat = st.save_chat(chat)
    return chat


@app.get("/api/chats/{cid}")
async def api_get_chat(cid: str):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    return chat


@app.patch("/api/chats/{cid}")
async def api_patch_chat(cid: str, body: ChatPatch):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    patch = body.model_dump(exclude_none=True)
    if "agent_model" in patch:
        patch["model_id"] = _agent_model_choice(
            patch.pop("agent_model"), chat.get("model_id")
        )
    chat.update(patch)
    chat = st.save_chat(chat)
    if body.computer_use is not None:
        try:
            if not chat.get("computer_use"):
                computer_overlay.set_overlay(False)
        except Exception:
            pass
    return chat


@app.delete("/api/chats/{cid}")
async def api_delete_chat(cid: str):
    if not st.delete_chat(cid):
        raise HTTPException(404)
    return {"ok": True}


def _agent_model_choice(requested: str | None, fallback: str | None = None) -> str | None:
    """Clamp an agentic selection to a model that is actually assigned to the role."""
    settings, registry = _registry()
    options = reg.models_for("agentic", settings, registry)
    # "hybrid" swapped models mid-run, which costs a full unload and reload.
    resolved = requested
    if resolved in options:
        return resolved
    if fallback in options:
        return fallback
    return reg.active("agentic", settings, registry)


def _is_known_model_key(key: str | None) -> bool:
    if not key:
        return False
    return key in reg.installed()


def _requested_role(body, chat: dict) -> str:
    """Which tab this turn came from. The request has to say, because one model
    can serve both roles and its id alone cannot tell them apart."""
    mode = getattr(body, "mode", None)
    if mode in ("code", "agentic", "computer"):
        return "agentic"
    if mode == "chat":
        return "chat"
    # A client that does not send it keeps whatever the chat already is.
    return _role(chat)


def _no_model_message(role: str) -> str:
    if role == "agentic":
        return ("No installed model can call tools, so agentic mode is unavailable. "
                "Install a tool-capable model, or assign one to Agentic in Models.")
    return "No model is assigned to chat. Install a model, or assign one in Models."


def _role(chat: dict, reasoning: bool | None = None) -> str:
    """Which job this chat is doing. Roles key the prompt, effort and window."""
    if (chat.get("mode") or "chat") in ("code", "agentic", "computer"):
        return "agentic"
    use_r = chat.get("reasoning", False) if reasoning is None else reasoning
    return "reasoning" if use_r else "chat"


def _model_key(chat: dict, reasoning: bool | None = None, model_key: str | None = None) -> str:
    """The model id this chat runs on."""
    settings, registry = _registry()
    role = _role(chat, reasoning)
    if model_key:
        wanted = model_key
        # A per-request override still has to be able to do the job: handing a
        # blind model to a computer-use chat gives it a mouse and no eyes.
        blind = chat.get("computer_use") and not (registry.get(wanted) or {}).get("vision")
        if not blind and wanted in reg.models_for(role, settings, registry):
            return wanted
    if role == "agentic":
        sel = chat.get("model_id") or chat.get("agent_model")
        if chat.get("computer_use"):
            # The chat's own pick if it can see, and only otherwise a model
            # chosen for it.
            eyes = reg.vision_model(settings, registry, preferred=model_key or sel)
            if eyes:
                return eyes
        if sel in reg.models_for("agentic", settings, registry):
            return sel
        chosen = reg.active("agentic", settings, registry)
        if chosen:
            return chosen
        # Fall back to the chat model only if it supports tool calling.
        chat_model = reg.active("chat", settings, registry)
        if chat_model and (registry.get(chat_model) or {}).get("tools"):
            return chat_model
        return ""
    sel = chat.get("model_id")
    if sel in reg.models_for(role, settings, registry):
        return sel
    return reg.active(role, settings, registry) or reg.active("chat", settings, registry) or ""


def _resolve_effort(requested: str | None, role: str) -> str:
    """Clamp a UI-supplied effort tier, falling back to the role's own default."""
    from config import EFFORT
    tier = (requested or "").strip().lower()
    if tier in EFFORT:
        return tier
    return cfg_effort_for(role)


def _apply_effort(meta: dict, opts: dict, effort: str | None = None) -> dict:
    from config import DEFAULT_EFFORT, EFFORT
    # Honour the requested tier; fall back to the model's own default, then global.
    tier = effort or meta.get("effort") or DEFAULT_EFFORT
    e = EFFORT.get(tier) or EFFORT[DEFAULT_EFFORT]
    out = dict(opts)
    base_temp = float(meta.get("temperature", 0.7))
    out["temperature"] = max(0.05, min(1.5, base_temp * float(e["temp_scale"])))
    out["num_predict"] = int(e["num_predict"])
    return out, e


def _chat_has_images(chat: dict) -> bool:
    for m in chat.get("messages") or []:
        if m.get("images"):
            return True
        for att in m.get("attachments") or []:
            if att.get("kind") in ("image", "screenshot") or att.get("has_image") or att.get("image_b64"):
                return True
    return False


def _num_ctx_for(chat: dict, model_key: str, msgs: list[dict] | None = None,
                 reply_tokens: int | None = None) -> int:
    limit = _ctx_limit(model_key, _role(chat))
    # Estimate what we will send this turn. Callers inside the agent loop pass the
    # prompt they already rendered rather than paying for a second full render.
    try:
        used = _estimate_chat_tokens(chat, model_key, msgs=msgs)
    except Exception:
        used = 2048
    has_images = _chat_has_images(chat)
    # Short prompts: smaller KV alloc (big win on first-token latency). Grow as chat grows.
    # Vision turns must never land on 4096: one screenshot is ~4.4k tokens alone.
    if has_images or chat.get("computer_use"):
        headroom = 2048 if used < 6000 else (3072 if used < 12000 else 4096)
        floor = 12288 if (has_images and chat.get("computer_use")) else 8192
    else:
        headroom = 1536 if used < 2000 else (3072 if used < 8000 else 4096)
        floor = 2048
    # Headroom must cover the full reply budget. Reserving less than num_predict
    # cuts generation off inside the thinking block, yielding no tool call.
    from config import EFFORT, DEFAULT_EFFORT
    tier = chat.get("effort") or cfg_effort_for(_role(chat)) or DEFAULT_EFFORT
    reply_budget = int((EFFORT.get(tier) or EFFORT[DEFAULT_EFFORT]).get("num_predict") or 0)
    headroom = max(headroom, max(reply_budget, int(reply_tokens or 0)) + 512)
    want = oc.effective_num_ctx(used, limit, reply_headroom=headroom, floor=floor)
    # Ollama reloads the model whenever num_ctx changes, so keep this monotonic
    # per chat+model: grow when needed, never shrink.
    seen = chat.get("num_ctx_used")
    if not isinstance(seen, dict):
        seen = {}
    # Honor a user lowering the limit on a chat that grew larger previously.
    stored_prev = int(seen.get(model_key) or 0)
    prev = min(stored_prev, limit)
    val = max(want, prev)
    if val != stored_prev:
        seen[model_key] = val
        chat["num_ctx_used"] = seen
    return val


def _ctx_limit(model_id: str, role: str = "chat") -> int:
    settings, registry = _registry()
    return reg.context_limit(model_id, role, settings, registry.get(model_id) or {})


def _profile(model_id: str) -> dict:
    """The capability profile the tool policy is derived from. An empty one
    means full tier, so a model we cannot size keeps the whole surface."""
    _, registry = _registry()
    return registry.get(model_id) or {}


# Keep recent tool results in full and shorten older ones to fit context.
# Allow up to 8K characters so a useful source excerpt survives.
TOOL_RESULT_MAX_CHARS = 8000
# Compact well below the threshold. Stopping just under it means the next tool
# result trips it again and the prompt prefix is rewritten every step.
COMPACT_LOW_WATER = 0.55
# Marks where earlier turns were folded into a summary. Messages before the
# newest boundary are not rendered, but nothing is ever deleted.
COMPACT_BOUNDARY_KIND = "compact_boundary"
# Drop older tool details before using model-based summarization.
# Keep the conversation text; this step needs no model call.
DETAIL_HORIZON_KIND = "detail_horizon"
DETAIL_HORIZON_LOW_WATER = 0.55
# Never evict below this many detail turns: the model needs its own last action
# and that action's result to continue at all.
DETAIL_HORIZON_MIN_TURNS = 4


def _boundary_index(msgs: list[dict]) -> int:
    """Index of the newest compaction boundary, or -1 if there has never been one."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("kind") == COMPACT_BOUNDARY_KIND:
            return i
    return -1


def _horizon_index(msgs: list[dict], after: int = -1) -> int:
    """Index of the newest detail horizon lying after `after`, or -1."""
    for i in range(len(msgs) - 1, after, -1):
        if msgs[i].get("kind") == DETAIL_HORIZON_KIND:
            return i
    return -1


def _retain_recent_tool_detail(chat: dict, limit: int) -> None:
    """Set one recovery boundary so later tool turns can accumulate without changing the cached prefix."""
    msgs = chat.get("messages") or []
    start = max(_boundary_index(msgs), _horizon_index(msgs)) + 1
    turns = [i for i in range(start, len(msgs))
             if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls")]
    if len(turns) > limit:
        msgs.insert(turns[-max(1, limit)], {
            "role": "system", "kind": DETAIL_HORIZON_KIND,
            "ts": time.time(), "quiet": True,
        })


PINNED_READ_SHARE = 0.2


def _pinned_reads(msgs: list[dict], start: int, end: int, model_key: str) -> set[int]:
    """Keep earlier reads that still match the current file. Only inspect turns before the boundary."""
    if end <= start:
        return set()
    budget = int(_ctx_limit(model_key, "agentic") * PINNED_READ_SHARE) if model_key else 0
    seen: dict[str, list[tuple[int, int]]] = {}
    changed: set[str] = set()
    keep: set[int] = set()
    used = 0
    i = end - 1
    while i >= start:
        m = msgs[i]
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            i -= 1
            continue
        results = []
        j = i + 1
        while j < end and msgs[j].get("role") == "tool":
            results.append(j)
            j += 1
        payloads = []
        for k in results:
            try:
                payloads.append((msgs[k].get("name"), json.loads(msgs[k].get("content") or "")))
            except (TypeError, ValueError):
                payloads.append((msgs[k].get("name"), {}))
        reads = []
        for name, payload in payloads:
            if not isinstance(payload, dict):
                continue
            path = str(payload.get("path") or "")
            if name in {"write_file", "edit_file", "append_file"} and payload.get("changed") and path:
                changed.add(path)
            elif name == "read_file" and path and "content" in payload and not payload.get("error"):
                reads.append((path, int(payload.get("start") or 1), int(payload.get("end") or 0)))
        calls = m.get("tool_calls") or []
        if reads and len(reads) == len(calls) == len(results):
            fresh = any(
                path not in changed and not any(a <= lo and hi <= b for a, b in seen.get(path, []))
                for path, lo, hi in reads
            )
            # The stored assistant turn carries the UI work log, which is never sent.
            cost = oc.estimate_messages_tokens([msgs[k] for k in results]) + 64 * len(calls)
            if fresh and used + cost <= budget:
                used += cost
                keep.update([i, *results])
                for path, lo, hi in reads:
                    seen.setdefault(path, []).append((lo, hi))
        i -= 1
    return keep


def _narrow_if_crowded(chat: dict, model_key: str, limit: int) -> bool:
    """Narrow replay after a failed sample only when it fills over half the context window."""
    if not model_key or _estimate_chat_tokens(chat, model_key) <= _ctx_limit(model_key, "agentic") // 2:
        return False
    _retain_recent_tool_detail(chat, limit)
    return True


def _advance_detail_horizon(chat: dict, model_key: str, msgs: list[dict] | None = None) -> bool:
    """Hide older tool details behind a marker when context is tight.

    Keep all messages on disk. Rendering stays append-only between markers,
    so Ollama can reuse the prompt cache. Estimate each message once.
    """
    limit = _ctx_limit(model_key, _role(chat))
    threshold = float(st.load_settings().get("auto_compact_at", 0.85))
    high = int(limit * threshold * 0.9)
    used = _estimate_chat_tokens(chat, model_key, msgs=msgs)
    if used <= high:
        return False
    all_msgs = chat.get("messages") or []
    boundary = _boundary_index(all_msgs)
    start = max(boundary, _horizon_index(all_msgs, boundary)) + 1
    detail = [
        i for i in range(start, len(all_msgs))
        if all_msgs[i].get("role") == "tool"
        or (all_msgs[i].get("role") == "assistant" and all_msgs[i].get("tool_calls"))
    ]
    if len(detail) <= DETAIL_HORIZON_MIN_TURNS:
        return False
    cost = {i: oc.estimate_messages_tokens([all_msgs[i]]) for i in detail}
    # Everything that is not evictable detail still has to fit.
    overhead = max(0, used - sum(cost.values()))
    budget = max(0, int(high * DETAIL_HORIZON_LOW_WATER) - overhead)
    kept = 0
    cut = start
    for i in reversed(detail):
        if kept + cost[i] > budget:
            cut = i + 1
            break
        kept += cost[i]
    # Never orphan a tool result from the assistant turn that requested it.
    while cut < len(all_msgs) and all_msgs[cut].get("role") == "tool":
        cut += 1
    if cut <= start or cut >= len(all_msgs):
        return False
    all_msgs.insert(cut, {
        "role": "system",
        "kind": DETAIL_HORIZON_KIND,
        "ts": time.time(),
        "quiet": True,
    })
    return True


def _active_summary(chat: dict) -> str:
    """The summary that currently stands in for everything before the boundary."""
    msgs = chat.get("messages") or []
    idx = _boundary_index(msgs)
    if idx >= 0:
        return (msgs[idx].get("summary") or "").strip()
    # Chats compacted before Phase 2 kept their summary on the chat itself.
    return (chat.get("summary") or "").strip()


def _contains_replay_elision(value: Any) -> bool:
    if isinstance(value, str):
        return agent.REPLAY_ELISION_PREFIX in value
    if isinstance(value, dict):
        return any(_contains_replay_elision(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_replay_elision(item) for item in value)
    return False


def _rewrite_stale_tool_result(name: str, content: str) -> str:
    """Mark historical capability errors as obsolete before replaying a chat."""
    if name != "edit_file":
        return content
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content
    if (
        isinstance(payload, dict)
        and payload.get("error") == "Too many edit ranges in one batch"
        and int(payload.get("maximum") or 0) < agent.MAX_EDIT_RANGES
    ):
        return json.dumps({
            "tool": "edit_file",
            "obsolete_error": True,
            "previous_maximum": payload.get("maximum"),
            "current_maximum": agent.MAX_EDIT_RANGES,
            "note": (
                "This result came from an older Olith runtime. The limit is now higher; "
                "do not ask the user to split or choose work because of this old error."
            ),
        })
    return content


def _is_plan_deferral(content: str) -> bool:
    """Recognize legacy assistant replies that handed an ordered plan back to the user."""
    text = " ".join((content or "").lower().split())
    if not text:
        return False
    return any(marker in text for marker in (
        "which one?",
        "which one should",
        "pick one",
        "what to do next",
        "tell me which step",
        "tell me what to do next",
        "try narrowing the request to one concrete next step",
        "i'm stopping instead of retrying",
        "i am stopping instead of retrying",
        "the model returned no assistant message or tool call",
    ))


def _is_simple_resume(content: str) -> bool:
    """Return whether a user message only asks an existing job to keep going."""
    return bool(re.fullmatch(
        r"\s*(?:continue|resume|go on|keep going|carry on|proceed|finish it)\s*[.!]*\s*",
        content or "",
        re.IGNORECASE,
    ))


def _tool_succeeded(name: str, result: str) -> bool:
    """Return whether a tool produced a usable new observation or mutation."""
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    if name == "run_shell":
        return payload.get("exit_code") == 0
    validation = payload.get("validation")
    if isinstance(validation, dict) and validation.get("ok") is False:
        return False
    return payload.get("ok") is not False


def _verification_summary(report: dict) -> str:
    commands = report.get("commands") or []
    ran = [command for command in commands if "result" in command]
    passed = sum(command["result"].get("exit_code") == 0 for command in ran)
    skipped = sum(command.get("status") == "needs_approval" for command in commands)
    errors = sum(item.get("severity") == 1 for entry in report.get("language_servers", []) for item in entry.get("diagnostics", []))
    parts = [f"{passed} command(s) passed, {len(ran) - passed} failed" if ran else "no test or build command was run"]
    if skipped:
        parts.append(f"{skipped} command(s) still need approval")
    if errors:
        parts.append(f"{errors} language-server error(s) remain")
    return "Automatic checks: " + "; ".join(parts) + "."




def _start_new_job(chat: dict) -> None:
    """Drop per-job state when a message starts fresh work."""
    chat["asked_this_job"] = False
    # Written by the implementation_plan tool, which no longer exists. Popped so
    # a chat saved before Phase 1 stops feeding a dead scaffold to the prompt.
    chat.pop("agent_spec", None)
    # Replay anchors to a boundary message, not a stored offset. Dropped from
    # chats that still carry one.
    chat.pop("replay_floor", None)


def _rejection_nudge(rejections: dict[str, dict]) -> str:
    """Build a retry instruction from the tool's rejection so the model knows what to change."""
    lines = []
    for payload in list(rejections.values())[-3:]:
        detail = payload.get("hint") or payload.get("error") or ""
        if not detail:
            continue
        limits = ", ".join(
            f"{k}={payload[k]}" for k in ("requested", "maximum", "maximum_chars")
            if payload.get(k) is not None
        )
        lines.append(f"- {detail}" + (f" ({limits})" if limits else ""))
    if not lines:
        return ""
    return (
        "[agent-loop] A tool rejected your last calls and you resent them unchanged. "
        "Do not send those arguments again. Satisfy these constraints instead:\n"
        + "\n".join(lines)
        + "\nSplit the operation into smaller valid calls yourself and continue the job. "
        "Do not hand this choice back to the user."
    )


# How many screenshots keep their pixels in the prompt. Each frame is ~2.4k
# tokens, and anything older than the last action is a caption.
COMPUTER_FRAME_KEEP = 2


def _mark_computer(chat: dict | None, on: bool) -> None:
    """Show the desktop overlay while a computer-use run is active. Hide it when the run stops."""
    try:
        if on and not (chat or {}).get("computer_use"):
            return
        if on:
            # Desktop state (what is selected, how many blank lines) belongs to
            # the run, not to the process.
            computer.begin_run()
        computer_overlay.set_overlay(bool(on), label="Olith is using your computer")
    except Exception:
        pass


# Consecutive desktop actions that leave the screen untouched before the run is
# told to stop trying.
COMPUTER_STALL_LIMIT = 4


# Limits for repeated desktop actions and the corrections allowed before stopping.
COMPUTER_REPEAT_LIMIT = 4
COMPUTER_REPEAT_BLOCKS = 3
# Limit consecutive observations with no action. Observations skip the duplicate-call guard.
LOOK_LIMIT = 5


# Acting on the world, as opposed to looking at it. Both tools reach the same
# screen, so a run-away through one is a run-away through the other.
_WORLD_TOOLS = ("computer", "browser")
# Calls that only observe. Counting them would reset the run of repeats this is
# here to catch, since a model that looks between every keystroke is working
# normally.
_LOOKING = {"screenshot", "wait", "elements", "snapshot", "read", "status"}


def _is_a_look(call: dict) -> bool:
    """Whether this call only observes. Observations are never duplicates."""
    if call.get("name") not in _WORLD_TOOLS:
        return False
    args = call.get("arguments") or {}
    steps = args.get("actions")
    if isinstance(steps, list) and steps:
        return all(
            str((a or {}).get("action") or "").strip().lower() in _LOOKING
            for a in steps if isinstance(a, dict)
        )
    return str(args.get("action") or "").strip().lower() in _LOOKING


def _computer_repeat_key(args: dict, tool: str = "computer") -> str | None:
    """Identify an action across browser and desktop tools. Return None for an observation."""
    steps = (args or {}).get("actions")
    if isinstance(steps, list) and steps:
        # A batch is one identity, so two identical batches are a run-away and
        # a batch differing in its last step is a different plan.
        keys = [_computer_repeat_key(a, tool) for a in steps if isinstance(a, dict)]
        keys = [k for k in keys if k]
        return json.dumps(keys) if keys else None
    action = str((args or {}).get("action") or "").strip().lower()
    if action in _LOOKING:
        return None
    return json.dumps(
        [action, args.get("x"), args.get("y"), args.get("text"),
         args.get("ref"), args.get("direction"), args.get("count")],
        sort_keys=True, default=str,
    )


def _computer_progress(result: str, stalls: int) -> tuple[str, int]:
    """Stop after repeated actions leave the screen unchanged, even if coordinates differ."""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return result, stalls
    if not isinstance(payload, dict) or payload.get("error"):
        return result, stalls
    if payload.get("action") == "screenshot" or payload.get("screen_changed"):
        return result, 0
    stalls += 1
    if stalls < COMPUTER_STALL_LIMIT:
        return result, stalls
    return json.dumps({
        "error": f"{stalls} desktop actions in a row left the screen unchanged.",
        "hint": (
            "Nothing you send is reaching the desktop, so a fifth attempt will not "
            "land either. Stop acting now and tell the user what you were trying to "
            "do and what the screen shows instead."
        ),
    }), stalls


def _prune_stored_frames(chat: dict) -> None:
    """Remove older screenshot pixels from saved chats to keep repeated saves small."""
    frames = [m for m in (chat.get("messages") or []) if m.get("computer_frame")]
    for m in frames[:-COMPUTER_FRAME_KEEP]:
        if m.pop("images", None):
            m["content"] = "[screen] an earlier screenshot, no longer stored."


def _frame_message(frame: dict) -> dict:
    """Add a screenshot as an internal user turn so vision templates display it.

    Most templates ignore images on tool messages. Mark it as internal so it
    does not count as a new user request.
    """
    return {
        "role": "user",
        "content": (
            f"[screen] {frame['w']}x{frame['h']}, after the action above. "
            "Coordinates you send are pixels on this image."
        ),
        "images": [frame["b64"]],
        "computer_frame": True,
        "quiet": True,
        "ts": time.time(),
    }


def _shell_probe_identity(command: str, root: Path | None):
    """Identical C/C++ probes stay identical when only renamed."""
    if root is None:
        return None
    match = re.search(r"\bcat\s+>\s*([^\s<>;&|'\"]+\.(?:cpp|cxx|cc|c))\s*<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\2(?:\n|$)", command, re.S)
    if not match:
        return None
    source = Path(match[1])
    # Writing over real project source is an edit, not a probe.
    if not source.is_absolute() and (root / source).is_file():
        return None
    revisions = {}
    for include in re.findall(r'^\s*#include\s+"([^"\n]+)"', match[3], re.M):
        path = (root / include).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return None
        revisions[str(path)] = agent._file_revision(path)
    # The probe's own name shows up as source, object and binary paths.
    name = re.compile(r"(?<![\w-])" + re.escape(source.stem) + r"(?![\w-])")
    def normalize(text):
        return name.sub("<probe>", text)
    return {"command": normalize(command[:match.start(3)]) + match[3] + normalize(command[match.end(3):]), "inputs": revisions}


def _call_signature(call: dict, project_root: Path | None) -> str:
    """Include the file revision in repeat checks so an edit can retry after its source changes."""
    args = call.get("arguments") or {}
    payload: dict[str, Any] = {"n": call.get("name"), "a": args}
    if call.get("name") == "run_shell":
        try:
            probe = _shell_probe_identity(str(args.get("command") or ""), project_root)
        except (OSError, ValueError):
            probe = None
        if probe:
            payload["a"] = {**args, "command": probe}
    if call.get("name") in _WORLD_TOOLS:
        # Use each tool's change counter so a repeat click can run after the screen changes.
        payload["world"] = (computer.frame_id(), browser.state_id())
    target = args.get("path")
    if isinstance(target, str) and target:
        try:
            resolved, err = agent.resolve_existing(target, project_root, want_dir=False)
            if resolved is not None and not err:
                payload["rev"] = agent._file_revision(resolved)
        except (OSError, ValueError, PermissionError):
            pass
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


THINKING_REPLY_TOKENS = 8192
SALVAGE_CHARS = 12000


def _salvage_directive(reasoning: str) -> str:
    """Carry a cut-off step's reasoning into its retry instead of discarding it."""
    text = reasoning.strip()
    if len(text) > SALVAGE_CHARS:
        text = "[earlier reasoning omitted]\n" + text[-SALVAGE_CHARS:]
    return (
        "[agent-loop] Your last reply ran out of room while still reasoning, so nothing ran. "
        "Your reasoning so far is below. Do not start over. Decide from it and make the next "
        "tool call now, or give the final answer if the work is done.\n<reasoning>\n"
        + text + "\n</reasoning>"
    )


def _result_in_view(chat: dict, message: dict) -> bool:
    """Whether a stored tool result still renders, or was dropped by a horizon or compaction."""
    msgs = chat.get("messages") or []
    idx = next((i for i, m in enumerate(msgs) if m is message), -1)
    if idx < 0:
        return False
    boundary = _boundary_index(msgs)
    return idx > max(boundary, _horizon_index(msgs, boundary))


def _current_run_messages(chat: dict) -> list[dict]:
    """Return messages from the current request. Ignore internal loop instructions when finding its start."""
    msgs = chat.get("messages") or []
    start = 0
    for idx in range(len(msgs) - 1, -1, -1):
        m = msgs[idx]
        if m.get("role") == "user" and not m.get("agent_nudge") and not m.get("computer_frame"):
            start = idx
            break
    return msgs[start:]


def _build_messages(
    chat: dict,
    model_key: str,
    clarify_first: bool = False,
    tool_replay_limit: int | None = None,
    repo_context: str = "",
    plan_context: str | None = None,
) -> list[dict]:
    chat_role = _role(chat)
    sys = reg.system_prompt(chat_role, reg.installed().get(model_key) or {})
    mem = st.memory_as_prompt("global", chat.get("project_id"))
    if mem:
        sys += "\n\n# Memory\n" + mem
    active_summary = _active_summary(chat)
    if active_summary:
        sys += "\n\n# Compacted earlier context\n" + active_summary
    hist = st.history_index(exclude_id=chat.get("id"))
    if hist:
        sys += "\n\n" + hist
    lib_block = ar.library_prompt_block(chat.get("file_library"))
    if lib_block:
        sys += "\n\n" + lib_block
    if chat_role == "agentic":
        sys += agent.tools_system_addon(
            agent.tool_defs_for(chat, _ctx_limit(model_key, chat_role), _profile(model_key))
        )
        if st.load_settings().get("approval_mode") == "full_access":
            sys += (
                "\n\n# Approval mode: Full access\n"
                "The user enabled Full access in Settings. Shell commands, Git/GitHub changes "
                "and connected service actions run without authorization prompts. Carry out "
                "the user's requested work without asking for permission to use those tools. "
                "Stay within the requested task; ask only when necessary information is missing."
            )
        proj = st.get_project(chat["project_id"]) if chat.get("project_id") else None
        if proj:
            sys += f"\n\nActive project: {proj['name']}\nRoot: {proj['root']}\n{proj.get('description') or ''}"
            if repo_context:
                sys += "\n\n" + repo_context
        canon = st.canon_as_prompt(chat.get("project_id"))
        if canon:
            sys += "\n\n" + canon
        if not chat.get("subagent"):
            direct = mcp_connections.prompt_block()
            if direct:
                sys += "\n\n" + direct
        if clarify_first:
            sys += (
                "\n\n# Clarify-first is ON\n"
                "Before doing real work on this task, call ask_user once with the questions "
                "whose answers would most change what you build. Skip anything already settled "
                "in the Established canon, and skip anything you could answer yourself with a "
                "tool. If the task is genuinely unambiguous, say so in one line and continue."
            )
        plan = agent.todos_prompt_block(chat) if plan_context is None else plan_context
        if plan:
            sys += "\n\n" + plan
    if chat.get("computer_use"):
        sys += config.COMPUTER_USE_PROMPT
        linked = connections.prompt_block()
        if linked:
            # Which accounts are live changes the plan, and knowing stops a run
            # trying to log in to something it cannot log in to.
            sys += "\n\n" + linked
        ok, why = computer.available()
        if not ok:
            sys += (
                f"\n\nThe desktop cannot be driven right now: {why}. Say so and do the "
                "parts of the task that do not need it."
            )
    elif chat_role == "agentic" and "subagent" not in chat:
        # No browser schema here, but a run that cannot see a connected account
        # goes hunting for a login. Keys keep their tool, and sessions say why not.
        linked = connections.prompt_block(browser=False)
        if linked:
            sys += "\n\n" + linked
    all_msgs = chat.get("messages") or []
    # Start after the latest summary marker. Keep rendering append-only until
    # the next marker so the prompt cache stays valid.
    boundary = _boundary_index(all_msgs)
    render = all_msgs[boundary + 1:]
    msgs: list[dict] = [{"role": "system", "content": sys}]
    # A summary paraphrases, and the model checks its work against the literal
    # request, so re-emit the latest user turn verbatim once it falls behind.
    if boundary >= 0:
        latest_user = _latest_user_index(all_msgs)
        if 0 <= latest_user < boundary:
            msgs.append({
                "role": "user",
                "content": all_msgs[latest_user].get("content") or "",
            })
    # Detail-bearing turns before the newest horizon are dropped from the render.
    # The narrative (user turns, assistant prose, debriefs) is untouched.
    horizon_abs = _horizon_index(all_msgs, boundary)
    detail_from = (horizon_abs - (boundary + 1)) if horizon_abs >= 0 else 0
    pinned = {i - (boundary + 1) for i in _pinned_reads(all_msgs, boundary + 1, horizon_abs, model_key)}
    # Only the newest frame shows the screen as it is now. The rest keep their
    # caption and lose the pixels, which would fill the window in ten steps.
    frame_idxs = [i for i, mm in enumerate(render) if mm.get("computer_frame")]
    live_frames = set(frame_idxs[-COMPUTER_FRAME_KEEP:])
    keep_tool_turns: set[int] | None = None
    if tool_replay_limit is not None:
        # Explicit narrowing (the length-truncation recovery lane) deliberately
        # discards history, so a rolling window is what is wanted there.
        tool_turn_idxs = [
            i for i, mm in enumerate(render)
            if mm.get("role") == "assistant" and (mm.get("tool_calls") or [])
        ]
        keep_tool_turns = set(tool_turn_idxs[-max(1, int(tool_replay_limit)):])
    # A boundary can land between an assistant tool_call and its result, which
    # Ollama rejects. Drop the orphan rather than send an invalid sequence.
    pending_tool_calls = False
    for idx, m in enumerate(render):
        role = m.get("role")
        content = m.get("content") or ""
        tool_calls = m.get("tool_calls") or []
        # Older boundaries are pure bookkeeping; only the newest one contributes
        # a summary, and it sits before `render` by construction.
        if m.get("kind") in (COMPACT_BOUNDARY_KIND, DETAIL_HORIZON_KIND):
            continue
        # Older detail: drop the assistant tool-call turn, and the orphan check
        # below then drops its results too.
        if idx < detail_from and idx not in pinned and (role == "tool" or (role == "assistant" and tool_calls)):
            if role == "assistant":
                pending_tool_calls = False
            continue
        if role == "tool":
            if not pending_tool_calls:
                continue
            content = _rewrite_stale_tool_result(m.get("name") or "", content)
            # Replay later results as tool turns. Keep earlier results unchanged
            # until an explicit context boundary drops them.
            body = content[:TOOL_RESULT_MAX_CHARS]
            if len(content) > TOOL_RESULT_MAX_CHARS:
                body += "\n…[truncated, re-read a narrower slice if you need more]"
            entry: dict[str, Any] = {"role": "tool", "content": body}
            if m.get("name"):
                entry["tool_name"] = m["name"]
            msgs.append(entry)
            continue
        if role == "assistant":
            if any(call.get("name") == "run_shell" and agent.is_internal_verification_command((call.get("arguments") or {}).get("command")) for call in tool_calls):
                pending_tool_calls = False
                continue
            # Loop-control placeholders are internal state, never conversation.
            # Dropping them here also heals older transcripts.
            if agent.is_internal_continuation_placeholder(content):
                content = ""
            if tool_calls and keep_tool_turns is not None and idx not in keep_tool_turns:
                pending_tool_calls = False
                continue
            # Interim turns without tool calls hold only UI notes. Replaying one
            # teaches the model to echo it as a final answer.
            if m.get("interim") and not tool_calls:
                continue
            # Heal legacy chats that returned a todo plan as a question. The plan
            # is the authority; replaying the deferral teaches it to ask again.
            if not tool_calls and agent.todos_incomplete(chat) and _is_plan_deferral(content):
                continue
            # A mutation call that copied Olith's replay placeholder never
            # changed the file. Drop the poisoned call/result pair.
            if tool_calls and _contains_replay_elision(tool_calls):
                pending_tool_calls = False
                continue
            # Must keep empty-content assistants that emitted tool_calls; dropping
            # those breaks the assistant→tool sequence required by Ollama.
            if not content and not m.get("images") and not tool_calls:
                continue
            # Replay the model's own words, not Olith's record above them.
            entry = {"role": "assistant", "content": m.get("model_content") or content}
            if m.get("images"):
                entry["images"] = m["images"]
            if tool_calls:
                # Ollama needs thinking replayed with the calls it produced.
                # That round's native thinking only, not the UI work log.
                if m.get("native_thinking"):
                    entry["thinking"] = m["native_thinking"]
                # Keep full tool arguments. Placeholder text can be copied into later writes.
                entry["tool_calls"] = agent.to_ollama_tool_calls(tool_calls)
            pending_tool_calls = bool(tool_calls)
            msgs.append(entry)
            continue
        if m.get("computer_frame") and idx not in live_frames:
            msgs.append({
                "role": "user",
                "content": "[screen] an earlier screenshot, no longer shown.",
            })
            pending_tool_calls = False
            continue
        if role not in ("user", "system") or not content and not m.get("images"):
            continue
        # Loop nudges must not look like fresh user turns.
        if m.get("agent_nudge"):
            # Skipping this must preserve pending_tool_calls: older transcripts
            # sometimes stored a nudge between the assistant call and tool result.
            continue
        # Chat and reasoning have no file tools, so expand attached files into
        # prompt only when the file router marks this turn (or legacy same-message attach).
        send_content = content
        if m.get("resume_job") or (
            m.get("course_correct") and _is_simple_resume(send_content)
        ):
            send_content = agent.CONTINUE_NUDGE + "\n\n" + send_content
        elif m.get("course_correct"):
            send_content = agent.COURSE_CORRECT_PREFIX + send_content
        if role == "user" and chat_role != "agentic":
            load_paths = m.get("load_files") or None
            if load_paths:
                extra = _expand_attachments_for_chat_model(
                    m, only_paths=load_paths, library=chat.get("file_library")
                )
                if extra:
                    send_content = (send_content + "\n\n" + extra).strip()
        entry = {"role": role, "content": send_content}
        if m.get("images"):
            entry["images"] = m["images"]
        pending_tool_calls = False
        msgs.append(entry)
    return msgs


# Learn the estimate correction per chat. Clamp it to 0.75–1.00 so a bad
# measurement cannot claim too much context space.
_ESTIMATE_BIAS_FLOOR = 0.75
_ESTIMATE_BIAS_MIN_SAMPLES = 3


def record_estimate_accuracy(chat: dict, model_key: str, estimated: int, actual: int) -> None:
    """Adjust the token estimate using Ollama's reported prompt count."""
    if not isinstance(chat, dict) or estimated <= 0 or not actual or actual <= 0:
        return
    seen = chat.get("estimate_bias")
    if not isinstance(seen, dict):
        seen = {}
    entry = seen.get(model_key) or {}
    ratio = float(entry.get("ratio") or 0.0)
    count = int(entry.get("n") or 0)
    observed = actual / estimated
    # Plain EMA once seeded, so a single odd step cannot move the window much.
    ratio = observed if count == 0 else ratio + (observed - ratio) * 0.25
    seen[model_key] = {"ratio": ratio, "n": count + 1}
    chat["estimate_bias"] = seen


def _estimate_bias(chat: dict, model_key: str) -> float:
    entry = ((chat or {}).get("estimate_bias") or {}).get(model_key) or {}
    if int(entry.get("n") or 0) < _ESTIMATE_BIAS_MIN_SAMPLES:
        return 1.0
    # Keep a small margin over the learned ratio; being slightly over costs
    # headroom, being under costs the run.
    return max(_ESTIMATE_BIAS_FLOOR, min(1.0, float(entry.get("ratio") or 1.0) * 1.06))


def _estimate_chat_tokens(chat: dict, model_key: str, msgs: list[dict] | None = None) -> int:
    """Estimate the full outgoing prompt. Reuse msgs when already rendered to avoid rebuilding it."""
    if msgs is None:
        msgs = _build_messages(chat, model_key)
    used = oc.estimate_messages_tokens(msgs)
    # The schema is sent beside messages and consumes the same KV context.
    if _role(chat) == "agentic":
        defs = agent.tool_defs_for(chat, _ctx_limit(model_key, "agentic"), _profile(model_key))
        used += oc.estimate_tokens(json.dumps(defs, ensure_ascii=False)) + 128
    return int(used * _estimate_bias(chat, model_key))


async def _compact_chat(chat: dict, model_key: str, upto: int) -> str:
    """Summarize messages[:upto] with the active model, preserving file paths, decisions and next steps."""
    key = model_key if model_key in reg.installed() else (reg.active("chat") or model_key)
    meta = await oc.ensure_model_loaded(key, meta=_meta(key, config.COMPACT_MODEL_ROLE))
    ctx = min(_ctx_limit(key, _role(chat)), 32768)
    transcript = []
    for m in (chat.get("messages") or [])[:upto]:
        if m.get("kind") == COMPACT_BOUNDARY_KIND:
            # An earlier brief is the best record of what came before it.
            transcript.append(f"[earlier summary]\n{m.get('summary') or ''}")
            continue
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "")[:6000]
        if m.get("role") == "tool":
            content = f"[{m.get('name') or 'tool'}] {content}"
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                content += f"\n[called {fn.get('name')}]"
        if content.strip():
            transcript.append(f"{role}: {content}")
    text = "\n\n".join(transcript)[-100000:]
    summary = await oc.chat_once(
        meta["id"],
        [
            {"role": "system", "content": COMPACT_PROMPT},
            {"role": "user", "content": text},
        ],
        options=oc.model_options(meta, ctx),
        keep_alive="30m",
    )
    body, _ = _split_thinking(summary)
    return (body or summary).strip()


def _latest_user_index(msgs: list[dict]) -> int:
    """Index of the newest real user turn (loop nudges are internal), or -1."""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "user" and not m.get("agent_nudge"):
            return i
    return -1


def _compact_split_point(msgs: list[dict], keep: int) -> int:
    """Keep the newest messages without splitting tool calls from their results.

    The boundary can pass the latest user turn; rendering repeats that request
    after the summary.
    """
    upto = max(0, len(msgs) - max(1, keep))
    # Walk back off a tool result so the boundary never orphans one.
    while upto > 0 and msgs[upto].get("role") == "tool":
        upto -= 1
    return max(0, upto)


async def _maybe_auto_compact(chat: dict, model_key: str, emit=None, msgs=None):
    """Insert a summary boundary when the prompt nears its limit. Keep the full transcript on disk."""
    settings = st.load_settings()
    threshold = float(settings.get("auto_compact_at", 0.85))
    keep = int(settings.get("compact_keep_recent", 10))
    limit = _ctx_limit(model_key, _role(chat))
    did = False

    for _pass in range(4):
        used = _estimate_chat_tokens(chat, model_key, msgs=msgs)
        msgs = None  # only the caller's first render is still valid
        pct = used / max(limit, 1)
        all_msgs = chat.get("messages") or []
        if pct < threshold or len(all_msgs) < 3:
            break
        # Hysteresis: summarize well past the trigger so the following steps can
        # append without immediately compacting again.
        keep_now = max(2, keep - _pass * 4)
        upto = _compact_split_point(all_msgs, keep_now)
        if upto <= 0:
            break
        # Never re-summarize ground already covered by the newest boundary.
        if upto <= _boundary_index(all_msgs):
            break

        if emit and not did:
            await emit({
                "type": "compacting",
                "pct": round(100 * pct, 1),
                "used": used,
                "limit": limit,
            })

        summary = await _compact_chat(chat, model_key, upto)
        if not summary:
            break
        all_msgs.insert(upto, {
            "role": "system",
            "kind": COMPACT_BOUNDARY_KIND,
            "summary": summary,
            "ts": time.time(),
            # The UI hides quiet messages, so the marker never renders as a turn.
            "quiet": True,
        })
        # Mirrored for the context panel and for chats read by older code paths.
        chat["summary"] = summary
        st.save_chat(chat)
        did = True

        # Far enough under the trigger that several append-only steps fit before
        # the next boundary. Otherwise loop with a smaller tail.
        if _estimate_chat_tokens(chat, model_key) <= limit * threshold * COMPACT_LOW_WATER:
            break

    if did and emit:
        used_final = _estimate_chat_tokens(chat, model_key)
        await emit({
            "type": "compacted",
            "summary": (chat.get("summary") or "")[:800],
            "messages": chat.get("messages") or [],
            "context": {
                "used": used_final,
                "limit": limit,
                "pct": round(100 * used_final / max(limit, 1), 1),
            },
        })
    return chat, did


# Ollama surfaces reasoning in its own `thinking` field for most models, but
# some still emit it inline, and not every family spells the tag the same way.
THINK_TAGS = ("think", "thinking", "reasoning")
_THINK_CLOSED = re.compile(
    r"<(" + "|".join(THINK_TAGS) + r")>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)
_THINK_OPEN = re.compile(r"^\s*<(" + "|".join(THINK_TAGS) + r")>(.*)$", re.DOTALL | re.IGNORECASE)


def _split_thinking(content: str) -> tuple[str, str]:
    think = ""
    body = content
    m = _THINK_CLOSED.search(content)
    if m:
        think = m.group(2).strip()
        body = (content[: m.start()] + content[m.end() :]).strip()
    else:
        m2 = _THINK_OPEN.match(content)
        # An unclosed tag means the reply was cut off mid-thought, so all of it
        # is reasoning and none of it is an answer.
        if m2 and f"</{m2.group(1).lower()}>" not in content.lower():
            think = m2.group(2).strip()
            body = ""
    return body, think


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _read_attachment_text(path: Path, limit: int = 120_000) -> str:
    if path.is_dir():
        entries = []
        for p in sorted(path.iterdir())[:200]:
            entries.append(("📁 " if p.is_dir() else "📄 ") + p.name)
        return f"[folder {path}]\n" + "\n".join(entries)
    if not path.is_file():
        return f"[missing {path}]"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return f"[image file {path}]"
    try:
        if path.stat().st_size > 2_000_000:
            return f"[file too large: {path}, {path.stat().st_size} bytes]"
        raw = path.read_bytes()[: min(limit + 4096, path.stat().st_size)]
        # Treat as text if mostly decodable UTF-8 / no NUL
        if b"\x00" in raw[:4096]:
            return f"[binary file {path.name}, {path.stat().st_size} bytes; open/inspect with tools if needed]"
        text = raw.decode("utf-8", errors="replace")[:limit]
        return text
    except Exception as e:
        return f"[unreadable {path}: {e}]"


_TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".html", ".htm", ".css", ".scss", ".xml",
    ".svg", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp", ".rs",
    ".go", ".java", ".kt", ".swift", ".rb", ".php", ".sql", ".r", ".m", ".mm", ".lua",
    ".vim", ".dockerfile", ".gitignore", ".env", ".log", ".rst", ".tex", ".bib",
}


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _attachment_pointer(name: str, kind: str, path: Path | None) -> str:
    """Short reference for the visible user message. Never dump file bodies here."""
    label = name or (path.name if path else "attachment")
    if path and path.exists():
        try:
            if path.is_dir():
                return f"[Attached folder: {label}]\npath: {path}"
            size = _fmt_size(path.stat().st_size)
            return (
                f"[Attached file: {label}]\n"
                f"path: {path}\n"
                f"size: {size} · kind: {kind or 'file'}\n"
                f"Read this path with tools (or ask to open it); contents are not inlined."
            )
        except Exception:
            pass
    return f"[Attached {kind or 'file'}: {label}]" + (f"\npath: {path}" if path else "")


def _expand_attachments_for_chat_model(
    message: dict,
    *,
    limit: int = 80_000,
    only_paths: list[str] | None = None,
    library: list[dict] | None = None,
) -> str:
    """For non-agentic models (no read_file), inject file text at prompt-build time only."""
    blocks: list[str] = []
    budget = limit
    want = set(only_paths) if only_paths else None
    # Prefer explicit load list; else message attachments; else full library
    sources: list[dict] = []
    if want and library:
        by_path = {str(i.get("path")): i for i in (library or []) if i.get("path")}
        for p in only_paths or []:
            if p in by_path:
                sources.append(by_path[p])
            else:
                sources.append({"name": Path(p).name, "path": p, "kind": "file"})
    else:
        sources = list(message.get("attachments") or [])
    for att in sources:
        if budget <= 0:
            blocks.append("[additional attachments omitted, budget reached]")
            break
        kind = att.get("kind") or "file"
        if kind in ("image", "screenshot") or att.get("has_image"):
            continue
        path_s = att.get("path")
        if not path_s:
            continue
        if want is not None and path_s not in want:
            continue
        p = Path(path_s)
        name = att.get("name") or p.name
        if p.is_dir():
            listing = _read_attachment_text(p, limit=min(8000, budget))
            blocks.append(f"--- folder: {name} ({p}) ---\n{listing}")
            budget -= len(listing)
            continue
        if not p.is_file():
            blocks.append(f"--- missing attachment: {name} ({p}) ---")
            continue
        suffix = p.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            continue
        body = _read_attachment_text(p, limit=min(budget, 60_000))
        blocks.append(f"--- file: {name} ({p}) ---\n{body}")
        budget -= len(body)
    if not blocks:
        return ""
    return "\n\n# Attached files (loaded for this model turn)\n" + "\n\n".join(blocks)


def _sync_file_library(chat: dict, incoming: list[dict] | None = None) -> list[dict]:
    """Persist chat-scoped file library from new attaches + historical message attaches."""
    harvested: list[dict] = []
    for m in chat.get("messages") or []:
        for att in m.get("attachments") or []:
            harvested.append(att)
    lib = ar.merge_library(chat.get("file_library"), harvested)
    if incoming:
        lib = ar.merge_library(lib, incoming)
    chat["file_library"] = lib
    return lib


def _related_chats_block(query: str, exclude_id: str | None, *, force: bool = False) -> str:
    """Attach past-chat excerpts only on explicit cross-chat intent or exact title mention."""
    q = (query or "").strip()
    if not q:
        return ""
    intents = memory_router.classify(q)
    title_hits = []
    ql = q.lower()
    for meta in st.list_chats():
        if exclude_id and meta["id"] == exclude_id:
            continue
        title = (meta.get("title") or "").strip()
        if len(title) >= 5 and title.lower() in ql and not st.is_placeholder_title(title):
            title_hits.append(meta)
    if not force and not intents.cross_chat and not title_hits:
        return ""
    hits = st.search_chats(q, exclude_id=exclude_id, limit=4) if (intents.cross_chat or force) else []
    seen = set()
    blocks = []
    for meta in title_hits + hits:
        cid = meta.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        ex = st.chat_excerpt(cid, max_chars=4500)
        if not ex:
            continue
        blocks.append(f"--- past chat: {ex['title']} [{ex['id']}] ---\n{ex['excerpt']}")
        if len(blocks) >= 3:
            break
    if not blocks:
        if intents.cross_chat:
            return (
                "\n\n# Retrieved past chats\n"
                "No matching other chat was found. Say you do not have that conversation on file."
            )
        return ""
    return "\n\n# Retrieved past chats\n" + "\n\n".join(blocks)


async def _post_reply_jobs(cid: str, model_key: str) -> None:
    """Title and deferred compact, isolated from the chat stream and GPU lock."""
    try:
        final = st.get_chat(cid)
        if not final:
            return
        if _role(final) == "agentic" and final.get("project_id") and st.load_settings().get("semantic_search"):
            # Refresh after agent/shell edits; generation batches wait for the
            # active run to finish and reuse unchanged vectors.
            await api_build_index(final["project_id"], IndexBuild(semantic=True))
        await _maybe_auto_title(final)
        used = _estimate_chat_tokens(final, model_key)
        limit = _ctx_limit(model_key, _role(final))
        over = used / max(limit, 1) >= float(st.load_settings().get("auto_compact_at", 0.85))
        if over and len(final.get("messages") or []) >= 3:
            await _maybe_auto_compact(final, model_key, None)
    except Exception:
        pass


async def _heuristic_title(user_text: str) -> str:
    line = (user_text or "").strip().splitlines()[0].strip()
    line = re.sub(r"\s+", " ", line)
    # drop attachment noise
    line = re.sub(r"\[System:.*?\]", "", line).strip()
    words = line.split()
    title = " ".join(words[:6]).strip(" \"'.,:;!?")
    if len(title) > 48:
        title = title[:45].rstrip() + "…"
    return title or "New chat"


async def _maybe_auto_title(chat: dict) -> str | None:
    """Title via the smallest installed model so the main GPU model is never interrupted."""
    if chat.get("title_auto") and not st.is_placeholder_title(chat.get("title")):
        return None
    msgs = chat.get("messages") or []
    user = next((m for m in msgs if m.get("role") == "user" and m.get("content")), None)
    asst = next((m for m in reversed(msgs) if m.get("role") == "assistant" and m.get("content")), None)
    if not user or not asst:
        return None
    title = None
    title_id = reg.title_model() or reg.active("chat")
    try:
        snippet = f"USER: {(user.get('content') or '')[:400]}\nASSISTANT: {(asst.get('content') or '')[:400]}"
        # num_gpu=0 means CPU only, so the resident chat model stays in VRAM
        raw = await oc.chat_once(
            title_id,
            [
                {"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": snippet},
            ],
            options={"num_ctx": 1024, "num_predict": 24, "temperature": 0.2, "num_gpu": 0},
            keep_alive="0",
        )
        body, _ = _split_thinking(raw)
        title = (body or raw).strip().splitlines()[0].strip().strip("\"'")
        title = re.sub(r"^[Tt]itle:\s*", "", title).strip()
        if len(title) > 60:
            title = title[:57] + "…"
        if not title or len(title) < 2:
            title = None
    except Exception:
        title = None
    if not title:
        title = await _heuristic_title(user.get("content") or "")
    if title and len(title) >= 2:
        chat["title"] = title
        chat["title_auto"] = True
        st.save_chat(chat)
        return title
    return None


def _uploads_url(path: Path) -> str | None:
    try:
        path = path.resolve()
        root = UPLOADS.resolve()
        if path.is_file() and (path == root or root in path.parents):
            return f"/uploads/{path.name}"
    except Exception:
        return None
    return None


def _ingest_to_uploads(src: Path) -> Path:
    """Copy an arbitrary local file into UPLOADS so the UI can serve it."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if src.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("File is larger than Olith's 25 MB attachment limit")
    dest = UPLOADS / f"{uuid.uuid4().hex[:10]}_{src.name}"
    dest.write_bytes(src.read_bytes())
    return dest


def _prepare_user_message(body: SendMessage, chat: dict | None = None) -> dict:
    parts = [body.content.strip()]
    images: list[str] = []
    attachments_meta = []
    for att in body.attachments:
        meta = {"name": att.name, "kind": att.kind, "path": att.path}
        if att.image_b64:
            raw = att.image_b64
            if "," in raw and raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            images.append(raw)
            meta["has_image"] = True
            # Persist under uploads if we only have b64 (rare) or attach URL for existing path
            if att.path:
                p = Path(att.path)
                url = _uploads_url(p)
                if not url and p.is_file():
                    try:
                        p = _ingest_to_uploads(p)
                        meta["path"] = str(p)
                        url = _uploads_url(p)
                    except Exception:
                        url = None
                if url:
                    meta["url"] = url
            else:
                try:
                    UPLOADS.mkdir(parents=True, exist_ok=True)
                    dest = UPLOADS / f"paste_{uuid.uuid4().hex[:10]}.png"
                    dest.write_bytes(base64.b64decode(raw))
                    meta["path"] = str(dest)
                    meta["url"] = _uploads_url(dest)
                except Exception:
                    pass
            parts.append(f"\n\n[Attached image: {att.name or 'image'}]")
        elif att.path:
            p = Path(att.path)
            # Keep UI/chat transcript clean: path pointer only (no full file dump).
            if att.kind in ("image", "screenshot") or p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                try:
                    if not _uploads_url(p) and p.is_file():
                        p = _ingest_to_uploads(p)
                        meta["path"] = str(p)
                    images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
                    meta["has_image"] = True
                    meta["kind"] = att.kind if att.kind in ("image", "screenshot") else "image"
                    url = _uploads_url(p)
                    if url:
                        meta["url"] = url
                    parts.append(f"\n\n[Attached image: {att.name or p.name}]")
                except Exception as e:
                    parts.append(f"\n\n[Attached image failed: {att.name}: {e}]")
            else:
                if p.is_file():
                    meta["size"] = p.stat().st_size
                    meta["kind"] = att.kind or "file"
                elif p.is_dir():
                    meta["kind"] = "folder"
                parts.append("\n\n" + _attachment_pointer(att.name, meta.get("kind") or att.kind, p))
        elif att.text:
            # Explicit pasted snippets only. Keep them short in the visible message.
            snippet = att.text[:500]
            parts.append(f"\n\n--- {att.name} (excerpt) ---\n{snippet}" + ("…" if len(att.text) > 500 else ""))
            meta["text_chars"] = len(att.text)
        attachments_meta.append(meta)

    raw_q = body.content.strip()
    decision = None
    if raw_q:
        decision = sr.route_search(
            raw_q,
            messages=(chat or {}).get("messages") or [],
            last_search_query=(chat or {}).get("last_search_query") or "",
            force_on=bool(body.web_search),
            agentic=_role(chat or {}) == "agentic",
        )
    if decision and decision.search and decision.query:
        try:
            pack = search_web.run_web_research(decision.query, max_results=6)
            pack["router_reason"] = decision.reason
            parts.append("\n\n" + search_web.format_search_context(pack))
            if chat is not None:
                chat["last_search_query"] = decision.query
        except Exception as e:
            parts.append(f"\n\n[web search failed: {e}]")

    msg = {
        "role": "user",
        "content": "\n".join(parts).strip(),
        "ts": time.time(),
        "attachments": attachments_meta,
    }
    if images:
        msg["images"] = images
    return msg


@app.post("/api/chats/{cid}/send")
async def api_send(cid: str, body: SendMessage):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)

    # Model picker / computer-use toggle
    if _is_known_model_key(body.model_key):
        mk = body.model_key
        if _requested_role(body, chat) == "agentic":
            chat["mode"] = "agentic"
            chat["model_id"] = _agent_model_choice(body.agent_model or mk)
            chat["computer_use"] = bool(body.computer_use)
            chat["reasoning"] = False
        else:
            chat["mode"] = "chat"
            chat["computer_use"] = False
            chat["reasoning"] = bool(body.reasoning)
            chat["model_id"] = mk
    else:
        if body.reasoning is not None and (chat.get("mode") or "chat") == "chat":
            chat["reasoning"] = body.reasoning
        if chat.get("mode") in ("agentic", "code", "computer"):
            chat["mode"] = "agentic"
            chat["computer_use"] = bool(body.computer_use)

    if body.project_id is not None:
        chat["project_id"] = body.project_id

    if body.effort:
        chat["effort"] = body.effort
    if body.think is not None:
        chat["think"] = bool(body.think)

    # Resume after stop / open plan: treat the next user message as a course-correction
    # on the SAME job (do not restart; do not re-dump huge attachments).
    continuing_job = bool(chat.get("interrupted")) or agent.todos_incomplete(chat)
    if chat.get("interrupted"):
        chat["interrupted"] = False

    user_msg = _prepare_user_message(body, chat)
    # Ensure message ids for later edit/delete
    if not user_msg.get("id"):
        user_msg["id"] = uuid.uuid4().hex[:10]

    # Persist attaches for the whole chat + route whether to read them this turn
    new_atts = list(user_msg.get("attachments") or [])
    lib = _sync_file_library(chat, new_atts)
    decision = ar.route_attachment_read(
        body.content,
        lib,
        new_attachments=new_atts,
    )
    # Mid-job corrections often say "if you read the doc", which must not force a re-read.
    if continuing_job and not new_atts and decision.read and decision.reason != "new_attach_vague":
        decision = ar.AttachmentDecision(False, reason="skip_reread_on_continue")
    model_key_early = _model_key(chat, model_key=body.model_key)
    if decision.read:
        user_msg["load_files"] = decision.paths
        nudge = ar.format_read_nudge(decision, agentic=(_role(chat) == "agentic"))
        if nudge:
            user_msg["content"] = (user_msg.get("content") or "") + nudge

    if continuing_job:
        if _is_simple_resume(body.content):
            user_msg["resume_job"] = True
        else:
            user_msg["course_correct"] = True

    intents = memory_router.classify(body.content)
    memory_events: list[dict] = []
    if decision.read:
        memory_events.append({
            "type": "file_router",
            "reason": decision.reason,
            "paths": decision.paths,
            "names": decision.names,
        })

    # Explicit lasting-memory STORE only (remember / memorize / store this…)
    if intents.store_memory:
        note = memory_router.extract_store_text(body.content)
        if note:
            scope = "global"
            if chat.get("project_id") and re.search(r"\bproject\b", body.content, re.I):
                scope = f"project_{chat['project_id']}"
            st.add_memory_note(scope, note)
            memory_events.append({"type": "memory_saved", "scope": scope, "text": note})
            user_msg["content"] = (
                (user_msg.get("content") or "")
                + f"\n\n[System: saved to lasting memory ({scope}): {note}]"
            )

    # Explicit memory RECALL
    if intents.recall_memory:
        hits = memory_router.search_memory_notes(body.content, chat.get("project_id"))
        user_msg["content"] = (user_msg.get("content") or "") + "\n\n" + memory_router.format_memory_recall(hits)
        memory_events.append({"type": "memory_recall", "hits": len(hits)})

    # Cross-chat excerpts only when asked (or title named)
    related = _related_chats_block(body.content, exclude_id=cid)
    if related:
        user_msg["content"] = (user_msg.get("content") or "") + related

    chat.setdefault("messages", []).append(user_msg)
    st.ensure_message_ids(chat)
    st.save_chat(chat)
    model_key = _model_key(chat, model_key=body.model_key)
    if not model_key:
        raise HTTPException(409, _no_model_message(_role(chat)))
    effort = _resolve_effort(body.effort or chat.get("effort"), model_key)
    chat["effort"] = effort
    _start_new_job(chat)
    st.save_chat(chat)
    _mark_computer(chat, True)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict):
            await queue.put(ev)

        async def run():
            try:
                await asyncio.wait_for(_gen_lock.acquire(), timeout=GEN_LOCK_TIMEOUT)
            except asyncio.TimeoutError:
                await emit({
                    "type": "error",
                    "message": (
                        "Olith is still busy with a previous response and did not free up. "
                        "Press Stop, or restart Olith if this keeps happening."
                    ),
                })
                await queue.put(None)
                return
            try:
                try:
                    await emit({"type": "user_message", "message": user_msg})
                    for mev in memory_events:
                        await emit(mev)
                    chat_local = st.get_chat(cid)
                    chat_local, _ = await _maybe_auto_compact(chat_local, model_key, emit)
                    meta = await oc.ensure_model_loaded(model_key, meta=_meta(model_key, _role(chat_local)))
                    await emit({"type": "status", "model": meta, "key": model_key, "effort": effort})
                    if _role(chat_local) == "agentic":
                        async for ev in _agent_loop(
                            chat_local, meta, model_key, effort, think=chat_local.get("think")
                        ):
                            await emit(ev)
                    else:
                        async for ev in _plain_stream(chat_local, meta, model_key, effort, think=chat_local.get("think")):
                            await emit(ev)
                    final = st.get_chat(cid)
                    used = _estimate_chat_tokens(final, model_key)
                    limit = _ctx_limit(model_key, _role(chat))
                    # Unlock the client now: title and compact must not block sending
                    await emit({
                        "type": "done",
                        "chat_id": cid,
                        "context": {
                            "used": used,
                            "limit": limit,
                            "pct": round(100 * used / max(limit, 1), 1),
                        },
                    })
                    # Fire-and-forget: never blocks the next user message / GPU model
                    asyncio.create_task(_post_reply_jobs(cid, model_key))
                except asyncio.CancelledError:
                    try:
                        stopped = st.get_chat(cid) or chat_local
                        stopped["interrupted"] = True
                        st.save_chat(stopped)
                        await emit({
                            "type": "stopped",
                            "interrupted": True,
                            "todos": agent.todos_progress(stopped.get("agent_todos")),
                        })
                    except Exception:
                        await emit({"type": "stopped"})
                    raise
                except Exception as e:
                    await emit({"type": "error", "message": str(e)})
                finally:
                    _mark_computer(None, False)
                    await queue.put(None)
            finally:
                _gen_lock.release()

        task = asyncio.create_task(run())
        _active_gens[cid] = task
        finished_ok = False
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    finished_ok = True
                    break
                yield _sse(ev)
        finally:
            _active_gens.pop(cid, None)
            # Only cancel if the client disconnected mid-stream
            if not finished_ok and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _empty_reply_reason(meta: dict, final_chunk: dict) -> str:
    """Explain an empty reply from a model without a usable chat template."""
    label = meta.get("label") or meta.get("id") or "The model"
    reason = str(final_chunk.get("done_reason") or "")
    generated = final_chunk.get("eval_count")
    if reason == "stop" and isinstance(generated, int) and generated <= 1:
        return (
            f"{label} ended its turn without generating anything. That usually "
            "means it is a base or text-completion model with no chat template, "
            "so it cannot hold a conversation. Pick an instruct or chat model."
        )
    detail = f" (stopped on {reason})" if reason else ""
    return f"{label} returned an empty response{detail}. Try again, or pick another model."


async def _plain_stream(chat: dict, meta: dict, model_key: str, effort: str | None = None, think: bool | None = None):
    msgs = _build_messages(chat, model_key)
    opts = oc.model_options(meta, _num_ctx_for(chat, model_key))
    resolved_effort = _resolve_effort(effort, model_key)
    opts, effort_meta = _apply_effort(meta, opts, resolved_effort)
    if _role(chat) == "reasoning":
        msgs = list(msgs)
        msgs[0] = {
            **msgs[0],
            "content": msgs[0]["content"] + "\nUse thorough step-by-step reasoning before the final answer.",
        }
    assistant = {
        "role": "assistant",
        "content": "",
        "thinking": "",
        "model": meta["id"],
        "effort": effort,
        "ts": time.time(),
    }
    chat["messages"].append(assistant)
    st.save_chat(chat)
    full = ""
    think_flag = None
    if _role(chat) in ("chat", "reasoning"):
        # Respect UI Thinking toggle (default on when unset).
        think_flag = True if think is None else bool(think)
    elif chat.get("computer_use"):
        # VL answers belong in content for UI guidance; thinking burns tokens on screenshots
        think_flag = False if think is None else bool(think)
    # The effort tier is a ceiling: dragging the slider to "low" means no thinking
    # pass at all, whatever the per-model default was.
    if not effort_meta.get("think", True):
        think_flag = False
    # Ollama 400s on think=true for a model without the capability.
    if think_flag and not meta.get("think"):
        think_flag = False
    final_chunk: dict = {}
    async for chunk in oc.stream_chat(meta["id"], msgs, keep_alive=meta["keep_alive"], options=opts, think=think_flag):
        msg = chunk.get("message") or {}
        native = msg.get("thinking") or ""
        delta = msg.get("content") or ""
        if native:
            assistant["thinking"] = (assistant.get("thinking") or "") + native
        if delta:
            full += delta
            body, parsed = _split_thinking(full)
            assistant["content"] = body
            if parsed:
                assistant["thinking"] = parsed
            yield {"type": "delta", "content": body, "thinking": assistant.get("thinking") or ""}
        elif native:
            yield {"type": "delta", "content": assistant.get("content") or "", "thinking": assistant.get("thinking") or ""}
        if chunk.get("done"):
            final_chunk = chunk
            break
    body, parsed = _split_thinking(full)
    if not (body or full).strip() and not (assistant.get("thinking") or "").strip():
        # A turn that generated nothing is not a turn: persisting it leaves a
        # blank bubble and replays an empty message into the next prompt.
        if chat.get("messages") and chat["messages"][-1] is assistant:
            chat["messages"].pop()
        st.save_chat(chat)
        yield {"type": "error", "message": _empty_reply_reason(meta, final_chunk)}
        return
    assistant["content"] = body or full
    if parsed:
        assistant["thinking"] = parsed
    st.save_chat(chat)
    yield {"type": "message", "message": assistant}


# Give approvals more time than questions. An unanswered question can
# continue with a default; a command still needs an answer.
ASK_USER_TIMEOUT = 90.0
# A denied command derails a run, so an approval the user simply has not
# noticed yet must not expire quickly. The dock keeps it visible meanwhile.
SHELL_APPROVAL_TIMEOUT = 300.0

# Set OLITH_TIMING_LOG to a path to append one JSON line of per-step timings.
_TIMING_LOG = os.environ.get("OLITH_TIMING_LOG", "")


def _allowlist_for(settings: dict, project_id: str | None) -> dict:
    """The remembered approvals for one project (plus the global ones)."""
    store = settings.get("shell_allowlist")
    if not isinstance(store, dict):
        return {"commands": [], "binaries": []}
    scope = store.get(project_id or "global") or {}
    shared = store.get("global") or {} if project_id else {}
    return {
        "commands": list(scope.get("commands") or []) + list(shared.get("commands") or []),
        "binaries": list(scope.get("binaries") or []) + list(shared.get("binaries") or []),
    }


def remember_shell_approval(project_id: str | None, command: str, scope: str) -> None:
    """Save a command or program approval for later runs."""
    if scope not in {"command", "binary"}:
        return
    settings = st.load_settings()
    store = dict(settings.get("shell_allowlist") or {})
    key = project_id or "global"
    entry = dict(store.get(key) or {})
    if scope == "command":
        values = list(entry.get("commands") or [])
        item = (command or "").strip()
    else:
        values = list(entry.get("binaries") or [])
        binaries = agent.shell_review_binaries(command)
        item = binaries[0] if len(binaries) == 1 else ""
    if item and item not in values:
        values.append(item)
        values = values[-200:]
    entry["commands" if scope == "command" else "binaries"] = values
    store[key] = entry
    st.save_settings({"shell_allowlist": store})


def _shell_gate(settings: dict, command: str, project_id: str | None = None) -> tuple[bool, str]:
    """Check whether a shell command needs approval.

    Full access skips approvals. Otherwise use the selected shell mode, falling
    back to confirm_shell_commands for old settings. Remembered approvals apply
    in safe_auto; always_ask still prompts.
    """
    if settings.get("approval_mode") == "full_access":
        return False, "Full access mode is on"
    mode = settings.get("shell_approval_mode")
    if mode not in {"always_ask", "safe_auto", "never_ask"}:
        mode = "always_ask" if settings.get("confirm_shell_commands", True) else "never_ask"
    if mode == "never_ask":
        return False, "auto-approve is on for every command"
    if mode == "safe_auto":
        verdict, why = agent.classify_shell(command)
        if verdict == "safe":
            return False, why
        allowed = _allowlist_for(settings, project_id)
        if (command or "").strip() in allowed["commands"]:
            return False, "you approved this exact command before"
        binaries = agent.shell_review_binaries(command)
        if binaries and all(binary in allowed["binaries"] for binary in binaries):
            return False, f"you always allow {', '.join(binaries)}"
        return True, why
    return True, "Olith confirms every command"


def _git_approval_why(arguments: dict) -> str:
    """Say what a repository change does, above all when it goes public."""
    options = arguments.get("options")
    options = options if isinstance(options, dict) else {}
    if arguments.get("action") != "create_repo":
        return "Repository changes require your approval"
    kind = "private" if options.get("private", True) else "PUBLIC"
    push = " and pushes this project" if options.get("publish") in (True, "true") else ""
    return f"Creates a {kind} GitHub repository{push}"


def _apply_user_answers(chat: dict, questions: list[dict], reply: dict) -> str:
    """Turn a UI answer payload into a tool result, and record it as canon."""
    answers = reply.get("answers") or {}
    if reply.get("cancelled"):
        return json.dumps({
            "cancelled": True,
            "timed_out": bool(reply.get("timed_out")),
            "note": (
                "The user did not answer. Do not ask again. Make the most reasonable "
                "assumption, state it plainly in your final debrief, and continue."
            ),
        })
    pid = chat.get("project_id")
    out = []
    for q in questions:
        qid = q.get("id") or ""
        raw = answers.get(qid, answers.get(q.get("question") or "", ""))
        if isinstance(raw, list):
            text = ", ".join(str(x) for x in raw if str(x).strip())
        else:
            text = str(raw or "").strip()
        if not text:
            continue
        out.append({"question": q.get("question"), "answer": text})
        try:
            st.record_canon(pid, q.get("question") or "", text, header=q.get("header") or "")
        except Exception:
            pass
    if not out:
        return json.dumps({
            "cancelled": True,
            "note": "No answers were provided. Assume sensible defaults and continue.",
        })
    return json.dumps({
        "answers": out,
        "note": "Recorded as canon. These decisions are settled; do not ask them again.",
    }, indent=2)


# The summary is the whole point: a sub-task that reads 40k tokens must not hand
# 40k tokens back, or delegation costs more than doing the work inline.
SUBAGENT_REPORT_MAX_CHARS = 6000

# Forward question and approval events from sub-tasks to the parent UI.
# Malformed-call retries have a per-run budget that successful calls do not reset.
MALFORMED_CALL_LIMIT = 3

_PROMPT_EVENTS = frozenset({
    "ask_user", "ask_user_done", "shell_approval", "shell_approval_done",
})


def _allowed_tool_names(chat: dict, ctx_limit: int | None = None,
                        profile: dict | None = None) -> set[str]:
    """Tools this run may actually execute, not merely the ones it was offered."""
    return {t["function"]["name"] for t in agent.tool_defs_for(chat, ctx_limit, profile)}


async def _run_subagent(
    parent: dict,
    args: dict,
    meta: dict,
    model_key: str,
    effort: str | None = None,
):
    """Run a sub-task with a fresh transcript. Forward its events and return a capped summary to the parent."""
    task = str(args.get("task") or "").strip()
    wanted = args.get("tools")
    child = {
        "id": f"{parent.get('id') or 'chat'}-sub-{uuid.uuid4().hex[:6]}",
        "mode": parent.get("mode") or "agentic",
        "project_id": parent.get("project_id"),
        "model_id": parent.get("model_id"),
        "title": f"Sub-task: {task[:48]}",
        "subagent": {
            "parent": parent.get("id"),
            "task": task,
            "tools": list(wanted) if isinstance(wanted, list) else None,
        },
        # Park sub-run prompts under the parent id. The child id is synthetic and
        # an answer posted to it would resolve nothing.
        "approval_key": parent.get("id"),
        "editor_session_id": parent.get("editor_session_id"),
        "messages": [{"role": "user", "content": task}],
    }
    yield {"type": "status", "model": {"label": "Delegating a sub-task…"}, "key": model_key}

    # Attempted, not executed: tool_start fires before the allowlist guard.
    report, tried_tools, rounds = "", [], 0
    try:
        # think=False: sub-tasks inspect and report rather than design, and the
        # thinking budget is the same allowance the tool call has to fit in.
        async for event in _agent_loop(child, meta, model_key, effort=effort, think=False):
            kind = event.get("type")
            if kind in _PROMPT_EVENTS:
                # A sub-run blocks on approvals like the parent does. Dropping
                # these leaves it waiting on a card that was never shown.
                yield event
            elif kind == "tool_start":
                tried_tools.append(event.get("name"))
                yield {
                    "type": "status",
                    "model": {"label": f"Sub-task: {event.get('name')}…"},
                    "key": model_key,
                }
            elif kind == "message":
                content = (event.get("message") or {}).get("content") or ""
                if content:
                    report = content
                    rounds = len([m for m in child["messages"] if m.get("role") == "assistant"])
    except Exception as exc:  # a failed sub-task must not kill the parent run
        yield {
            "type": "subagent_result",
            "result": json.dumps({
                "error": f"Sub-task failed: {exc}",
                "hint": "Do this work directly with your own tools.",
            }),
        }
        return

    truncated = len(report) > SUBAGENT_REPORT_MAX_CHARS
    payload = {
        "delegated": True,
        "task": task[:400],
        "report": report[:SUBAGENT_REPORT_MAX_CHARS] or
                  "The sub-task produced no findings.",
        "tools_attempted": tried_tools[:20],
        "rounds": rounds,
        "transcript": child["id"],
    }
    if truncated:
        payload["note"] = (
            "The sub-task's report was longer than the delegation budget and was "
            "cut. Delegate a narrower question if you need the rest."
        )
    yield {"type": "subagent_result", "result": json.dumps(payload)}


async def _agent_loop(chat: dict, meta: dict, model_key: str, effort: str | None = None, think: bool | None = None):
    project = st.get_project(chat["project_id"]) if chat.get("project_id") else None
    root = Path(project["root"]).resolve() if project else None
    review = None
    commands_context = ""
    if root and root.is_dir() and not chat.get("subagent"):
        chat.pop("verification", None)
        chat.pop("change_review", None)
        yield {"type": "status", "model": {"label": "Preparing project tools…"}, "key": model_key}
        try:
            commands = await asyncio.to_thread(project_commands.discover, root)
            commands_context = project_commands.prompt(commands)
            if st.load_settings().get("task_change_review", True):
                review = await asyncio.to_thread(task_changes.ChangeSet(root).start)
                chat["change_set_id"] = review.ident
                st.save_chat(chat)
        except Exception as exc:
            commands_context += f"\nProject preparation unavailable: {exc}"
    try:
        async for event in _agent_loop_body(chat, meta, model_key, effort, think,
                                            review=review, commands_context=commands_context):
            yield event
    finally:
        if review:
            try:
                result = await asyncio.to_thread(review.finish, True)
                chat["change_review"] = {"id": result["id"], "files": len(result["files"]), "partial": result["partial"]}
                st.save_chat(chat)
            except Exception as exc:
                chat["change_review"] = {"id": review.ident, "error": str(exc)}
                st.save_chat(chat)
    if review:
        yield {"type": "change_review", **chat["change_review"]}


async def _agent_loop_body(chat: dict, meta: dict, model_key: str, effort: str | None = None,
                           think: bool | None = None, *, review=None, commands_context=""):
    """Run tools and return their results until the model finishes or the tool budget is spent.

    Repeated calls and stalled replies have separate guards.
    """
    settings = st.load_settings()
    repeat_limit = max(2, int(settings.get("agent_repeat_limit", 3)))
    plan_context = agent.todos_prompt_block(chat)
    # Identical tool calls repeated across steps mean the model is spinning, not
    # working. The circuit breaker is independent of productive step count.
    call_counts: dict[str, int] = {}
    # Structured rejections keyed by call signature, so a repeat of a call the
    # tool refused is answered with the constraint it violated.
    call_rejections: dict[str, dict] = {}
    repeat_stalls = 0
    no_action_stalls = 0
    # Desktop actions in a row that produced no visible change.
    computer_stalls = 0
    # The last desktop action that was not a look, how many times running, and
    # how often it has been refused.
    last_desktop_action: str | None = None
    desktop_repeats = 0
    desktop_blocks = 0
    # Looks since anything was last done, and how often that has been refused.
    looks_in_a_row = 0
    look_blocks = 0
    # Whether the previous failed sample ran out of clock (as opposed to
    # returning nothing). The two need opposite recovery budgets.
    last_stall_timed_out = False
    # Whether this run has written anything yet. ask_user is allowed before the
    # first mutation and refused after it.
    mutated_this_run = False
    verified_revision = ""
    plan_prose_stalls = 0
    plan_repeat_recoveries = 0
    force_action_next = False
    malformed_stalls = 0
    rejection_nudge = ""
    plan_checkpoint_due = False
    # Resume an interrupted plan on the first sample, without waiting for the
    # model to defer once more first.
    plan_continuation_due = agent.todos_incomplete(chat)
    latest_user = next(
        (
            message
            for message in reversed(chat.get("messages") or [])
            if message.get("role") == "user"
        ),
        {},
    )
    # Do not resume with the same oversized replay that just exhausted context.
    # Six turns hold a typical set of source slices and still leave room.
    if (
        agent.todos_incomplete(chat)
        and (
            latest_user.get("course_correct")
            or latest_user.get("resume_job")
            or _is_simple_resume(latest_user.get("content") or "")
        )
    ):
        _narrow_if_crowded(chat, model_key, 6)
    # Let the model inspect files and finish its plan without extra read-count gates.
    step_timings: list[dict] = []
    work_log: list[str] = []
    proj = st.get_project(chat["project_id"]) if chat.get("project_id") else None
    project_root = Path(proj["root"]) if proj else None
    # Only worth offering where surveying is expensive. Decided once per run,
    # since tool_defs_for cannot reach the project root itself.
    chat["delegate_available"] = agent.project_is_large(project_root)
    # One model per run. Swapping a planner for an executor mid-run costs a full
    # unload and reload, because two 17-19GB models cannot share a 24GB card.
    active_key, active_meta = model_key, meta
    # num_ctx is deliberately not computed here. Sizing the KV cache once from
    # the opening prompt caps long runs and sends them re-exploring.
    agent_effort = _resolve_effort(effort, model_key)
    # Clarify-first: nudge via the system prompt (not a fake user turn) until the
    # model actually calls ask_user, then stop.
    clarify_first = bool(settings.get("clarify_first_turn")) and not chat.get("asked_this_job")
    from config import EFFORT as _EFFORT
    # Agentic thinking follows the effort tier, not the chat toggle, which
    # defaults off and left every run reasoning-free.
    think_flag = bool(meta.get("think")) and bool((_EFFORT.get(agent_effort) or {}).get("think", True))
    tool_budget = int(settings.get("agent_tool_budget", 80))
    # 24K was tested and was no faster; a cut-off step's reasoning is salvaged instead.
    thinking_reply_tokens = int(settings.get("agent_thinking_reply_tokens", THINKING_REPLY_TOKENS))
    salvaged_reasoning = ""
    ledger = evidence.Ledger(project_root)
    honesty_checked = False
    tool_calls_run = 0
    wrapping_up = False
    verify_rounds = 0
    # The latest result message per call signature, to tell whether it is still in the prompt.
    result_for: dict[str, dict] = {}

    def _log(line: str) -> str:
        line = (line or "").strip()
        if not line:
            return "\n".join(work_log)
        work_log.append(line)
        # Cap growth so the dropdown stays usable
        if len(work_log) > 200:
            del work_log[:-180]
        return "\n".join(work_log)

    # Build once per run, outside message rendering. A file edit must not rewrite
    # the prefix on every step and force a full Ollama prompt evaluation.
    repo_context = commands_context
    if project_root and settings.get("repo_map_enabled", True):
        yield {"type": "status", "model": {"label": "Mapping project…"}, "key": active_key}
        try:
            budget = min(int(settings.get("repo_map_tokens", 1400)), _ctx_limit(active_key, "agentic") // 40)
            index = repo_index.ProjectIndex(agent.resolve_path(str(project_root), project_root))
            repo_context += "\n\n" + await asyncio.to_thread(index.repo_map, latest_user.get("content") or "", budget)
        except Exception as exc:
            _log(f"[context] Repository map unavailable: {exc}")

    for step in itertools.count():
        # A budget, not a gate: it never refuses a move, it ends the sub-run and
        # hands the parent whatever was learned.
        if chat.get("subagent") and step >= agent.SUBAGENT_MAX_STEPS:
            # A fresh message: `assistant` from the previous iteration is already
            # in chat["messages"], so reusing it would append a duplicate.
            capped = {
                "role": "assistant",
                "content": (
                    f"Sub-task budget reached after {agent.SUBAGENT_MAX_STEPS} rounds "
                    "without a conclusion. Findings so far are in the tool results above."
                ),
                "thinking": "\n".join(work_log),
                "model": active_meta["id"],
                "ts": time.time(),
                "interim": False,
            }
            chat["messages"].append(capped)
            st.save_chat(chat)
            yield {"type": "message", "message": capped}
            return
        if step == 0 and (chat.get("agent_todos") or []):
            yield {"type": "todos", **agent.todos_progress(chat.get("agent_todos"))}
        action_only = force_action_next
        force_action_next = False
        # Recovery already gets the full window and a larger reply budget, so
        # only narrow replay when the prompt really is crowding the context.
        if action_only:
            _narrow_if_crowded(chat, active_key, max(2, 6 - 2 * no_action_stalls))
        # Render once per step and reuse it for the budget checks. Between two
        # boundaries this render is append-only, so the KV cache holds.
        msgs = _build_messages(
            chat,
            active_key,
            clarify_first=clarify_first,
            repo_context=repo_context,
            plan_context=plan_context,
        )
        def _rerender():
            return _build_messages(
                chat,
                active_key,
                clarify_first=clarify_first,
                repo_context=repo_context,
                plan_context=plan_context,
            )

        # Progressive reduction, cheapest first: drop older tool detail, and only
        # summarize if that still leaves the prompt over the limit.
        if _advance_detail_horizon(chat, active_key, msgs=msgs):
            _log("[context] dropped older tool detail to keep the prompt small")
            msgs = _rerender()
        _, compacted = await _maybe_auto_compact(chat, active_key, None, msgs=msgs)
        if compacted:
            _log("[context] compacted earlier turns into a summary")
            msgs = _rerender()
        # Report the window every step, so the meter fills as the run goes
        # instead of jumping at the end. Taken from the prompt just rendered.
        step_used = _estimate_chat_tokens(chat, active_key, msgs=msgs)
        step_limit = _ctx_limit(active_key, "agentic")
        step_profile = _profile(active_key)
        yield {
            "type": "context",
            "used": step_used,
            "limit": step_limit,
            "pct": round(100 * step_used / max(step_limit, 1), 1),
            "key": active_key,
        }
        # Directives never go in msgs[0]. Editing the system prompt changes the
        # prefix every step and forces the whole conversation to be reprocessed.
        directives: list[str] = []
        if plan_checkpoint_due:
            directives.append(
                "[agent-loop] A project mutation just succeeded. If the current plan milestone "
                "is now complete, call todo_write before starting the next phase. Complete only that "
                "one milestone; Olith promotes the next pending item automatically. If the same "
                "milestone genuinely needs more work, continue it."
            )
            plan_checkpoint_due = False
        if plan_continuation_due:
            active = agent.todos_progress(chat.get("agent_todos")).get("active") or {}
            directives.append(
                "[agent-loop] OPEN PLAN, CONTINUE AUTONOMOUSLY. Do not ask the user which "
                "todo to do; the plan already determines the order. Call the next required tool "
                f"for the active milestone now: {active.get('id')}: {active.get('content')}. "
                "If implementation is complete, "
                "update only the active todo, then continue with the automatically promoted item. "
                "If a tool call was rejected repeatedly, change strategy or split that operation "
                "into valid smaller calls yourself; do not return that choice to the user."
            )
            plan_continuation_due = False
        if rejection_nudge:
            directives.append(rejection_nudge)
            rejection_nudge = ""
        if not chat.get("subagent") and not wrapping_up and tool_calls_run >= tool_budget:
            wrapping_up = True
            directives.append(
                f"[agent-loop] Tool budget reached ({tool_budget} calls). Stop now and write your "
                "final debrief: what is done, what is not, and what you would try next."
            )
        if action_only:
            # A large native tool call is invisible until its JSON is complete.
            # Tell the model to stop narrating and emit the action.
            directives.append(agent.ACTION_ONLY_NUDGE)
        if salvaged_reasoning:
            directives.append(_salvage_directive(salvaged_reasoning))
            salvaged_reasoning = ""
        if directives:
            # Put per-step instructions on the newest tool result. Qwen moves system
            # messages to the front, which would invalidate the cached prefix.
            text = "\n\n".join(directives)
            if msgs and msgs[-1].get("role") == "tool":
                tail = dict(msgs[-1])
                tail["content"] = (tail.get("content") or "") + "\n\n" + text
                msgs[-1] = tail
            else:
                msgs.append({"role": "user", "content": text})
        # Re-size the KV cache for what we are actually about to send this step.
        thinking_step = bool(think_flag and not action_only)
        # Tool schemas share the window, so size for them as well as the reply.
        schema_tokens = oc.estimate_tokens(json.dumps(agent.tool_defs_for(chat, step_limit, step_profile), ensure_ascii=False))
        opts = oc.model_options(active_meta, _num_ctx_for(
            chat, active_key, msgs=msgs,
            reply_tokens=thinking_reply_tokens + schema_tokens + 1024 if thinking_step else None))
        opts, _ = _apply_effort(active_meta, opts, agent_effort)
        if thinking_step:
            room = int(opts["num_ctx"]) - oc.estimate_messages_tokens(msgs) - schema_tokens - 1024
            opts["num_predict"] = max(int(opts["num_predict"]), min(thinking_reply_tokens, room))
        if action_only:
            # Shrinking a retry that already hit the token limit guarantees
            # another truncation. Retry in the full configured context.
            recovery_ctx = max(int(opts.get("num_ctx") or 0), _ctx_limit(active_key, "agentic"))
            opts["num_ctx"] = recovery_ctx
            # Size against the reduced recovery prompt, not the full ten-turn
            # replay that was intentionally discarded above.
            prompt_tokens = oc.estimate_messages_tokens(msgs)
            prompt_tokens += oc.estimate_tokens(
                json.dumps(agent.tool_defs_for(chat, step_limit, step_profile), ensure_ascii=False)
            ) + 384
            available = max(2048, recovery_ctx - prompt_tokens - 1024)
            current_predict = int(opts.get("num_predict") or 8192)
            recovery_multiplier = 2 ** max(1, min(no_action_stalls, 2))
            target_predict = min(
                24576,
                max(current_predict, current_predict * recovery_multiplier),
            )
            opts["num_predict"] = min(target_predict, available)
            seen = chat.setdefault("num_ctx_used", {})
            if isinstance(seen, dict):
                seen[active_key] = recovery_ctx
        assistant = {
            "role": "assistant",
            "content": "",
            "thinking": "\n".join(work_log),
            "model": active_meta["id"],
            "tool_calls": [],
            "ts": time.time(),
            "step": step,
            "effort": effort,
            "interim": True,
        }
        chat["messages"].append(assistant)
        full = ""
        native_think = ""
        native_calls: list = []
        final_chunk: dict[str, Any] = {}
        base_sample_timeout = max(30, int(settings.get("agent_sample_timeout", 150)))
        # Give timed-out replies more time. Shrink the budget only for empty replies.
        # An 8192-token reply needs about 99 seconds at 82.6 tokens per second.
        reply_budget = int(opts.get("num_predict") or 2048)
        # Conservative floor: half the measured rate, plus room for prompt eval.
        generation_floor = int(reply_budget / 40) + 60
        if last_stall_timed_out or think_flag:
            sample_timeout = max(base_sample_timeout, generation_floor)
        else:
            sample_timeout = max(60, base_sample_timeout - no_action_stalls * 30)
        # Splitting time-to-first-token from generation says whether a slow run
        # is paying for reprocessed context or for tokens.
        step_started = time.monotonic()
        try:
            async with asyncio.timeout(sample_timeout):
                async for chunk in oc.stream_chat(
                    active_meta["id"],
                    msgs,
                    keep_alive=active_meta["keep_alive"],
                    options=opts,
                    tools=None if wrapping_up else agent.tool_defs_for(chat, _ctx_limit(active_key, "agentic"), step_profile),
                    think=thinking_step,
                ):
                    final_chunk = chunk
                    msg = chunk.get("message") or {}
                    if msg.get("tool_calls"):
                        # Calls can arrive across chunks. Replacing this list
                        # discards all but the last.
                        native_calls.extend(msg["tool_calls"])
                    native = msg.get("thinking") or ""
                    if native:
                        native_think += native
                    delta = msg.get("content") or ""
                    if delta:
                        full += delta
                    if native or delta:
                        body, parsed = _split_thinking(full)
                        think_view = "\n".join(work_log)
                        extra = (native_think or parsed or "").strip()
                        if extra:
                            think_view = (think_view + "\n" + extra).strip() if think_view else extra
                        assistant["thinking"] = think_view
                        assistant["content"] = body  # held privately until final
                        yield {
                            "type": "delta",
                            "content": "",
                            "thinking": think_view,
                            "step": step,
                            "quiet": True,
                        }
                    if chunk.get("done"):
                        break
        except asyncio.TimeoutError:
            final_chunk = {
                "done": True,
                "done_reason": f"sample timeout after {sample_timeout}s",
            }

        step_elapsed = time.monotonic() - step_started

        def _secs(key: str) -> float:
            raw = final_chunk.get(key)
            return round(raw / 1e9, 2) if isinstance(raw, (int, float)) else 0.0

        # Ollama's counters, not wall clock: tool calls arrive as one chunk at
        # the end, so measured time-to-first-token includes the generation.
        prompt_evaluated = final_chunk.get("prompt_eval_count")
        step_stats = {
            "step": step,
            "model": active_meta["id"],
            # Tools are sent beside the messages and occupy the same window, so
            # a count that omits them is not what the model receives.
            "prompt_tokens_sent": (
                oc.estimate_messages_tokens(msgs)
                + oc.estimate_tokens(json.dumps(agent.tool_defs_for(chat, step_limit, step_profile), ensure_ascii=False))
                + 128
            ),
            # Use prompt_eval_s to check cache reuse. prompt_eval_count includes
            # the full prompt even when cached tokens were reused.
            "prompt_eval_count": prompt_evaluated,
            "prompt_eval_s": _secs("prompt_eval_duration"),
            "eval_count": final_chunk.get("eval_count"),
            "eval_s": _secs("eval_duration"),
            "load_s": _secs("load_duration"),
            "total_s": round(step_elapsed, 2),
            "num_ctx": opts.get("num_ctx"),
        }
        step_timings.append(step_stats)
        record_estimate_accuracy(
            chat, active_key, step_stats["prompt_tokens_sent"], prompt_evaluated
        )
        _log(
            f"[timing] step {step}: sent~{step_stats['prompt_tokens_sent']}tok "
            f"prompt={prompt_evaluated}tok ({step_stats['prompt_eval_s']}s"
            f"{'' if step_stats['prompt_eval_s'] < 5 else ' CACHE MISS'}) "
            f"gen={step_stats['eval_count']} ({step_stats['eval_s']}s) "
            f"total={step_stats['total_s']}s"
        )
        if _TIMING_LOG:
            try:
                with open(_TIMING_LOG, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(step_stats) + "\n")
            except OSError:
                pass

        body, parsed = _split_thinking(full)
        content = (body or full or "").strip()
        if agent.is_internal_continuation_placeholder(content):
            content = ""
            _log("[loop guard] discarded an internal continuation placeholder")
        if native_think:
            _log(native_think.strip()[:6000])
        if parsed:
            _log(parsed[:4000])
        assistant["native_thinking"] = (native_think or parsed or "").strip()
        assistant["thinking"] = "\n".join(work_log)
        calls = agent.extract_tool_calls({"content": content, "tool_calls": native_calls})
        calls = agent.dedupe_tool_calls(calls, limit=8)
        if wrapping_up and calls:
            calls = []
            if not content:
                content = "I hit the tool budget for this run before finishing. Changes so far are in place; say continue to keep going."
        if calls and not native_calls:
            # Weaker models write the call into the reply instead of the
            # tool_calls field. Worth seeing in the log when it happens.
            _log(f"[loop guard] recovered {len(calls)} tool call(s) from the reply body")

        assistant["tool_calls"] = [
            {
                "id": c.get("id"),
                "index": c.get("index"),
                "name": c["name"],
                "arguments": c["arguments"],
            }
            for c in calls
        ]

        if calls:
            # A native call is an action, so an earlier empty-generation retry
            # no longer counts against this turn.
            no_action_stalls = 0
            plan_prose_stalls = 0
            # Publish native tool commentary, never recovered call JSON or private thinking.
            if content:
                assistant["thinking"] = _log(f"[note] {content[:600]}")
                commentary = (body or "").strip()
                if native_calls and commentary and "<tool_call" not in commentary.lower() and not agent.parse_content_tool_calls(commentary) and not agent.looks_like_broken_tool_call(commentary):
                    assistant["commentary"] = commentary[:2000]
                    yield {"type": "commentary", "content": assistant["commentary"]}
            assistant["content"] = ""
            assistant["interim"] = True
            assistant["thinking"] = "\n".join(work_log)
            st.save_chat(chat)
            yield {"type": "message", "message": {**assistant, "content": "", "thinking": assistant["thinking"]}}
            # Appended after the loop, not inside it: a user message between
            # tool_calls and the next result breaks the sequence Ollama
            # expects.
            pending_frames: list[dict] = []
            for call in calls:
                sig = _call_signature(call, project_root)
                tool_calls_run += 1
                # Redoing a call whose result has left the prompt is not a repeat.
                earlier = result_for.get(sig)
                if earlier is not None and not _result_in_view(chat, earlier):
                    call_counts[sig] = 0
                # Do not count observations as duplicate calls; the screen may have changed.
                if not _is_a_look(call):
                    call_counts[sig] = call_counts.get(sig, 0) + 1
                if call_counts.get(sig, 0) > 2:
                    # Hand back a correction instead of the same result again.
                    repeat_stalls += 1
                    # A rejected call is a different failure from a repeated
                    # one. The model needs the constraint it violated, not a
                    # generic "stop repeating".
                    rejection = call_rejections.get(sig)
                    if rejection:
                        correction = {
                            **rejection,
                            "repeated": call_counts[sig],
                            "error": (
                                f"Rejected again. This is attempt {call_counts[sig]} with "
                                "identical arguments, so the constraint below still applies. "
                                "Do not resend this call. Change the arguments to satisfy it: "
                                f"{rejection.get('hint') or rejection.get('error') or 'see fields above'}"
                            ),
                        }
                    else:
                        correction = {
                            "error": (
                                f"You already called {call['name']} with these exact arguments "
                                f"{call_counts[sig]} times and the result has not changed. Stop "
                                "repeating it. Take the next concrete action now: write the file, "
                                "run the test, or give your final answer."
                            )
                        }
                    chat["messages"].append({
                        "role": "tool",
                        "name": call["name"],
                        "content": json.dumps(correction),
                        "ts": time.time(),
                        "quiet": True,
                    })
                    yield {
                        "type": "delta",
                        "content": "",
                        "thinking": _log(f"[loop guard] skipped repeated {call['name']} call"),
                        "quiet": True,
                    }
                    continue
                if _is_a_look(call):
                    looks_in_a_row += 1
                    if looks_in_a_row > LOOK_LIMIT:
                        look_blocks += 1
                        if look_blocks >= COMPUTER_REPEAT_BLOCKS:
                            assistant["content"] = (
                                "I spent this run looking at the screen without doing "
                                "anything with what I saw, so I stopped instead of "
                                "carrying on. Whatever is on screen is where I got to. "
                                "Tell me the one next thing you want changed and I will "
                                "do that rather than survey it again."
                            )
                            assistant["interim"] = False
                            assistant["thinking"] = "\n".join(work_log)
                            chat["interrupted"] = True
                            st.save_chat(chat)
                            yield {
                                "type": "delta",
                                "content": assistant["content"],
                                "thinking": assistant["thinking"],
                            }
                            yield {"type": "message", "message": assistant}
                            return
                        chat["messages"].append({
                            "role": "tool",
                            "name": call["name"],
                            "content": json.dumps({
                                "error": (
                                    f"Refused: {looks_in_a_row} looks in a row with nothing "
                                    "done in between."
                                ),
                                "hint": (
                                    "You have seen the screen. Looking again will show you "
                                    "the same thing, because nothing has happened since. Do "
                                    "the next step now, or if the job is finished, stop and "
                                    "write the debrief."
                                ),
                            }),
                            "ts": time.time(),
                            "quiet": True,
                        })
                        st.save_chat(chat)
                        yield {
                            "type": "delta",
                            "content": "",
                            "thinking": _log(
                                f"[loop guard] refused a {looks_in_a_row}th look with no "
                                f"action between"
                            ),
                            "quiet": True,
                        }
                        continue
                else:
                    looks_in_a_row = 0
                    look_blocks = 0
                # Count repeated Return presses directly; each adds a line and changes the frame.
                repeat_key = (
                    _computer_repeat_key(call["arguments"], call["name"])
                    if call["name"] in _WORLD_TOOLS else None
                )
                if repeat_key is not None:
                    if repeat_key == last_desktop_action:
                        desktop_repeats += 1
                    else:
                        last_desktop_action = repeat_key
                        desktop_repeats, desktop_blocks = 1, 0
                    if desktop_repeats >= COMPUTER_REPEAT_LIMIT:
                        # Not reset here: every further repeat of the same
                        # action is refused until a different one arrives.
                        desktop_blocks += 1
                        if desktop_blocks >= COMPUTER_REPEAT_BLOCKS:
                            # Told three times and still sending it. The stall
                            # counter cannot end this, since a screenshot
                            # between two presses resets it.
                            assistant["content"] = (
                                "I kept sending the same thing to the same place and it was "
                                "not getting the job done, so I stopped rather than carry "
                                "on pressing it. Whatever is on screen now is where I got "
                                "to; ctrl+z will undo anything I typed that you do not "
                                "want. Tell me what it should look like and I will take a "
                                "different route."
                            )
                            assistant["interim"] = False
                            assistant["thinking"] = "\n".join(work_log)
                            chat["interrupted"] = True
                            st.save_chat(chat)
                            yield {
                                "type": "delta",
                                "content": assistant["content"],
                                "thinking": assistant["thinking"],
                            }
                            yield {"type": "message", "message": assistant}
                            return
                        chat["messages"].append({
                            "role": "tool",
                            "name": call["name"],
                            "content": json.dumps({
                                "error": (
                                    f"Refused: this is the same action on the screen "
                                    f"{desktop_repeats} times in a row."
                                ),
                                "hint": (
                                    "Repeating it again will not help, and it will be "
                                    "refused again. Take a screenshot, read what is "
                                    "actually on screen, and either do something different "
                                    "or stop and report. To undo a mess use ctrl+z; to "
                                    "repeat a key or a scroll on purpose use count or "
                                    "amount in one call."
                                ),
                            }),
                            "ts": time.time(),
                            "quiet": True,
                        })
                        st.save_chat(chat)
                        yield {
                            "type": "delta",
                            "content": "",
                            "thinking": _log(
                                f"[loop guard] refused desktop action repeated "
                                f"{desktop_repeats} times"
                            ),
                            "quiet": True,
                        }
                        continue
                # tool_start already drives the status line. Logging the activity
                # line here as well printed every tool twice in the thinking log.
                if call["name"] in {"run_shell", "connection_call"}:
                    await asyncio.to_thread(ledger.before_shell, str(call["arguments"].get("command") or ""))
                yield {"type": "tool_start", "name": call["name"], "arguments": call["arguments"]}
                if call["name"] == "computer":
                    # The person watching their own cursor move gets to read
                    # what it is doing, on the desktop, not only in the app.
                    computer_overlay.set_label(computer.status_line(call["arguments"]))
                elif call["name"] == "browser":
                    computer_overlay.set_label(
                        f"Olith is using the browser ({call['arguments'].get('action')})"
                    )
                # Withholding a schema is advice, not enforcement: run_tool
                # dispatches on name, so a withheld tool still executes.
                if call["name"] not in _allowed_tool_names(chat, step_limit, step_profile):
                    result = json.dumps({
                        "error": f"{call['name']} is not available in this sub-task.",
                        "available": sorted(_allowed_tool_names(chat, step_limit, step_profile)),
                        "hint": "Use one of the available tools, or report what you have found.",
                    })
                elif call["name"] == "run_shell" and agent._rewrite_shell_guard(call["arguments"].get("command") or "", project_root, chat):
                    result = json.dumps(agent._rewrite_shell_guard(call["arguments"].get("command") or "", project_root, chat))
                elif call["name"] == "delegate":
                    # Run the sub-task with its own transcript and return only its summary.
                    # Execution stays sequential on the same model.
                    task = str((call.get("arguments") or {}).get("task") or "").strip()
                    if chat.get("subagent"):
                        result = json.dumps({
                            "error": "A sub-agent cannot delegate further.",
                            "hint": "Do this work directly with your own tools.",
                        })
                    elif not task:
                        result = json.dumps({
                            "error": "delegate needs a self-contained `task`.",
                        })
                    else:
                        result = ""
                        async for sub_event in _run_subagent(
                            chat, call.get("arguments") or {}, active_meta, active_key,
                            effort=effort,
                        ):
                            if sub_event.get("type") == "subagent_result":
                                result = sub_event["result"]
                            else:
                                yield sub_event
                        yield {"type": "status", "model": {"label": "Working…"}, "key": active_key}
                elif call["name"] == "ask_user" and mutated_this_run:
                    # Questions belong at the start or not at all. Asking mid-run
                    # strands a half-built change behind a prompt.
                    result = json.dumps({
                        "error": "Too late to ask; you have already started writing files.",
                        "hint": (
                            "Make the most reasonable assumption, finish the job, and state "
                            "what you assumed in your final debrief."
                        ),
                    })
                elif call["name"] == "ask_user":
                    # The only blocking tool: park the loop, hand the questions to the
                    # UI, and wait for a real answer before doing anything else.
                    questions = agent.normalize_questions(call["arguments"])
                    if not questions:
                        result = json.dumps({
                            "error": "ask_user needs at least one question with 2+ options.",
                        })
                    else:
                        answer_key = _approval_key(chat)
                        ask_event = {
                            "type": "ask_user",
                            "questions": questions,
                            "chat_id": answer_key,
                        }
                        fut = _park_prompt(answer_key, ask_event)
                        yield dict(ask_event)
                        yield {"type": "status", "model": {"label": "Waiting for you…"}, "key": active_key}
                        try:
                            reply = await asyncio.wait_for(fut, timeout=ASK_USER_TIMEOUT)
                        except asyncio.TimeoutError:
                            reply = {"answers": {}, "cancelled": True, "timed_out": True}
                        finally:
                            _pending_questions.pop(answer_key, None)
                        result = _apply_user_answers(chat, questions, reply)
                        clarify_first = False
                        chat["asked_this_job"] = True
                        yield {"type": "ask_user_done", "cancelled": bool(reply.get("cancelled"))}
                        yield {"type": "status", "model": {"label": "Working…"}, "key": active_key}
                elif call["name"] == "connection_call" and chat.get("subagent"):
                    result = json.dumps({"error": "Direct service connections are unavailable in sub-tasks"})
                elif call["name"] == "connection_call" and mcp_connections.preflight(call["arguments"]):
                    result = json.dumps(mcp_connections.preflight(call["arguments"]))
                elif settings.get("approval_mode") != "full_access" and ((call["name"] == "connection_call" and mcp_connections.requires_approval(call["arguments"])) or (call["name"] == "git_status" and call["arguments"].get("action", "status") not in github_tools.READ_ACTIONS) or (call["name"] == "run_shell" and _shell_gate(
                    settings,
                    str(call.get("arguments", {}).get("command") or ""),
                    chat.get("project_id"),
                )[0])):
                    git_action = call["name"] == "git_status"
                    direct_action = call["name"] == "connection_call"
                    direct_ticket = mcp_connections.approval_ticket(call["arguments"]) if direct_action else None
                    command = (("Service action " if direct_action else "Git/GitHub ") + json.dumps(call["arguments"], ensure_ascii=False)) if git_action or direct_action else str(call.get("arguments", {}).get("command") or "").strip()
                    why = "This service tool can change data or does not declare itself read-only." if direct_action else _git_approval_why(call["arguments"]) if git_action else _shell_gate(settings, command, chat.get("project_id"))[1]
                    binaries = [] if git_action or direct_action else agent.shell_review_binaries(command)
                    answer_key = _approval_key(chat)
                    # A dedicated event, not an ask_user card: an approval is a
                    # yes/no on one command.
                    approval_event = {
                        "type": "shell_approval",
                        "chat_id": answer_key,
                        "command": command[:2000],
                        "binary": binaries[0] if len(binaries) == 1 else "",
                        "rememberable": not (git_action or direct_action),
                        "why": why,
                    }
                    fut = _park_prompt(answer_key, approval_event)
                    yield dict(approval_event)
                    yield {"type": "status", "model": {"label": "Waiting for approval…"}, "key": active_key}
                    try:
                        reply = await asyncio.wait_for(fut, timeout=SHELL_APPROVAL_TIMEOUT)
                    except asyncio.TimeoutError:
                        reply = {"answers": {}, "cancelled": True, "timed_out": True}
                    finally:
                        _pending_questions.pop(answer_key, None)
                    approved = (
                        not reply.get("cancelled")
                        and reply.get("answers", {}).get("shell_approval") == "Run command"
                    )
                    if approved:
                        if not (git_action or direct_action):
                            remember_shell_approval(chat.get("project_id"), command, str(reply.get("remember") or ""))
                        settings = st.load_settings()
                        if direct_action:
                            result = json.dumps(await _index_work(mcp_connections.call, call["arguments"], approved=True, ticket=direct_ticket))
                        else:
                            result = await _index_work(agent.run_tool,
                                call["name"], call["arguments"], project_root,
                                chat.get("project_id"), chat=chat,
                            )
                    elif reply.get("timed_out"):
                        # Reporting an unanswered prompt as a refusal sent the
                        # model looking for a reason the user never gave.
                        result = json.dumps({
                            "error": "Approval request expired without an answer",
                            "command": command[:400],
                            "hint": (
                                "Nobody answered the prompt; this is not a refusal. If another "
                                "tool can do the same job, use it. Otherwise continue with the "
                                "rest of the plan and say in your debrief that this command is "
                                "still waiting on approval."
                            ),
                        })
                    else:
                        result = json.dumps({
                            "error": "Command denied by the user",
                            "hint": (
                                "The user refused this command. Do not resend it. Achieve the "
                                "goal another way or continue with the rest of the plan."
                            ),
                        })
                    yield {"type": "shell_approval_done", "cancelled": not approved}
                    yield {"type": "status", "model": {"label": "Working…"}, "key": active_key}
                elif call["name"] == "connection_call" and settings.get("approval_mode") == "full_access":
                    result = json.dumps(await _index_work(
                        mcp_connections.call, call["arguments"], approved=True,
                        ticket=mcp_connections.approval_ticket(call["arguments"]),
                    ))
                elif call["name"] in {"git_status", "run_shell", "connection_call"} or (chat.get("editor_session_id") and call["name"] in {"write_file", "edit_file", "append_file"}) or (call["name"] == "search_files" and call["arguments"].get("mode", "regex") != "regex") or (call["name"] == "read_file" and call["arguments"].get("language_action")):
                    # Parsing and embeddings can take time. Keep Stop and the UI
                    # responsive while this read-only tool runs in a worker.
                    result = await _index_work(
                        agent.run_tool, call["name"], call["arguments"], project_root,
                        chat.get("project_id"), chat=chat,
                    )
                else:
                    result = agent.run_tool(
                        call["name"],
                        call["arguments"],
                        project_root,
                        chat.get("project_id"),
                        chat=chat,
                    )
                if call["name"] == "computer":
                    result, computer_stalls = _computer_progress(result, computer_stalls)
                # Success is judged on the real result, before any offloading
                # rewrites it into a pointer.
                tool_succeeded = _tool_succeeded(call["name"], result)
                if call["name"] in {"write_file", "edit_file", "append_file", "run_shell", "connection_call"}:
                    try:
                        outcome = json.loads(result)
                    except (TypeError, json.JSONDecodeError):
                        outcome = None
                    if call["name"] == "connection_call":
                        await asyncio.to_thread(ledger.after_shell, "", None)
                    elif call["name"] == "run_shell":
                        await asyncio.to_thread(ledger.after_shell, str(call["arguments"].get("command") or ""),
                                                outcome.get("exit_code") if isinstance(outcome, dict) else None)
                    elif isinstance(outcome, dict) and call["name"] != "run_shell":
                        ledger.after_write(call["name"], str(call["arguments"].get("path") or ""), outcome)
                # Oversized output goes to disk and rides in the prompt as
                # head + tail + path. Truncating instead cuts the tail the model
                # needs next and sends it re-reading.
                stored = agent.offload_tool_output(
                    call["name"], result, chat.get("id") or "", len(chat["messages"])
                ) or result
                result_for[sig] = {
                    "role": "tool",
                    "name": call["name"],
                    "content": stored,
                    "ts": time.time(),
                    "quiet": True,
                }
                chat["messages"].append(result_for[sig])
                if call["name"] == "computer":
                    frame = computer.pop_frame()
                    if frame:
                        pending_frames.append(frame)
                if not tool_succeeded:
                    try:
                        payload = json.loads(result)
                    except (TypeError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, dict) and payload.get("error"):
                        call_rejections[sig] = {
                            k: v for k, v in payload.items()
                            if k in {"error", "hint", "maximum", "requested",
                                     "maximum_chars", "path", "validation", "suggestions"}
                        }
                else:
                    call_rejections.pop(sig, None)
                if tool_succeeded:
                    no_action_stalls = 0
                    plan_repeat_recoveries = 0
                    # A redundant read earlier in a long job is no longer a
                    # consecutive stall once an edit or check actually succeeds.
                    repeat_stalls = 0
                    if call["name"] in {"write_file", "append_file", "edit_file"}:
                        if json.loads(result).get("changed", True):
                            call_counts.clear()
                        plan_checkpoint_due = bool(chat.get("agent_todos"))
                        # Building has started; questions are closed from here on.
                        mutated_this_run = True
                    elif call["name"] == "run_shell" and project_root:
                        try:
                            project_commands.learn_build(project_root, call["arguments"].get("command") or "")
                        except OSError as error:
                            _log(f"[checks] Could not save compiler command: {error}")
                st.save_chat(chat)
                activity = agent.tool_activity_line(call["name"], call.get("arguments"), result)
                yield {
                    "type": "delta",
                    "content": "",
                    "thinking": _log(activity),
                    "quiet": True,
                }
                # No tool card dumps: the activity is already in the thinking log
                yield {
                    "type": "tool_result",
                    "name": call["name"],
                    "result": "",
                    "quiet": True,
                    "summary": activity,
                    "ok": tool_succeeded,
                }
                if (
                    tool_succeeded
                    and project_root is not None
                    and chat.get("project_id")
                    and call["name"] in {"write_file", "append_file", "edit_file", "run_shell"}
                ):
                    try:
                        tree_result = json.loads(agent.run_tool(
                            "list_dir",
                            {"path": "."},
                            project_root,
                            chat.get("project_id"),
                        ))
                    except Exception:
                        tree_result = {}
                    yield {
                        "type": "project_tree",
                        "project_id": chat.get("project_id"),
                        "entries": tree_result.get("entries") or [],
                    }
                if call["name"] == "todo_write":
                    prog = agent.todos_progress(chat.get("agent_todos"))
                    yield {"type": "todos", **prog}
                    if tool_succeeded:
                        plan_checkpoint_due = False
            if pending_frames:
                for frame in pending_frames:
                    chat["messages"].append(_frame_message(frame))
                _prune_stored_frames(chat)
                computer_overlay.set_label("Olith is using your computer")
                st.save_chat(chat)
            if repeat_stalls >= repeat_limit:
                # An open plan is authority to recover on its own. Bounded
                # retries beat asking the user to re-order work already planned.
                if agent.todos_incomplete(chat):
                    plan_repeat_recoveries += 1
                    repeat_stalls = 0
                    if plan_repeat_recoveries <= repeat_limit:
                        plan_continuation_due = True
                        # The plan directive says "change strategy"; this says
                        # which constraint to change it to.
                        rejection_nudge = _rejection_nudge(call_rejections)
                        yield {
                            "type": "status",
                            "model": {"label": "Changing strategy for open plan…"},
                            "key": active_key,
                        }
                        continue
                    assistant["content"] = (
                        "The model repeatedly retried a rejected tool operation without changing "
                        "strategy. Olith stopped this run without changing the open plan."
                    )
                    assistant["interim"] = False
                    assistant["thinking"] = "\n".join(work_log)
                    chat["interrupted"] = True
                    st.save_chat(chat)
                    yield {"type": "delta", "content": assistant["content"], "thinking": assistant["thinking"]}
                    yield {"type": "message", "message": assistant}
                    return
                # No open plan is not a reason to abandon a job. A rejected call
                # carries a constraint, so restate it before giving up.
                if call_rejections:
                    plan_repeat_recoveries += 1
                    repeat_stalls = 0
                    if plan_repeat_recoveries <= repeat_limit:
                        rejection_nudge = _rejection_nudge(call_rejections)
                        yield {
                            "type": "status",
                            "model": {"label": "Changing strategy…"},
                            "key": active_key,
                        }
                        continue
                # With no open plan and nothing actionable left, stop rather than
                # burning every remaining step in silence.
                assistant["content"] = (
                    "I got stuck repeating the same tool call without making progress, so I "
                    "stopped instead of looping. Earlier successful tool changes remain in place. "
                    "Try narrowing the request to one concrete next step."
                )
                assistant["interim"] = False
                assistant["thinking"] = "\n".join(work_log)
                chat["interrupted"] = True
                st.save_chat(chat)
                yield {"type": "delta", "content": assistant["content"], "thinking": assistant["thinking"]}
                yield {"type": "message", "message": assistant}
                return
            continue

        # A normal reply ends the turn. Retry promised actions and malformed calls
        # instead of showing them as completed work.
        broken = agent.looks_like_broken_tool_call(content) if content and not wrapping_up else None
        if broken and malformed_stalls < MALFORMED_CALL_LIMIT:
            # Its own budget, not the stall budget: that one resets on every
            # successful call, so a model alternating good and mangled calls
            # would retry the mangled one without bound.
            malformed_stalls += 1
            force_action_next = True
            _log(f"[loop guard] malformed {broken} call in the reply body "
                 f"({malformed_stalls}/{MALFORMED_CALL_LIMIT})")
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            rejection_nudge = (
                f"[agent-loop] Your last reply wrote a {broken} call into the message body and "
                "the JSON was malformed, so nothing ran. Do not print tool calls as text. "
                "Emit it as a native tool call."
            )
            st.save_chat(chat)
            yield {
                "type": "status",
                "model": {"label": f"Retrying a malformed tool call "
                                   f"({malformed_stalls}/{MALFORMED_CALL_LIMIT})…"},
                "key": active_key,
            }
            continue

        # `broken` already had its own budget above; charging the narration
        # budget for the same reply spends six retries on one failure.
        if content and not broken and not wrapping_up and no_action_stalls < 3 and agent.looks_incomplete(content):
            no_action_stalls += 1
            force_action_next = True
            _log(f"[loop] narration instead of an action ({no_action_stalls}/3)")
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            st.save_chat(chat)
            yield {
                "type": "status",
                "model": {"label": f"Turning narration into action ({no_action_stalls}/3)…"},
                "key": active_key,
            }
            continue
        # Nudge an unfinished plan a limited number of times, then accept the reply.
        if content and not wrapping_up and agent.todos_incomplete(chat) and plan_prose_stalls < 2:
            plan_prose_stalls += 1
            plan_continuation_due = True
            _log(f"[plan] nudged to continue an open plan ({plan_prose_stalls}/2)")
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            st.save_chat(chat)
            yield {
                "type": "status",
                "model": {"label": f"Continuing open plan ({plan_prose_stalls}/2)…"},
                "key": active_key,
            }
            continue
        # Past the budget the turn ends on the model's own words and open
        # milestones stay open. Bulk-closing them rewrites the record.
        if content:
            if review and settings.get("auto_verify_project", True):
                current = await asyncio.to_thread(review.finish)
                revision = hashlib.sha256(json.dumps([(f["file"], f["after_hash"]) for f in current["files"]]).encode()).hexdigest()
                if current["files"] and revision != verified_revision:
                    verified_revision = revision
                    commands = await asyncio.to_thread(project_commands.discover, project_root)
                    results = []
                    changed = [f["file"] for f in current["files"]]
                    for command in project_commands.applicable(commands, changed):
                        text = project_commands.shell_command(project_root, command)
                        gated, why = _shell_gate(st.load_settings(), text, chat.get("project_id"))
                        script = command.get("script")
                        if script:
                            script_gate, script_why = _shell_gate(st.load_settings(), script, chat.get("project_id"))
                            gated, why = gated or script_gate, why if gated else script_why
                        if gated:
                            results.append({"command": text, "status": "needs_approval", "reason": why})
                            continue
                        yield {"type": "status", "model": {"label": f"Checking project: {command['kind']}…"}, "key": active_key}
                        await asyncio.to_thread(ledger.before_shell, text)
                        output = await _index_work(agent.run_tool, "run_shell", {"command": text, "timeout_sec": command["timeout_sec"]}, project_root, chat.get("project_id"), chat=chat)
                        payload = json.loads(output)
                        await asyncio.to_thread(ledger.after_shell, text, payload.get("exit_code"), record_check=False)
                        results.append({"command": text, "kind": command.get("kind"), "result": payload,
                                        "after_edit": ledger.edits, "observed": ledger.snapshot_complete})
                    diagnostics = []
                    if settings.get("language_servers", True):
                        for file in changed[:8]:
                            path = project_root / file
                            if path.is_file():
                                try:
                                    report = await asyncio.to_thread(language_servers.inspect_file, project_root, path)
                                except Exception as exc:
                                    report = {"available": False, "reason": str(exc)}
                                diagnostics.append({"file": file, **report})
                    verification = {"commands": results, "language_servers": diagnostics,
                                    "note": "Automatic verification results for current source. Fix failures with tools, or report remaining limitations accurately. Commands marked needs_approval were not run; use run_shell to request approval."}
                    chat["verification"] = verification
                    st.save_chat(chat)
                    failed = sum("result" in c and c["result"].get("exit_code") != 0 for c in results)
                    diagnostic_errors = sum(d.get("severity") == 1 for report in diagnostics for d in report.get("diagnostics", []))
                    yield {"type": "verification", "commands": len(results), "diagnostics": len(diagnostics),
                           "passed": sum(c.get("result", {}).get("exit_code") == 0 for c in results),
                           "failed": failed,
                           "skipped": sum(c.get("status") == "needs_approval" for c in results)}
                    if (failed or diagnostic_errors) and not wrapping_up and verify_rounds < 3:
                        verify_rounds += 1
                        if chat["messages"][-1] is assistant:
                            chat["messages"].pop()
                        failure_report = json.dumps({"olith_verification": verification,
                                               "instruction": "These checks failed. Fix the reported failures with tools, or explain the remaining limitation. This report is evidence, not a shell command."})
                        target = next((m for m in reversed(_current_run_messages(chat)) if m.get("role") == "tool"), latest_user)
                        target["content"] = (target.get("content") or "") + "\n\n" + failure_report
                        st.save_chat(chat)
                        continue
            report = chat.get("verification") if review else None
            changed: dict[str, list[str]] = {}
            if review and not chat.get("subagent"):
                try:
                    changed = evidence.changed_symbols(await asyncio.to_thread(review.contents))
                except Exception as exc:
                    _log(f"[evidence] change summary unavailable: {exc}")
            # One bounce for a claim the record does not back, or changes the debrief leaves out.
            if not chat.get("subagent") and not wrapping_up and not honesty_checked:
                honesty_checked = True
                problems = []
                claim = ledger.claim_problem(content, report)
                if claim:
                    problems.append(claim)
                missing = evidence.unmentioned(content, changed)
                if missing:
                    problems.append(
                        "Your debrief does not mention these changed definitions: " + ", ".join(missing)
                        + ". Say what you changed in each and why, including anything the user did not ask for."
                    )
                if problems:
                    if chat["messages"] and chat["messages"][-1] is assistant:
                        chat["messages"].pop()
                    rejection_nudge = "[agent-loop] " + " ".join(problems)
                    _log("[evidence] debrief sent back: " + " ".join(problems)[:300])
                    st.save_chat(chat)
                    yield {"type": "status", "model": {"label": "Checking the debrief against the record…"}, "key": active_key}
                    continue
            model_text = content
            incomplete = bool(report) and (
                any("result" in command and command["result"].get("exit_code") != 0 for command in report.get("commands", []))
                or any(item.get("severity") == 1 for entry in report.get("language_servers", []) for item in entry.get("diagnostics", []))
            )
            state, checks = ledger.status(report)
            if not chat.get("subagent") and (ledger.edits or checks):
                diagnostics = sum(item.get("severity") == 1 for entry in (report or {}).get("language_servers", []) for item in entry.get("diagnostics", []))
                content = evidence.block(state, checks, changed, diagnostics) + "\n\n" + model_text
            elif report:
                content = model_text + "\n\n" + _verification_summary(report)
            if incomplete:
                content = "This task is incomplete: automatic verification still fails for the current files.\n\n" + content
            assistant["model_content"] = model_text
            assistant["content"] = content
            assistant["interim"] = False
            assistant["thinking"] = "\n".join(work_log)
            st.save_chat(chat)
            yield {"type": "delta", "content": content, "thinking": assistant["thinking"]}
            yield {"type": "message", "message": assistant}
            return

        # A truncated thinking-only response is a failed sample, not a turn.
        # Retry through progressively larger deterministic action lanes.
        no_action_stalls += 1
        reason = str(final_chunk.get("done_reason") or "empty response")
        # A timeout means mid-generation, not idle, so the retry needs more time
        # rather than less.
        last_stall_timed_out = reason.startswith("sample timeout")
        if assistant.get("native_thinking") and (reason == "length" or last_stall_timed_out):
            salvaged_reasoning = assistant["native_thinking"]
        eval_count = final_chunk.get("eval_count")
        metric = f", {eval_count} generated tokens" if isinstance(eval_count, int) else ""
        assistant["content"] = ""
        assistant["interim"] = True
        assistant["thinking"] = _log(f"[model retry] {reason}{metric}; retrying without thinking")
        yield {"type": "message", "message": {**assistant, "content": ""}}
        if no_action_stalls <= 3:
            # Failed sample, not a turn. Keep its thinking in the work log but
            # never persist a phantom assistant message before the retry.
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            st.save_chat(chat)
            force_action_next = True
            yield {
                "type": "status",
                "model": {
                    "label": f"Retrying model action ({no_action_stalls}/3)…"
                },
                "key": active_key,
            }
            continue

        assistant["content"] = (
            "The model returned no assistant message or tool call after four progressively "
            f"smaller recovery prompts ({reason}{metric}), so Olith stopped this turn. "
            "Successful tool changes remain in place."
        )
        assistant["interim"] = False
        assistant["thinking"] = "\n".join(work_log)
        chat["interrupted"] = True
        st.save_chat(chat)
        yield {"type": "delta", "content": assistant["content"], "thinking": assistant["thinking"]}
        yield {"type": "message", "message": assistant}
        return


@app.post("/api/chats/{cid}/compact")
async def api_compact(cid: str):
    """Force-compact a chat (used by tests / recovery). Auto-compact runs on send."""
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    model_key = _model_key(chat)

    async def stream():
        async with _gen_lock:
            yield _sse({"type": "compacting"})
            msgs = chat.get("messages") or []
            if len(msgs) < 2:
                yield _sse({"type": "compacted", "messages": msgs, "summary": chat.get("summary") or ""})
                return
            keep = int(st.load_settings().get("compact_keep_recent", 10))
            upto = _compact_split_point(msgs, keep)
            if upto <= 0:
                yield _sse({"type": "compacted", "messages": msgs, "summary": chat.get("summary") or ""})
                return
            summary = await _compact_chat(chat, model_key, upto)
            # Insert a boundary; never delete. The transcript stays whole on disk.
            msgs.insert(upto, {
                "role": "system",
                "kind": COMPACT_BOUNDARY_KIND,
                "summary": summary,
                "ts": time.time(),
                "quiet": True,
            })
            chat["summary"] = summary
            st.save_chat(chat)
            used = _estimate_chat_tokens(chat, model_key)
            limit = _ctx_limit(model_key, _role(chat))
            yield _sse({
                "type": "compacted",
                "messages": chat.get("messages") or [],
                "summary": (chat.get("summary") or "")[:800],
                "context": {
                    "used": used,
                    "limit": limit,
                    "pct": round(100 * used / max(limit, 1), 1),
                },
            })

    return StreamingResponse(stream(), media_type="text/event-stream")


class MemoryPatch(BaseModel):
    text: str


class ProjectPatch(BaseModel):
    name: str | None = None
    root: str | None = None
    description: str | None = None


class MessagePatch(BaseModel):
    content: str


@app.patch("/api/memory/{scope}/{note_id}")
async def api_patch_memory(scope: str, note_id: str, body: MemoryPatch):
    mem = st.update_memory_note(scope, note_id, body.text)
    if not mem:
        raise HTTPException(404)
    return mem


@app.patch("/api/projects/{pid}")
async def api_patch_project(pid: str, body: ProjectPatch):
    proj = st.update_project(pid, body.model_dump(exclude_none=True))
    if not proj:
        raise HTTPException(404)
    return proj


@app.patch("/api/chats/{cid}/messages/{mid}")
async def api_patch_message(cid: str, mid: str, body: MessagePatch):
    chat = st.update_message(cid, mid, body.content)
    if not chat:
        raise HTTPException(404)
    return chat


class ResubmitBody(BaseModel):
    content: str
    model_key: str | None = None
    agent_model: str | None = None
    mode: str | None = None
    think: bool | None = None
    effort: str | None = None


@app.post("/api/chats/{cid}/messages/{mid}/resubmit")
async def api_resubmit_message(cid: str, mid: str, body: ResubmitBody):
    """Edit a user message, drop later turns, and stream a fresh assistant reply."""
    chat = st.edit_and_truncate(cid, mid, body.content)
    if not chat:
        raise HTTPException(404, "Message not found or not a user message")

    if _is_known_model_key(body.model_key):
        mk = body.model_key
        if _requested_role(body, chat) == "agentic":
            chat["mode"] = "agentic"
            chat["model_id"] = _agent_model_choice(body.agent_model or mk)
            chat["reasoning"] = False
        else:
            chat["mode"] = "chat"
            chat["computer_use"] = False
            chat["model_id"] = mk
        st.save_chat(chat)

    if body.think is not None:
        chat["think"] = bool(body.think)
        st.save_chat(chat)

    model_key = _model_key(chat, model_key=body.model_key)
    effort = _resolve_effort(body.effort or chat.get("effort"), model_key)
    # Editing a message restarts the work it asked for, so the scaffold written
    # for the previous attempt must not carry over.
    _start_new_job(chat)
    st.save_chat(chat)
    _mark_computer(chat, True)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict):
            await queue.put(ev)

        async def run():
            async with _gen_lock:
                try:
                    chat_local = st.get_chat(cid)
                    chat_local, _ = await _maybe_auto_compact(chat_local, model_key, emit)
                    meta = await oc.ensure_model_loaded(model_key, meta=_meta(model_key, _role(chat_local)))
                    await emit({"type": "status", "model": meta, "key": model_key, "effort": effort})
                    if _role(chat_local) == "agentic":
                        async for ev in _agent_loop(
                            chat_local, meta, model_key, effort, think=chat_local.get("think")
                        ):
                            await emit(ev)
                    else:
                        async for ev in _plain_stream(chat_local, meta, model_key, effort, think=chat_local.get("think")):
                            await emit(ev)
                    final = st.get_chat(cid)
                    used = _estimate_chat_tokens(final, model_key)
                    limit = _ctx_limit(model_key, _role(chat))
                    await emit({
                        "type": "done",
                        "chat_id": cid,
                        "context": {
                            "used": used,
                            "limit": limit,
                            "pct": round(100 * used / max(limit, 1), 1),
                        },
                    })
                    asyncio.create_task(_post_reply_jobs(cid, model_key))
                except asyncio.CancelledError:
                    try:
                        stopped = st.get_chat(cid) or chat_local
                        stopped["interrupted"] = True
                        st.save_chat(stopped)
                        await emit({
                            "type": "stopped",
                            "interrupted": True,
                            "todos": agent.todos_progress(stopped.get("agent_todos")),
                        })
                    except Exception:
                        await emit({"type": "stopped"})
                    raise
                except Exception as e:
                    await emit({"type": "error", "message": str(e)})
                finally:
                    _mark_computer(None, False)
                    await queue.put(None)

        task = asyncio.create_task(run())
        _active_gens[cid] = task
        finished_ok = False
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    finished_ok = True
                    break
                yield _sse(ev)
        finally:
            _active_gens.pop(cid, None)
            if not finished_ok and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.delete("/api/chats/{cid}/messages/{mid}")
async def api_delete_message(cid: str, mid: str):
    chat = st.delete_message(cid, mid)
    if not chat:
        raise HTTPException(404)
    return {"ok": True, "chat": chat}


@app.get("/api/memory/{scope}")
async def api_get_memory(scope: str):
    return st.load_memory(scope)


@app.post("/api/memory/{scope}")
async def api_add_memory(scope: str, body: MemoryNote):
    return st.add_memory_note(scope, body.text, body.tags)


@app.delete("/api/memory/{scope}/{note_id}")
async def api_del_memory(scope: str, note_id: str):
    return st.delete_memory_note(scope, note_id)


@app.get("/api/projects")
async def api_list_projects():
    return st.list_projects()


@app.post("/api/projects")
async def api_new_project(body: ProjectCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Enter a project name")
    if body.create_folder:
        # Use one portable folder name so creation cannot escape the projects folder.
        if (len(name) > 120 or name in {".", ".."} or name.endswith((".", " "))
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", name)):
            raise HTTPException(400, "Choose a folder name without slashes or special characters")
        root = config.PROJECT_HOME.expanduser() / name
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Could not prepare Olith's projects folder: {exc}")
        try:
            root.mkdir()
        except FileExistsError:
            raise HTTPException(409, "That folder already exists. Choose another name or add it as an existing folder.")
        except OSError as exc:
            raise HTTPException(400, f"Could not create project folder: {exc}")
    else:
        if not body.root.strip():
            raise HTTPException(400, "Choose an existing project folder")
        root = Path(body.root).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"Folder does not exist: {body.root}")
    return st.new_project(name, str(root.resolve()), body.description)


@app.get("/api/project-location")
async def api_project_location():
    return {"root": str(config.PROJECT_HOME.expanduser().resolve())}


@app.get("/api/projects/{pid}")
async def api_get_project(pid: str):
    p = st.get_project(pid)
    if not p:
        raise HTTPException(404)
    return p


@app.delete("/api/projects/{pid}")
async def api_del_project(pid: str):
    if not st.delete_project(pid):
        raise HTTPException(404)
    return {"ok": True}


@app.get("/api/projects/{pid}/tree")
async def api_project_tree(pid: str, path: str = "."):
    proj = st.get_project(pid)
    if not proj:
        raise HTTPException(404)
    if path == ".":
        try:
            await asyncio.to_thread(project_commands.discover, Path(proj["root"]))
        except (OSError, ValueError):
            pass
    listing = agent.run_tool("list_dir", {"path": path}, Path(proj["root"]), pid)
    return json.loads(listing)


def _project_index(pid: str) -> repo_index.ProjectIndex:
    project = st.get_project(pid)
    if not project:
        raise HTTPException(404, "Unknown project")
    root = agent.resolve_path(project["root"], Path(project["root"]))
    if not root.is_dir():
        raise HTTPException(400, "Project folder is unavailable")
    return repo_index.ProjectIndex(root)


class IndexBuild(BaseModel):
    semantic: bool = False
    force: bool = False


class IndexSearch(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: str = "hybrid"
    limit: int = Field(default=6, ge=1, le=20)


class CommandOverrides(BaseModel):
    commands: list[dict] = Field(default_factory=list, max_length=30)


@app.get("/api/projects/{pid}/commands")
async def api_project_commands(pid: str):
    root = _project_index(pid).root
    return {"commands": await asyncio.to_thread(project_commands.discover, root)}


@app.put("/api/projects/{pid}/commands")
async def api_project_commands_override(pid: str, body: CommandOverrides):
    root = _project_index(pid).root
    try:
        return {"commands": project_commands.overrides(root, body.commands)}
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/chats/{cid}/changes")
async def api_task_changes(cid: str):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404, "Unknown chat")
    project = st.get_project(chat.get("project_id")) if chat.get("project_id") else None
    if not project or not chat.get("change_set_id"):
        return {"files": [], "note": "No task changes recorded for this chat."}
    try:
        review = task_changes.ChangeSet(Path(project["root"]), chat["change_set_id"])
        return {**await asyncio.to_thread(review.review), "verification": chat.get("verification")}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class UndoSelection(BaseModel):
    selections: list[dict] = Field(min_length=1, max_length=100)
    change_set_id: str


@app.post("/api/chats/{cid}/changes/undo")
async def api_task_undo(cid: str, body: UndoSelection):
    if _gen_lock.locked():
        raise HTTPException(409, "Wait for active agent work to finish before undoing changes")
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404, "Unknown chat")
    if chat.get("change_set_id") != body.change_set_id:
        raise HTTPException(409, "A newer task has replaced this review. Reopen Changes.")
    project = st.get_project(chat.get("project_id")) if chat.get("project_id") else None
    if not project:
        raise HTTPException(400, "No active project")
    async with _gen_lock:
        try:
            review = task_changes.ChangeSet(Path(project["root"]), body.change_set_id)
            result = await asyncio.to_thread(review.undo, body.selections)
            chat["change_review"] = {"id": result["id"], "files": len(result["files"]), "partial": result["partial"]}
            chat.pop("verification", None)
            st.save_chat(chat)
            await asyncio.to_thread(repo_index.ProjectIndex(Path(project["root"])).refresh)
            return result
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc))


@app.get("/api/projects/{pid}/index")
async def api_index_status(pid: str):
    index = _project_index(pid)
    settings = st.load_settings()
    status = await asyncio.to_thread(index.status, str(settings.get("embedding_model") or ""))
    job = _index_states.get(pid, {})
    return {**status, "job": job, "semantic_enabled": bool(settings.get("semantic_search")),
            "embedding_model": settings.get("embedding_model", "nomic-embed-text")}


@app.post("/api/projects/{pid}/index")
async def api_build_index(pid: str, body: IndexBuild):
    index = _project_index(pid)
    if pid in _index_tasks and not _index_tasks[pid].done():
        return {"job": _index_states[pid]}
    settings = st.load_settings()
    if body.semantic and not settings.get("semantic_search"):
        raise HTTPException(400, "Enable semantic search in Settings first")
    state = {"phase": "indexing", "files": 0, "embedded": 0, "error": None, "cancel": False}
    _index_states[pid] = state

    async def build():
        try:
            def progress(counts):
                state.update(counts)
                if state["cancel"]:
                    raise InterruptedError("Indexing stopped")
            stats = await asyncio.to_thread(index.refresh, body.force, progress)
            state.update(stats)
            if body.semantic:
                embedder = repo_index.OllamaEmbedder(settings)
                state["phase"] = "waiting"
                while not state["cancel"]:
                    # Release between batches so a chat can start. Embeddings
                    # share the generation lock with the agent and title work.
                    async with _gen_lock:
                        if state["cancel"]:
                            break
                        state["phase"] = "embedding"
                        done = await _index_work(index.embedding_batch, embedder)
                    if not done:
                        break
                    state["embedded"] += done
                    state["phase"] = "waiting"
                    await asyncio.sleep(0)
            state["phase"] = "stopped" if state["cancel"] else "ready"
        except InterruptedError:
            state["phase"] = "stopped"
        except Exception as exc:
            state["phase"], state["error"] = "error", str(exc)
        finally:
            _index_tasks.pop(pid, None)

    _index_tasks[pid] = asyncio.create_task(build())
    return {"job": state}


@app.post("/api/projects/{pid}/index/stop")
async def api_stop_index(pid: str):
    _project_index(pid)
    state = _index_states.get(pid)
    if state:
        # A running thread finishes its current batch before releasing the lock.
        state["cancel"] = True
    return {"ok": True}


@app.post("/api/projects/{pid}/index/search")
async def api_index_search(pid: str, body: IndexSearch):
    index = _project_index(pid)
    if body.mode == "map":
        return {"map": await asyncio.to_thread(index.repo_map, body.query)}
    if body.mode not in {"hybrid", "symbols", "references"}:
        raise HTTPException(400, "Unknown search mode")
    settings = st.load_settings()
    embedder = repo_index.OllamaEmbedder(settings) if settings.get("semantic_search") and body.mode == "hybrid" else None
    async def search():
        result = await _index_work(index.search, body.query, body.mode, body.limit, embedder=embedder)
        if embedder:
            await api_build_index(pid, IndexBuild(semantic=True))
        return result
    if embedder:
        # An interactive search can load a model too, so it uses the same lock.
        try:
            await asyncio.wait_for(_gen_lock.acquire(), timeout=GEN_LOCK_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(409, "Olith is working. Try again after the current run.")
        try:
            return await search()
        finally:
            _gen_lock.release()
    return await search()


async def _index_work(function, *args, **kwargs):
    """A stopped request must not release the model lock over a live worker."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except Exception:
            pass
        raise


@app.get("/api/context/{cid}")
async def api_context(cid: str):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    key = _model_key(chat)
    meta = _meta(key, _role(chat))
    used = _estimate_chat_tokens(chat, key)
    limit = _ctx_limit(key, _role(chat))
    settings = st.load_settings()
    role = _role(chat)
    return {
        "used": used,
        "limit": limit,
        "pct": round(100 * used / max(limit, 1), 1),
        "model": meta,
        "key": key,
        "effort_levels": reg.effort_levels(meta),
        "thinking": reg.thinking_summary(meta),
        "role": role,
        "context_max": int(meta.get("context_max") or limit),
        "has_summary": bool(chat.get("summary")),
    }


class ConnectionIn(BaseModel):
    id: str
    label: str | None = None
    kind: str | None = None
    sign_in_url: str | None = None
    note: str | None = None


class SecretIn(BaseModel):
    value: str


# Olith's own face on a sign-in. The window opens on this page rather than on
# someone else's login form, so it can say who is asking and for what.
CONSENT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Sign in to {label} in Olith's browser</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0b0d14; color: #edf2ff;
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .card {{
    width: min(560px, calc(100vw - 48px)); padding: 34px 36px 30px;
    background: #12151f; border: 1px solid rgba(255,255,255,0.09); border-radius: 18px;
    box-shadow: 0 24px 60px rgba(0,0,0,0.45);
  }}
  .brand {{ display: flex; align-items: center; gap: 11px; margin-bottom: 22px; }}
  .mark {{ width: 30px; height: 30px; object-fit: contain; }}
  .brand b {{ font-size: 1.06rem; letter-spacing: 0.05em; font-weight: 700; }}
  h1 {{ font-size: 1.34rem; margin: 0 0 10px; font-weight: 650; }}
  p {{ margin: 0 0 14px; color: #b9c2d6; }}
  ul {{ margin: 0 0 22px; padding-left: 19px; color: #b9c2d6; }}
  li {{ margin-bottom: 6px; }}
  .row {{ display: flex; gap: 10px; align-items: center; }}
  a.go {{
    display: inline-block; text-decoration: none; font-weight: 650;
    padding: 11px 20px; border-radius: 11px; color: #fff;
    background: linear-gradient(135deg, #4f7cff, #7c5cff);
  }}
  .later {{ color: #8b95a5; font-size: 0.86rem; }}
  .foot {{ margin-top: 22px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.07);
          font-size: 0.82rem; color: #8b95a5; }}
</style></head>
<body><main class="card">
  <div class="brand">
    <img class="mark" src="/static/assets/olith-mark.svg" alt="" />
    <b>OLITH</b>
  </div>
  <h1>Sign in to {label} in Olith's browser</h1>
  <p>This window belongs to Olith, on your own machine. It opened because you
     chose Browser sign-in in the Connections panel.</p>
  <ul>
    <li>You sign in on {label}'s own page, in this window. Olith does not fill
        the form and never sees your password.</li>
    <li>What Olith keeps afterwards is the session, in its own browser profile,
        the same thing any browser keeps. Forget removes Olith's connection record;
        sign out in this browser to end the website session.</li>
    <li>This is website access for computer use, not authorization for direct
        service tools. Choose Connect tools in Olith for those.</li>
  </ul>
  <div class="row">
    <a class="go" href="{sign_in_url}">Continue to {label}</a>
    <span class="later">Then return to Olith and confirm your browser session.</span>
  </div>
  <p class="foot">If you did not press Connect in Olith, close this window.
     Nothing has been shared.</p>
</main></body></html>
"""


@app.get("/connect/{cid}", response_class=HTMLResponse)
async def api_connect_page(cid: str):
    """Olith's own page, shown before any service's sign-in."""
    entry = connections.spec(cid)
    if entry is None or not entry.get("sign_in_url"):
        raise HTTPException(404, "No such connection.")
    return HTMLResponse(CONSENT_PAGE.format(
        label=html.escape(entry.get("label") or cid),
        sign_in_url=html.escape(entry["sign_in_url"], quote=True),
    ))


@app.get("/api/connections")
async def api_connections():
    return await asyncio.to_thread(connections.listing)


@app.post("/api/connections")
async def api_add_connection(body: ConnectionIn):
    try:
        return connections.add(body.id, body.label, body.kind, body.sign_in_url, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/connections/{cid}/sign-in")
async def api_connection_sign_in(cid: str):
    try:
        # Browser launch/navigation is blocking and requests /connect/{cid}
        # from this server. Running it on the event loop deadlocks that page.
        return await asyncio.to_thread(connections.sign_in, cid)
    except KeyError as e:
        raise HTTPException(404, f"No connection called {cid}.") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/connections/{cid}/check")
async def api_connection_check(cid: str):
    try:
        return await asyncio.to_thread(connections.check, cid)
    except KeyError as e:
        raise HTTPException(404, f"No connection called {cid}.") from e


@app.post("/api/connections/{cid}/secret")
async def api_connection_secret(cid: str, body: SecretIn):
    """The value goes straight to the OS keyring. It is never stored here, never
    logged, and never returned."""
    if not body.value.strip():
        raise HTTPException(400, "A key cannot be empty.")
    try:
        return await asyncio.to_thread(connections.store_secret, cid, body.value)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/connections/{cid}/confirm")
async def api_confirm_sign_in(cid: str):
    try:
        return await asyncio.to_thread(connections.confirm_sign_in, cid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/connections/{cid}/status")
async def api_browser_session_status(cid: str):
    try:
        return await asyncio.to_thread(connections.session_status, cid)
    except KeyError as exc:
        raise HTTPException(404, "No such browser sign-in.") from exc


@app.delete("/api/connections/{cid}")
async def api_forget_connection(cid: str):
    return await asyncio.to_thread(connections.forget, cid)


@app.post("/api/computer-overlay")
async def api_computer_overlay(body: dict):
    enabled = bool(body.get("enabled"))
    try:
        return computer_overlay.set_overlay(enabled)
    except Exception as e:
        return {"ok": False, "enabled": False, "error": str(e)}


@app.get("/api/computer-overlay")
async def api_computer_overlay_get():
    return computer_overlay.status()


@app.post("/api/pick-folder")
async def api_pick_folder():
    import asyncio

    import native_picker

    try:
        path = await asyncio.to_thread(native_picker.pick_folder, "Select project folder")
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    if not path:
        return {"ok": False, "path": None}
    return {"ok": True, "path": str(path)}


@app.post("/api/pick-files")
async def api_pick_files():
    import asyncio

    import native_picker

    try:
        paths = await asyncio.to_thread(native_picker.pick_files, "Select files")
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "paths": [str(p) for p in (paths or [])]}

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = file.filename or f"upload-{uuid.uuid4().hex[:8]}"
    dest = UPLOADS / f"{uuid.uuid4().hex[:10]}_{Path(name).name}"
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than Olith's 25 MB attachment limit")
    dest.write_bytes(data)
    kind = "image" if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file"
    out: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "path": str(dest),
        "size": len(data),
        "url": f"/uploads/{dest.name}",
    }
    if kind == "image":
        out["image_b64"] = base64.b64encode(data).decode("ascii")
    return out


class IngestPath(BaseModel):
    path: str


@app.post("/api/ingest-path")
async def api_ingest_path(body: IngestPath):
    src = Path(body.path)
    if not src.is_file():
        raise HTTPException(404, "File not found")
    dest = _ingest_to_uploads(src)
    kind = "image" if dest.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file"
    out: dict[str, Any] = {
        "name": src.name,
        "kind": kind,
        "path": str(dest),
        "size": dest.stat().st_size,
        "url": f"/uploads/{dest.name}",
    }
    if kind == "image":
        out["image_b64"] = base64.b64encode(dest.read_bytes()).decode("ascii")
    return out


@app.post("/api/screenshot")
async def api_screenshot():
    try:
        from PIL import ImageGrab
    except ImportError as e:
        raise HTTPException(500, "Pillow not installed") from e
    img = ImageGrab.grab()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS / f"shot_{uuid.uuid4().hex[:10]}.png"
    img.save(dest, format="PNG")
    raw = dest.read_bytes()
    return {
        "name": dest.name,
        "kind": "screenshot",
        "path": str(dest),
        "url": f"/uploads/{dest.name}",
        "image_b64": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
    }


@app.post("/api/search")
async def api_search(q: str = Form(...)):
    try:
        return search_web.run_web_research(q)
    except Exception as e:
        raise HTTPException(500, str(e))


from integrations import router as integration_router
app.include_router(integration_router)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("OLITH_PORT") or 7878)
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)
