# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local UI routes for configuring and authenticating MCP connections."""
import asyncio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import mcp_connections as mcp

router = APIRouter()


class ToolConnection(BaseModel):
    id: str = Field(max_length=64)
    label: str = Field(default="", max_length=100)
    transport: str = "http"
    provider: str = Field(default="", max_length=64)
    url: str = Field(default="", max_length=2000)
    auth: str = "oauth"
    command: str = Field(default="", max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=100)
    token: str | None = Field(default=None, max_length=16000)
    client_id: str = Field(default="", max_length=500)
    client_secret: str | None = Field(default=None, max_length=16000)
    scope: str = Field(default="", max_length=2000)
    env: dict[str, str] | None = None
    enabled: bool = True


class Enabled(BaseModel):
    enabled: bool


@router.get("/api/tool-connections")
async def list_connections():
    return mcp.listing()


@router.post("/api/tool-connections")
async def save_connection(body: ToolConnection):
    await mcp.cancel(body.id)
    try:
        return await asyncio.to_thread(mcp.save, body.model_dump())
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise HTTPException(400, str(exc))
        raise HTTPException(409, "Could not save this connection. Check that your system keyring is unlocked.")


@router.patch("/api/tool-connections/{cid}")
async def enable_connection(cid: str, body: Enabled):
    try:
        return mcp.set_enabled(cid, body.enabled)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/tool-connections/{cid}/connect")
async def connect(cid: str):
    try:
        return await mcp.start_connect(cid)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/api/tool-connections/{cid}/status")
async def status(cid: str):
    try:
        return mcp.auth_status(cid)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/api/tool-connections/{cid}/callback", response_class=HTMLResponse)
async def callback(cid: str, request: Request):
    query = dict(request.query_params)
    # Uvicorn reads the scope after responding; authorization codes must stay out of access logs.
    request.scope["query_string"] = b""
    try:
        mcp.finish_callback(cid, query.get("code"), query.get("state"), query.get("error"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return HTMLResponse("<html><head><title>Olith connection</title><meta name='referrer' content='no-referrer'></head><body><h1>Return to Olith</h1><p>Sign-in received. You can close this tab and check the connection in Olith.</p></body></html>",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'"})


@router.post("/api/tool-connections/{cid}/cancel")
async def cancel(cid: str):
    await mcp.cancel(cid)
    try:
        return mcp._update(cid, status="not tested", note="Connection attempt cancelled.")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/api/tool-connections/{cid}")
async def forget(cid: str):
    try:
        return await mcp.forget(cid)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception:
        raise HTTPException(409, "Could not remove saved credentials. Unlock your system keyring and try again.")
