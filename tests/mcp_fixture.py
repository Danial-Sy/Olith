# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated MCP provider for real stdio, HTTP, and OAuth integration tests."""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("Olith fixture", stateless_http=True, json_response=True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lookup(query: str) -> dict:
    """Search fixture records by query."""
    if query == "error":
        raise ValueError("Fixture tool error")
    return {"query": query, "value": "fixture result", "environment": os.environ.get("FIXTURE_SECRET", "")}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=os.environ.get("FIXTURE_WRITE_READONLY") == "1"))
def write_note(text: str) -> dict:
    """Write a fixture note."""
    target = os.environ.get("FIXTURE_MARKER")
    if target:
        Path(target).write_text(text)
    return {"written": text}


if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1] == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        from starlette.responses import JSONResponse, RedirectResponse
        from starlette.routing import Route
        from urllib.parse import urlencode, parse_qs
        port = int(sys.argv[2])
        base = f"http://127.0.0.1:{port}"
        mode = sys.argv[1]
        state = {"generation": 0, "expired": False, "challenges": {}}
        app = mcp.streamable_http_app()

        async def protected(request):
            return JSONResponse({"resource": base + "/mcp", "authorization_servers": [base], "scopes_supported": ["read", "write"]})

        async def metadata(request):
            return JSONResponse({"issuer": base, "authorization_endpoint": base + "/authorize",
                "token_endpoint": base + "/token", "registration_endpoint": base + "/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})

        async def register(request):
            data = await request.json()
            return JSONResponse({**data, "client_id": "fixture-client"}, status_code=201)

        async def authorize(request):
            query = request.query_params
            state["challenges"]["fixture-code"] = query["code_challenge"]
            return RedirectResponse(query["redirect_uri"] + "?" + urlencode({"code": "fixture-code", "state": query["state"]}))

        async def token(request):
            data = parse_qs((await request.body()).decode())
            if data.get("grant_type") == ["authorization_code"]:
                verifier = data.get("code_verifier", [""])[0]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if challenge != state["challenges"].get("fixture-code"):
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
            elif data.get("refresh_token") != ["fixture-refresh"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            state["generation"] += 1
            state["expired"] = False
            result = {"access_token": f"fixture-token-{state['generation']}", "token_type": "Bearer", "expires_in": 3600}
            if data.get("grant_type") == ["authorization_code"]:
                result["refresh_token"] = "fixture-refresh"
            return JSONResponse(result)

        async def expire(request):
            state["expired"] = True
            return JSONResponse({"ok": True})

        app.routes.extend([Route("/.well-known/oauth-protected-resource", protected),
            Route("/.well-known/oauth-authorization-server", metadata), Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize), Route("/token", token, methods=["POST"]), Route("/expire", expire, methods=["POST"])])

        class Authentication:
            def __init__(self, app): self.app = app
            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope["path"].startswith("/mcp") and mode in {"oauth", "bearer"}:
                    headers = dict(scope["headers"])
                    expected = "fixture-bearer" if mode == "bearer" else f"fixture-token-{state['generation']}"
                    if state["expired"] or headers.get(b"authorization") != ("Bearer " + expected).encode():
                        response = JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
                        return await response(scope, receive, send)
                await self.app(scope, receive, send)
        uvicorn.run(Authentication(app), host="127.0.0.1", port=port, log_level="error")
