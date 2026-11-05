# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local VS Code test server with simulated inference and temporary data.

Run .venv/bin/python tests/ide_smoke_server.py, then test/host.js with
OLITH_IDE_SMOKE_URL=http://127.0.0.1:7879.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import config
    with tempfile.TemporaryDirectory(prefix="olith-ide-smoke-") as folder:
        base = Path(folder)
        config.DATA = base / "data"
        for name in ("CHATS", "MEMORY", "PROJECTS", "UPLOADS"):
            setattr(config, name, config.DATA / name.lower())
        config.SETTINGS_PATH = config.DATA / "settings.json"
        config.DEFAULT_SETTINGS.update(semantic_search=False, language_servers=False, repo_map_enabled=False)
        import server
        import editor_bridge as bridge
        import task_changes
        import uvicorn
        from fastapi.responses import StreamingResponse
        server.app.router.on_startup.clear()
        server.app.router.on_shutdown.clear()
        server._warm_state["status"] = "ready"
        server._warm_event.set()
        @server.app.post("/__fixture/restart-bridge")
        async def restart_bridge():
            with bridge._lock:
                bridge._sessions.clear(); bridge._codes.clear(); bridge._loaded = False
            return {"ok": True}
        fixture_models = {key: {"id": key, "label": label, "tools": True} for key, label in (("fixture", "Fixture small"), ("fixture-large", "Fixture large"))}
        server._registry = lambda: ({"model_active": {"agentic": "fixture"}}, fixture_models)
        samples = 0
        async def stream(model, messages, **kwargs):
            # Deterministic inference, with the real tool loop, snapshot and
            # verification path. Read the active fixture source through its chat.
            nonlocal samples
            samples += 1
            if samples > 2:
                yield {"message": {"content": "Changed the source through VS Code."}, "done": True}
            elif samples == 1:
                yield {"message": {"content": "I will inspect stream.txt before making the change.", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "stream.txt"}}}]}, "done": True}
            else:
                yield {"message": {"content": "The source matches. I will update it through the editor now.", "tool_calls": [{"function": {"name": "edit_file", "arguments": {"path": "stream.txt", "find": "before", "replace": "after"}}}]}, "done": True}
        server.oc.stream_chat = stream
        async def send(cid, body):
            nonlocal samples
            samples = 0
            chat = server.st.get_chat(cid)
            assert body.model_key == chat.get("model_id"), "The picker selection must reach the generation request"
            assert '"file":"stream.txt"' in body.content and 'before' in body.content, "Attached source must reach inference"
            chat["messages"].append({"role": "user", "content": body.content})
            server.st.save_chat(chat)
            async def events():
                async with server._gen_lock:
                    async for event in server._agent_loop(chat, {"id": body.model_key, "tools": True, "keep_alive": "0"}, body.model_key):
                        yield "data: " + json.dumps(event) + "\n\n"
                    yield 'data: {"type":"done"}\n\n'
            return StreamingResponse(events(), media_type="text/event-stream")
        server.api_send = send
        uvicorn.run(server.app, host="127.0.0.1", port=7879)


if __name__ == "__main__":
    main()
