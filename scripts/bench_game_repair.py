# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check a model's SDL game repair in a temporary folder.

Keep the source read-only. This checks one repair task, not general coding ability.
"""
import argparse
import asyncio
import hashlib
import difflib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HARNESS = r'''#define main original_game_main
#include "game.cpp"
#undef main
#include <iostream>
int main() {
    Game run; run.started = true; run.spawnTimer = 1000;
    for (int i=0; i<600; ++i) step(run, 1.0/60.0);
    Game hit; hit.started = true; hit.spawnTimer = 1000;
    Obstacle cactus{}; cactus.kind = 0; cactus.x = kDinoX;
    hit.obstacles.push_back(cactus); step(hit, 1.0/60.0);
    Game jump; jump.started = true; jump.spawnTimer = 1000; jump.dinoY = 200;
    jump.obstacles.push_back(cactus); step(jump, 1.0/60.0);
    const auto before=run.score; run.paused=true; step(run, 1.0);
    std::cout << "{\"score\":" << run.score << ",\"speed_increased\":" << (run.speed>kBaseSpeed)
        << ",\"ground_collision\":" << hit.dead << ",\"jump_clear\":" << !jump.dead
        << ",\"pause_preserved\":" << (run.score==before) << "}";
}
'''

TEST = r'''import json, pathlib, shlex, subprocess, unittest
class GameBehavior(unittest.TestCase):
    def test_score_difficulty_collision_jump_and_pause(self):
        flags=shlex.split(subprocess.check_output(["pkg-config","--cflags","--libs","sdl2","SDL2_ttf"],text=True))
        build=subprocess.run(["g++","-std=c++17","-O0","logic.cpp","-o","logic-check",*flags],capture_output=True,text=True)
        self.assertEqual(build.returncode,0,build.stdout+build.stderr)
        values=json.loads(subprocess.check_output(["./logic-check"],text=True))
        self.assertGreater(values["score"],100,values)
        self.assertTrue(values["speed_increased"],values)
        self.assertTrue(values["ground_collision"],values)
        self.assertTrue(values["jump_clear"],values)
        self.assertTrue(values["pause_preserved"],values)
'''


async def run(args):
    import models
    registry = models.installed()
    if args.model not in registry or not registry[args.model].get("tools"):
        raise ValueError("Choose an installed tool-calling model configured in Olith")
    meta = registry[args.model]
    source = args.source.resolve().read_bytes()
    original_hash = hashlib.sha256(source).hexdigest()
    with tempfile.TemporaryDirectory(prefix="olith-game-repair-") as folder:
        base = Path(folder)
        root = base / "project"; root.mkdir()
        import config
        config.DATA = base / "app-data"
        config.CHATS = config.DATA / "chats"; config.PROJECTS = config.DATA / "projects"
        config.MEMORY = config.DATA / "memory"; config.UPLOADS = config.DATA / "uploads"
        config.SETTINGS_PATH = config.DATA / "settings.json"
        timings = base / "timings.jsonl"
        os.environ["OLITH_TIMING_LOG"] = str(timings)
        import server
        if args.exclusive_model:
            await server.oc.unload_all_models()
        (root / "game.cpp").write_bytes(source)
        (root / "logic.cpp").write_text(HARNESS)
        (root / "test_game.py").write_text(TEST)
        def verify():
            completed = subprocess.run([sys.executable,"-m","unittest","test_game"],cwd=root,capture_output=True,text=True,timeout=30)
            return {"exit_code":completed.returncode,"output":(completed.stdout+completed.stderr)[-3000:]}
        baseline = verify()
        settings = {**config.DEFAULT_SETTINGS,"allowed_roots":[str(root)],"semantic_search":True,"repo_map_enabled":True,
                    "language_servers":False,"shell_approval_mode":"never_ask","role_context":{"agentic":65536},"agent_sample_timeout":120}
        chat = {"id":"game-repair-bench","project_id":"bench","mode":"agentic","messages":[{"role":"user","content":
            "The runner in game.cpp is broken: the score never increases, difficulty stays fixed, and ground obstacles do not kill the dinosaur. Fix those bugs while preserving the existing game. The existing test_game.py checks the gameplay without opening a window. Do not modify the test or logic.cpp. Run the relevant checks and report what you verified."}]}
        project = {"id":"bench","name":"Isolated game repair","root":str(root)}
        events=[]; started=time.monotonic(); stopped=None
        with patch.object(server.st,"get_project",return_value=project), patch.object(server.st,"save_chat",side_effect=lambda value:value), \
             patch.object(server.st,"load_settings",return_value=settings), patch.object(server.agent,"load_settings",return_value=settings), \
             patch.object(server.st,"history_index",return_value=""), patch.object(server.st,"memory_as_prompt",return_value=""), \
             patch.object(server.st,"canon_as_prompt",return_value=""), patch.object(server.reg,"installed",return_value={args.model:meta}):
            try:
                async with asyncio.timeout(args.timeout):
                    async for event in server._agent_loop(chat,meta,args.model,effort=args.effort,think=False):
                        if event["type"] in {"tool_start","tool_result","verification","error","done"}:
                            events.append(event)
                        if event["type"] == "tool_start":
                            print(f"{time.monotonic()-started:.1f}s {event['name']}",flush=True)
                            if sum(e["type"]=="tool_start" for e in events)>args.steps:
                                stopped="Tool budget reached"; break
            except TimeoutError:
                stopped="Time budget reached"
        elapsed=round(time.monotonic()-started,3)
        final=verify()
        unchanged_test=(root/"logic.cpp").read_text()==HARNESS and (root/"test_game.py").read_text()==TEST
        report={"model":args.model,"effort":args.effort,"model_profile":meta,"elapsed_s":elapsed,"baseline":baseline,"final":final,"checks_unchanged":unchanged_test,
                "user_source_unchanged":hashlib.sha256(args.source.resolve().read_bytes()).hexdigest()==original_hash,
                "source_before_bytes":len(source),"source_after_bytes":(root/"game.cpp").stat().st_size,
                "stopped":stopped,"events":events,"messages":chat["messages"],"timings":[json.loads(line) for line in timings.read_text().splitlines()] if timings.exists() else []}
        args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2))
        args.output.with_suffix(".patch").write_text("".join(difflib.unified_diff(source.decode().splitlines(True),(root/"game.cpp").read_text().splitlines(True),fromfile="a/dino_sdl.cpp",tofile="b/dino_sdl.cpp")))
        print(json.dumps({k:v for k,v in report.items() if k not in {"events","messages","model_profile","timings","baseline","final"}}),flush=True)
        print("baseline:",baseline["exit_code"],"final:",final["exit_code"],"report:",str(args.output),flush=True)
        return 0 if final["exit_code"]==0 and unchanged_test and report["user_source_unchanged"] and not stopped else 1


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--model",default="qwen3.8:27b")
    parser.add_argument("--effort",choices=("minimal","low","medium","high","max"),default="high")
    parser.add_argument("--exclusive-model",action="store_true",help="Unload resident models before this isolated probe")
    parser.add_argument("--timeout",type=int,default=240)
    parser.add_argument("--steps",type=int,default=48)
    parser.add_argument("--output",type=Path,default=Path("data/benchmarks/game-repair.json"))
    raise SystemExit(asyncio.run(run(parser.parse_args())))
