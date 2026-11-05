"""Local UI fixture with real project storage and Git, and simulated accounts.

--connections also simulates a login browser, never opening a real account.
--cli-ready supplies a fake Vercel executable to exercise successful CLI setup.
"""
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import config
    with tempfile.TemporaryDirectory(prefix="olith-workspace-ui-") as folder:
        base = Path(folder)
        config.DATA = base / "data"
        config.PROJECT_HOME = base / "Olith"
        for name in ("CHATS", "MEMORY", "PROJECTS", "UPLOADS"):
            setattr(config, name, config.DATA / name.lower())
        config.SETTINGS_PATH = config.DATA / "settings.json"
        config.DEFAULT_SETTINGS.update(semantic_search=False, language_servers=False, repo_map_enabled=False)
        import server
        import github_tools as git
        import editor_bridge as bridge
        import integrations
        import mcp_connections
        import uvicorn
        mcp_connections.STORE = base / "tool-connections.json"

        if "--cli-ready" in sys.argv:
            import cli_tools
            script = base / "fake-vercel.py"
            script.write_text('import json, sys\nprint(json.dumps({"username":"fixture-user"}) if "whoami" in sys.argv else "Vercel test CLI help")\n')
            cli_tools.launcher = lambda provider: [sys.executable, str(script)]

        if "--connections" in sys.argv:
            import browser
            import json
            import urllib.request
            login = {"url": "about:blank", "running": True, "ready_state": "complete"}

            def navigate(args):
                url = args.get("url", "")
                if url.startswith("http://127.0.0.1:7878/connect/"):
                    # This local request must complete while the sign-in POST
                    # is in flight: the original event-loop bug deadlocked it.
                    with urllib.request.urlopen(url, timeout=2) as response:
                        response.read()
                    login.update(url="https://vercel.com/dashboard", running=True)
                else:
                    login["url"] = url
                return json.dumps({"ok": True, "url": login["url"]})

            browser.run = navigate
            browser.status = lambda: dict(login)

            @server.app.post("/__fixture/browser/{phase}")
            async def browser_phase(phase: str):
                login.update(running=phase != "closed", url="https://vercel.com/login" if phase == "login" else "https://vercel.com/dashboard")
                return {"ok": True}

        git.connections.add("github")
        git.connections.add("google")
        git.connections._mark("google", status="connected")

        def run(root, *args):
            return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

        payments = base / "payments"; payments.mkdir()
        (payments / "auth.py").write_text("def authenticate(token):\n    return bool(token)\n")
        run(payments, "init", "-b", "main")
        run(payments, "config", "user.name", "UI Test")
        run(payments, "config", "user.email", "ui@example.invalid")
        run(payments, "config", "commit.gpgsign", "false")
        run(payments, "add", "."); run(payments, "commit", "-m", "Initial")
        run(payments, "remote", "add", "origin", "git@github.com:demo/payments.git")
        source = base / "docs-source.git"
        run(base, "clone", "--bare", str(payments), str(source))
        notes = base / "notes"; notes.mkdir()
        server.st.new_project("Payments", str(payments))
        server.st.new_project("Notes and experiments", str(notes))
        account = {"connected": True, "editor": True}
        git.token = lambda: "fixture" if account["connected"] else None
        git.api = lambda method, endpoint, body=None: {"login": "demo", "html_url": "https://github.com/demo"} if endpoint == "/user" else [{"full_name": "demo/payments", "private": True, "language": "Python"}, {"full_name": "demo/docs", "private": False, "language": "Markdown"}, {"full_name": "demo/local-llm-playground", "private": True, "language": "TypeScript"}]

        def connect(token):
            account["connected"] = True
            return {"login": "demo", "connected": True}
        git.connect = connect
        forget = git.connections.forget
        def disconnect(key):
            if key == "github-api":
                account["connected"] = False
                return {"ok": True}
            return forget(key)
        git.connections.forget = disconnect

        def clone(repo, destination):
            target = Path(destination)
            result = run(base, "clone", str(source), str(target))
            run(target, "remote", "set-url", "origin", "https://github.com/" + repo + ".git")
            return {"ok": True, "root": str(target), "output": result.stdout}
        git.clone = clone
        code = bridge.pair_code()["code"]
        session = bridge.connect(code, payments)
        stopped = threading.Event()
        def heartbeat():
            while not stopped.is_set():
                if account["editor"]:
                    bridge.pending(session["id"], "0.1.4")
                stopped.wait(2)
        threading.Thread(target=heartbeat, daemon=True).start()

        @server.app.post("/__fixture/editor/{active}")
        async def editor_active(active: str):
            account["editor"] = active == "online"
            if not account["editor"]:
                with bridge._lock:
                    bridge._sessions[session["id"]]["editor_seen"] -= bridge.ACTIVE_TTL
            return {"ok": True}

        server.app.router.on_startup.clear()
        server.app.router.on_shutdown.clear()
        server._warm_state["status"] = "ready"; server._warm_event.set()
        server.reg.installed = lambda *args, **kwargs: {"fixture": {"id": "fixture", "label": "Local coder", "tools": True}}
        async def no_models(): return []
        server.oc.list_models = no_models
        async def resident(): return ["fixture"]
        server.oc.loaded_models = resident
        async def running_models(): return [{"name": "fixture"}]
        server.oc.list_running_models = running_models
        integrations._install_editor_extension = lambda: {"ok": True, "version": "0.1.4", "note": "Installed. In VS Code, run Developer: Reload Window to activate the model picker."}
        try:
            uvicorn.run(server.app, host="127.0.0.1", port=7878)
        finally:
            stopped.set()


if __name__ == "__main__":
    main()
