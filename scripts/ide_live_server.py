# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the real backend for VS Code tests on port 7880 by default.

Use the user's settings, but keep app data in a temporary folder and skip shell approvals.
"""
import argparse
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7880)
    parser.add_argument("--model", default="qwen3.8:27b")
    parser.add_argument("--settings", type=Path, help="Settings file to copy, default this checkout's")
    args = parser.parse_args()
    import json
    import config
    # Read the user's settings without importing storage, which binds its paths on import.
    settings = {**config.DEFAULT_SETTINGS, **json.loads((args.settings or config.SETTINGS_PATH).read_text()),
                "shell_approval_mode": "never_ask",
                "model_active": {"agentic": args.model}}
    with tempfile.TemporaryDirectory(prefix="olith-ide-live-") as folder:
        config.DATA = Path(folder) / "data"
        for name in ("CHATS", "MEMORY", "PROJECTS", "UPLOADS"):
            setattr(config, name, config.DATA / name.lower())
        config.SETTINGS_PATH = config.DATA / "settings.json"
        import server
        import uvicorn
        server.st.save_settings(settings)
        # Load only the model the test uses, when the run asks for it.
        server._ensure_warm_task = lambda: None
        server._warm_state["status"] = "ready"
        server._warm_event.set()
        uvicorn.run(server.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
